"""Select platform - battery work mode and direction."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, WORK_MODES
from .coordinator import AeccBatteryCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AeccBatteryCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        [
            AeccWorkModeSelect(coordinator, config_entry),
            AeccBatteryDirection(coordinator, config_entry),
        ]
    )


class AeccWorkModeSelect(CoordinatorEntity[AeccBatteryCoordinator], SelectEntity):
    """Dropdown to switch the battery between Self-Consumption, Custom, and Disabled."""

    _attr_icon = "mdi:battery-sync"
    _attr_has_entity_name = True
    _attr_name = "Work Mode"
    _attr_options = WORK_MODES

    def __init__(
        self,
        coordinator: AeccBatteryCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_work_mode"
        self._current_mode: str | None = coordinator.initial_work_mode

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def current_option(self) -> str | None:
        return self._current_mode

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_select_option(self, option: str) -> None:
        _LOGGER.info("User selected work mode: %s", option)
        success = await self.coordinator.async_set_work_mode(option)
        if success:
            self._current_mode = option
            self.async_write_ha_state()
        else:
            _LOGGER.error("Failed to set work mode to '%s'", option)


DIRECTION_OPTIONS = ["Charge", "Discharge", "Idle"]


class AeccBatteryDirection(CoordinatorEntity[AeccBatteryCoordinator], SelectEntity):
    """Select charge direction. Automatically switches to Custom mode when Charge/Discharge is selected."""

    _attr_icon = "mdi:battery-charging-wireless"
    _attr_has_entity_name = True
    _attr_name = "Battery Direction"
    _attr_options = DIRECTION_OPTIONS

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_battery_direction"
        charge = coordinator.get_value("battery_charging_power") or 0
        discharge = coordinator.get_value("battery_discharging_power") or 0
        try:
            if float(charge) > 0:
                self._current_direction = "Charge"
            elif float(discharge) > 0:
                self._current_direction = "Discharge"
            else:
                self._current_direction = "Idle"
        except (TypeError, ValueError):
            self._current_direction = "Idle"
        coordinator.commanded_direction = self._current_direction

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def current_option(self) -> str:
        return self._current_direction

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_select_option(self, option: str) -> None:
        _LOGGER.info("User selected battery direction: %s", option)
        power = self.coordinator.commanded_power or 0
        if option == "Idle":
            power = 0
        success = await self.coordinator.async_set_battery_control(option, power)
        if success:
            self._current_direction = option
            self.coordinator.commanded_direction = option
            self.async_write_ha_state()
        else:
            _LOGGER.error("Failed to set battery direction to '%s'", option)
