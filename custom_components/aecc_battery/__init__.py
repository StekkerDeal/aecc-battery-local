"""AECC Battery - local TCP integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import (
    BRAND_PROFILES,
    CONF_EXTENDED_POWER,
    CONF_HOST,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    DEFAULT_BRAND_PROFILE,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .coordinator import AeccBatteryCoordinator
from .tcp_client import AeccTcpClient
from .tcp_manager import TCPClientManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.SELECT]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    name: str = entry.data[CONF_NAME]
    manufacturer: str = entry.data.get(CONF_MANUFACTURER, "AECC")
    model: str = entry.data.get(CONF_MODEL, "")

    client = AeccTcpClient(host, port, timeout=DEFAULT_TIMEOUT)
    try:
        await client.async_connect()
    except (TimeoutError, OSError, ConnectionError) as exc:
        raise ConfigEntryNotReady(f"Cannot connect to {host}:{port} - {exc}") from exc

    extended_power = entry.options.get(CONF_EXTENDED_POWER, False)
    brand_profile = BRAND_PROFILES.get(manufacturer, DEFAULT_BRAND_PROFILE)
    coordinator = AeccBatteryCoordinator(
        hass,
        client,
        name,
        manufacturer=manufacturer,
        model=model,
        extended_power=extended_power,
        brand_profile=brand_profile,
    )
    await coordinator.async_config_entry_first_refresh()

    # Read initial register state and probe DeviceManagement
    await coordinator.async_read_initial_state()
    await coordinator.async_probe_device_management()

    # Up to 1.4.3 the device was keyed by host:port (the serial never parsed on
    # JET). 1.4.4 fixed the parse, so the identifier flips to the serial and HA
    # would otherwise create a second device and orphan the old one. Migrate the
    # old device in place before entities attach.
    _migrate_device_identifier(hass, host, port, coordinator.device_serial)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("AECC Battery '%s' (%s) set up at %s:%s", name, manufacturer, host, port)
    return True


def _migrate_device_identifier(hass: HomeAssistant, host: str, port: int, serial: str | None) -> None:
    """Move the pre-1.4.4 host:port device onto the serial identifier.

    No-op when the device never reported a serial (identifier stays host:port) or
    when there is no legacy device to migrate. If a serial-keyed device already
    exists (e.g. a 1.4.4 install already created the duplicate), the orphaned
    host:port device is removed instead of renamed.
    """
    if not serial:
        return
    registry = dr.async_get(hass)
    old = registry.async_get_device(identifiers={(DOMAIN, f"{host}:{port}")})
    if old is None:
        return
    existing = registry.async_get_device(identifiers={(DOMAIN, serial)})
    if existing is not None and existing.id != old.id:
        registry.async_remove_device(old.id)
        _LOGGER.info("Removed orphaned host:port device for %s (now keyed by serial)", host)
    else:
        registry.async_update_device(old.id, new_identifiers={(DOMAIN, serial)})
        _LOGGER.info("Migrated device identifier from %s:%s to serial", host, port)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: AeccBatteryCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.client.async_disconnect()
        TCPClientManager.remove_instance(entry.data[CONF_HOST], entry.data[CONF_PORT])
    return unloaded
