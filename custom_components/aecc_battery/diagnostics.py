"""Diagnostics support for the AECC Battery (Local TCP) integration.

Produces a single JSON dump that captures everything we need to triage a
user-reported bug without asking the user to install Python or run debug
scripts on the battery host:

- Integration + device identity (PII redacted)
- Live coordinator state (commanded values, initial values, cleaner state)
- Last raw poll response (StorageSN redacted)
- Fresh control-register dump 3000-3130 read at download time
- Last 20 control writes with payloads and verify outcomes

The intended capture protocol is documented in
``debug/diagnostics-capture-tweakers.txt``: take one snapshot per step of
the failing flow and diff to see which registers stay wrong.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    DOMAIN,
    REG_AI_SMART_CHARGE,
    REG_AI_SMART_DISC,
    REG_CONTROL_TIME1,
    REG_CUSTOM_MODE,
    REG_EMS_ENABLE,
    REG_MAX_FEED_POWER,
    REG_MAX_SOC,
    REG_MIN_SOC,
    REG_SCHEDULE_MODE,
)
from .coordinator import AeccBatteryCoordinator

_LOGGER = logging.getLogger(__name__)

# Keys whose values should never leave the user's machine.
#
# The first group covers fields known to appear in this integration's data
# (device serial, host IP, Storage_list serials). The second group is
# defensive: AECC firmware varies between brands and a future update could
# surface unexpected fields like Wi-Fi credentials in a poll response we
# pass through verbatim. Including these names costs nothing and prevents
# regressions if the device starts returning them.
_REDACT_KEYS = {
    # Known fields from the current AECC protocol
    "host",
    "serial",
    "device_serial",
    "StorageSN",
    "datalogSn",
    "deviceSn",
    # Defensive: never leak credentials or location even if the device
    # surprises us by returning them in a future firmware
    "password",
    "Password",
    "token",
    "Token",
    "secret",
    "api_key",
    "apiKey",
    "email",
    "ssid",
    "SSID",
    "wifi_password",
    "wifiPassword",
    "WifiPassword",
    "mac",
    "MAC",
    "mac_address",
    "macAddress",
    "latitude",
    "longitude",
}

# Range to dump fresh at diagnostic-download time. 3000-3039 are the
# documented control registers; 3040-3129 hold a secondary (mostly empty)
# schedule-slot table on Sunpura. 3131 is the upper bound observed before
# the device returns empty strings. One TCP call covers the whole range.
_REGISTER_RANGE = list(range(3000, 3131))
_REGISTER_RANGE_FALLBACK = list(range(3000, 3040))

# Friendly labels for the registers we already understand. Anything not
# in this map is included with raw address only — useful for spotting
# changes during the antiReflux register hunt.
_KEY_REGISTER_LABELS: dict[str, str] = {
    REG_EMS_ENABLE: "EMS enable (3000)",
    REG_CONTROL_TIME1: "Control time slot 1 (3003)",
    REG_SCHEDULE_MODE: "Schedule mode (3020)",
    REG_AI_SMART_CHARGE: "AI smart charge (3021)",
    REG_AI_SMART_DISC: "AI smart discharge (3022)",
    REG_MIN_SOC: "Min SOC (3023)",
    REG_MAX_SOC: "Max SOC (3024)",
    REG_CUSTOM_MODE: "Custom mode (3030)",
    REG_MAX_FEED_POWER: "Max feed power (3039)",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return a dict suitable for the HA Download Diagnostics button."""
    coordinator: AeccBatteryCoordinator = hass.data[DOMAIN][entry.entry_id]

    integration_section = {
        "domain": DOMAIN,
        "version": await _read_integration_version(hass),
        "iot_class": "local_polling",
    }

    device_section = {
        "manufacturer": coordinator._manufacturer,
        "model": coordinator._model,
        "firmware_version": coordinator.firmware_version,
        "device_serial": coordinator.device_serial,
        "host": coordinator.client.host,
        "port": coordinator.client.port,
    }

    poll_seconds: int | None = None
    if coordinator.update_interval is not None:
        poll_seconds = int(coordinator.update_interval.total_seconds())

    config_section = {
        "extended_power": coordinator.extended_power,
        "max_register_power": coordinator.max_register_power,
        "brand_profile": dict(coordinator.brand_profile),
        "poll_interval_seconds": poll_seconds,
    }

    live_state_section = {
        "last_update_success": coordinator.last_update_success,
        "consecutive_failures": coordinator._consecutive_failures,
        "commanded_power": coordinator.commanded_power,
        "commanded_direction": coordinator.commanded_direction,
        "commanded_min_soc": coordinator._commanded_min_soc,
        "commanded_max_soc": coordinator._commanded_max_soc,
        "initial_min_soc": coordinator.initial_min_soc,
        "initial_max_soc": coordinator.initial_max_soc,
        "initial_work_mode": coordinator.initial_work_mode,
        "current_work_mode": coordinator.current_work_mode,
        "initial_power": coordinator.initial_power,
    }

    cleaner_state_section = {
        "last_accepted": dict(coordinator._cleaner_last_accepted),
        "last_accepted_at": dict(coordinator._cleaner_last_accepted_at),
    }

    last_poll_section = coordinator.data or coordinator._last_good_data or {}

    control_registers_section = await _fetch_control_registers(coordinator)

    write_history_section = coordinator.write_history

    payload: dict[str, Any] = {
        "integration": integration_section,
        "device": device_section,
        "config": config_section,
        "live_state": live_state_section,
        "cleaner_state": cleaner_state_section,
        "last_poll": last_poll_section,
        "control_registers": control_registers_section,
        "write_history": write_history_section,
    }

    return async_redact_data(payload, _REDACT_KEYS)


