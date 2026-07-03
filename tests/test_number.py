"""Tests for the number platform, focused on the signed Power Setpoint entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aecc_battery.const import REG_CONTROL_TIME1
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator
from custom_components.aecc_battery.number import AeccPowerSetpoint, AeccPowerSlider
from custom_components.aecc_battery.select import AeccBatteryDirection


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.host = "192.168.1.100"
    client.port = 8080
    client.set_control_parameters = AsyncMock(return_value={"result": "ok"})
    # Readback None so write-verify exits silently.
    client.get_control_parameters = AsyncMock(return_value=None)
    return client


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_client) -> AeccBatteryCoordinator:
    coord = AeccBatteryCoordinator(
        hass,
        mock_client,
        device_name="Test Battery",
        manufacturer="Sunpura",
        model="S2400",
    )
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    return coord


@pytest.fixture
def extended_coordinator(hass: HomeAssistant, mock_client) -> AeccBatteryCoordinator:
    coord = AeccBatteryCoordinator(
        hass,
        mock_client,
        device_name="Test Battery",
        manufacturer="Sunpura",
        model="S2400",
        extended_power=True,
    )
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    return coord


@pytest.fixture
def config_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def _slot_power(mock_client) -> int:
    """Register power encoded in the last written control slot (field 3, signed)."""
    payload = mock_client.set_control_parameters.call_args[0][0]
    return int(payload[REG_CONTROL_TIME1].split(",")[3])


def test_setpoint_range_default(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    entity = AeccPowerSetpoint(coordinator, config_entry)
    assert entity.native_min_value == -800
    assert entity.native_max_value == 800
    assert entity.unique_id == "test_entry_signed_power_setpoint"


def test_setpoint_range_extended(extended_coordinator: AeccBatteryCoordinator, config_entry) -> None:
    entity = AeccPowerSetpoint(extended_coordinator, config_entry)
    assert entity.native_min_value == -2400
    assert entity.native_max_value == 2400


async def test_setpoint_positive_charges(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    entity = AeccPowerSetpoint(coordinator, config_entry)
    await entity.async_set_native_value(500)
    # Charge encodes as a negative register power in the control slot.
    assert _slot_power(coordinator.client) == -500
    assert coordinator.commanded_direction == "Charge"
    assert coordinator.commanded_power == 500
    assert entity.native_value == 500
    # The other control entities read the same coordinator state.
    assert AeccPowerSlider(coordinator, config_entry).native_value == 500
    assert AeccBatteryDirection(coordinator, config_entry).current_option == "Charge"


async def test_setpoint_negative_discharges(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    entity = AeccPowerSetpoint(coordinator, config_entry)
    await entity.async_set_native_value(-500)
    assert _slot_power(coordinator.client) == 500
    assert coordinator.commanded_direction == "Discharge"
    assert coordinator.commanded_power == 500
    assert entity.native_value == -500


async def test_setpoint_zero_idles(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    entity = AeccPowerSetpoint(coordinator, config_entry)
    await entity.async_set_native_value(0)
    payload = coordinator.client.set_control_parameters.call_args[0][0]
    assert payload[REG_CONTROL_TIME1].startswith("0,")  # disabled slot
    assert coordinator.commanded_direction == "Idle"
    assert entity.native_value == 0


async def test_setpoint_failed_write_keeps_state(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    entity = AeccPowerSetpoint(coordinator, config_entry)
    await entity.async_set_native_value(300)
    coordinator.client.set_control_parameters = AsyncMock(return_value=None)
    await entity.async_set_native_value(-700)
    # Failed write: commanded state (and thus every entity) keeps the last
    # successful command.
    assert coordinator.commanded_direction == "Charge"
    assert coordinator.commanded_power == 300
    assert entity.native_value == 300


async def test_battery_control_records_commanded_power(coordinator: AeccBatteryCoordinator) -> None:
    """Regression: async_set_battery_control must record power, not just direction."""
    assert await coordinator.async_set_battery_control("Charge", 700) is True
    assert coordinator.commanded_power == 700
    assert coordinator.commanded_direction == "Charge"
