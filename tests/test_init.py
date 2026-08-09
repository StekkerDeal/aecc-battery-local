"""Tests for the integration setup helpers (device-identifier migration)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aecc_battery import _migrate_device_identifier, resolve_power_limits
from custom_components.aecc_battery.const import (
    CONF_EXTENDED_POWER,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    DOMAIN,
)

_HOST = "192.168.1.50"
_PORT = 8080
_LEGACY = f"{_HOST}:{_PORT}"
_SERIAL = "JM0000000ASG0001"


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=_LEGACY)
    entry.add_to_hass(hass)
    return entry


# ── Power limit resolution (per-direction options + legacy extended_power) ────


def test_power_limits_default() -> None:
    assert resolve_power_limits({}) == (800, 800)


def test_power_limits_legacy_extended_maps_to_2400() -> None:
    """Pre-1.5.2 entries with the extended boolean keep 2400W both ways."""
    assert resolve_power_limits({CONF_EXTENDED_POWER: True}) == (2400, 2400)
    assert resolve_power_limits({CONF_EXTENDED_POWER: False}) == (800, 800)


def test_power_limits_new_keys_win_over_legacy() -> None:
    options = {
        CONF_EXTENDED_POWER: True,
        CONF_MAX_CHARGE_POWER: 2400,
        CONF_MAX_DISCHARGE_POWER: 800,
    }
    assert resolve_power_limits(options) == (2400, 800)


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