async def _fetch_control_registers(
    coordinator: AeccBatteryCoordinator,
) -> dict[str, Any]:
    """Read the control-register range fresh from the device.

    A device-side timeout or an unsupported address must not crash the
    whole diagnostic, so the failure is captured into the returned dict
    and the user still gets every other section.
    """
    section: dict[str, Any] = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "registers": {},
        "key_registers": {},
        "range": [_REGISTER_RANGE[0], _REGISTER_RANGE[-1]],
        "error": None,
    }
    try:
        resp = await coordinator.client.get_control_parameters(_REGISTER_RANGE)
    except Exception as exc:  # noqa: BLE001 — diagnostics must never raise
        _LOGGER.debug("Diagnostics wide register read failed: %s", exc)
        section["error"] = f"wide read failed: {exc}"
        resp = None

    # If the wide read failed, retry the documented narrow range so the
    # user still gets the registers we care most about for the WorkMode
    # bug investigation.
    if resp is None and section["error"] is None:
        section["error"] = "wide read returned no response"
    if resp is None:
        try:
            resp = await coordinator.client.get_control_parameters(_REGISTER_RANGE_FALLBACK)
            section["range"] = [
                _REGISTER_RANGE_FALLBACK[0],
                _REGISTER_RANGE_FALLBACK[-1],
            ]
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Diagnostics narrow register read failed: %s", exc)
            section["error"] = f"{section['error']}; fallback also failed: {exc}"
            return section
        if resp is None:
            section["error"] = f"{section['error']}; fallback also returned no response"
            return section

    params = resp.get("ControlInfo") or resp.get("GetParameters") or resp.get("Parameters") or {}
    if not isinstance(params, dict):
        section["error"] = f"unexpected response shape: {type(params).__name__}, keys={list(resp.keys())}"
        return section

    # Normalise keys to strings (the device sometimes returns int keys).
    normalised: dict[str, Any] = {}
    for k, v in params.items():
        normalised[str(k)] = v
    section["registers"] = normalised

    section["key_registers"] = {
        label: normalised.get(reg) for reg, label in _KEY_REGISTER_LABELS.items() if reg in normalised
    }
    return section


async def _read_integration_version(hass: HomeAssistant) -> str | None:
    """Return the integration version from its loaded manifest.

    Avoids hardcoding the version in two places. Returns ``None`` if HA
    cannot resolve the integration for any reason — diagnostics still
    render every other section.
    """
    try:
        integration = await async_get_integration(hass, DOMAIN)
    except Exception as exc:  # noqa: BLE001 — diagnostics must never raise
        _LOGGER.debug("Could not read integration version for diagnostics: %s", exc)
        return None
    return integration.version
