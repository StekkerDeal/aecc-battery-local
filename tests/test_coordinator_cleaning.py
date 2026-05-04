"""Tests for coordinator-level cleaning integration and write-verify."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aecc_battery.const import (
    BRAND_PROFILES,
    REG_MAX_SOC,
    REG_MIN_SOC,
)
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.host = "192.168.1.100"
    client.port = 8080
    # Default readback returns None so write-verify exits silently unless
    # an individual test explicitly stubs a response.
    client.get_control_parameters = AsyncMock(return_value=None)
    return client


@pytest.fixture
def lunergy_coordinator(hass: HomeAssistant, mock_client) -> AeccBatteryCoordinator:
    """Coordinator configured for the known-bad Lunergy device."""
    coord = AeccBatteryCoordinator(
        hass,
        mock_client,
        device_name="Test Lunergy",
        manufacturer="Lunergy",
        model="Hub 2400 AC",
        brand_profile=BRAND_PROFILES["Lunergy"],
    )
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    return coord


# ── get_value cleaning ───────────────────────────────────────────────────────


def test_get_value_rejects_soc_zero_during_discharge(
    lunergy_coordinator: AeccBatteryCoordinator,
) -> None:
    """Coordinator should publish None when SOC=0 collides with active flow."""
    # Seed an accepted SOC reading first so the cleaner has history.
    lunergy_coordinator.data = {
        "Storage_list": [{"BatterySoc": "70", "BatteryDischargingPower": "0"}],
        "SSumInfoList": {},
    }
    assert lunergy_coordinator.get_value("battery_soc") == 70.0

    # Next poll: sensor stuck at 0 while battery actively discharging.
    lunergy_coordinator.data = {
        "Storage_list": [
            {"BatterySoc": "0", "BatteryDischargingPower": "8000"}  # 800W
        ],
        "SSumInfoList": {},
    }
    assert lunergy_coordinator.get_value("battery_soc") is None


def test_get_value_accepts_normal_soc_progression(
    lunergy_coordinator: AeccBatteryCoordinator,
) -> None:
    """Realistic SOC drift (within rate limits) passes through cleanly."""
    lunergy_coordinator.data = {
        "Storage_list": [{"BatterySoc": "70"}],
        "SSumInfoList": {},
    }
    assert lunergy_coordinator.get_value("battery_soc") == 70.0

    lunergy_coordinator.data = {
        "Storage_list": [{"BatterySoc": "68"}],
        "SSumInfoList": {},
    }
    assert lunergy_coordinator.get_value("battery_soc") == 68.0


def test_get_value_records_acceptance_timestamp(
    lunergy_coordinator: AeccBatteryCoordinator,
) -> None:
    """cleaner_last_accepted_at returns an epoch second after a successful read."""
    lunergy_coordinator.data = {
        "Storage_list": [{"BatterySoc": "55"}],
        "SSumInfoList": {},
    }
    assert lunergy_coordinator.cleaner_last_accepted_at("battery_soc") is None
    lunergy_coordinator.get_value("battery_soc")
    ts = lunergy_coordinator.cleaner_last_accepted_at("battery_soc")
    assert ts is not None
    assert abs(ts - time.time()) < 5


def test_get_value_rejection_does_not_advance_timestamp(
    lunergy_coordinator: AeccBatteryCoordinator,
) -> None:
    """A rejected reading must not update the last-accepted timestamp."""
    lunergy_coordinator.data = {
        "Storage_list": [{"BatterySoc": "70"}],
        "SSumInfoList": {},
    }
    lunergy_coordinator.get_value("battery_soc")
    accepted_at = lunergy_coordinator.cleaner_last_accepted_at("battery_soc")
    assert accepted_at is not None

    # Now feed a glitch
    lunergy_coordinator.data = {
        "Storage_list": [{"BatterySoc": "0", "BatteryDischargingPower": "10000"}],
        "SSumInfoList": {},
    }
    assert lunergy_coordinator.get_value("battery_soc") is None
    # Timestamp must not advance
    assert lunergy_coordinator.cleaner_last_accepted_at("battery_soc") == accepted_at


def test_get_value_no_cleaner_passes_through(
    lunergy_coordinator: AeccBatteryCoordinator,
) -> None:
    """Keys without a registered cleaner publish raw values unchanged."""
    lunergy_coordinator.data = {
        "Storage_list": [{"OffGridLoadPower": "2040"}],  # backup_power
        "SSumInfoList": {},
    }
    # backup_power has no cleaner, value flows through.
    assert lunergy_coordinator.get_value("backup_power") == 2040.0


# ── Write-back verification ──────────────────────────────────────────────────


async def test_set_min_soc_runs_write_verify(
    lunergy_coordinator: AeccBatteryCoordinator,
) -> None:
    """SET min_soc should trigger a follow-up GET to confirm the write."""
    lunergy_coordinator.client.set_control_parameters.return_value = {"result": "ok"}
    lunergy_coordinator.client.get_control_parameters.return_value = {"ControlInfo": {REG_MIN_SOC: "15"}}
    result = await lunergy_coordinator.async_set_min_soc(15)
    assert result is True
    # Verify the readback was called with the right register
    lunergy_coordinator.client.get_control_parameters.assert_called_with([int(REG_MIN_SOC)])


async def test_set_max_soc_warns_on_mismatch(
    lunergy_coordinator: AeccBatteryCoordinator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mismatch between expected and actual register state should log a WARNING."""
    lunergy_coordinator.client.set_control_parameters.return_value = {"result": "ok"}
    # Device acknowledged the SET but actually applied a different value.
    lunergy_coordinator.client.get_control_parameters.return_value = {
        "ControlInfo": {REG_MAX_SOC: "98"}  # asked for 95, got 98
    }
    with caplog.at_level("WARNING"):
        result = await lunergy_coordinator.async_set_max_soc(95)
    assert result is True  # SET response was OK, return True regardless
    assert any("mismatch" in r.message.lower() for r in caplog.records)


