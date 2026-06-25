"""Sensor platform tests, focused on the hybrid hold-then-unavailable behavior.

These tests cover the entity-level fallback that pairs with the cleaner-level
rejection in the coordinator. When the cleaner rejects readings, the entity
must keep returning its last accepted value for ``hold_last_value_seconds``,
then transition to unavailable so users see an honest signal that the
underlying sensor has stopped responding.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.aecc_battery.const import BRAND_PROFILES
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator
from custom_components.aecc_battery.sensor import (
    AeccFirmwareSensor,
    AeccSensor,
    AeccWifiSignalSensor,
)


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.host = "192.168.1.100"
    client.port = 8080
    return client


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_client) -> AeccBatteryCoordinator:
    coord = AeccBatteryCoordinator(
        hass,
        mock_client,
        device_name="Test",
        manufacturer="Lunergy",
        brand_profile=BRAND_PROFILES["Lunergy"],
    )
    return coord


@pytest.fixture
def config_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def _make_sensor(coordinator, config_entry) -> AeccSensor:
    return AeccSensor(
        coordinator=coordinator,
        config_entry=config_entry,
        key="battery_soc",
        name="Battery SOC",
        canonical_key="battery_soc",
        unit="%",
        icon="mdi:battery",
        is_power=False,
    )


def test_holds_last_value_immediately_after_rejection(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """A single rejected reading falls back to the last accepted value."""
    sensor = _make_sensor(coordinator, config_entry)
    # First poll: clean SOC of 70.
    coordinator.data = {
        "Storage_list": [{"BatterySoc": "70"}],
        "SSumInfoList": {},
    }
    assert sensor.native_value == 70.0

    # Glitch: SOC=0 during active discharge, cleaner rejects.
    coordinator.data = {
        "Storage_list": [{"BatterySoc": "0", "BatteryDischargingPower": "10000"}],
        "SSumInfoList": {},
    }
    assert sensor.native_value == 70.0  # held last value
    assert sensor.available is True


def test_goes_unavailable_after_hold_window_expires(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """After hold_last_value_seconds with no fresh acceptance, entity = unavailable."""
    sensor = _make_sensor(coordinator, config_entry)

    coordinator.data = {
        "Storage_list": [{"BatterySoc": "70"}],
        "SSumInfoList": {},
    }
    assert sensor.native_value == 70.0
    accepted_at = coordinator.cleaner_last_accepted_at("battery_soc")
    assert accepted_at is not None

    # Manually age the acceptance timestamp past the hold window.
    hold_seconds = coordinator.brand_profile["hold_last_value_seconds"]
    coordinator._cleaner_last_accepted_at["battery_soc"] = time.time() - hold_seconds - 10

    # Glitch, and the hold window has long expired.
    coordinator.data = {
        "Storage_list": [{"BatterySoc": "0", "BatteryDischargingPower": "10000"}],
        "SSumInfoList": {},
    }
    assert sensor.native_value is None
    assert sensor.available is False


def test_recovery_resets_availability(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """When the cleaner accepts again, entity returns to available."""
    sensor = _make_sensor(coordinator, config_entry)

    # Establish baseline + age beyond hold window
    coordinator.data = {
        "Storage_list": [{"BatterySoc": "70"}],
        "SSumInfoList": {},
    }
    sensor.native_value
    coordinator._cleaner_last_accepted_at["battery_soc"] = (
        time.time() - coordinator.brand_profile["hold_last_value_seconds"] - 10
    )

    # Sensor recovers, clean reading
    coordinator.data = {
        "Storage_list": [{"BatterySoc": "65"}],
        "SSumInfoList": {},
    }
    assert sensor.native_value == 65.0
    assert sensor.available is True


def test_first_reading_treated_as_in_window(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """Before any acceptance, hold-window check should not preemptively hide entity."""
    sensor = _make_sensor(coordinator, config_entry)
    # No coordinator data yet, no cleaner state, entity falls back to
    # coordinator.last_update_success for availability. Native value is None.
    assert sensor.native_value is None


def test_wifi_signal_sensor_reports_rssi(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """The WiFi sensor surfaces the coordinator's RSSI and the throttled updates."""
    coordinator.wifi_rssi = -35
    sensor = AeccWifiSignalSensor(coordinator, config_entry)
    assert sensor.native_value == -35
    # A later throttled refresh propagates without re-creating the entity.
    coordinator.wifi_rssi = -55
    assert sensor.native_value == -55


def test_diagnostic_sensors_use_entity_category_enum(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """entity_category must resolve to the EntityCategory enum, not a bare string.

    HA rejects a string at registration ("entity_category must be a valid
    EntityCategory instance"), which silently dropped both diagnostic sensors
    before 1.4.5.
    """
    assert AeccFirmwareSensor(coordinator, config_entry).entity_category is EntityCategory.DIAGNOSTIC
    assert AeccWifiSignalSensor(coordinator, config_entry).entity_category is EntityCategory.DIAGNOSTIC
