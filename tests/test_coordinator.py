"""Tests for the AECC Battery coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.aecc_battery.const import (
    DOMAIN,
    MAX_BATTERY_POWER_W,
    MAX_REGISTER_POWER_DEFAULT,
    REG_AI_SMART_CHARGE,
    REG_AI_SMART_DISC,
    REG_CONTROL_TIME1,
    REG_CUSTOM_MODE,
    REG_EMS_ENABLE,
    REG_MAX_FEED_POWER,
    REG_SCHEDULE_MODE,
)
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator


@pytest.fixture
def mock_client():
    """Return a lightweight mock TCP client."""
    client = AsyncMock()
    client.host = "192.168.1.100"
    client.port = 8080
    return client


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_client) -> AeccBatteryCoordinator:
    """Return a coordinator with mocked client."""
    return AeccBatteryCoordinator(
        hass,
        mock_client,
        device_name="Test Battery",
        manufacturer="Sunpura",
        model="S2400",
    )


# ── DeviceInfo ──────────────────────────────────────────────────────────────


def test_device_info_basic(coordinator: AeccBatteryCoordinator) -> None:
    """Test DeviceInfo returns correct manufacturer and model."""
    info = coordinator.device_info
    assert info["manufacturer"] == "Sunpura"
    assert info["model"] == "S2400"
    assert info["name"] == "Test Battery"
    assert (DOMAIN, "192.168.1.100:8080") in info["identifiers"]
    assert info["sw_version"] is None


def test_device_info_with_serial(coordinator: AeccBatteryCoordinator) -> None:
    """Test DeviceInfo uses serial as identifier when available."""
    coordinator.device_serial = "SN123456"
    coordinator.firmware_version = "v2.1.0"
    info = coordinator.device_info
    assert (DOMAIN, "SN123456") in info["identifiers"]
    assert info["sw_version"] == "v2.1.0"


def test_device_info_empty_model(hass: HomeAssistant, mock_client) -> None:
    """Test DeviceInfo returns None for empty model string."""
    coord = AeccBatteryCoordinator(hass, mock_client, "Test", model="")
    assert coord.device_info["model"] is None


# ── Field mapping / get_value ────────────────────────────────────────────────


def test_get_value_from_summary(coordinator: AeccBatteryCoordinator) -> None:
    """Test get_value reads from SSumInfoList."""
    coordinator.data = {
        "SSumInfoList": {"TotalPVPower": "1200"},
    }
    assert coordinator.get_value("pv_power") == 1200.0


def test_get_value_from_storage(coordinator: AeccBatteryCoordinator) -> None:
    """Test get_value reads from Storage_list with 10x scaling."""
    coordinator.data = {
        "Storage_list": [{"AcChargingPower": "5000"}],  # 5000 / 10 = 500W
        "SSumInfoList": {},
    }
    assert coordinator.get_value("ac_charging_power") == 500.0


def test_get_value_storage_preferred_over_summary(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test Storage_list is preferred over SSumInfoList for sensors mapped storage-first."""
    coordinator.data = {
        "Storage_list": [{"BatterySoc": "80"}],
        "SSumInfoList": {"AverageBatteryAverageSOC": "75"},
    }
    # battery_soc maps storage first
    assert coordinator.get_value("battery_soc") == 80.0


def test_get_value_fallback_to_default(coordinator: AeccBatteryCoordinator) -> None:
    """Test get_value returns default when field not found."""
    coordinator.data = {"SSumInfoList": {}}
    assert coordinator.get_value("pv1_power") is None
    assert coordinator.get_value("pv1_power", default=0) == 0


def test_get_value_unknown_key(coordinator: AeccBatteryCoordinator) -> None:
    """Test get_value returns default for unknown canonical keys."""
    coordinator.data = {"SSumInfoList": {}}
    assert coordinator.get_value("nonexistent_sensor") is None


# ── Commanded state properties ───────────────────────────────────────────────


def test_commanded_power_property(coordinator: AeccBatteryCoordinator) -> None:
    """Test commanded_power getter and setter."""
    assert coordinator.commanded_power == 0
    coordinator.commanded_power = 500
    assert coordinator.commanded_power == 500


