"""Tests for the AECC Battery coordinator."""

from __future__ import annotations

import asyncio
import copy
import time
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
    SCHEDULE_MODE_CUSTOM,
    SCHEDULE_MODE_CUSTOM_AEG,
    SLOT_DISABLED,
    WIFI_RSSI_REFRESH_INTERVAL,
    WORK_MODES,
)
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator
from tests.test_multiunit import ISSUE9_POLL, SN2


@pytest.fixture
def mock_client():
    """Return a lightweight mock TCP client."""
    client = AsyncMock()
    client.host = "192.168.1.100"
    client.port = 8080
    client.consecutive_failures = 0
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
    coord._WRITE_RETRY_DELAY_SECONDS = 0
    return coord


@pytest.fixture
def aeg_coordinator(hass: HomeAssistant, mock_client) -> AeccBatteryCoordinator:
    """Coordinator configured as AEG, which uses field 6 = 0 in the control slot."""
    coord = AeccBatteryCoordinator(
        hass,
        mock_client,
        device_name="Test AEG",
        manufacturer="AEG",
        model="Solarcube AS-BBL09",
    )
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    coord._WRITE_RETRY_DELAY_SECONDS = 0
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


def test_device_info_model_prefers_config(coordinator: AeccBatteryCoordinator) -> None:
    """A user-entered config model wins over the reg-20 code (friendlier)."""
    coordinator.device_model = "GTSW0000"
    assert coordinator.device_info["model"] == "S2400"


def test_device_info_model_falls_back_to_reg20(hass: HomeAssistant, mock_client) -> None:
    """With no config model, the reg-20 code populates the model."""
    coord = AeccBatteryCoordinator(hass, mock_client, "Test", model="")
    coord.device_model = "GTSW0000"
    assert coord.device_info["model"] == "GTSW0000"


# ── DeviceManagement probe ────────────────────────────────────────────────────

# JET returns identity + RSSI under the ControlInfo key.
JET_DM_RESPONSE = {
    "Response": "DeviceManagement",
    "SerialNumber": 1,
    "Target": "HA",
    "ControlInfo": {
        "8": "JM0225391ASG0290",
        "20": "GTSW0000",
        "21": "1.4.9.9.9.1.5",
        "76": "-35",
    },
}

# Sunpura-style devices use the DeviceManagementInfo key (regression guard).
SUNPURA_DM_RESPONSE = {
    "Response": "DeviceManagement",
    "DeviceManagementInfo": {"8": "SP123456", "21": "v3.0.1"},
}


