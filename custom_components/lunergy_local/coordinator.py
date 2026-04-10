"""DataUpdateCoordinator for the Lunergy Local Battery integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, Dict, List, Tuple

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, POLL_INTERVAL, MIN_POLL_INTERVAL,
    MAX_BATTERY_POWER_W, MAX_REGISTER_POWER_DEFAULT,
    REG_MAX_FEED_POWER, MODE_REGISTERS, REG_MIN_SOC, REG_MAX_SOC,
)
from .tcp_client import LunergyBatteryClient

_LOGGER = logging.getLogger(__name__)

# ── Unified field mapping ─────────────────────────────────────────────────────
# Maps canonical sensor keys to (source, field_name, scale) tuples.
# Storage_list is tried first (Sunpura), then SSumInfoList (Lunergy fallback).
# Storage_list power values are 10x scaled; SSumInfoList values are in watts.
# ──────────────────────────────────────────────────────────────────────────────

_FIELD_MAP: Dict[str, List[Tuple[str, str, float]]] = {
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
        ("storage", "PvChargingPower", 0.1),
        ("summary", "TotalPVPower", 1.0),
    ],
    "pv_charging_power": [
        ("storage", "PvChargingPower", 0.1),
        ("summary", "TotalPVChargePower", 1.0),
    ],
    "grid_power": [
        ("storage", "AcInActivePower", 0.1),
        ("summary", "MeterTotalActivePower", 1.0),
    ],
    "grid_export_power": [
        ("summary", "TotalGridOutputPower", 1.0),
    ],
    "backup_power": [
        ("storage", "OffGridLoadPower", 0.1),
        ("summary", "TotalBackUpPower", 1.0),
    ],
    "home_consumption": [
        ("summary", "TotalSmartLoadElectricalPower", 1.0),
    ],
}

# ── Register map (confirmed from live scan) ───────────────────────────────────
# 3000  EMS enable           1 = on
# 3003  controlTime1         active power schedule slot  ← we write here
# 3023  min discharge SOC    (10 %)
# 3024  max charge SOC       (98 %)
# 3030  custom mode          1 = on
#
# Time-slot format (11 fields):
#   "switch,start,end,power,temp,mode,0,0,0,chargingSOC,dischargingSOC"
#   e.g. "1,14:02,23:59,-2400,0,6,0,0,0,100,10"
#
# SIGN CONVENTION (confirmed empirically and from live cloud behaviour):
#   register negative → charge    (e.g. -2400 = charge at 2400 W)
#   register positive → discharge (e.g. +2400 = discharge at 2400 W)
# ─────────────────────────────────────────────────────────────────────────────

_SLOT_DISABLED   = "0,00:00,00:00,0,0,0,0,0,0,100,10"
_CHARGING_SOC    = 100
_DISCHARGING_SOC = 10


class LunergyLocalCoordinator(DataUpdateCoordinator[Dict[str, Any]]):

    def __init__(self, hass: HomeAssistant, client: LunergyBatteryClient,
                 device_name: str, poll_interval: int = POLL_INTERVAL,
                 extended_power: bool = False) -> None:
        self.client = client
        self.device_name = device_name
        self._last_set_response: Any = "never sent"
        self._consecutive_failures: int = 0
        self._last_good_data: Dict[str, Any] | None = None
        self._failure_tolerance: int = 5
        self.device_serial: str | None = None
        self.firmware_version: str | None = None
        self._commanded_power: int = 0
        self._commanded_direction: str = "Idle"
        self.extended_power: bool = extended_power
        self.max_register_power: int = (
            MAX_BATTERY_POWER_W if extended_power else MAX_REGISTER_POWER_DEFAULT
        )
        self.initial_min_soc: int | None = None
        self.initial_max_soc: int | None = None
        self.initial_work_mode: str | None = None
        self.initial_power: int | None = None
        super().__init__(
            hass, _LOGGER,
            name=f"{DOMAIN}_{device_name}",
            update_interval=timedelta(seconds=max(poll_interval, MIN_POLL_INTERVAL)),
        )

    async def _async_setup(self) -> None:
        await self.client.async_connect()

    async def _async_update_data(self) -> Dict[str, Any]:
        raw = await self.client.get_energy_parameters()

        valid = (
            raw is not None
            and (raw.get("Storage_list") or raw.get("SSumInfoList"))
        )

        if not valid:
            self._consecutive_failures += 1
            if (self._consecutive_failures <= self._failure_tolerance
                    and self._last_good_data is not None):
                _LOGGER.debug(
                    "Incomplete/missing poll response (%d/%d) — keeping last known data",
                    self._consecutive_failures, self._failure_tolerance,
                )
                return self._last_good_data
            raise UpdateFailed(
                f"No valid response from {self.client.host}:{self.client.port} "
                f"after {self._consecutive_failures} consecutive failures"
            )

        self._consecutive_failures = 0
        self._last_good_data = raw
        return raw

    # ── Device info ───────────────────────────────────────────────────────────

    @property
    def device_info(self) -> DeviceInfo:
        identifier = self.device_serial or f"{self.client.host}:{self.client.port}"
        info = DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=self.device_name,
            manufacturer="Lunergy",
            model="Hub 2400 AC",
        )
        if self.firmware_version:
            info["sw_version"] = self.firmware_version
        return info

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def storage(self) -> Dict[str, Any]:
        if not self.data:
            return {}
        return (self.data.get("Storage_list") or [{}])[0]

    @property
    def summary(self) -> Dict[str, Any]:
        return self.data.get("SSumInfoList", {}) if self.data else {}

    _STORAGE_POWER_KEYS = {
        "PvChargingPower", "AcChargingPower", "BatteryDischargingPower",
        "AcInActivePower", "OffGridLoadPower", "BatteryChargingPower",
        "Pv1Power", "Pv2Power", "Pv3Power", "Pv4Power",
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
        """Get a sensor value using the canonical key.

        Tries Storage_list first (Sunpura), falls back to SSumInfoList (Lunergy).
        Applies the correct scaling per source automatically.
        """
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

    # ── Battery control (direction + power) ───────────────────────────────────

    async def async_set_battery_control(self, direction: str, power_w: int) -> bool:
        """Write battery control registers for the given direction and power.

        Direction: "Charge", "Discharge", or "Idle".
        Power: absolute watts (0-2400).
        """
        # Auto-detect firmware variant from poll data
        has_storage = bool(self.data and self.data.get("Storage_list"))
        field7 = 5 if has_storage else 4

        if direction == "Idle" or power_w == 0:
            slot1 = _SLOT_DISABLED
        else:
            reg_power = -power_w if direction == "Charge" else power_w
            slot1 = (
                f"1,00:00,23:59,{reg_power},0,6,{field7},0,0,"
                f"{_CHARGING_SOC},{_DISCHARGING_SOC}"
            )

        payload = {
            "3000": "1",      # EMS enable
            "3020": "6",      # Energy mode = custom/manual
            "3021": "0",      # AI smart charge OFF
            "3022": "0",      # AI smart discharge OFF
            "3030": "1",      # Custom mode ON
            "3003": slot1,    # Schedule slot
        }

        # Write register 3039 (maxFeedPower) to unlock power above 800W.
        # Without this, the firmware clamps local TCP power writes to 800W.
        if self.extended_power:
            payload[REG_MAX_FEED_POWER] = str(MAX_BATTERY_POWER_W)

        if power_w > MAX_REGISTER_POWER_DEFAULT and not self.extended_power:
            _LOGGER.warning(
                "Power %d W exceeds default 800 W limit. "
                "Enable 'Extended power range' in integration options to allow up to %d W.",
                power_w, MAX_BATTERY_POWER_W,
            )

        _LOGGER.info(
            "SET battery_control direction=%s power=%d W → 3003=%r",
            direction, power_w, slot1,
        )

        resp = await self.client.set_control_parameters(payload)
        self._last_set_response = resp

        if resp is None:
            _LOGGER.warning("SET battery_control failed — no response from battery")
            return False

        _LOGGER.debug("SET battery_control response: %s", resp)
        return True

    # ── Legacy power setpoint (kept for automation backward compat) ───────────

    async def async_set_power_setpoint(self, watts: float) -> bool:
        """Write the power setpoint. Thin wrapper around async_set_battery_control."""
        power_w = int(watts)
        if power_w == 0:
            return await self.async_set_battery_control("Idle", 0)
        elif power_w > 0:
            return await self.async_set_battery_control("Charge", power_w)
        else:
            return await self.async_set_battery_control("Discharge", abs(power_w))

    # ── Work mode & SOC ────────────────────────────────────────────────────────

    async def async_set_work_mode(self, mode: str) -> bool:
        registers = MODE_REGISTERS.get(mode)
        if registers is None:
            _LOGGER.warning("SET work_mode: unknown mode %r", mode)
            return False
        _LOGGER.info("SET work_mode %r → registers=%s", mode, registers)
        resp = await self.client.set_control_parameters(registers)
        self._last_set_response = resp
        if resp is None:
            _LOGGER.warning("SET work_mode %r failed — no response", mode)
        return resp is not None

    async def async_set_min_soc(self, value: int) -> bool:
        resp = await self.client.set_control_parameters({"3023": str(value)})
        self._last_set_response = resp
        return resp is not None

    async def async_set_max_soc(self, value: int) -> bool:
        resp = await self.client.set_control_parameters({"3024": str(value)})
        self._last_set_response = resp
        return resp is not None

    # ── Initial state read ──────────────────────────────────────────────────

    async def async_read_initial_state(self) -> None:
        """Read current SOC limits and work mode from registers at startup."""
        resp = await self.client.get_control_parameters([3000, 3003, 3021, 3022, 3023, 3024, 3030])
        if resp is None:
            return
        params = (
            resp.get("ControlInfo")
            or resp.get("GetParameters")
            or resp.get("Parameters")
            or {}
        )
        if not isinstance(params, dict):
            return

        def _int(key: str) -> int | None:
            val = params.get(key) or params.get(int(key))
            if val is None:
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        min_soc = _int("3023")
        max_soc = _int("3024")
        ems_on = _int("3000")
        ai_charge = _int("3021")
        ai_discharge = _int("3022")
        custom_mode = _int("3030")

        if min_soc is not None:
            self.initial_min_soc = min_soc
            _LOGGER.info("Read initial min SOC: %d%%", min_soc)
        if max_soc is not None:
            self.initial_max_soc = max_soc
            _LOGGER.info("Read initial max SOC: %d%%", max_soc)

        # Parse power from register 3003 time-slot string
        # Format: "switch,start,end,power,temp,mode,field7,0,0,chargingSOC,dischargingSOC"
        # e.g. "1,00:00,23:59,-2400,0,6,5,0,0,100,10"
        slot_str = params.get("3003") or params.get(3003)
        if slot_str and isinstance(slot_str, str):
            try:
                parts = slot_str.split(",")
                if len(parts) >= 4 and parts[0] == "1":
                    reg_power = int(parts[3])
                    # Register sign: negative = charge, positive = discharge
                    self.initial_power = abs(reg_power)
                    self._commanded_power = self.initial_power
                    _LOGGER.info("Read initial power: %d W (register value: %d)", self.initial_power, reg_power)
            except (ValueError, IndexError):
                pass

        # Derive work mode from register state
        from .const import MODE_SELF_CONSUMPTION, MODE_CUSTOM, MODE_DISABLED
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

    # ── DeviceManagement probe ────────────────────────────────────────────────

    async def async_probe_device_management(self) -> None:
        """Try to read serial/firmware via DeviceManagement (works on Sunpura, times out on Lunergy)."""
        info = await self.client.get_device_management_info()
        if info is None:
            _LOGGER.debug("DeviceManagement probe returned nothing (expected on Lunergy)")
            return

        params = (
            info.get("DeviceManagementInfo")
            or info.get("Parameters")
            or info.get("GetParameters")
            or {}
        )
        if not isinstance(params, dict):
            return

        serial = params.get("8") or params.get(8)
        firmware = params.get("21") or params.get(21)

        if serial:
            self.device_serial = str(serial).strip()
            _LOGGER.info("DeviceManagement serial: %s", self.device_serial)
        if firmware:
            self.firmware_version = str(firmware).strip()
            _LOGGER.info("DeviceManagement firmware: %s", self.firmware_version)
