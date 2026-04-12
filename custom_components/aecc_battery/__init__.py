"""AECC Battery - local TCP integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_HOST, CONF_MANUFACTURER, CONF_MODEL, CONF_NAME, CONF_PORT,
    CONF_EXTENDED_POWER, DEFAULT_TIMEOUT, DOMAIN,
)
from .coordinator import AeccBatteryCoordinator
from .tcp_client import AeccTcpClient
from .tcp_manager import TCPClientManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.SWITCH, Platform.SELECT]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    name: str = entry.data[CONF_NAME]
    manufacturer: str = entry.data.get(CONF_MANUFACTURER, "AECC")
    model: str = entry.data.get(CONF_MODEL, "")

    client = AeccTcpClient(host, port, timeout=DEFAULT_TIMEOUT)
    try:
        await client.async_connect()
    except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
        raise ConfigEntryNotReady(f"Cannot connect to {host}:{port} - {exc}") from exc

    extended_power = entry.options.get(CONF_EXTENDED_POWER, False)
    coordinator = AeccBatteryCoordinator(
        hass, client, name,
        manufacturer=manufacturer, model=model,
        extended_power=extended_power,
    )
    await coordinator.async_config_entry_first_refresh()

    # Read initial register state and probe DeviceManagement
    await coordinator.async_read_initial_state()
    await coordinator.async_probe_device_management()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("AECC Battery '%s' (%s) set up at %s:%s", name, manufacturer, host, port)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: AeccBatteryCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.client.async_disconnect()
        TCPClientManager.remove_instance(entry.data[CONF_HOST], entry.data[CONF_PORT])
    return unloaded
