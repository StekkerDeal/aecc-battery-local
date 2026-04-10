"""Number platform – Power Setpoint only."""
from __future__ import annotations
import logging
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import CONF_HOST, CONF_NAME, CONF_PORT, DOMAIN, MAX_BATTERY_POWER_W
from .coordinator import LunergyLocalCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: LunergyLocalCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([LunergyPowerSetpoint(coordinator, config_entry)])

class LunergyPowerSetpoint(CoordinatorEntity[LunergyLocalCoordinator], NumberEntity):
    """Battery power setpoint: +2400 W = full charge, -2400 W = full discharge."""
    _attr_has_entity_name = True
    _attr_name = "Power Setpoint"
    _attr_icon = "mdi:battery-sync"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = -MAX_BATTERY_POWER_W
    _attr_native_max_value =  MAX_BATTERY_POWER_W
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_power_setpoint"
        self._commanded: float | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._config_entry.data[CONF_HOST]}:{self._config_entry.data[CONF_PORT]}")},
            name=self._config_entry.data[CONF_NAME],
            manufacturer="Lunergy", model="Hub 2400 AC",
        )

    @property
    def native_value(self) -> float | None:
        if self._commanded is None:
            return None
        v = self._commanded
        return int(v) if v == int(v) else v

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success or self._commanded is not None

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {"last_set_response": getattr(self.coordinator, "_last_set_response", "never sent")}
        scan = getattr(self.coordinator, "_register_scan", None)
        if scan:
            attrs["register_scan"] = scan.get("non_empty_registers", scan)
        return attrs

    async def async_set_native_value(self, value: float) -> None:
        success = await self.coordinator.async_set_power_setpoint(value)
        if success:
            self._commanded = value
            self.async_write_ha_state()
        else:
            _LOGGER.warning("Failed to set power setpoint to %s W", value)
