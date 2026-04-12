"""Number platform - Power Slider, Min SOC, Max SOC."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AeccBatteryCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AeccBatteryCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        [
            AeccPowerSlider(coordinator, config_entry),
            AeccMinSoc(coordinator, config_entry),
            AeccMaxSoc(coordinator, config_entry),
        ]
    )


class AeccPowerSlider(CoordinatorEntity[AeccBatteryCoordinator], NumberEntity):
    """Battery power slider. Max depends on extended power setting (800W or 2400W)."""

    _attr_has_entity_name = True
    _attr_name = "Battery Power"
    _attr_icon = "mdi:battery-sync"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0
    _attr_native_step = 100
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_power_setpoint"
        self._attr_native_max_value = coordinator.max_register_power
        self._commanded: float = coordinator.initial_power if coordinator.initial_power is not None else 0

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> float:
        return self._commanded

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_set_native_value(self, value: float) -> None:
        power_w = int(value)
        self._commanded = power_w
        self.coordinator.commanded_power = power_w

        direction = self.coordinator.commanded_direction
        if direction == "Idle" and power_w > 0:
            direction = "Charge"

        success = await self.coordinator.async_set_battery_control(direction, power_w)
        if success:
            self.async_write_ha_state()
        else:
            _LOGGER.warning("Failed to set power to %s W", power_w)


class AeccMinSoc(CoordinatorEntity[AeccBatteryCoordinator], NumberEntity):
    """Minimum discharge SOC (register 3023)."""

    _attr_has_entity_name = True
    _attr_name = "Discharge Limit"
    _attr_icon = "mdi:battery-arrow-down"
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 5
    _attr_native_max_value = 50
    _attr_native_step = 5
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_min_soc"
        self._commanded: float = coordinator.initial_min_soc if coordinator.initial_min_soc is not None else 10

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> float:
        return self._commanded

    async def async_set_native_value(self, value: float) -> None:
        soc = int(value)
        success = await self.coordinator.async_set_min_soc(soc)
        if success:
            self._commanded = soc
            self.async_write_ha_state()
        else:
            _LOGGER.warning("Failed to set min SOC to %s%%", soc)


class AeccMaxSoc(CoordinatorEntity[AeccBatteryCoordinator], NumberEntity):
    """Maximum charge SOC (register 3024)."""

    _attr_has_entity_name = True
    _attr_name = "Charge Limit"
    _attr_icon = "mdi:battery-arrow-up"
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_max_soc"
        self._commanded: float = coordinator.initial_max_soc if coordinator.initial_max_soc is not None else 98

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> float:
        return self._commanded

    async def async_set_native_value(self, value: float) -> None:
        soc = int(value)
        success = await self.coordinator.async_set_max_soc(soc)
        if success:
            self._commanded = soc
            self.async_write_ha_state()
        else:
            _LOGGER.warning("Failed to set max SOC to %s%%", soc)
