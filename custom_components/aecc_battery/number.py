"""Number platform - Power Slider, Power Setpoint, Min SOC, Max SOC."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AeccBatteryCoordinator
from .entity import raise_set_failed

_LOGGER = logging.getLogger(__name__)

DEPRECATION_NOTE = (
    "%s is deprecated and will be removed in 2.0.0. Use the signed Power Setpoint "
    "entity instead: one write, positive charges, negative discharges, 0 idles."
)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AeccBatteryCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        [
            AeccPowerSlider(coordinator, config_entry),
            AeccPowerSetpoint(coordinator, config_entry),
            AeccMinSoc(coordinator, config_entry),
            AeccMaxSoc(coordinator, config_entry),
        ]
    )


class AeccPowerSetpoint(CoordinatorEntity[AeccBatteryCoordinator], NumberEntity):
    """Signed power setpoint: positive = charge, negative = discharge, 0 = idle.

    One-write control surface for external energy managers (EMHASS, evcc);
    the direction+power entities need two ordered writes. Step 1 because
    optimizers command arbitrary watt values.
    """

    _attr_has_entity_name = True
    _attr_name = "Power Setpoint"
    _attr_icon = "mdi:battery-sync-outline"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        # "_power_setpoint" is historically taken by AeccPowerSlider.
        self._attr_unique_id = f"{config_entry.entry_id}_signed_power_setpoint"
        # Asymmetric bounds: HA itself rejects a command beyond either
        # direction's configured limit (positive = charge).
        self._attr_native_min_value = -coordinator.max_discharge_power
        self._attr_native_max_value = coordinator.max_charge_power

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> float:
        direction = self.coordinator.commanded_direction
        if direction == "Charge":
            return self.coordinator.commanded_power
        if direction == "Discharge":
            return -self.coordinator.commanded_power
        return 0

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_set_native_value(self, value: float) -> None:
        # The coordinator records direction + power + Custom mode and calls
        # async_update_listeners() on success, refreshing every entity.
        if not await self.coordinator.async_set_power_setpoint(value):
            raise_set_failed(self._attr_name)


class AeccPowerSlider(CoordinatorEntity[AeccBatteryCoordinator], NumberEntity):
    """Battery power slider. Max is the larger of the two per-direction limits.

    Deprecated: an unsigned magnitude needs the Battery Direction select to
    mean anything, which is two ordered writes for one intent. The signed
    Power Setpoint says the same thing in one. Removal in 2.0.0.
    """

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
        self._deprecation_logged = False

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def native_value(self) -> float:
        return self.coordinator.commanded_power

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_set_native_value(self, value: float) -> None:
        if not self._deprecation_logged:
            _LOGGER.warning(DEPRECATION_NOTE, "Battery Power")
            self._deprecation_logged = True

        power_w = int(value)
        direction = self.coordinator.commanded_direction
        if direction == "Idle" and power_w > 0:
            direction = "Charge"

        # The coordinator records direction + power + Custom mode and calls
        # async_update_listeners() on success, refreshing every entity. Note
        # that commanded_power is left alone until the write lands: claiming
        # it up front made the slider show a value the battery never took.
        if not await self.coordinator.async_set_battery_control(direction, power_w):
            raise_set_failed(self._attr_name)


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
        if not await self.coordinator.async_set_min_soc(soc):
            raise_set_failed(self._attr_name)
        self._commanded = soc
        self.async_write_ha_state()


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
        if not await self.coordinator.async_set_max_soc(soc):
            raise_set_failed(self._attr_name)
        self._commanded = soc
        self.async_write_ha_state()
