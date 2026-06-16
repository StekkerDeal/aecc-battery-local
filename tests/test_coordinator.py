"""Tests for the AECC Battery coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.aecc_battery.const import (
    DOMAIN,
    MAX_BATTERY_POWER_W,
    MAX_REGISTER_POWER_DEFAULT,
    MODE_CUSTOM,
    MODE_SELF_CONSUMPTION,
    REG_AI_SMART_CHARGE,
    REG_AI_SMART_DISC,
    REG_CONTROL_TIME1,
    REG_CUSTOM_MODE,
    REG_EMS_ENABLE,
    REG_MAX_FEED_POWER,
    REG_SCHEDULE_MODE,
    SLOT_DISABLED,
    WORK_MODES,
)
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator


@pytest.fixture
def mock_client():
    """Return a lightweight mock TCP client."""
    client = AsyncMock()
    client.host = "192.168.1.100"
    client.port = 8080
    # Default the readback to None so write-verify exits silently in SET
    # tests that don't explicitly stub it. Individual tests can override.
    client.get_control_parameters = AsyncMock(return_value=None)
    return client


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_client) -> AeccBatteryCoordinator:
    """Return a coordinator with mocked client."""
    coord = AeccBatteryCoordinator(
        hass,
        mock_client,
        device_name="Test Battery",
        manufacturer="Sunpura",
        model="S2400",
    )
    # Skip the post-write asyncio.sleep so SET tests don't each pause 500ms.
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    return coord


@pytest.fixture
def aeg_coordinator(hass: HomeAssistant, mock_client) -> AeccBatteryCoordinator:
    """Coordinator configured as AEG, which uses the two-field slot layout."""
    coord = AeccBatteryCoordinator(
        hass,
        mock_client,
        device_name="Test AEG",
        manufacturer="AEG",
        model="Solarcube AS-BBL09",
    )
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    return coord


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
    coord = AeccBatteryCoordinator(hass, mock_client, "Test", extended_power=True)
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


async def test_aeg_charge_uses_two_field_layout(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """AEG charge puts power in field 3 (unsigned), field 4 = 0, no signed value."""
    aeg_coordinator.data = {"SSumInfoList": {}}  # field7 = 4
    result = await aeg_coordinator.async_set_battery_control("Charge", 500)
    assert result is True

    slot = aeg_coordinator.client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert slot == "1,00:00,23:59,500,0,6,4,0,0,100,10"
    assert "-500" not in slot


async def test_aeg_discharge_uses_two_field_layout(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """AEG discharge puts power in field 4, field 3 = 0."""
    aeg_coordinator.data = {"Storage_list": [{}]}  # field7 = 5
    result = await aeg_coordinator.async_set_battery_control("Discharge", 800)
    assert result is True

    slot = aeg_coordinator.client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert slot == "1,00:00,23:59,0,800,6,5,0,0,100,10"


async def test_aeg_idle_slot_unchanged(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """AEG idle uses the same all-zero, layout-neutral slot as other brands."""
    aeg_coordinator.data = {"SSumInfoList": {}}
    result = await aeg_coordinator.async_set_battery_control("Idle", 0)
    assert result is True

    slot = aeg_coordinator.client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert slot == "0,00:00,00:00,0,0,0,0,0,0,100,10"


async def test_non_aeg_still_signed(coordinator: AeccBatteryCoordinator) -> None:
    """Regression guard: non-AEG brands keep the single signed-field encoding."""
    coordinator.data = {"SSumInfoList": {}}
    await coordinator.async_set_battery_control("Charge", 500)
    slot = coordinator.client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert slot == "1,00:00,23:59,-500,0,6,4,0,0,100,10"


async def test_aeg_slot_round_trips_through_reader(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """An AEG charge slot is read back as Charge, not misread as Discharge."""
    aeg_coordinator.client.get_control_parameters.return_value = {
        "ControlInfo": {
            "3003": "1,00:00,23:59,500,0,6,5,0,0,100,11",
            "3000": "1",
            "3030": "1",
        }
    }
    await aeg_coordinator.async_read_initial_state()
    assert aeg_coordinator.initial_power == 500
    assert aeg_coordinator.commanded_power == 500
    assert aeg_coordinator.commanded_direction == "Charge"


async def test_set_battery_control_extended_writes_max_feed(hass: HomeAssistant, mock_client) -> None:
    """Test extended power mode writes REG_MAX_FEED_POWER."""
    coord = AeccBatteryCoordinator(hass, mock_client, "Test", extended_power=True)
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
    # Must reset the schedule mode and clear the leftover custom slot,
    # otherwise the device keeps running the previous schedule (issues #2, #3).
    assert payload[REG_SCHEDULE_MODE] == "3"
    assert payload[REG_CONTROL_TIME1] == SLOT_DISABLED


async def test_set_work_mode_disabled_removed(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Disabled is no longer a valid work mode and is rejected."""
    assert "Disabled" not in WORK_MODES
    result = await coordinator.async_set_work_mode("Disabled")
    assert result is False


async def test_set_work_mode_updates_current_work_mode(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Selecting a work mode updates the shared source of truth."""
    result = await coordinator.async_set_work_mode(MODE_SELF_CONSUMPTION)
    assert result is True
    assert coordinator.current_work_mode == MODE_SELF_CONSUMPTION


async def test_battery_control_flips_work_mode_to_custom(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Using direction/power switches the shared work mode to Custom."""
    coordinator.current_work_mode = MODE_SELF_CONSUMPTION
    result = await coordinator.async_set_battery_control("Charge", 500)
    assert result is True
    assert coordinator.current_work_mode == MODE_CUSTOM
    assert coordinator.commanded_direction == "Charge"


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
    coordinator.client.get_energy_parameters.return_value = {"SSumInfoList": {"AverageBatteryAverageSOC": "80"}}
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


async def test_read_initial_state_ems_off_maps_to_custom(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """With Disabled removed, an EMS-off device reads back as Custom."""
    coordinator.client.get_control_parameters.return_value = {"ControlInfo": {"3000": "0"}}
    await coordinator.async_read_initial_state()
    assert coordinator.initial_work_mode == MODE_CUSTOM
    assert coordinator.current_work_mode == MODE_CUSTOM


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