async def test_probe_parses_controlinfo_jet(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """JET's ControlInfo-keyed response populates serial, firmware, model, RSSI."""
    mock_client.get_device_management_info = AsyncMock(return_value=JET_DM_RESPONSE)
    await coordinator.async_probe_device_management()
    assert coordinator.device_serial == "JM0225391ASG0290"
    assert coordinator.firmware_version == "1.4.9.9.9.1.5"
    assert coordinator.device_model == "GTSW0000"
    assert coordinator.wifi_rssi == -35


async def test_probe_parses_devicemanagementinfo_regression(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """The legacy DeviceManagementInfo key still parses (no Sunpura regression)."""
    mock_client.get_device_management_info = AsyncMock(return_value=SUNPURA_DM_RESPONSE)
    await coordinator.async_probe_device_management()
    assert coordinator.device_serial == "SP123456"
    assert coordinator.firmware_version == "v3.0.1"
    assert coordinator.wifi_rssi is None  # reg 76 absent -> no sensor later


async def test_probe_none_response_populates_nothing(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """A device that does not answer DeviceManagement leaves all fields unset."""
    mock_client.get_device_management_info = AsyncMock(return_value=None)
    await coordinator.async_probe_device_management()
    assert coordinator.device_serial is None
    assert coordinator.wifi_rssi is None


async def test_probe_rssi_non_numeric_ignored(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """A non-numeric reg 76 does not crash and leaves wifi_rssi unset."""
    mock_client.get_device_management_info = AsyncMock(return_value={"ControlInfo": {"8": "S", "76": "n/a"}})
    await coordinator.async_probe_device_management()
    assert coordinator.wifi_rssi is None


# ── Throttled WiFi RSSI refresh ───────────────────────────────────────────────


async def test_rssi_refresh_skipped_when_unsupported(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """No DeviceManagement read when the device never reported RSSI at setup."""
    coordinator.wifi_rssi = None
    mock_client.get_device_management_info = AsyncMock(return_value=JET_DM_RESPONSE)
    await coordinator._async_maybe_refresh_rssi()
    mock_client.get_device_management_info.assert_not_called()


async def test_rssi_refresh_runs_first_time(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """First refresh after setup reads and updates RSSI."""
    coordinator.wifi_rssi = -40
    mock_client.get_device_management_info = AsyncMock(return_value={"ControlInfo": {"76": "-50"}})
    await coordinator._async_maybe_refresh_rssi()
    mock_client.get_device_management_info.assert_called_once()
    assert coordinator.wifi_rssi == -50


async def test_rssi_refresh_throttled_within_window(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """A refresh inside the throttle window does not re-read DeviceManagement."""
    coordinator.wifi_rssi = -40
    coordinator._last_rssi_refresh = time.monotonic()
    mock_client.get_device_management_info = AsyncMock(return_value={"ControlInfo": {"76": "-50"}})
    await coordinator._async_maybe_refresh_rssi()
    mock_client.get_device_management_info.assert_not_called()
    assert coordinator.wifi_rssi == -40


async def test_rssi_refresh_past_window(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """Past the throttle window the value updates again."""
    coordinator.wifi_rssi = -40
    coordinator._last_rssi_refresh = time.monotonic() - WIFI_RSSI_REFRESH_INTERVAL - 1
    mock_client.get_device_management_info = AsyncMock(return_value={"ControlInfo": {"76": "-55"}})
    await coordinator._async_maybe_refresh_rssi()
    mock_client.get_device_management_info.assert_called_once()
    assert coordinator.wifi_rssi == -55


async def test_rssi_refresh_none_keeps_last_value(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """A None read (timeout) holds the last RSSI rather than clearing it."""
    coordinator.wifi_rssi = -40
    mock_client.get_device_management_info = AsyncMock(return_value=None)
    await coordinator._async_maybe_refresh_rssi()
    assert coordinator.wifi_rssi == -40


async def test_rssi_refresh_exception_does_not_propagate(coordinator: AeccBatteryCoordinator, mock_client) -> None:
    """A raising read is swallowed and the last RSSI is kept."""
    coordinator.wifi_rssi = -40
    mock_client.get_device_management_info = AsyncMock(side_effect=OSError("boom"))
    await coordinator._async_maybe_refresh_rssi()
    assert coordinator.wifi_rssi == -40


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


def test_get_value_summary_preferred_over_storage(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """SSumInfoList wins over Storage_list for system values."""
    coordinator.data = {
        "Storage_list": [{"BatterySoc": "80"}],
        "SSumInfoList": {"AverageBatteryAverageSOC": "75"},
    }
    assert coordinator.get_value("battery_soc") == 75.0


def test_backup_power_summary_is_10w_units(coordinator: AeccBatteryCoordinator) -> None:
    """TotalBackUpPower is 10W units (JET EPS test 2026-07-10: 183.2 for ~1830W)."""
    coordinator.data = {
        "Storage_list": [{"OffGridLoadPower": "1832"}],
        "SSumInfoList": {"TotalBackUpPower": "178.4"},
    }
    assert coordinator.get_value("backup_power") == 1784.0


def test_backup_power_storage_fallback_is_plain_watts(coordinator: AeccBatteryCoordinator) -> None:
    """Without the summary field, OffGridLoadPower is used unscaled (watts)."""
    coordinator.data = {
        "Storage_list": [{"OffGridLoadPower": "2040"}],
        "SSumInfoList": {},
    }
    assert coordinator.get_value("backup_power") == 2040.0


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


# ── Power limits ─────────────────────────────────────────────────────────────


def test_max_power_default(hass: HomeAssistant, mock_client) -> None:
    """Test default limits are 800W in both directions."""
    coord = AeccBatteryCoordinator(hass, mock_client, "Test")
    assert coord.max_charge_power == MAX_REGISTER_POWER_DEFAULT
    assert coord.max_discharge_power == MAX_REGISTER_POWER_DEFAULT
    assert coord.max_register_power == MAX_REGISTER_POWER_DEFAULT


def test_max_register_power_is_larger_limit(hass: HomeAssistant, mock_client) -> None:
    coord = AeccBatteryCoordinator(hass, mock_client, "Test", max_charge_power=2400, max_discharge_power=800)
    assert coord.max_register_power == MAX_BATTERY_POWER_W


async def test_charge_clamped_to_charge_limit(hass: HomeAssistant, mock_client) -> None:
    """A command above the direction's limit is clamped, not rejected."""
    coord = AeccBatteryCoordinator(hass, mock_client, "Test", max_charge_power=2400, max_discharge_power=800)
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    coord._WRITE_RETRY_DELAY_SECONDS = 0
    coord.data = {"SSumInfoList": {}}
    mock_client.set_control_parameters.return_value = {"result": "ok"}

    assert await coord.async_set_battery_control("Discharge", 900) is True
    slot = mock_client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert int(slot.split(",")[3]) == 800  # clamped to the discharge limit
    assert coord.commanded_power == 800

    assert await coord.async_set_battery_control("Charge", 2400) is True
    slot = mock_client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert int(slot.split(",")[3]) == -2400  # charge limit allows the full range
    assert coord.commanded_power == 2400


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


async def test_aeg_charge_uses_signed_field_with_field6_zero(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """AEG charge is the signed setpoint (negative) with field 6 = 0 (issue #8)."""
    aeg_coordinator.data = {"SSumInfoList": {}}  # field7 would be 4 for non-AEG
    result = await aeg_coordinator.async_set_battery_control("Charge", 500)
    assert result is True

    slot = aeg_coordinator.client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert slot == "1,00:00,23:59,-500,0,6,0,0,0,100,10"


async def test_aeg_discharge_uses_signed_field_with_field6_zero(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """AEG discharge is the signed setpoint (positive) with field 6 = 0."""
    aeg_coordinator.data = {"Storage_list": [{}]}  # field7 would be 5 for non-AEG
    result = await aeg_coordinator.async_set_battery_control("Discharge", 800)
    assert result is True

    slot = aeg_coordinator.client.set_control_parameters.call_args[0][0][REG_CONTROL_TIME1]
    assert slot == "1,00:00,23:59,800,0,6,0,0,0,100,10"


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


async def test_aeg_battery_control_uses_schedule_mode_3(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """AEG pairs a manual setpoint with 3020=3, mirroring its own app (#16)."""
    aeg_coordinator.data = {"SSumInfoList": {}}
    await aeg_coordinator.async_set_battery_control("Charge", 500)

    payload = aeg_coordinator.client.set_control_parameters.call_args[0][0]
    assert payload[REG_SCHEDULE_MODE] == SCHEDULE_MODE_CUSTOM_AEG
    # The rest of the custom-control block is unchanged by the brand gate.
    assert payload[REG_EMS_ENABLE] == "1"
    assert payload[REG_CUSTOM_MODE] == "1"


async def test_aeg_work_mode_custom_uses_schedule_mode_3(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """Selecting Custom / Manual on AEG writes 3020=3, not 6 (#16)."""
    result = await aeg_coordinator.async_set_work_mode(MODE_CUSTOM)
    assert result is True

    payload = aeg_coordinator.client.set_control_parameters.call_args[0][0]
    assert payload[REG_SCHEDULE_MODE] == SCHEDULE_MODE_CUSTOM_AEG


async def test_non_aeg_work_mode_custom_keeps_schedule_mode_6(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Regression guard for #2/#3: other brands still get 3020=6 in Custom."""
    result = await coordinator.async_set_work_mode(MODE_CUSTOM)
    assert result is True

    payload = coordinator.client.set_control_parameters.call_args[0][0]
    assert payload[REG_SCHEDULE_MODE] == SCHEDULE_MODE_CUSTOM == "6"


async def test_aeg_self_consumption_schedule_mode_unchanged(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """The AEG gate touches Custom only; Self-Consumption keeps its own reset."""
    await aeg_coordinator.async_set_work_mode(MODE_SELF_CONSUMPTION)

    payload = aeg_coordinator.client.set_control_parameters.call_args[0][0]
    assert payload[REG_SCHEDULE_MODE] == "3"
    assert payload[REG_CONTROL_TIME1] == SLOT_DISABLED


async def test_aeg_slot_round_trips_through_reader(
    aeg_coordinator: AeccBatteryCoordinator,
) -> None:
    """An AEG charge slot is read back as Charge, not misread as Discharge."""
    aeg_coordinator.client.get_control_parameters.return_value = {
        "ControlInfo": {
            "3003": "1,00:00,23:59,-500,0,6,0,0,0,100,11",
            "3000": "1",
            "3030": "1",
        }
    }
    await aeg_coordinator.async_read_initial_state()
    assert aeg_coordinator.initial_power == 500
    assert aeg_coordinator.commanded_power == 500
    assert aeg_coordinator.commanded_direction == "Charge"


async def test_raised_limit_writes_max_feed(hass: HomeAssistant, mock_client) -> None:
    """REG_MAX_FEED_POWER carries the larger limit when either exceeds 800W."""
    coord = AeccBatteryCoordinator(hass, mock_client, "Test", max_charge_power=2400, max_discharge_power=800)
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    coord._WRITE_RETRY_DELAY_SECONDS = 0
    coord.data = {"SSumInfoList": {}}
    mock_client.set_control_parameters.return_value = {"result": "ok"}
    await coord.async_set_battery_control("Charge", 2000)

    payload = mock_client.set_control_parameters.call_args[0][0]
    assert REG_MAX_FEED_POWER in payload
    assert payload[REG_MAX_FEED_POWER] == str(MAX_BATTERY_POWER_W)


async def test_default_limits_do_not_write_max_feed(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.data = {"SSumInfoList": {}}
    await coordinator.async_set_battery_control("Charge", 500)
    payload = coordinator.client.set_control_parameters.call_args[0][0]
    assert REG_MAX_FEED_POWER not in payload


async def test_set_battery_control_no_response(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Test control returns False when battery doesn't respond."""
    coordinator.data = {"SSumInfoList": {}}
    coordinator.client.set_control_parameters.return_value = None
    result = await coordinator.async_set_battery_control("Charge", 500)
    assert result is False


# ── Write retry (issue #12) ──────────────────────────────────────────────────


async def test_logged_write_retries_unconfirmed(coordinator: AeccBatteryCoordinator) -> None:
    """An unconfirmed write is re-sent and succeeds on the second attempt."""
    coordinator.client.set_control_parameters = AsyncMock(side_effect=[None, {"result": "ok"}])
    result = await coordinator._logged_write({"3023": "15"}, "min_soc(15%)")
    assert result is True
    assert coordinator.client.set_control_parameters.call_count == 2
    entry = coordinator.write_history[-1]
    assert entry["attempts"] == 2
    assert entry["response_received"] is True
    # Verify readback runs once, on the final success only.
    coordinator.client.get_control_parameters.assert_called_once()


async def test_logged_write_gives_up_after_retries(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.set_control_parameters = AsyncMock(return_value=None)
    result = await coordinator._logged_write({"3023": "15"}, "min_soc(15%)")
    assert result is False
    assert coordinator.client.set_control_parameters.call_count == 3  # 1 + 2 retries
    entry = coordinator.write_history[-1]
    assert entry["attempts"] == 3
    assert entry["response_received"] is False
    coordinator.client.get_control_parameters.assert_not_called()


async def test_logged_write_no_retry_on_success(coordinator: AeccBatteryCoordinator) -> None:
    result = await coordinator._logged_write({"3023": "15"}, "min_soc(15%)")
    assert result is True
    assert coordinator.client.set_control_parameters.call_count == 1
    assert coordinator.write_history[-1]["attempts"] == 1


async def test_logged_write_no_retry_during_sustained_outage(coordinator: AeccBatteryCoordinator) -> None:
    """A high connection-failure streak means outage, not blip: fail fast."""
    coordinator.client.set_control_parameters = AsyncMock(return_value=None)
    coordinator.client.consecutive_failures = 3
    result = await coordinator._logged_write({"3023": "15"}, "min_soc(15%)")
    assert result is False
    assert coordinator.client.set_control_parameters.call_count == 1
    assert coordinator.write_history[-1]["attempts"] == 1


async def test_retry_cannot_overwrite_newer_write(coordinator: AeccBatteryCoordinator) -> None:
    """The write lock is FIFO: a retrying write finishes all its attempts
    before the next write starts, so a re-sent (stale) payload can never
    land after a newer command and silently overwrite it."""
    coordinator._WRITE_RETRY_DELAY_SECONDS = 0.05
    calls: list[dict] = []

    async def flaky_set(payload):
        calls.append(dict(payload))
        return None if len(calls) == 1 else {"result": "ok"}

    coordinator.client.set_control_parameters = AsyncMock(side_effect=flaky_set)
    old_write = asyncio.ensure_future(coordinator._logged_write({"3003": "old"}, "old"))
    await asyncio.sleep(0.01)  # old write failed attempt 1 and is sleeping before its retry
    new_write = asyncio.ensure_future(coordinator._logged_write({"3003": "new"}, "new"))
    assert await asyncio.gather(old_write, new_write) == [True, True]
    assert [c["3003"] for c in calls] == ["old", "old", "new"]


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


# ── Frame guard (issue #15) ──────────────────────────────────────────────────


def _two_unit_frame() -> dict:
    return copy.deepcopy(ISSUE9_POLL)


async def _prime_baseline(coordinator: AeccBatteryCoordinator, frame: dict) -> None:
    coordinator.client.get_energy_parameters.return_value = frame
    await coordinator._async_update_data()


async def test_frame_guard_missing_unit_rejected(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """A frame missing a previously seen unit returns the last good frame."""
    await _prime_baseline(coordinator, _two_unit_frame())

    partial = _two_unit_frame()
    partial["Storage_list"] = [u for u in partial["Storage_list"] if u["StorageSN"] != SN2]
    coordinator._consecutive_failures = 2
    coordinator.client.get_energy_parameters.return_value = partial

    data = await coordinator._async_update_data()
    assert len(data["Storage_list"]) == 2
    assert coordinator._suspect_streak == 1
    assert coordinator._suspect_frames_total == 1
    assert "DevAddr 2" in coordinator._last_suspect_reason
    # The serial must never leak into the free-text reason: diagnostics
    # redaction is key-based and the reason is shared on GitHub verbatim.
    assert SN2 not in coordinator._last_suspect_reason
    assert coordinator._last_suspect_at is not None
    # The device answered, so the connection-level counter still resets.
    assert coordinator._consecutive_failures == 0


async def test_frame_guard_accepts_changed_frame_after_tolerance(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """A persistently smaller frame is accepted after the suspect tolerance."""
    await _prime_baseline(coordinator, _two_unit_frame())

    partial = _two_unit_frame()
    partial["Storage_list"] = [u for u in partial["Storage_list"] if u["StorageSN"] != SN2]
    coordinator.client.get_energy_parameters.return_value = partial

    for i in range(3):
        data = await coordinator._async_update_data()
        assert len(data["Storage_list"]) == 2
        assert coordinator._suspect_streak == i + 1

    data = await coordinator._async_update_data()
    assert len(data["Storage_list"]) == 1
    assert coordinator._suspect_streak == 0
    assert coordinator._last_good_data == partial


async def test_frame_guard_soc_collapse_rejected(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """A unit SOC collapsing from 48 to 0 in one poll rejects the frame."""
    await _prime_baseline(coordinator, _two_unit_frame())

    glitched = _two_unit_frame()
    glitched["Storage_list"][1]["BatterySoc"] = 0
    coordinator.client.get_energy_parameters.return_value = glitched

    data = await coordinator._async_update_data()
    assert data["Storage_list"][1]["BatterySoc"] == 48
    assert coordinator._suspect_streak == 1
    assert "SOC collapsed" in coordinator._last_suspect_reason
    assert SN2 not in coordinator._last_suspect_reason


async def test_frame_guard_soc_zero_below_floor_accepted(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """SOC 0 after a last good SOC below the floor is a legitimate drain."""
    nearly_empty = _two_unit_frame()
    for unit in nearly_empty["Storage_list"]:
        unit["BatterySoc"] = 3
    await _prime_baseline(coordinator, nearly_empty)

    empty = _two_unit_frame()
    for unit in empty["Storage_list"]:
        unit["BatterySoc"] = 0
    coordinator.client.get_energy_parameters.return_value = empty

    data = await coordinator._async_update_data()
    assert data["Storage_list"][0]["BatterySoc"] == 0
    assert coordinator._suspect_streak == 0


async def test_frame_guard_clean_frame_resets_streak(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """A good frame between suspect ones resets the streak, not the total."""
    await _prime_baseline(coordinator, _two_unit_frame())

    partial = _two_unit_frame()
    partial["Storage_list"] = [u for u in partial["Storage_list"] if u["StorageSN"] != SN2]

    coordinator.client.get_energy_parameters.return_value = partial
    await coordinator._async_update_data()
    assert coordinator._suspect_streak == 1

    coordinator.client.get_energy_parameters.return_value = _two_unit_frame()
    await coordinator._async_update_data()
    assert coordinator._suspect_streak == 0

    coordinator.client.get_energy_parameters.return_value = partial
    await coordinator._async_update_data()
    assert coordinator._suspect_streak == 1
    assert coordinator._suspect_frames_total == 2


async def test_frame_guard_new_unit_accepted(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """A frame with an extra unit is accepted immediately."""
    single = _two_unit_frame()
    single["Storage_list"] = [u for u in single["Storage_list"] if u["StorageSN"] != SN2]
    await _prime_baseline(coordinator, single)

    coordinator.client.get_energy_parameters.return_value = _two_unit_frame()
    data = await coordinator._async_update_data()
    assert len(data["Storage_list"]) == 2
    assert coordinator._suspect_streak == 0


async def test_frame_guard_reorder_accepted(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Reordered Storage_list entries are not suspect (keys match)."""
    await _prime_baseline(coordinator, _two_unit_frame())

    reordered = _two_unit_frame()
    reordered["Storage_list"].reverse()
    coordinator.client.get_energy_parameters.return_value = reordered

    data = await coordinator._async_update_data()
    assert data["Storage_list"][0]["StorageSN"] == SN2
    assert coordinator._suspect_streak == 0


async def test_frame_guard_first_frame_accepted(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """The first frame ever is accepted, there is no baseline to compare."""
    frame = _two_unit_frame()
    frame["Storage_list"][0]["BatterySoc"] = 0
    coordinator.client.get_energy_parameters.return_value = frame

    data = await coordinator._async_update_data()
    assert data == frame
    assert coordinator._suspect_streak == 0


async def test_frame_guard_summary_only_never_suspect(
    coordinator: AeccBatteryCoordinator,
) -> None:
    """Brands without Storage_list have no units to compare and never trip."""
    await _prime_baseline(coordinator, {"SSumInfoList": {"AverageBatteryAverageSOC": "75"}})

    updated = {"SSumInfoList": {"AverageBatteryAverageSOC": "0"}}
    coordinator.client.get_energy_parameters.return_value = updated

    data = await coordinator._async_update_data()
    assert data == updated
    assert coordinator._suspect_streak == 0


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