async def test_set_min_soc_silent_when_verify_response_missing(
    lunergy_coordinator: AeccBatteryCoordinator,
) -> None:
    """If readback gets no response, do not raise, verification is best-effort."""
    lunergy_coordinator.client.set_control_parameters.return_value = {"result": "ok"}
    lunergy_coordinator.client.get_control_parameters.return_value = None
    result = await lunergy_coordinator.async_set_min_soc(20)
    assert result is True


async def test_set_min_soc_no_verify_on_failed_write(
    lunergy_coordinator: AeccBatteryCoordinator,
) -> None:
    """If the SET itself failed (no response), we don't try to verify."""
    lunergy_coordinator.client.set_control_parameters.return_value = None
    result = await lunergy_coordinator.async_set_min_soc(20)
    assert result is False
    lunergy_coordinator.client.get_control_parameters.assert_not_called()


# ── Brand-profile plumbing ───────────────────────────────────────────────────


def test_default_profile_when_brand_unknown(hass: HomeAssistant, mock_client) -> None:
    """Unknown brand falls back to the conservative DEFAULT_BRAND_PROFILE."""
    coord = AeccBatteryCoordinator(
        hass,
        mock_client,
        device_name="Test",
        manufacturer="UnknownBrand",
        # __init__.py would do BRAND_PROFILES.get(brand, DEFAULT_BRAND_PROFILE);
        # at coordinator level, no profile passed = default applied.
    )
    assert coord.brand_profile["soc_zero_reject_during_active_w"] == 100  # "Other" value


def test_lunergy_profile_more_strict_than_sunpura() -> None:
    """Sanity: Lunergy thresholds catch more SOC pathology than Sunpura."""
    lunergy = BRAND_PROFILES["Lunergy"]
    sunpura = BRAND_PROFILES["Sunpura"]
    assert lunergy["soc_zero_reject_during_active_w"] < sunpura["soc_zero_reject_during_active_w"]
    assert lunergy["soc_max_rate_pct_per_min"] < sunpura["soc_max_rate_pct_per_min"]