def test_commanded_direction_property(coordinator: AeccBatteryCoordinator) -> None:
    """Test commanded_direction getter and setter."""
    assert coordinator.commanded_direction == "Idle"
    coordinator.commanded_direction = "Charge"
    assert coordinator.commanded_direction == "Charge"


# ── Extended power ───────────────────────────────────────────────────────────


def test_max_power_default(hass: HomeAssistant, mock_client) -> None:
    """Test default max power is 800W."""
    coord = AeccBatteryCoordinator(hass, mock_client, "Test")
    assert coord.max_register_power == MAX_REGISTER_POWER_DEFAULT


def test_max_power_extended(hass: HomeAssistant, mock_client) -> None:
    """Test extended max power is 2400W."""
    coord = AeccBatteryCoordinator(
        hass, mock_client, "Test", extended_power=True
    )
    assert coord.max_register_power == MAX_BATTERY_POWER_W


# ── Battery control ──────────────────────────────────────────────────────────


async def test_set_battery_control_charge(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test charging sends correct register payload."""
    coordinator.data = {"SSumInfoList": {}}  # No Storage_list -> field7 = 4
    result = await coordinator.async_set_battery_control("Charge", 500)
    assert result is True

    call_args = coordinator.client.set_control_parameters.call_args[0][0]
    assert call_args[REG_EMS_ENABLE] == "1"
    assert call_args[REG_SCHEDULE_MODE] == "6"
    assert call_args[REG_CUSTOM_MODE] == "1"
    # Charge = negative register value
    assert "-500" in call_args[REG_CONTROL_TIME1]


async def test_set_battery_control_discharge(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test discharging sends positive register value."""
    coordinator.data = {"SSumInfoList": {}}
    result = await coordinator.async_set_battery_control("Discharge", 800)
    assert result is True

    slot = coordinator.client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert "800" in slot
    assert "-800" not in slot


async def test_set_battery_control_idle(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test idle sends disabled slot."""
    coordinator.data = {"SSumInfoList": {}}
    result = await coordinator.async_set_battery_control("Idle", 0)
    assert result is True

    slot = coordinator.client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert slot.startswith("0,")


async def test_set_battery_control_extended_writes_max_feed(
    hass: HomeAssistant, mock_client
) -> None:
    """Test extended power mode writes REG_MAX_FEED_POWER."""
    coord = AeccBatteryCoordinator(
        hass, mock_client, "Test", extended_power=True
    )
    coord.data = {"SSumInfoList": {}}
    mock_client.set_control_parameters.return_value = {"result": "ok"}
    await coord.async_set_battery_control("Charge", 2000)

    payload = mock_client.set_control_parameters.call_args[0][0]
    assert REG_MAX_FEED_POWER in payload
    assert payload[REG_MAX_FEED_POWER] == str(MAX_BATTERY_POWER_W)


async def test_set_battery_control_no_response(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test control returns False when battery doesn't respond."""
    coordinator.data = {"SSumInfoList": {}}
    coordinator.client.set_control_parameters.return_value = None
    result = await coordinator.async_set_battery_control("Charge", 500)
    assert result is False


# ── Work mode ────────────────────────────────────────────────────────────────


async def test_set_work_mode_self_consumption(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test Self-Consumption mode sends correct registers."""
    result = await coordinator.async_set_work_mode("Self-Consumption (AI)")
    assert result is True

    payload = coordinator.client.set_control_parameters.call_args[0][0]
    assert payload[REG_EMS_ENABLE] == "1"
    assert payload[REG_AI_SMART_CHARGE] == "1"
    assert payload[REG_AI_SMART_DISC] == "1"
    assert payload[REG_CUSTOM_MODE] == "0"


async def test_set_work_mode_disabled(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test Disabled mode turns off EMS."""
    result = await coordinator.async_set_work_mode("Disabled")
    assert result is True

    payload = coordinator.client.set_control_parameters.call_args[0][0]
    assert payload[REG_EMS_ENABLE] == "0"


async def test_set_work_mode_unknown(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test unknown mode returns False."""
    result = await coordinator.async_set_work_mode("Nonexistent")
    assert result is False


# ── SOC limits ───────────────────────────────────────────────────────────────


async def test_set_min_soc(coordinator: AeccBatteryCoordinator) -> None:
    """Test setting min SOC writes register 3023."""
    result = await coordinator.async_set_min_soc(15)
    assert result is True
    coordinator.client.set_control_parameters.assert_called_with({"3023": "15"})


async def test_set_max_soc(coordinator: AeccBatteryCoordinator) -> None:
    """Test setting max SOC writes register 3024."""
    result = await coordinator.async_set_max_soc(95)
    assert result is True
    coordinator.client.set_control_parameters.assert_called_with({"3024": "95"})


# ── Data update / failure tolerance ──────────────────────────────────────────


async def test_update_data_success(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test successful data update resets failure counter."""
    coordinator.client.get_energy_parameters.return_value = {
        "SSumInfoList": {"AverageBatteryAverageSOC": "80"}
    }
    data = await coordinator._async_update_data()
    assert data["SSumInfoList"]["AverageBatteryAverageSOC"] == "80"
    assert coordinator._consecutive_failures == 0


async def test_update_data_failure_tolerance(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test stale data is returned within failure tolerance window."""
    coordinator._last_good_data = {"SSumInfoList": {"AverageBatteryAverageSOC": "75"}}
    coordinator.client.get_energy_parameters.return_value = None

    # Should return stale data for up to 5 failures
    for i in range(5):
        data = await coordinator._async_update_data()
        assert data == coordinator._last_good_data
        assert coordinator._consecutive_failures == i + 1


async def test_update_data_exceeds_tolerance(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test UpdateFailed is raised after exceeding failure tolerance."""
    coordinator._last_good_data = {"SSumInfoList": {"AverageBatteryAverageSOC": "75"}}
    coordinator._consecutive_failures = 5
    coordinator.client.get_energy_parameters.return_value = None

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_data_no_prior_data(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test UpdateFailed is raised immediately when there's no prior data."""
    coordinator.client.get_energy_parameters.return_value = None

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# ── Initial state reading ────────────────────────────────────────────────────


async def test_read_initial_state_soc(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test initial SOC limits are read from registers."""
    coordinator.client.get_control_parameters.return_value = {
        "ControlInfo": {
            "3023": "15",
            "3024": "95",
            "3000": "1",
            "3021": "0",
            "3022": "0",
            "3030": "1",
        }
    }
    await coordinator.async_read_initial_state()
    assert coordinator.initial_min_soc == 15
    assert coordinator.initial_max_soc == 95
    assert coordinator.initial_work_mode == "Custom / Manual"


async def test_read_initial_state_self_consumption(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test AI mode is detected from registers."""
    coordinator.client.get_control_parameters.return_value = {
        "ControlInfo": {
            "3000": "1",
            "3021": "1",
            "3022": "1",
            "3030": "0",
        }
    }
    await coordinator.async_read_initial_state()
    assert coordinator.initial_work_mode == "Self-Consumption (AI)"


async def test_read_initial_state_disabled(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test disabled mode is detected when EMS is off."""
    coordinator.client.get_control_parameters.return_value = {
        "ControlInfo": {"3000": "0"}
    }
    await coordinator.async_read_initial_state()
    assert coordinator.initial_work_mode == "Disabled"


async def test_read_initial_power_from_slot(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test initial power is parsed from time slot register."""
    coordinator.client.get_control_parameters.return_value = {
        "ControlInfo": {
            "3003": "1,00:00,23:59,-800,0,6,0,0,0,100,10",
            "3000": "1",
            "3030": "1",
        }
    }
    await coordinator.async_read_initial_state()
    assert coordinator.initial_power == 800
    assert coordinator.commanded_power == 800


async def test_read_initial_state_no_response(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test graceful handling when battery doesn't respond to control read."""
    coordinator.client.get_control_parameters.return_value = None
    await coordinator.async_read_initial_state()
    # Should not crash; values stay at defaults
    assert coordinator.initial_min_soc is None
    assert coordinator.initial_work_mode is None
