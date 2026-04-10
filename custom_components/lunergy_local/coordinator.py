"""DataUpdateCoordinator for the Lunergy Local Battery integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, Dict, List, Tuple

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, POLL_INTERVAL, MIN_POLL_INTERVAL, MAX_BATTERY_POWER_W,
    MODE_REGISTERS, REG_MIN_SOC, REG_MAX_SOC,
)
from .tcp_client import LunergyBatteryClient

_LOGGER = logging.getLogger(__name__)

# ── Unified field mapping ─────────────────────────────────────────────────────
# Maps canonical sensor keys to (source, field_name, scale) tuples.
# Storage_list is tried first (Lunergy), then SSumInfoList (Lunergy fallback).
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
                 device_name: str, poll_interval: int = POLL_INTERVAL) -> None:
        self.client = client
        self.device_name = device_name
        self._last_set_response: Any = "never sent"
        self._register_scan: Any = None
        self._consecutive_failures: int = 0
        self._last_good_data: Dict[str, Any] | None = None
        self._failure_tolerance: int = 5
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

        Tries Storage_list first (Lunergy), falls back to SSumInfoList (Lunergy).
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

    # ── Power setpoint ─────────────────────────────────────────────────────────

    async def async_set_power_setpoint(self, watts: float) -> bool:
        """Write the power setpoint to register 3003 (controlTime1).

        Sign convention (confirmed empirically):
            UI +2400 W = charge    → register value -2400
            UI -2400 W = discharge → register value +2400
        """
        power_w = int(watts)
        reg_power = -power_w

        # Auto-detect firmware variant from poll data:
        #   Storage_list present → Sunpura (field7=5)
        #   Only SSumInfoList   → Lunergy (field7=4)
        has_storage = bool(self.data and self.data.get("Storage_list"))
        field7 = 5 if has_storage else 4

        if power_w == 0:
            slot1 = _SLOT_DISABLED
        else:
            # Slot format: switch,start,end,power,temp,mode,field7,0,0,chargingSOC,dischargingSOC
            slot1 = (
                f"1,00:00,23:59,{reg_power},0,6,{field7},0,0,"
                f"{_CHARGING_SOC},{_DISCHARGING_SOC}"
            )

        # Ensure custom mode is fully active
        payload = {
            "3000": "1",      # EMS enable
            "3020": "6",      # Energy mode = custom/manual
            "3021": "0",      # AI smart charge OFF
            "3022": "0",      # AI smart discharge OFF
            "3030": "1",      # Custom mode ON
            "3003": slot1,    # Schedule slot
        }

        _LOGGER.info(
            "SET power %+d W → reg_power=%+d → 3003=%r",
            power_w, reg_power, slot1,
        )

        resp = await self.client.set_control_parameters(payload)
        self._last_set_response = resp

        if resp is None:
            _LOGGER.warning("SET power %+d W failed — no response from battery", power_w)
            return False

        _LOGGER.debug("SET power response: %s", resp)
        return True

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
        resp = await self.client.set_control_parameters({"3002": str(value)})
        self._last_set_response = resp
        return resp is not None

    # ── Register scan ──────────────────────────────────────────────────────────

    async def async_scan_power_registers(self) -> None:
        all_addrs = list(range(3000, 3150)) + list(range(4000, 4050))
        batch_size = 10
        all_results: dict = {}
        raw_batches: list = []

        for i in range(0, len(all_addrs), batch_size):
            batch = all_addrs[i:i + batch_size]
            resp = await self.client.get_control_parameters(batch)
            if resp is None:
                continue
            raw_batches.append(resp)
            params = (resp.get("ControlInfo")
                      or resp.get("GetParameters")
                      or resp.get("Parameters")
                      or resp.get("SetParameters")
                      or {})
            if isinstance(params, dict):
                for addr, val in params.items():
                    str_val = str(val).strip()
                    if str_val not in ("", "0", "None"):
                        all_results[addr] = str_val

        self._register_scan = {
            "non_empty_registers": all_results,
            "all_batches": raw_batches,
        }
        _LOGGER.info("Lunergy register scan: %s", all_results)
