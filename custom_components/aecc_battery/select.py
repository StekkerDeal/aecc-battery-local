"""Select platform - battery work mode and direction."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, WORK_MODES
from .coordinator import AeccBatteryCoordinator
from .entity import raise_set_failed, raise_set_rejected
from .number import DEPRECATION_NOTE

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
    """Dropdown to switch the battery between Self-Consumption and Custom.

    Reads its displayed value from the coordinator so it reflects Custom when
    the Battery Direction or Power slider is used, instead of going stale.
    """

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

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def current_option(self) -> str | None:
        return self.coordinator.current_work_mode

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_select_option(self, option: str) -> None:
        _LOGGER.info("User selected work mode: %s", option)
        if option not in WORK_MODES:
            raise_set_rejected(self._attr_name, f"'{option}' is not one of {', '.join(WORK_MODES)}")
        # The coordinator records the mode and calls async_update_listeners()
        # on success, which refreshes this entity (and the others).
        if not await self.coordinator.async_set_work_mode(option):
            raise_set_failed(self._attr_name)


DIRECTION_OPTIONS = ["Charge", "Discharge", "Idle"]


class AeccBatteryDirection(CoordinatorEntity[AeccBatteryCoordinator], SelectEntity):
    """Select charge direction. Automatically switches to Custom mode when Charge/Discharge is selected.

    Deprecated: direction plus magnitude is two writes for one intent, and the
    two entities have to be kept in sync. The signed Power Setpoint carries the
    direction in its sign. Removal in 2.0.0.
    """

    _attr_icon = "mdi:battery-charging-wireless"
    _attr_has_entity_name = True
    _attr_name = "Battery Direction"
    _attr_options = DIRECTION_OPTIONS

    def __init__(self, coordinator: AeccBatteryCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_battery_direction"
        self._deprecation_logged = False

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def current_option(self) -> str:
        return self.coordinator.commanded_direction

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_select_option(self, option: str) -> None:
        _LOGGER.info("User selected battery direction: %s", option)
        if not self._deprecation_logged:
            _LOGGER.warning(DEPRECATION_NOTE, "Battery Direction")
            self._deprecation_logged = True

        if option not in DIRECTION_OPTIONS:
            raise_set_rejected(self._attr_name, f"'{option}' is not one of {', '.join(DIRECTION_OPTIONS)}")

        power = self.coordinator.commanded_power or 0
        if option == "Idle":
            power = 0
        # The coordinator records the direction + Custom mode and calls
        # async_update_listeners() on success, refreshing every entity.
        if not await self.coordinator.async_set_battery_control(option, power):
            raise_set_failed(self._attr_name)
