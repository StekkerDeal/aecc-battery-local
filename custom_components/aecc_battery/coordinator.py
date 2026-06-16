"""DataUpdateCoordinator for the AECC Battery (Local TCP) integration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cleaners import CLEANERS, CleanerContext
from .const import (
    BRAND_AEG,
    DEFAULT_BRAND_PROFILE,
    DOMAIN,
    MAX_BATTERY_POWER_W,
    MAX_REGISTER_POWER_DEFAULT,
    MIN_POLL_INTERVAL,
    MODE_CUSTOM,
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
        # OffGridLoadPower is reported in watts directly, unlike other storage
        # power fields which are in deciwatts (0.1x scale). Verified against a
        # 2000W heater test on 2026-04-20: battery_discharging_power read
        # ~2040W while backup_power with the 0.1 multiplier read ~193W.
        ("storage", "OffGridLoadPower", 1.0),
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
        brand_profile: dict[str, Any] | None = None,
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
        # Single source of truth for the displayed work mode. All three
        # control entities (Work Mode select, Battery Direction, Power
        # slider) read from the coordinator so they never contradict each
        # other. Updated on every successful control write, after which
        # async_update_listeners() refreshes every entity at once.
        self._current_work_mode: str | None = None
        # Per-brand cleaning profile (thresholds for the physics-aware
        # cleaners). Defaults to the conservative "Other" profile so a
        # missing/typo'd brand still gets light protection without rejecting
        # legitimate readings.
        self.brand_profile: dict[str, Any] = dict(brand_profile or DEFAULT_BRAND_PROFILE)
        # State for the cleaner pipeline, last accepted (cleaned) value and
        # timestamp per canonical key. Used for rate-of-change checks and
        # for the hybrid hold-then-unavailable behavior in AeccSensor.
        self._cleaner_last_accepted: dict[str, float] = {}
        self._cleaner_last_accepted_at: dict[str, float] = {}
        # Rolling audit trail of recent control writes. Surfaced through
        # diagnostics so we can correlate user-reported misbehaviour with
        # the exact register payloads sent and the post-write verify
        # results from the device.
        self._write_history: deque[dict[str, Any]] = deque(maxlen=20)
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
    def current_work_mode(self) -> str | None:
        return self._current_work_mode

    @current_work_mode.setter
    def current_work_mode(self, value: str | None) -> None:
        self._current_work_mode = value

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

    def _wall_power_signal_w(self) -> float | None:
        """Best-effort wall-side power magnitude for cleaner physics checks.

        Returns a signed value: positive when the battery is charging,
        negative when discharging. None when neither AECC source has the
        data (cleaners then skip checks that depend on observable flow).

        Reads raw fields directly to avoid triggering nested cleaner calls
        from get_value, this is the activity signal cleaners depend on,
        not a published sensor value.
        """
        for field, scale in (
            ("AcChargingPower", 0.1),
            ("BatteryChargingPower", 0.1),
        ):
            val = self.storage.get(field)
            if val is not None:
                try:
                    charge = float(val) * scale
                    if charge > 0:
                        return charge
                except (TypeError, ValueError):
                    pass
        for field, scale in (
            ("BatteryDischargingPower", 0.1),
            ("AcChargingPower", 0.1),
        ):
            val = self.storage.get(field)
            if val is not None:
                try:
                    discharge = float(val) * scale
                    if discharge > 0:
                        return -discharge if field == "BatteryDischargingPower" else discharge
                except (TypeError, ValueError):
                    pass
        # Fall back to the summary fields (Lunergy primary).
        ac = self.summary.get("TotalACChargePower")
        out = self.summary.get("TotalBatteryOutputPower")
        try:
            ac_f = float(ac) if ac is not None else 0.0
            out_f = float(out) if out is not None else 0.0
            if ac_f > 0:
                return ac_f
            if out_f > 0:
                return -out_f
            if ac is not None or out is not None:
                return 0.0
        except (TypeError, ValueError):
            pass
        return None

    def get_value(self, canonical_key: str, default: Any = None) -> Any:
        entries = _FIELD_MAP.get(canonical_key)
        if not entries:
            return default
        raw_value: float | None = None
        for source, field, scale in entries:
            container = self.storage if source == "storage" else self.summary
            val = container.get(field)
            if val is not None:
                try:
                    raw_value = round(float(val) * scale, 1)
                    break
                except (TypeError, ValueError):
                    continue
        if raw_value is None:
            return default

        cleaner = CLEANERS.get(canonical_key)
        if cleaner is None:
            return raw_value

        ctx = CleanerContext(
            key=canonical_key,
            raw_value=raw_value,
            last_accepted_value=self._cleaner_last_accepted.get(canonical_key),
            last_accepted_at=self._cleaner_last_accepted_at.get(canonical_key),
            now=time.time(),
            wall_power_w=self._wall_power_signal_w(),
            profile=self.brand_profile,
        )
        cleaned = cleaner(ctx)
        if cleaned is None:
            _LOGGER.debug(
                "Cleaner rejected %s=%s (last_accepted=%s, wall_power=%s)",
                canonical_key,
                raw_value,
                ctx.last_accepted_value,
                ctx.wall_power_w,
            )
            return None
        # Record the accepted value/timestamp so the next call has fresh
        # state for rate-of-change checks. Only updates on accept.
        self._cleaner_last_accepted[canonical_key] = cleaned
        self._cleaner_last_accepted_at[canonical_key] = ctx.now
        return cleaned

    def cleaner_last_accepted_at(self, canonical_key: str) -> float | None:
        """Last epoch-second timestamp when this key passed the cleaner.

        AeccSensor uses this for the hybrid hold-then-unavailable behavior:
        once readings have been rejected for longer than the brand's
        ``hold_last_value_seconds``, the entity goes unavailable instead
        of indefinitely showing a stale value.
        """
        return self._cleaner_last_accepted_at.get(canonical_key)

    # Delay between a SET command and the readback that verifies the device
    # actually accepted the change. Some AECC devices apply writes lazily;
    # a too-fast readback can hit the pre-write state and produce false
    # "mismatch" warnings. Half a second is empirically enough for Lunergy
    # and Sunpura without noticeably slowing user-facing UI updates.
    _WRITE_VERIFY_DELAY_SECONDS: float = 0.5

    async def _verify_write(
        self,
        expected: dict[str, str],
        operation: str,
    ) -> list[dict[str, Any]] | None:
        """Re-read registers after a write and warn on mismatch.

        Best-effort verification: the SET response already returned OK
        (otherwise the caller would have logged + returned False), so we
        do NOT change the return value of the calling write method. We
        only surface a WARNING when the device claimed success but the
        actual register state diverges, which is a real failure mode on
        AECC devices under load. Schedule slot strings (the long CSV) are
        log-only and never compared character-for-character because the
        device may normalise whitespace or trailing zeros.

        Returns a list of per-register verify entries (or ``None`` if the
        readback could not be performed) so callers like ``_logged_write``
        can persist the result for diagnostics. Each entry is
        ``{"register", "expected", "actual", "match"}`` where ``match`` is
        ``None`` for the schedule-slot string (log-only).
        """
        try:
            await asyncio.sleep(self._WRITE_VERIFY_DELAY_SECONDS)
            reg_addrs = [int(k) for k in expected.keys()]
            resp = await self.client.get_control_parameters(reg_addrs)
            if resp is None:
                _LOGGER.debug("Write-back verify for %s: no response (skipping)", operation)
                return None
            actual = resp.get("ControlInfo") or resp.get("GetParameters") or {}
            if not isinstance(actual, dict):
                return None
            results: list[dict[str, Any]] = []
            for reg, expected_val in expected.items():
                actual_val = actual.get(reg) or actual.get(int(reg))
                if reg == REG_CONTROL_TIME1:
                    # Schedule slot is a CSV string, device may reorder or
                    # rewrite parts. Log-only, no equality check.
                    _LOGGER.debug(
                        "Write-back verify for %s: %s = %r (expected %r)",
                        operation,
                        reg,
                        actual_val,
                        expected_val,
                    )
                    results.append(
                        {
                            "register": reg,
                            "expected": expected_val,
                            "actual": actual_val,
                            "match": None,
                        }
                    )
                    continue
                if actual_val is None:
                    results.append(
                        {
                            "register": reg,
                            "expected": expected_val,
                            "actual": None,
                            "match": None,
                        }
                    )
                    continue
                match = str(actual_val).strip() == str(expected_val).strip()
                results.append(
                    {
                        "register": reg,
                        "expected": expected_val,
                        "actual": actual_val,
                        "match": match,
                    }
                )
                if not match:
                    _LOGGER.warning(
                        "Write-back verify mismatch for %s: register %s expected %r, "
                        "device reports %r, write may have been silently dropped",
                        operation,
                        reg,
                        expected_val,
                        actual_val,
                    )
            return results
        except (TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
            _LOGGER.debug("Write-back verify for %s failed: %s", operation, exc)
            return None

    async def _logged_write(self, payload: dict[str, str], operation: str) -> bool:
        """Send a control-register write and append an entry to the audit trail.

        Wraps ``client.set_control_parameters`` + ``_verify_write`` so all
        mutating coordinator methods record what they sent, whether the
        device acknowledged, and the per-register verify outcome. The
        rolling buffer is exposed via ``write_history`` for diagnostics.
        """
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "operation": operation,
            "payload": dict(payload),
            "response_received": False,
            "verify_result": None,
        }
        self._write_history.append(entry)
        resp = await self.client.set_control_parameters(payload)
        entry["response_received"] = resp is not None
        if resp is None:
            _LOGGER.warning("SET %s failed - no response from battery", operation)
            return False
        _LOGGER.debug("SET %s response: %s", operation, resp)
        entry["verify_result"] = await self._verify_write(payload, operation)
        return True

    @property
    def write_history(self) -> list[dict[str, Any]]:
        """Recent control writes with verify outcomes (newest last)."""
        return list(self._write_history)

    def _encode_active_slot(
        self,
        direction: str,
        power_w: int,
        field7: int,
        charge_soc: int,
        discharge_soc: int,
    ) -> str:
        """Build the active (timeSwitch=1) control slot string for register 3003.

        Most brands encode the setpoint as a single signed power field (negative =
        charge, positive = discharge), confirmed on AFERIY. AEG (Solarcube) ignores
        that and expects two unsigned fields instead - charge in field 3, discharge in
        field 4 - so it gets its own layout. Idle/disabled slots are all-zero and
        layout-neutral, so they do not go through here. See REG_CONTROL_TIME1 in const.
        """
        if self._manufacturer == BRAND_AEG:
            charge_p = power_w if direction == "Charge" else 0
            discharge_p = power_w if direction == "Discharge" else 0
            return f"1,00:00,23:59,{charge_p},{discharge_p},6,{field7},0,0,{charge_soc},{discharge_soc}"
        reg_power = -power_w if direction == "Charge" else power_w
        return f"1,00:00,23:59,{reg_power},0,6,{field7},0,0,{charge_soc},{discharge_soc}"

    def _decode_active_slot(self, parts: list[str]) -> tuple[int, str]:
        """Recover (power_w, direction) from an active slot's CSV fields.

        Inverse of ``_encode_active_slot``: AEG reads field 3 as charge and field 4 as
        discharge (both unsigned); other brands read field 3 as a signed setpoint.
        """
        if self._manufacturer == BRAND_AEG:
            charge_p = int(parts[3])
            discharge_p = int(parts[4])
            if charge_p > 0:
                return charge_p, "Charge"
            if discharge_p > 0:
                return discharge_p, "Discharge"
            return 0, "Idle"
        reg_power = int(parts[3])
        if reg_power > 0:
            return reg_power, "Discharge"
        if reg_power < 0:
            return -reg_power, "Charge"
        return 0, "Idle"

    async def async_set_battery_control(self, direction: str, power_w: int) -> bool:
        has_storage = bool(self.data and self.data.get("Storage_list"))
        field7 = 5 if has_storage else 4

        charge_soc = self._commanded_max_soc
        discharge_soc = self._commanded_min_soc

        if direction == "Idle" or power_w == 0:
            slot1 = f"0,00:00,00:00,0,0,0,0,0,0,{charge_soc},{discharge_soc}"
        else:
            slot1 = self._encode_active_slot(
                direction, power_w, field7, charge_soc, discharge_soc
            )

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

        success = await self._logged_write(payload, f"battery_control({direction}, {power_w}W)")
        if success:
            # Any manual direction/power command puts the device in Custom
            # mode. Record it and refresh every control entity so the Work
            # Mode selector reflects Custom instead of its stale value.
            self._commanded_direction = direction
            self._current_work_mode = MODE_CUSTOM
            self.async_update_listeners()
        return success

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
        success = await self._logged_write(dict(registers), f"work_mode({mode})")
        if success:
            self._current_work_mode = mode
            self.async_update_listeners()
        return success

    async def async_set_min_soc(self, value: int) -> bool:
        self._commanded_min_soc = value
        payload = {REG_MIN_SOC: str(value)}
        return await self._logged_write(payload, f"min_soc({value}%)")

    async def async_set_max_soc(self, value: int) -> bool:
        self._commanded_max_soc = value
        payload = {REG_MAX_SOC: str(value)}
        return await self._logged_write(payload, f"max_soc({value}%)")

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
        ai_charge = _int(REG_AI_SMART_CHARGE)
        ai_discharge = _int(REG_AI_SMART_DISC)

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
                if len(parts) >= 5 and parts[0] == "1":
                    power, direction = self._decode_active_slot(parts)
                    self.initial_power = power
                    self._commanded_power = power
                    self._commanded_direction = direction
                    _LOGGER.info(
                        "Read initial power: %d W (direction: %s, slot: %r)",
                        self.initial_power,
                        self._commanded_direction,
                        slot_str,
                    )
            except (ValueError, IndexError):
                _LOGGER.debug("Failed to parse control time slot: %r", slot_str)

        # There is no "Disabled" mode. A device reporting EMS off (ems_on==0)
        # is shown as Custom; selecting a mode or direction re-enables it.
        if ai_charge == 1 or ai_discharge == 1:
            self.initial_work_mode = MODE_SELF_CONSUMPTION
        else:
            self.initial_work_mode = MODE_CUSTOM
        self._current_work_mode = self.initial_work_mode

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
