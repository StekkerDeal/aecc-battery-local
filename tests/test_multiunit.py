"""Multi-unit (master/slave) tests.

The fixture data is a real 2x AEG Solarcube AS-BBL09 capture: both units
charging from AC at ~392W/~397W, SoC 48%, with the master's system totals in
SSumInfoList. Only the serials are synthetic (diagnostics redact them).
"""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aecc_battery import async_remove_config_entry_device
from custom_components.aecc_battery import sensor as sensor_module
from custom_components.aecc_battery.const import DOMAIN
from custom_components.aecc_battery.coordinator import _FIELD_MAP, AeccBatteryCoordinator
from custom_components.aecc_battery.sensor import (
    _UNIT_SENSORS,
    AeccUnitSensor,
    AeccUnitStatusSensor,
)

SN1 = "AEGSN000001"
SN2 = "AEGSN000002"

ISSUE9_POLL: dict = {
    "Storage_list": [
        {
            "DevAddr": 1,
            "StorageSN": SN1,
            "StorageStatus": 1,
            "PvChargingPower": 0,
            "AcChargingPower": 3920,
            "BatterySoc": 48,
            "BatteryDischargingPower": 0,
            "AcInActivePower": -3920,
            "OffGridLoadPower": 0,
            "BatteryChargingPower": 0,
            "PvStringCount": 4,
            "Pv1Power": 0,
            "Pv2Power": 0,
            "Pv3Power": 0,
            "Pv4Power": 0,
        },
        {
            "DevAddr": 2,
            "StorageSN": SN2,
            "StorageStatus": 1,
            "PvChargingPower": 0,
            "AcChargingPower": 3970,
            "BatterySoc": 48,
            "BatteryDischargingPower": 0,
            "AcInActivePower": -3970,
            "OffGridLoadPower": 0,
            "BatteryChargingPower": 0,
            "PvStringCount": 0,
            "Pv1Power": 0,
            "Pv2Power": 0,
            "Pv3Power": 0,
            "Pv4Power": 0,
        },
    ],
    "SSumInfoList": {
        "ControlEnableStatus": 1,
        "MeterTotalActivePower": -86.3,
        "TotalPVPower": 0,
        "TotalPVChargePower": 0,
        "TotalACChargePower": 789,
        "TotalSmartLoadElectricalPower": 0,
        "AverageBatteryAverageSOC": 48,
        "TotalBatteryOutputPower": 0,
        "TotalGridOutputPower": -789,
        "TotalBackUpPower": 0,
        "TotalChargePower": 732,
    },
}

# System keys whose summary field must equal the aggregate of the units on
# the real capture. grid_power is deliberately absent: MeterTotalActivePower
# is the site CT meter (-86.3W), not a sum of the units' AcInActivePower.
_SUMMARY_VALIDATED_KEYS = [
    "battery_soc",
    "ac_charging_power",
    "battery_discharging_power",
    "pv_power",
    "pv_charging_power",
    "backup_power",
]


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
        device_name="Test Battery",
        manufacturer="AEG",
        model="Solarcube AS-BBL09",
    )
    coord.data = copy.deepcopy(ISSUE9_POLL)
    return coord


@pytest.fixture
def config_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


# ── Summary-vs-sum validation ─────────────────────────────────────────────────


def test_summary_totals_match_unit_aggregates() -> None:
    """Each summary-mapped field must equal the aggregate of the units."""
    units = ISSUE9_POLL["Storage_list"]
    summary = ISSUE9_POLL["SSumInfoList"]
    for key in _SUMMARY_VALIDATED_KEYS:
        summary_field, summary_scale, storage_field, storage_scale, mode = _FIELD_MAP[key]
        assert summary_field is not None, f"{key} lost its summary mapping"
        values = [float(u[storage_field]) * storage_scale for u in units]
        expected = sum(values) / (len(values) if mode == "avg" else 1)
        assert float(summary[summary_field]) * summary_scale == pytest.approx(expected, abs=1.0), (
            f"{key}: {summary_field} diverges from the unit aggregate"
        )


def test_total_charge_power_is_not_the_unit_sum() -> None:
    """TotalChargePower diverges from the unit sum, so it stays unmapped."""
    summary_field = _FIELD_MAP["battery_charging_power"][0]
    assert summary_field is None
    unit_sum = sum(float(u["BatteryChargingPower"]) for u in ISSUE9_POLL["Storage_list"])
    assert float(ISSUE9_POLL["SSumInfoList"]["TotalChargePower"]) != unit_sum


