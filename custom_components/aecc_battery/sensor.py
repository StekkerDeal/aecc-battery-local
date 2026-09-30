"""Sensor platform for AECC Battery (Local TCP)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import utcnow

from .const import (
    DOMAIN,
    IDLE_DEADBAND_W,
    MB_ALARM_FLAGS,
    MB_AVAILABLE_CHARGE_POWER,
    MB_ENERGY_CHARGED,
    MB_ENERGY_DISCHARGED,
    MB_ENERGY_TO_GRID,
    MB_NOMINAL_BATTERY_POWER,
    MB_NOMINAL_POWER,
    MB_TEMP_1,
    MB_TEMP_2,
    MB_TEMP_3,
    MODBUS_REGISTERS,
)
from .coordinator import AeccBatteryCoordinator

_LOGGER = logging.getLogger(__name__)

# ── Standard power/measurement sensors ────────────────────────────────────────
# (key, name, canonical_key, unit, icon, is_power); icon None = let HA pick
_SENSORS = [
    ("ac_charging_power", "AC Charging Power", "ac_charging_power", UnitOfPower.WATT, "mdi:power-plug", True),
    (
        "battery_discharging_power",
        "Battery Discharging Power",
        "battery_discharging_power",
        UnitOfPower.WATT,
        "mdi:battery-arrow-down",
        True,
    ),
    # No icon on purpose: a battery-device-class percentage sensor without an
    # icon of its own gets Home Assistant's dynamic level icon (mdi:battery-10
    # through mdi:battery). Setting one pins the frontend to that single glyph
    # and the SOC stops being readable from the icon (#19).
    ("battery_soc", "Battery SOC", "battery_soc", PERCENTAGE, None, False),
    ("pv_power", "PV Power", "pv_power", UnitOfPower.WATT, "mdi:solar-power", True),
    ("pv_charging_power", "PV Charging Power", "pv_charging_power", UnitOfPower.WATT, "mdi:solar-panel", True),
    ("grid_power", "Grid / Meter Power", "grid_power", UnitOfPower.WATT, "mdi:transmission-tower", True),
    ("backup_power", "Backup Power", "backup_power", UnitOfPower.WATT, "mdi:power-plug-battery", True),
    ("pv1_power", "PV String 1 Power", "pv1_power", UnitOfPower.WATT, "mdi:solar-panel", True),
    ("pv2_power", "PV String 2 Power", "pv2_power", UnitOfPower.WATT, "mdi:solar-panel", True),
]

# The device's own figure; some models leave it at 0 while the panels charge.
_DIAGNOSTIC_KEYS = {"pv_charging_power"}

# ── Per-unit sensors (multi-unit systems only) ────────────────────────────────
# _SENSORS minus pv_power/grid_power (system-level quantities), plus
# battery_charging_power (the hub only exposes it folded into Battery Power).
_UNIT_SENSORS = [s for s in _SENSORS if s[0] not in ("pv_power", "grid_power")] + [
    (
        "battery_charging_power",
        "Battery Charging Power",
        "battery_charging_power",
        UnitOfPower.WATT,
        "mdi:battery-arrow-up",
        True,
    ),
]

# ── Energy counter definitions ────────────────────────────────────────────────
# (key, name, power_fn, icon); power_fn returns watts, or None for no reading.


def _charged_w(coordinator: AeccBatteryCoordinator) -> float | None:
    power = coordinator.battery_power_w()
    return None if power is None else max(power, 0.0)


def _discharged_w(coordinator: AeccBatteryCoordinator) -> float | None:
    power = coordinator.battery_power_w()
    return None if power is None else max(-power, 0.0)


def _generated_w(coordinator: AeccBatteryCoordinator) -> float | None:
    return coordinator.get_value("pv_power")


_ENERGY_SENSORS = [
    ("energy_charged", "Energy Charged", _charged_w, "mdi:battery-charging"),
    ("energy_discharged", "Energy Discharged", _discharged_w, "mdi:battery-arrow-down-outline"),
    ("energy_generated", "Energy Generated", _generated_w, "mdi:solar-power"),
]

_MAX_GAP_SECONDS = 60

# ── Modbus telemetry sensors (only on devices that answer the map) ────────────
# (key, name, register, unit, device_class, state_class, entity_category, icon)
_TEMP = UnitOfTemperature.CELSIUS
_DIAG = EntityCategory.DIAGNOSTIC
_MEAS = SensorStateClass.MEASUREMENT
_TOTAL = SensorStateClass.TOTAL_INCREASING
_MODBUS_SENSORS = [
    # Labels as the vendor app shows them; the keys keep the register order.
    # The app numbers the first two the other way round from the registers.
    ("temperature_1", "PV Radiator Temperature", MB_TEMP_1, _TEMP, SensorDeviceClass.TEMPERATURE, _MEAS, _DIAG, None),
    ("temperature_2", "Inverter Temperature", MB_TEMP_2, _TEMP, SensorDeviceClass.TEMPERATURE, _MEAS, _DIAG, None),
    ("temperature_3", "Transformer Temperature", MB_TEMP_3, _TEMP, SensorDeviceClass.TEMPERATURE, _MEAS, _DIAG, None),
    # Lifetime counters count at the battery, behind the inverter, so they are
    # not Energy Dashboard inputs; the integrated energy sensors above are.
    (
        "lifetime_energy_charged",
        "Lifetime Energy Charged",
        MB_ENERGY_CHARGED,
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
        _TOTAL,
        None,
        "mdi:battery-charging",
    ),
    (
        "lifetime_energy_discharged",
        "Lifetime Energy Discharged",
        MB_ENERGY_DISCHARGED,
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
        _TOTAL,
        None,
        "mdi:battery-arrow-down-outline",
    ),
    (
        "lifetime_energy_to_grid",
        "Lifetime Energy to Grid",
        MB_ENERGY_TO_GRID,
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
        _TOTAL,
        None,
        "mdi:transmission-tower-export",
    ),
    (
        "available_charge_power",
        "Available Charge Power",
        MB_AVAILABLE_CHARGE_POWER,
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        _MEAS,
        None,
        "mdi:battery-arrow-up-outline",
    ),
    (
        "nominal_power",
        "Nominal Power",
        MB_NOMINAL_POWER,
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        None,
        _DIAG,
        "mdi:gauge",
    ),
    (
        "nominal_battery_power",
        "Nominal Battery Power",
        MB_NOMINAL_BATTERY_POWER,
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        None,
        _DIAG,
        "mdi:gauge",
    ),
    ("alarm_flags", "Alarm Flags", MB_ALARM_FLAGS, None, None, None, _DIAG, "mdi:alert-circle-outline"),
]


def _derive_status(charge: float, ac_charge: float, discharge: float) -> str:
    if charge > 0 or ac_charge > 0:
        return "Charging"
    if discharge > 0:
        return "Discharging"
    return "Idle"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AeccBatteryCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities: list[SensorEntity] = []

    for key, name, canonical_key, unit, icon, is_power in _SENSORS:
        entities.append(AeccSensor(coordinator, config_entry, key, name, canonical_key, unit, icon, is_power))

    for key, name, power_fn, icon in _ENERGY_SENSORS:
        entities.append(AeccEnergySensor(coordinator, config_entry, key, name, power_fn, icon))

    entities.append(AeccGridExportSensor(coordinator, config_entry))
    entities.append(AeccBatteryPowerSensor(coordinator, config_entry))
    entities.append(AeccBatteryStatusSensor(coordinator, config_entry))

    if coordinator.firmware_version is not None:
        entities.append(AeccFirmwareSensor(coordinator, config_entry))

    if coordinator.wifi_rssi is not None:
        entities.append(AeccWifiSignalSensor(coordinator, config_entry))

    if coordinator.modbus_supported:
        for spec in _MODBUS_SENSORS:
            entities.append(AeccModbusSensor(coordinator, config_entry, *spec))

    # Multi-unit systems: one child device per battery (controls stay hub-only;
    # the protocol has no per-unit control).
    units = coordinator.units
    if len(units) > 1:
        for unit in units:
            for key, name, canonical_key, unit_of_meas, icon, is_power in _UNIT_SENSORS:
                entities.append(
                    AeccUnitSensor(
                        coordinator, config_entry, unit, key, name, canonical_key, unit_of_meas, icon, is_power
                    )
                )
            entities.append(AeccUnitStatusSensor(coordinator, config_entry, unit))

    async_add_entities(entities)


class AeccSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: AeccBatteryCoordinator,
        config_entry: ConfigEntry,
        key: str,
        name: str,
        canonical_key: str,
        unit: str,
        icon: str | None,
        is_power: bool,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._canonical_key = canonical_key
        self._is_power = is_power
        self._attr_unique_id = f"{config_entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_device_class = SensorDeviceClass.POWER if is_power else SensorDeviceClass.BATTERY
        if key in _DIAGNOSTIC_KEYS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._last_value = None

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self):
        val = self.coordinator.get_value(self._canonical_key)
        if val is not None:
            self._last_value = val
            return val
        # Cleaner rejected the reading (or it was missing entirely).
        # Fall back to the last accepted value, but only while we're
        # still inside the hybrid "hold last value" window, beyond that
        # we report None so HA marks the entity unavailable rather than
        # publishing indefinitely-stale data.
        if not self._within_hold_window():
            return None
        return self._last_value

    @property
    def available(self) -> bool:
        # A currently-acceptable reading means available, regardless of how long
        # the previous hold has run. This also breaks a deadlock: once the hold
        # window expires HA stops evaluating native_value (the only path that
        # accepts a reading and refreshes the cleaner anchor), so without this a
        # stale anchor would keep the sensor unavailable indefinitely even while
        # the device reports good values (observed on JET: SOC stuck unavailable
        # for hours after a discharge glitch, until a reload).
        if self.coordinator.get_value(self._canonical_key) is not None:
            return True
        if self._last_value is None:
            return self.coordinator.last_update_success
        if self._within_hold_window():
            return True
        # Hold window has expired with no fresh accepted reading ,
        # entity goes unavailable until the cleaner accepts again.
        return False

    def _within_hold_window(self) -> bool:
        """True while the entity may keep returning its last accepted value.

        After a cleaner-rejected reading, the entity holds the previous
        good value for ``hold_last_value_seconds`` (per brand profile).
        Beyond that window we surface the failure as unavailable instead
        of continuing to publish stale data, honest signal to users
        and automations that the underlying sensor has stopped working.
        """
        last_accepted_at = self.coordinator.cleaner_last_accepted_at(self._canonical_key)
        if last_accepted_at is None:
            # No cleaner state yet, treat as fresh (don't hide the entity
            # before we've seen any accepted reading).
            return True
        hold_seconds = float(self.coordinator.brand_profile.get("hold_last_value_seconds", 120))
        return (time.time() - last_accepted_at) <= hold_seconds


class AeccUnitSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """One telemetry value of one battery unit; raw, keyed on its serial."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: AeccBatteryCoordinator,
        config_entry: ConfigEntry,
        unit: dict[str, Any],
        key: str,
        name: str,
        canonical_key: str,
        unit_of_meas: str,
        icon: str | None,
        is_power: bool,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._unit_key = coordinator.unit_key(unit)
        self._canonical_key = canonical_key
        self._attr_unique_id = f"{config_entry.entry_id}_unit{self._unit_key}_{key}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit_of_meas
        self._attr_icon = icon
        self._attr_device_class = SensorDeviceClass.POWER if is_power else SensorDeviceClass.BATTERY
        if key in _DIAGNOSTIC_KEYS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = coordinator.unit_device_info(unit)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.get_unit_value(self._unit_key, self._canonical_key)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class AeccUnitStatusSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """Per-unit Charging / Discharging / Idle, derived from power (StorageStatus is an online flag)."""

    _attr_has_entity_name = True
    _attr_name = "Battery Status"
    _attr_icon = "mdi:battery-heart-variant"

    def __init__(
        self,
        coordinator: AeccBatteryCoordinator,
        config_entry: ConfigEntry,
        unit: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._unit_key = coordinator.unit_key(unit)
        self._attr_unique_id = f"{config_entry.entry_id}_unit{self._unit_key}_battery_status"
        self._attr_device_info = coordinator.unit_device_info(unit)

    def _powers(self) -> tuple[float | None, float | None, float | None]:
        return (
            self.coordinator.get_unit_value(self._unit_key, "battery_charging_power"),
            self.coordinator.get_unit_value(self._unit_key, "ac_charging_power"),
            self.coordinator.get_unit_value(self._unit_key, "battery_discharging_power"),
        )

    @property
    def native_value(self) -> str | None:
        charge, ac_charge, discharge = self._powers()
        if charge is None and ac_charge is None and discharge is None:
            return None
        return _derive_status(charge or 0, ac_charge or 0, discharge or 0)

    @property
    def available(self) -> bool:
        return super().available and any(p is not None for p in self._powers())


class AeccEnergySensor(CoordinatorEntity[AeccBatteryCoordinator], RestoreSensor):
    """Accumulated energy (kWh) computed by integrating power over time."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: AeccBatteryCoordinator,
        config_entry: ConfigEntry,
        key: str,
        name: str,
        power_fn: Callable[[AeccBatteryCoordinator], float | None],
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._power_fn = power_fn
        self._attr_unique_id = f"{config_entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._accumulated_kwh: float = 0.0
        self._last_update_time: datetime | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> float:
        return round(self._accumulated_kwh, 3)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The stored sensor data keeps the total even when the entity was
        # unavailable at shutdown or reload; the state alone does not.
        last_data = await self.async_get_last_sensor_data()
        if last_data is not None and isinstance(last_data.native_value, (int, float)):
            self._accumulated_kwh = float(last_data.native_value)
            return
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            try:
                self._accumulated_kwh = float(last_state.state)
            except (TypeError, ValueError):
                self._accumulated_kwh = 0.0

    @callback
    def _handle_coordinator_update(self) -> None:
        now = utcnow()

        power_w = self._power_fn(self.coordinator)

        if power_w is not None and self._last_update_time is not None:
            delta_seconds = (now - self._last_update_time).total_seconds()
            if 0 < delta_seconds <= _MAX_GAP_SECONDS:
                delta_kwh = float(power_w) * delta_seconds / 3_600_000
                self._accumulated_kwh += delta_kwh

        if power_w is not None:
            self._last_update_time = now

        self.async_write_ha_state()


class AeccGridExportSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """Grid export power derived from grid_power. Export = positive grid values only."""

    _attr_has_entity_name = True
    _attr_name = "Grid Export Power"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_grid_export_power"

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> float | None:
        grid = self.coordinator.get_value("grid_power")
        if grid is None:
            return None
        try:
            return max(0, round(-float(grid), 1))
        except (TypeError, ValueError):
            return None


class AeccBatteryPowerSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """Single signed value: positive = charging, negative = discharging."""

    _attr_has_entity_name = True
    _attr_name = "Battery Power"
    _attr_icon = "mdi:battery-sync"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_battery_power"

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> float | None:
        return self.coordinator.battery_power_w()


class AeccBatteryStatusSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """Charging / Discharging / Idle from the sign of Battery Power, with an idle band."""

    _attr_has_entity_name = True
    _attr_name = "Battery Status"
    _attr_icon = "mdi:battery-heart-variant"

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_battery_status"

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> str | None:
        power = self.coordinator.battery_power_w()
        if power is None:
            return None
        if power > IDLE_DEADBAND_W:
            return "Charging"
        if power < -IDLE_DEADBAND_W:
            return "Discharging"
        return "Idle"


class AeccFirmwareSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """Firmware version from DeviceManagement probe (supported on some AECC devices)."""

    _attr_has_entity_name = True
    _attr_name = "Firmware Version"
    _attr_icon = "mdi:chip"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_firmware_version"

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> str | None:
        return self.coordinator.firmware_version


class AeccWifiSignalSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """WiFi signal strength of the datalogger from DeviceManagement reg 76."""

    _attr_has_entity_name = True
    _attr_name = "WiFi Signal"
    _attr_icon = "mdi:wifi"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_wifi_rssi"

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> int | None:
        return self.coordinator.wifi_rssi


class AeccModbusSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """One decoded Modbus register, refreshed on the Modbus cadence.

    No cleaner: these values never pass through get_value, and a failed
    refresh keeps the previous reading in the coordinator rather than
    publishing a zero.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AeccBatteryCoordinator,
        config_entry: ConfigEntry,
        key: str,
        name: str,
        register: int,
        unit: str | None,
        device_class: SensorDeviceClass | None,
        state_class: SensorStateClass | None,
        entity_category: EntityCategory | None,
        icon: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._register = register
        self._attr_unique_id = f"{config_entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_entity_category = entity_category
        self._attr_icon = icon
        if MODBUS_REGISTERS[register][1] != 1:
            self._attr_suggested_display_precision = 1

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> int | float | None:
        return self.coordinator.modbus.get(self._register)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._register in self.coordinator.modbus
