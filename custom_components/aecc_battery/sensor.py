"""Sensor platform for AECC Battery (Local TCP)."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import utcnow

from .const import DOMAIN
from .coordinator import AeccBatteryCoordinator

_LOGGER = logging.getLogger(__name__)

# ── Standard power/measurement sensors ────────────────────────────────────────
_SENSORS = [
    #  (key,                        name,                        canonical_key,                unit,             icon,                            is_power)
    ("ac_charging_power",           "AC Charging Power",         "ac_charging_power",          UnitOfPower.WATT, "mdi:power-plug",                True),
    ("battery_discharging_power",   "Battery Discharging Power", "battery_discharging_power",  UnitOfPower.WATT, "mdi:battery-arrow-down",        True),
    ("battery_soc",                 "Battery SOC",               "battery_soc",                PERCENTAGE,       "mdi:battery",                   False),
    ("pv_power",                    "PV Power",                  "pv_power",                   UnitOfPower.WATT, "mdi:solar-power",               True),
    ("pv_charging_power",           "PV Charging Power",         "pv_charging_power",          UnitOfPower.WATT, "mdi:solar-panel",               True),
    ("grid_power",                  "Grid / Meter Power",        "grid_power",                 UnitOfPower.WATT, "mdi:transmission-tower",        True),
    ("backup_power",                "Backup Power",              "backup_power",               UnitOfPower.WATT, "mdi:power-plug-battery",        True),
    ("pv1_power",                   "PV String 1 Power",         "pv1_power",                  UnitOfPower.WATT, "mdi:solar-panel",               True),
    ("pv2_power",                   "PV String 2 Power",         "pv2_power",                  UnitOfPower.WATT, "mdi:solar-panel",               True),
]

# ── Energy counter definitions ────────────────────────────────────────────────
_ENERGY_SENSORS = [
    ("energy_charged",    "Energy Charged",    ["ac_charging_power", "pv_charging_power"], "mdi:battery-charging"),
    ("energy_discharged", "Energy Discharged",  ["battery_discharging_power"],              "mdi:battery-arrow-down-outline"),
    ("energy_generated",  "Energy Generated",   ["pv_power"],                               "mdi:solar-power"),
]

_MAX_GAP_SECONDS = 60


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AeccBatteryCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities: list[SensorEntity] = []

    for key, name, canonical_key, unit, icon, is_power in _SENSORS:
        entities.append(
            AeccSensor(coordinator, config_entry, key, name, canonical_key, unit, icon, is_power)
        )

    for key, name, power_keys, icon in _ENERGY_SENSORS:
        entities.append(
            AeccEnergySensor(coordinator, config_entry, key, name, power_keys, icon)
        )

    entities.append(AeccGridExportSensor(coordinator, config_entry))
    entities.append(AeccBatteryPowerSensor(coordinator, config_entry))
    entities.append(AeccBatteryStatusSensor(coordinator, config_entry))

    if coordinator.firmware_version is not None:
        entities.append(AeccFirmwareSensor(coordinator, config_entry))

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
        icon: str,
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
        return self._last_value

    @property
    def available(self) -> bool:
        return self._last_value is not None or self.coordinator.last_update_success


class AeccEnergySensor(CoordinatorEntity[AeccBatteryCoordinator], RestoreEntity, SensorEntity):
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
        power_keys: list[str],
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._power_keys = power_keys
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
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            try:
                self._accumulated_kwh = float(last_state.state)
            except (TypeError, ValueError):
                self._accumulated_kwh = 0.0

    @callback
    def _handle_coordinator_update(self) -> None:
        now = utcnow()

        total_power_w = 0.0
        any_valid = False
        for key in self._power_keys:
            val = self.coordinator.get_value(key)
            if val is not None:
                try:
                    total_power_w += float(val)
                    any_valid = True
                except (TypeError, ValueError):
                    pass

        if any_valid and self._last_update_time is not None:
            delta_seconds = (now - self._last_update_time).total_seconds()
            if 0 < delta_seconds <= _MAX_GAP_SECONDS:
                delta_kwh = total_power_w * delta_seconds / 3_600_000
                self._accumulated_kwh += delta_kwh

        if any_valid:
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
        charge = self.coordinator.get_value("battery_charging_power") or 0
        ac_charge = self.coordinator.get_value("ac_charging_power") or 0
        discharge = self.coordinator.get_value("battery_discharging_power") or 0
        try:
            effective_charge = max(float(charge), float(ac_charge))
            return round(effective_charge - float(discharge), 1)
        except (TypeError, ValueError):
            return None


class AeccBatteryStatusSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """Derived text status: Charging / Discharging / Idle."""

    _attr_has_entity_name = True
    _attr_name = "Battery Status"
    _attr_icon = "mdi:battery-heart-variant"

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_battery_status"
        self._last_status: str = "Idle"

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> str:
        charge = self.coordinator.get_value("battery_charging_power")
        ac_charge = self.coordinator.get_value("ac_charging_power")
        discharge = self.coordinator.get_value("battery_discharging_power")
        if charge is None and ac_charge is None and discharge is None:
            return self._last_status
        try:
            charge_f = float(charge or 0)
            ac_charge_f = float(ac_charge or 0)
            discharge_f = float(discharge or 0)
        except (TypeError, ValueError):
            return self._last_status
        if charge_f > 0 or ac_charge_f > 0:
            status = "Charging"
        elif discharge_f > 0:
            status = "Discharging"
        else:
            status = "Idle"
        self._last_status = status
        return status


class AeccFirmwareSensor(CoordinatorEntity[AeccBatteryCoordinator], SensorEntity):
    """Firmware version from DeviceManagement probe (supported on some AECC devices)."""

    _attr_has_entity_name = True
    _attr_name = "Firmware Version"
    _attr_icon = "mdi:chip"
    _attr_entity_category = "diagnostic"

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
