"""Tests for the integration setup path and its helpers.

Covers the device-identifier migration, power-limit resolution, and the socket
discipline of a setup that keeps failing.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aecc_battery import _migrate_device_identifier, resolve_power_limits
from custom_components.aecc_battery.const import (
    CONF_EXTENDED_POWER,
    CONF_HOST,
    CONF_MANUFACTURER,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    DOMAIN,
    max_power_for_brand,
)
from custom_components.aecc_battery.tcp_manager import TCPClientManager

_HOST = "192.168.1.50"
_PORT = 8080
_LEGACY = f"{_HOST}:{_PORT}"
_SERIAL = "JM0000000ASG0001"

_ENTRY_DATA = {
    CONF_HOST: _HOST,
    CONF_PORT: _PORT,
    CONF_NAME: "Test Battery",
    CONF_MANUFACTURER: "Sunpura",
    CONF_MODEL: "S2400",
}


@pytest.fixture(autouse=True)
def _clear_registry():
    """Keep the class-level manager registry from leaking across tests."""
    TCPClientManager._connections.clear()
    yield
    TCPClientManager._connections.clear()


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


def test_brand_power_ceiling() -> None:
    """Only brands rated above 2400W get more; everything else keeps 2400W."""
    assert max_power_for_brand("TSUN") == 2500
    assert max_power_for_brand("Sunpura") == 2400
    assert max_power_for_brand("Other") == 2400
    assert max_power_for_brand(None) == 2400


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


# ── Socket discipline across failed setups ────────────────────────────────────


async def _three_failed_setups(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Fail setup three times the way a retrying entry does.

    HA does not call async_unload_entry for a SETUP_RETRY entry, so the manager
    and whatever socket it holds survive every attempt.
    """
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    for _ in range(2):
        await hass.config_entries.async_reload(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
    # Cancels the pending retry timer.
    await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_setup_never_holds_two_sockets(hass: HomeAssistant, socket_tracker) -> None:
    """One socket per outage, never two at once.

    A device that accepts connections and answers nothing must not cost a new
    socket per attempt: the abandoned one can own the session and lock us out.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=_ENTRY_DATA, unique_id=_LEGACY)
    entry.add_to_hass(hass)

    await _three_failed_setups(hass, entry)

    assert socket_tracker.max_live == 1
    assert socket_tracker.opened == 1


async def test_failed_setup_recycles_the_suspect_socket_once(
    hass: HomeAssistant, socket_tracker, caplog: pytest.LogCaptureFixture
) -> None:
    """The read-timeout streak must outlive the client, which setup rebuilds."""
    entry = MockConfigEntry(domain=DOMAIN, data=_ENTRY_DATA, unique_id=_LEGACY)
    entry.add_to_hass(hass)

    with caplog.at_level(logging.WARNING, logger="custom_components.aecc_battery.tcp_client"):
        await _three_failed_setups(hass, entry)

    assert caplog.text.count("recycling socket") == 1
