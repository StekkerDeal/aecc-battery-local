"""Sensor platform tests, focused on the hybrid hold-then-unavailable behavior.

These tests cover the entity-level fallback that pairs with the cleaner-level
rejection in the coordinator. When the cleaner rejects readings, the entity
must keep returning its last accepted value for ``hold_last_value_seconds``,
then transition to unavailable so users see an honest signal that the
underlying sensor has stopped responding.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import mock_restore_cache, mock_restore_cache_with_extra_data

from custom_components.aecc_battery.const import BRAND_PROFILES
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator
from custom_components.aecc_battery.sensor import (
    _ENERGY_SENSORS,
    _SENSORS,
    _UNIT_SENSORS,
    AeccBatteryPowerSensor,
    AeccBatteryStatusSensor,
    AeccEnergySensor,
    AeccFirmwareSensor,
    AeccSensor,
    AeccUnitSensor,
    AeccWifiSignalSensor,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _frame(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))["last_poll"]


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
        icon=None,
        is_power=False,
    )


def test_soc_sensor_has_no_static_icon() -> None:
    """SOC must stay icon-less so HA renders its dynamic battery-level icon (#19)."""
    soc = next(s for s in _SENSORS if s[0] == "battery_soc")
    assert soc[4] is None


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


def test_recovers_from_stuck_unavailable(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """A good reading after the hold window expired recovers the sensor (no reload).

    Reproduces the deadlock where `available` went False once the hold window
    expired, which stopped HA evaluating `native_value` (the only path that
    refreshes the cleaner anchor), so the sensor stayed unavailable forever even
    while the device reported valid values.
    """
    sensor = _make_sensor(coordinator, config_entry)

    # Establish a baseline accepted value (SOC 80), then age the anchor well past
    # the hold window so the entity would have gone unavailable.
    coordinator.data = {"Storage_list": [{"BatterySoc": "80"}], "SSumInfoList": {}}
    assert sensor.native_value == 80.0
    hold = coordinator.brand_profile["hold_last_value_seconds"]
    coordinator._cleaner_last_accepted_at["battery_soc"] = time.time() - hold - 6 * 3600

    # Device now reports a valid SOC at idle (no active flow). Pre-fix this stayed
    # unavailable with the anchor frozen at 80; it must now self-recover.
    coordinator.data = {"Storage_list": [{"BatterySoc": "22"}], "SSumInfoList": {}}
    assert sensor.available is True
    assert sensor.native_value == 22.0


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


def test_pv_charging_power_is_diagnostic(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """PV Charging Power is the device's own figure, not a dashboard input."""
    coordinator.data = _frame("aferiy-pv-two-unit.json")
    hub = AeccSensor(coordinator, config_entry, *next(s for s in _SENSORS if s[0] == "pv_charging_power"))
    assert hub.entity_category is EntityCategory.DIAGNOSTIC
    spec = next(s for s in _UNIT_SENSORS if s[0] == "pv_charging_power")
    unit = AeccUnitSensor(coordinator, config_entry, coordinator.units[0], *spec)
    assert unit.entity_category is EntityCategory.DIAGNOSTIC
    pv_power = AeccSensor(coordinator, config_entry, *next(s for s in _SENSORS if s[0] == "pv_power"))
    assert pv_power.entity_category is None


# ── Battery power: PV in minus what leaves the socket ─────────────────────────

# (fixture, battery power) from real frames.
_BALANCE_FRAMES = [
    ("sunpura-pv-only.json", 190.0),  # panels only, grid unplugged
    ("sunpura-pv-and-ac.json", 450.0),  # panels plus grid
    ("aferiy-pv-two-unit.json", 478.0),  # panels, part of the PV to the house
    ("tsun-discharging.json", -547.0),  # no panels, discharging
]


@pytest.mark.parametrize(("fixture", "expected"), _BALANCE_FRAMES)
def test_battery_power_on_real_frames(
    coordinator: AeccBatteryCoordinator, config_entry, fixture: str, expected: float
) -> None:
    coordinator.data = _frame(fixture)
    assert coordinator.battery_power_w() == expected
    assert AeccBatteryPowerSensor(coordinator, config_entry).native_value == expected


@pytest.mark.parametrize(("fixture", "expected"), [f for f in _BALANCE_FRAMES if f[1] > 0])
def test_battery_power_covers_cell_charge(coordinator: AeccBatteryCoordinator, fixture: str, expected: float) -> None:
    """Cross-check: the socket-side balance is at least the cell-side charge after losses."""
    coordinator.data = _frame(fixture)
    assert expected >= float(coordinator.summary["TotalChargePower"]) > 0


def test_backup_load_is_not_charge(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """A grid-fed load on the backup socket passes through; only the 9 W trickle is charge."""
    coordinator.data = _frame("jet-eps-load.json")
    assert coordinator.battery_power_w() == 9.0
    assert AeccBatteryStatusSensor(coordinator, config_entry).native_value == "Idle"


def test_battery_power_without_grid_output_has_no_value(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """No fallback: without TotalGridOutputPower there is no reading, not a wrong one."""
    coordinator.data = _frame("sunpura-pv-only.json")
    del coordinator.data["SSumInfoList"]["TotalGridOutputPower"]
    assert coordinator.battery_power_w() is None
    assert AeccBatteryPowerSensor(coordinator, config_entry).native_value is None
    assert AeccBatteryStatusSensor(coordinator, config_entry).native_value is None


def test_battery_power_without_pv_counts_pv_as_zero(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.data = _frame("tsun-discharging.json")
    del coordinator.data["SSumInfoList"]["TotalPVPower"]
    assert coordinator.battery_power_w() == -547.0


@pytest.mark.parametrize(
    ("pv", "grid_output", "status"),
    [
        (190, 0, "Charging"),
        (0, 547, "Discharging"),
        (0, -5, "Idle"),  # standby draw at rest
        (0, 2, "Idle"),
        (0, -25, "Idle"),  # the band edge is still idle
        (0, 25, "Idle"),
        (0, -26, "Charging"),
    ],
)
def test_battery_status_follows_sign_with_idle_band(
    coordinator: AeccBatteryCoordinator, config_entry, pv: int, grid_output: int, status: str
) -> None:
    coordinator.data = {"SSumInfoList": {"TotalPVPower": pv, "TotalGridOutputPower": grid_output}}
    assert AeccBatteryStatusSensor(coordinator, config_entry).native_value == status


def _energy_sensor(coordinator, config_entry, key: str) -> AeccEnergySensor:
    spec = next(s for s in _ENERGY_SENSORS if s[0] == key)
    sensor = AeccEnergySensor(coordinator, config_entry, *spec)
    sensor.async_write_ha_state = MagicMock()
    return sensor


def _tick(sensors: list[AeccEnergySensor], at) -> None:
    with patch("custom_components.aecc_battery.sensor.utcnow", return_value=at):
        for sensor in sensors:
            sensor._handle_coordinator_update()


@pytest.mark.parametrize(
    ("fixture", "charged_kwh", "discharged_kwh"),
    [
        ("sunpura-pv-only.json", 190 * 30 / 3_600_000, 0.0),
        ("tsun-discharging.json", 0.0, 547 * 30 / 3_600_000),
    ],
)
def test_energy_counters_split_battery_power(
    coordinator: AeccBatteryCoordinator, config_entry, fixture: str, charged_kwh: float, discharged_kwh: float
) -> None:
    """Energy Charged and Discharged integrate the positive and negative part of battery power."""
    coordinator.data = _frame(fixture)
    charged = _energy_sensor(coordinator, config_entry, "energy_charged")
    discharged = _energy_sensor(coordinator, config_entry, "energy_discharged")
    start = utcnow()
    _tick([charged, discharged], start)
    _tick([charged, discharged], start + timedelta(seconds=30))
    assert charged._accumulated_kwh == pytest.approx(charged_kwh)
    assert discharged._accumulated_kwh == pytest.approx(discharged_kwh)


def test_energy_counters_skip_frames_without_grid_output(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    coordinator.data = _frame("sunpura-pv-only.json")
    del coordinator.data["SSumInfoList"]["TotalGridOutputPower"]
    charged = _energy_sensor(coordinator, config_entry, "energy_charged")
    discharged = _energy_sensor(coordinator, config_entry, "energy_discharged")
    start = utcnow()
    _tick([charged, discharged], start)
    _tick([charged, discharged], start + timedelta(seconds=30))
    assert charged._accumulated_kwh == 0.0
    assert discharged._accumulated_kwh == 0.0
    assert charged._last_update_time is None


# ── Energy counters across a restart or reload ───────────────────────────────

_ENERGY_ENTITY = "sensor.test_energy_discharged"


async def _restored_kwh(hass: HomeAssistant, coordinator, config_entry) -> float:
    sensor = _energy_sensor(coordinator, config_entry, "energy_discharged")
    sensor.hass = hass
    sensor.entity_id = _ENERGY_ENTITY
    await sensor.async_added_to_hass()
    # Drop the coordinator listener again, or its refresh timer lingers.
    sensor._call_on_remove_callbacks()
    return sensor._accumulated_kwh


async def test_counter_keeps_total_when_unavailable_at_reload(
    hass: HomeAssistant, coordinator: AeccBatteryCoordinator, config_entry
) -> None:
    """A reload while the battery is not answering must not reset the total."""
    mock_restore_cache_with_extra_data(
        hass,
        [(State(_ENERGY_ENTITY, "unavailable"), {"native_value": 4.223, "native_unit_of_measurement": "kWh"})],
    )
    assert await _restored_kwh(hass, coordinator, config_entry) == 4.223


async def test_counter_restores_from_state_without_sensor_data(
    hass: HomeAssistant, coordinator: AeccBatteryCoordinator, config_entry
) -> None:
    """Upgrade path: earlier versions stored the state only."""
    mock_restore_cache(hass, [State(_ENERGY_ENTITY, "3.5")])
    assert await _restored_kwh(hass, coordinator, config_entry) == 3.5


async def test_counter_starts_at_zero_with_nothing_to_restore(
    hass: HomeAssistant, coordinator: AeccBatteryCoordinator, config_entry
) -> None:
    mock_restore_cache(hass, [State(_ENERGY_ENTITY, "unavailable")])
    assert await _restored_kwh(hass, coordinator, config_entry) == 0.0


def test_counter_stores_its_total(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """What gets stored for the next restore is the accumulated total."""
    coordinator.data = _frame("tsun-discharging.json")
    discharged = _energy_sensor(coordinator, config_entry, "energy_discharged")
    start = utcnow()
    _tick([discharged], start)
    _tick([discharged], start + timedelta(seconds=30))
    assert discharged.extra_restore_state_data.native_value == discharged.native_value > 0
