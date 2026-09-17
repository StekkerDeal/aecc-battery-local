"""Binary sensor platform for AECC Battery (Local TCP)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MB_DEVICE_STATUS
from .coordinator import AeccBatteryCoordinator


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AeccBatteryCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    if coordinator.modbus_supported:
        async_add_entities([AeccFaultSensor(coordinator, config_entry)])


class AeccFaultSensor(CoordinatorEntity[AeccBatteryCoordinator], BinarySensorEntity):
    """The device's general status register: 0 normal, 1 fault."""

    _attr_has_entity_name = True
    _attr_name = "Fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_fault"

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        status = self.coordinator.modbus.get(MB_DEVICE_STATUS)
        return None if status is None else status == 1

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and MB_DEVICE_STATUS in self.coordinator.modbus