# ── System aggregation ────────────────────────────────────────────────────────


def test_system_values_on_two_unit_capture(coordinator: AeccBatteryCoordinator) -> None:
    """System sensors show whole-stack values, not the first unit's."""
    assert coordinator.get_value("ac_charging_power") == 789.0
    assert coordinator.get_value("battery_soc") == 48.0
    assert coordinator.get_value("battery_discharging_power") == 0.0
    assert coordinator.get_value("grid_power") == -86.3


def test_sum_fallback_when_summary_field_absent(coordinator: AeccBatteryCoordinator) -> None:
    """Firmwares that omit a summary field fall back to summing the units."""
    del coordinator.data["SSumInfoList"]["TotalACChargePower"]
    assert coordinator.get_value("ac_charging_power") == 789.0  # (3920 + 3970) / 10


def test_soc_fallback_averages_units(coordinator: AeccBatteryCoordinator) -> None:
    del coordinator.data["SSumInfoList"]["AverageBatteryAverageSOC"]
    coordinator.data["Storage_list"][0]["BatterySoc"] = 40
    coordinator.data["Storage_list"][1]["BatterySoc"] = 60
    assert coordinator.get_value("battery_soc") == 50.0


def test_battery_charging_power_always_sums_units(coordinator: AeccBatteryCoordinator) -> None:
    """TotalChargePower is never used, even when present (unconfirmed semantics)."""
    coordinator.data["Storage_list"][0]["BatteryChargingPower"] = 1000
    coordinator.data["Storage_list"][1]["BatteryChargingPower"] = 2000
    coordinator.data["SSumInfoList"]["TotalChargePower"] = 9999
    assert coordinator.get_value("battery_charging_power") == 300.0


# ── Key-based per-unit addressing ────────────────────────────────────────────


def test_get_unit_value_by_serial(coordinator: AeccBatteryCoordinator) -> None:
    assert coordinator.get_unit_value(SN1, "ac_charging_power") == 392.0
    assert coordinator.get_unit_value(SN2, "ac_charging_power") == 397.0
    assert coordinator.get_unit_value(SN1, "battery_soc") == 48.0


def test_get_unit_value_survives_reordering(coordinator: AeccBatteryCoordinator) -> None:
    """A reordered Storage_list must never make a unit show another's data."""
    coordinator.data["Storage_list"].reverse()
    assert coordinator.get_unit_value(SN1, "ac_charging_power") == 392.0
    assert coordinator.get_unit_value(SN2, "ac_charging_power") == 397.0


def test_get_unit_value_absent_unit_returns_none(coordinator: AeccBatteryCoordinator) -> None:
    """A unit missing from the poll reads None (entity unavailable), never a neighbour."""
    coordinator.data["Storage_list"] = [u for u in coordinator.data["Storage_list"] if u["StorageSN"] != SN2]
    assert coordinator.get_unit_value(SN2, "ac_charging_power") is None
    assert coordinator.get_unit_value(SN1, "ac_charging_power") == 392.0


def test_unit_key_falls_back_to_devaddr() -> None:
    assert AeccBatteryCoordinator.unit_key({"StorageSN": " SN42 ", "DevAddr": 1}) == "SN42"
    assert AeccBatteryCoordinator.unit_key({"StorageSN": "", "DevAddr": 2}) == "addr2"
    assert AeccBatteryCoordinator.unit_key({"DevAddr": 3}) == "addr3"


# ── Child device identity ─────────────────────────────────────────────────────


def test_unit_device_info(coordinator: AeccBatteryCoordinator) -> None:
    unit = coordinator.units[1]
    info = coordinator.unit_device_info(unit)
    assert info["identifiers"] == {(DOMAIN, f"{coordinator.hub_identifier}_unit_{SN2}")}
    assert info["via_device"] == (DOMAIN, coordinator.hub_identifier)
    assert info["name"] == "Test Battery Battery 2"
    assert info["manufacturer"] == "AEG"


def test_unit_device_info_without_serial(coordinator: AeccBatteryCoordinator) -> None:
    unit = {"DevAddr": 1}
    info = coordinator.unit_device_info(unit)
    assert info["identifiers"] == {(DOMAIN, f"{coordinator.hub_identifier}_unit_addr1")}


def test_unit_device_name_without_devaddr(coordinator: AeccBatteryCoordinator) -> None:
    info = coordinator.unit_device_info({"StorageSN": "SN9"})
    assert info["name"] == "Test Battery Battery SN9"


