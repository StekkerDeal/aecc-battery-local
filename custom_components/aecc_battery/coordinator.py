"""DataUpdateCoordinator for the AECC Battery (Local TCP) integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    MAX_BATTERY_POWER_W,
    MAX_REGISTER_POWER_DEFAULT,
    MIN_POLL_INTERVAL,
    MODE_CUSTOM,
    MODE_DISABLED,
    MODE_REGISTERS,
    MODE_SELF_CONSUMPTION,
    POLL_INTERVAL,
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
from .tcp_client import AeccTcpClient

_LOGGER = logging.getLogger(__name__)

# ── Unified field mapping ─────────────────────────────────────────────────────
# Maps canonical sensor keys to (source, field_name, scale) tuples.
# Storage_list is tried first (Sunpura), then SSumInfoList (Lunergy fallback).
# Storage_list power values are 10x scaled; SSumInfoList values are in watts.
# ──────────────────────────────────────────────────────────────────────────────

_FIELD_MAP: dict[str, list[tuple[str, str, float]]] = {
    "battery_soc": [
        ("storage", "BatterySoc", 1.0),
        ("summary", "AverageBatteryAverageSOC", 1.0),
    ],
    "ac_charging_power": [
        ("storage", "AcChargingPower", 0.1),
        ("summary", "TotalACChargePower", 1.0),
    ],
    "battery_discharging_power": [
        ("storage", "BatteryDischargingPower", 0.1),
        ("summary", "TotalBatteryOutputPower", 1.0),
    ],
    "battery_charging_power": [
        ("storage", "BatteryChargingPower", 0.1),
        ("summary", "TotalACChargePower", 1.0),
    ],
    "pv_power": [
        ("summary", "TotalPVPower", 1.0),
        ("storage", "PvChargingPower", 0.1),
    ],
    "pv_charging_power": [
        ("summary", "TotalPVChargePower", 1.0),
        ("storage", "PvChargingPower", 0.1),
    ],
    "grid_power": [
        ("summary", "MeterTotalActivePower", 1.0),
        ("storage", "AcInActivePower", 0.1),
    ],
    "grid_export_power": [],  # Derived in sensor from grid_power (positive values only)
    "backup_power": [
        ("storage", "OffGridLoadPower", 0.1),
        ("summary", "TotalBackUpPower", 1.0),
    ],
    "pv1_power": [
        ("storage", "Pv1Power", 1.0),
    ],
    "pv2_power": [
        ("storage", "Pv2Power", 1.0),
    ],
}


class AeccBatteryCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(
        self,
        hass: HomeAssistant,
        client: AeccTcpClient,
        device_name: str,
        poll_interval: int = POLL_INTERVAL,
        manufacturer: str = "AECC",
        model: str = "",
        extended_power: bool = False,
    ) -> None:
        self.client = client
        self.device_name = device_name
        self._manufacturer = manufacturer
        self._model = model
        self._consecutive_failures: int = 0
        self._last_good_data: dict[str, Any] | None = None
        self._failure_tolerance: int = 5
        self.device_serial: str | None = None
        self.firmware_version: str | None = None
        self._commanded_power: int = 0
        self._commanded_direction: str = "Idle"
        self._commanded_min_soc: int = 10
        self._commanded_max_soc: int = 100
        self.extended_power: bool = extended_power
        self.max_register_power: int = MAX_BATTERY_POWER_W if extended_power else MAX_REGISTER_POWER_DEFAULT
        self.initial_min_soc: int | None = None
        self.initial_max_soc: int | None = None
        self.initial_work_mode: str | None = None
        self.initial_power: int | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device_name}",
            update_interval=timedelta(seconds=max(poll_interval, MIN_POLL_INTERVAL)),
        )

    async def _async_setup(self) -> None:
        await self.client.async_connect()

    async def _async_update_data(self) -> dict[str, Any]:
        raw = await self.client.get_energy_parameters()

        valid = raw is not None and (raw.get("Storage_list") or raw.get("SSumInfoList"))

        if not valid:
            self._consecutive_failures += 1
            if self._consecutive_failures == 1:
                _LOGGER.warning(
                    "Poll response missing expected data (Storage_list/SSumInfoList). "
                    "Raw response keys: %s, raw (truncated): %.500s",
                    list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__,
                    raw,
                )
            if self._consecutive_failures <= self._failure_tolerance and self._last_good_data is not None:
                _LOGGER.debug(
                    "Incomplete/missing poll response (%d/%d) - keeping last known data",
                    self._consecutive_failures,
                    self._failure_tolerance,
                )
                return self._last_good_data
            raise UpdateFailed(
                f"No valid response from {self.client.host}:{self.client.port} "
                f"after {self._consecutive_failures} consecutive failures"
            )

        self._consecutive_failures = 0
        self._last_good_data = raw
        return raw

    # ── Public access to commanded state (used by entity platforms) ──────────

    @property
    def commanded_power(self) -> int:
        return self._commanded_power

    @commanded_power.setter
    def commanded_power(self, value: int) -> None:
        self._commanded_power = value

    @property
    def commanded_direction(self) -> str:
        return self._commanded_direction

    @commanded_direction.setter
    def commanded_direction(self, value: str) -> None:
        self._commanded_direction = value

    @property
    def device_info(self) -> DeviceInfo:
        identifier = self.device_serial or f"{self.client.host}:{self.client.port}"
        return DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=self.device_name,
            manufacturer=self._manufacturer,
            model=self._model or None,
            sw_version=self.firmware_version,
            configuration_url="https://github.com/StekkerDeal/aecc-battery-local",
        )

    @property
    def storage(self) -> dict[str, Any]:
        if not self.data:
            return {}
        return (self.data.get("Storage_list") or [{}])[0]

    @property
    def summary(self) -> dict[str, Any]:
        return self.data.get("SSumInfoList", {}) if self.data else {}

    _STORAGE_POWER_KEYS = {
        "PvChargingPower",
        "AcChargingPower",
        "BatteryDischargingPower",
        "AcInActivePower",
        "OffGridLoadPower",
        "BatteryChargingPower",
        "Pv1Power",
        "Pv2Power",
        "Pv3Power",
        "Pv4Power",
    }

    def storage_val(self, key: str, default: Any = None) -> Any:
        val = self.storage.get(key, default)
        if val is None:
            return default
        if key in self._STORAGE_POWER_KEYS:
            try:
                return round(float(val) / 10, 1)
            except (TypeError, ValueError):
                return val
        return val

    def summary_val(self, key: str, default: Any = None) -> Any:
        return self.summary.get(key, default)

    def get_value(self, canonical_key: str, default: Any = None) -> Any:
        entries = _FIELD_MAP.get(canonical_key)
        if not entries:
            return default
        for source, field, scale in entries:
            container = self.storage if source == "storage" else self.summary
            val = container.get(field)
            if val is not None:
                try:
                    return round(float(val) * scale, 1)
                except (TypeError, ValueError):
                    continue
        return default

    async def async_set_battery_control(self, direction: str, power_w: int) -> bool:
        has_storage = bool(self.data and self.data.get("Storage_list"))
        field7 = 5 if has_storage else 4

        charge_soc = self._commanded_max_soc
        discharge_soc = self._commanded_min_soc

        if direction == "Idle" or power_w == 0:
            slot1 = f"0,00:00,00:00,0,0,0,0,0,0,{charge_soc},{discharge_soc}"
        else:
            reg_power = -power_w if direction == "Charge" else power_w
            slot1 = f"1,00:00,23:59,{reg_power},0,6,{field7},0,0,{charge_soc},{discharge_soc}"

        payload = {
            REG_EMS_ENABLE: "1",
            REG_SCHEDULE_MODE: "6",
            REG_AI_SMART_CHARGE: "0",
            REG_AI_SMART_DISC: "0",
            REG_CUSTOM_MODE: "1",
            REG_CONTROL_TIME1: slot1,
        }

        if self.extended_power:
            payload[REG_MAX_FEED_POWER] = str(MAX_BATTERY_POWER_W)

        if power_w > MAX_REGISTER_POWER_DEFAULT and not self.extended_power:
            _LOGGER.warning(
                "Power %d W exceeds default 800 W limit. "
                "Enable 'Extended power range' in integration options to allow up to %d W.",
                power_w,
                MAX_BATTERY_POWER_W,
            )

        _LOGGER.info(
            "SET battery_control direction=%s power=%d W -> 3003=%r",
            direction,
            power_w,
            slot1,
        )

        resp = await self.client.set_control_parameters(payload)

        if resp is None:
            _LOGGER.warning("SET battery_control failed - no response from battery")
            return False

        _LOGGER.debug("SET battery_control response: %s", resp)
        return True

    async def async_set_power_setpoint(self, watts: float) -> bool:
        power_w = int(watts)
        if power_w == 0:
            return await self.async_set_battery_control("Idle", 0)
        elif power_w > 0:
            return await self.async_set_battery_control("Charge", power_w)
        else:
            return await self.async_set_battery_control("Discharge", abs(power_w))

    async def async_set_work_mode(self, mode: str) -> bool:
        registers = MODE_REGISTERS.get(mode)
        if registers is None:
            _LOGGER.warning("SET work_mode: unknown mode %r", mode)
            return False
        _LOGGER.info("SET work_mode %r -> registers=%s", mode, registers)
        resp = await self.client.set_control_parameters(registers)

        if resp is None:
            _LOGGER.warning("SET work_mode %r failed - no response", mode)
        return resp is not None

    async def async_set_min_soc(self, value: int) -> bool:
        self._commanded_min_soc = value
        resp = await self.client.set_control_parameters({REG_MIN_SOC: str(value)})

        return resp is not None

    async def async_set_max_soc(self, value: int) -> bool:
        self._commanded_max_soc = value
        resp = await self.client.set_control_parameters({REG_MAX_SOC: str(value)})

        return resp is not None

    async def async_read_initial_state(self) -> None:
        resp = await self.client.get_control_parameters(
            [
                int(REG_EMS_ENABLE),
                int(REG_CONTROL_TIME1),
                int(REG_AI_SMART_CHARGE),
                int(REG_AI_SMART_DISC),
                int(REG_MIN_SOC),
                int(REG_MAX_SOC),
                int(REG_CUSTOM_MODE),
            ]
        )
        if resp is None:
            _LOGGER.warning("Failed to read initial control parameters (no response from battery)")
            return
        params = resp.get("ControlInfo") or resp.get("GetParameters") or resp.get("Parameters") or {}
        if not isinstance(params, dict):
            _LOGGER.debug(
                "Control parameters unexpected type: %s, response keys: %s",
                type(params).__name__,
                list(resp.keys()),
            )
            return
        if not params:
            _LOGGER.debug("Control parameters empty, response keys: %s", list(resp.keys()))

        def _int(key: str) -> int | None:
            val = params.get(key) or params.get(int(key))
            if val is None:
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        min_soc = _int(REG_MIN_SOC)
        max_soc = _int(REG_MAX_SOC)
        ems_on = _int(REG_EMS_ENABLE)
        ai_charge = _int(REG_AI_SMART_CHARGE)
        ai_discharge = _int(REG_AI_SMART_DISC)
        custom_mode = _int(REG_CUSTOM_MODE)

        if min_soc is not None:
            self.initial_min_soc = min_soc
            self._commanded_min_soc = min_soc
            _LOGGER.info("Read initial min SOC: %d%%", min_soc)
        if max_soc is not None:
            self.initial_max_soc = max_soc
            self._commanded_max_soc = max_soc
            _LOGGER.info("Read initial max SOC: %d%%", max_soc)

        slot_str = params.get(REG_CONTROL_TIME1) or params.get(int(REG_CONTROL_TIME1))
        if slot_str and isinstance(slot_str, str):
            try:
                parts = slot_str.split(",")
                if len(parts) >= 4 and parts[0] == "1":
                    reg_power = int(parts[3])
                    self.initial_power = abs(reg_power)
                    self._commanded_power = self.initial_power
                    if reg_power > 0:
                        self._commanded_direction = "Discharge"
                    elif reg_power < 0:
                        self._commanded_direction = "Charge"
                    else:
                        self._commanded_direction = "Idle"
                    _LOGGER.info(
                        "Read initial power: %d W (register value: %d, direction: %s)",
                        self.initial_power,
                        reg_power,
                        self._commanded_direction,
                    )
            except (ValueError, IndexError):
                _LOGGER.debug("Failed to parse control time slot: %r", slot_str)

        if ems_on == 0:
            self.initial_work_mode = MODE_DISABLED
        elif custom_mode == 1:
            self.initial_work_mode = MODE_CUSTOM
        elif ai_charge == 1 or ai_discharge == 1:
            self.initial_work_mode = MODE_SELF_CONSUMPTION
        else:
            self.initial_work_mode = MODE_CUSTOM

        if self.initial_work_mode:
            _LOGGER.info("Read initial work mode: %s", self.initial_work_mode)

    async def async_probe_device_management(self) -> None:
        info = await self.client.get_device_management_info()
        if info is None:
            _LOGGER.debug("DeviceManagement probe returned nothing (not supported on all AECC devices)")
            return

        params = info.get("DeviceManagementInfo") or info.get("Parameters") or info.get("GetParameters") or {}
        if not isinstance(params, dict):
            _LOGGER.debug(
                "DeviceManagement params unexpected type: %s, response keys: %s",
                type(params).__name__,
                list(info.keys()),
            )
            return

        serial = params.get("8") or params.get(8)
        firmware = params.get("21") or params.get(21)

        if serial:
            self.device_serial = str(serial).strip()
            _LOGGER.info("DeviceManagement serial: %s", self.device_serial)
        if firmware:
            self.firmware_version = str(firmware).strip()
            _LOGGER.info("DeviceManagement firmware: %s", self.firmware_version)
