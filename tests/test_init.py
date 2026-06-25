"""Tests for the integration setup helpers (device-identifier migration)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aecc_battery import _migrate_device_identifier
from custom_components.aecc_battery.const import DOMAIN

_HOST = "192.168.1.50"
_PORT = 8080
_LEGACY = f"{_HOST}:{_PORT}"
_SERIAL = "JM0225391ASG0290"


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=_LEGACY)
    entry.add_to_hass(hass)
    return entry


async def test_migration_renames_legacy_device(hass: HomeAssistant) -> None:
    """A host:port device is renamed onto the serial identifier (entities preserved)."""
    entry = _entry(hass)
    reg = dr.async_get(hass)
    reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, _LEGACY)})

    _migrate_device_identifier(hass, _HOST, _PORT, _SERIAL)

    assert reg.async_get_device(identifiers={(DOMAIN, _LEGACY)}) is None
    assert reg.async_get_device(identifiers={(DOMAIN, _SERIAL)}) is not None


async def test_migration_removes_orphan_when_serial_device_exists(hass: HomeAssistant) -> None:
    """When both devices exist (an already-upgraded 1.4.4 install), drop the orphan."""
    entry = _entry(hass)
    reg = dr.async_get(hass)
    reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, _LEGACY)})
    reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, _SERIAL)})

    _migrate_device_identifier(hass, _HOST, _PORT, _SERIAL)

    assert reg.async_get_device(identifiers={(DOMAIN, _LEGACY)}) is None
    assert reg.async_get_device(identifiers={(DOMAIN, _SERIAL)}) is not None


async def test_migration_noop_without_serial(hass: HomeAssistant) -> None:
    """No serial (device never answered DeviceManagement) leaves the legacy device alone."""
    entry = _entry(hass)
    reg = dr.async_get(hass)
    reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, _LEGACY)})

    _migrate_device_identifier(hass, _HOST, _PORT, None)

    assert reg.async_get_device(identifiers={(DOMAIN, _LEGACY)}) is not None


async def test_migration_noop_when_no_legacy_device(hass: HomeAssistant) -> None:
    """A fresh install (only the serial device, or nothing) is untouched."""
    entry = _entry(hass)
    reg = dr.async_get(hass)
    reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, _SERIAL)})

    _migrate_device_identifier(hass, _HOST, _PORT, _SERIAL)

    assert reg.async_get_device(identifiers={(DOMAIN, _SERIAL)}) is not None