async def test_remove_config_entry_device(
    hass: HomeAssistant, coordinator: AeccBatteryCoordinator, config_entry
) -> None:
    """Stale unit devices are deletable; the hub and live units are not."""
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator

    def device(identifier):
        entry = MagicMock()
        entry.identifiers = {(DOMAIN, identifier)}
        return entry

    stale = device(f"{coordinator.hub_identifier}_unit_REMOVEDSN")
    assert await async_remove_config_entry_device(hass, config_entry, stale) is True
    hub = device(coordinator.hub_identifier)
    assert await async_remove_config_entry_device(hass, config_entry, hub) is False
    live = device(coordinator.unit_identifier(coordinator.units[0]))
    assert await async_remove_config_entry_device(hass, config_entry, live) is False


# ── Entity platform setup ─────────────────────────────────────────────────────


async def _run_setup(hass: HomeAssistant, coordinator, config_entry) -> list:
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator
    added: list = []

    def _add_entities(entities, update_before_add=False):
        added.extend(entities)

    await sensor_module.async_setup_entry(hass, config_entry, _add_entities)
    return added


async def test_two_units_create_child_entities(
    hass: HomeAssistant, coordinator: AeccBatteryCoordinator, config_entry
) -> None:
    added = await _run_setup(hass, coordinator, config_entry)
    unit_entities = [e for e in added if isinstance(e, (AeccUnitSensor, AeccUnitStatusSensor))]
    # Per unit: the telemetry set plus the derived status sensor.
    assert len(unit_entities) == 2 * (len(_UNIT_SENSORS) + 1)
    unique_ids = {e.unique_id for e in unit_entities}
    assert f"test_entry_unit{SN1}_battery_soc" in unique_ids
    assert f"test_entry_unit{SN2}_battery_status" in unique_ids


async def test_single_unit_creates_no_children(
    hass: HomeAssistant, coordinator: AeccBatteryCoordinator, config_entry
) -> None:
    """Single-unit systems get no child devices."""
    coordinator.data["Storage_list"] = coordinator.data["Storage_list"][:1]
    added = await _run_setup(hass, coordinator, config_entry)
    assert not [e for e in added if isinstance(e, (AeccUnitSensor, AeccUnitStatusSensor))]


# ── Per-unit entities ─────────────────────────────────────────────────────────


def _make_unit_sensor(coordinator, config_entry, unit, canonical_key="ac_charging_power") -> AeccUnitSensor:
    return AeccUnitSensor(
        coordinator=coordinator,
        config_entry=config_entry,
        unit=unit,
        key=canonical_key,
        name="AC Charging Power",
        canonical_key=canonical_key,
        unit_of_meas="W",
        icon="mdi:power-plug",
        is_power=True,
    )


def test_unit_sensor_reads_its_own_unit(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    sensor = _make_unit_sensor(coordinator, config_entry, coordinator.units[1])
    assert sensor.unique_id == f"test_entry_unit{SN2}_ac_charging_power"
    assert sensor.native_value == 397.0
    assert sensor.available is True


def test_unit_sensor_unavailable_when_unit_drops(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    sensor = _make_unit_sensor(coordinator, config_entry, coordinator.units[1])
    coordinator.data["Storage_list"] = coordinator.data["Storage_list"][:1]
    assert sensor.native_value is None
    assert sensor.available is False


def test_unit_sensor_bypasses_cleaner(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    """Per-unit values are raw: an implausible jump the SoC cleaner would
    reject on the system sensor still surfaces on the unit sensor."""
    sensor = _make_unit_sensor(coordinator, config_entry, coordinator.units[0], canonical_key="battery_soc")
    coordinator.get_value("battery_soc")  # seed the cleaner's anchor at 48
    coordinator.data["Storage_list"][0]["BatterySoc"] = 100  # impossible jump
    assert sensor.native_value == 100.0


def test_unit_status_sensor_derives_direction_from_power(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    charging = AeccUnitStatusSensor(coordinator, config_entry, coordinator.units[0])
    assert charging.native_value == "Charging"  # AcChargingPower 3920

    coordinator.data["Storage_list"][1]["AcChargingPower"] = 0
    coordinator.data["Storage_list"][1]["BatteryDischargingPower"] = 5000
    discharging = AeccUnitStatusSensor(coordinator, config_entry, coordinator.units[1])
    assert discharging.native_value == "Discharging"

    coordinator.data["Storage_list"][1]["BatteryDischargingPower"] = 0
    assert discharging.native_value == "Idle"
