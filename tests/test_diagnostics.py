"""Tests for the AECC Battery diagnostics export."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aecc_battery.const import (
    DOMAIN,
    MODE_CUSTOM,
)
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator
from custom_components.aecc_battery.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_tcp_client) -> AeccBatteryCoordinator:
    """Return a coordinator wired to the shared mock TCP client."""
    coord = AeccBatteryCoordinator(
        hass,
        mock_tcp_client,
        device_name="Test Battery",
        manufacturer="Sunpura",
        model="S2400",
    )
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    coord.device_serial = "SN-SECRET-12345"
    coord.firmware_version = "v2.1.0"
    coord.data = {
        "Storage_list": [{"StorageSN": "INV-SECRET-67890", "BatterySoc": 75}],
        "SSumInfoList": {"AverageBatteryAverageSOC": "75"},
    }
    return coord


@pytest.fixture
def stored_entry(hass: HomeAssistant, mock_config_entry, coordinator):
    """Place the coordinator into hass.data the way __init__.py would."""
    hass.data.setdefault(DOMAIN, {})[mock_config_entry.entry_id] = coordinator
    return mock_config_entry


# ── Happy path ──────────────────────────────────────────────────────────────


async def test_diagnostics_returns_all_top_level_sections(hass: HomeAssistant, stored_entry) -> None:
    """The dump must include every section the support workflow expects."""
    diag = await async_get_config_entry_diagnostics(hass, stored_entry)

    assert set(diag.keys()) == {
        "integration",
        "device",
        "config",
        "live_state",
        "cleaner_state",
        "last_poll",
        "control_registers",
        "write_history",
    }
    assert diag["integration"]["domain"] == DOMAIN
    assert diag["integration"]["version"]  # version resolved from manifest


async def test_diagnostics_register_dump_populated(hass: HomeAssistant, stored_entry) -> None:
    """Register dump should pull from the device and surface key labels."""
    diag = await async_get_config_entry_diagnostics(hass, stored_entry)

    regs = diag["control_registers"]
    assert regs["error"] is None
    assert regs["registers"]["3000"] == "1"
    assert regs["registers"]["3020"] == "3"
    # Friendly labels are computed from the documented register set so we
    # don't have to grep raw addresses when reading the file by hand.
    assert "EMS enable (3000)" in regs["key_registers"]
    assert regs["key_registers"]["EMS enable (3000)"] == "1"


# ── Redaction ───────────────────────────────────────────────────────────────


async def test_diagnostics_redacts_pii(hass: HomeAssistant, stored_entry) -> None:
    """Serial numbers and host must not leak; model/firmware survive."""
    diag = await async_get_config_entry_diagnostics(hass, stored_entry)

    assert diag["device"]["device_serial"] == "**REDACTED**"
    assert diag["device"]["host"] == "**REDACTED**"
    # Storage_list serial inside the last_poll blob must also be redacted.
    storage_list = diag["last_poll"]["Storage_list"]
    assert storage_list[0]["StorageSN"] == "**REDACTED**"
    # These stay visible so we can tell users which firmware they're on.
    assert diag["device"]["model"] == "S2400"
    assert diag["device"]["firmware_version"] == "v2.1.0"


# ── Write history ───────────────────────────────────────────────────────────


async def test_diagnostics_includes_write_history(
    hass: HomeAssistant, stored_entry, coordinator: AeccBatteryCoordinator
) -> None:
    """Each mutating call should leave one entry in the audit trail."""
    await coordinator.async_set_work_mode(MODE_CUSTOM)
    diag = await async_get_config_entry_diagnostics(hass, stored_entry)

    history = diag["write_history"]
    assert len(history) == 1
    entry = history[0]
    assert entry["operation"] == f"work_mode({MODE_CUSTOM})"
    assert entry["response_received"] is True
    assert "3000" in entry["payload"]  # EMS_ENABLE in the Custom register set
    assert entry["verify_result"] is not None
    assert all({"register", "expected", "actual", "match"} <= set(r) for r in entry["verify_result"])


# ── Failure handling ────────────────────────────────────────────────────────


async def test_diagnostics_survives_register_read_failure(hass: HomeAssistant, stored_entry, mock_tcp_client) -> None:
    """A device timeout on the wide read must not crash the export."""
    mock_tcp_client.get_control_parameters = AsyncMock(return_value=None)

    diag = await async_get_config_entry_diagnostics(hass, stored_entry)

    # Other sections still render.
    assert diag["device"]["model"] == "S2400"
    # Failure is reported in-band so support can spot it.
    assert diag["control_registers"]["error"] is not None
    assert diag["control_registers"]["registers"] == {}


# ── Defence-in-depth leak detection ─────────────────────────────────────────


async def test_diagnostics_no_sensitive_strings_leak(hass: HomeAssistant, mock_config_entry, mock_tcp_client) -> None:
    """Plant unique sentinels in every leak-prone slot, serialise the dump,
    and assert no sentinel appears in the JSON output.

    This is a belt-and-braces test that catches PII regressions even when
    a future firmware adds fields we haven't anticipated — we prove the
    redaction list covers every path data takes into the dump, rather
    than relying on per-field assertions that only catch known fields.
    """
    serial_sentinel = "ZZ-SENTINEL-DEVICE-SERIAL"
    host_sentinel = "203.0.113.42"  # TEST-NET-3 — guaranteed not real
    storage_sn_sentinel = "ZZ-SENTINEL-STORAGE-SN"
    wifi_password_sentinel = "ZZ-SENTINEL-WIFI-PASSWORD"
    user_email_sentinel = "sentinel-user@example.invalid"

    mock_tcp_client.host = host_sentinel
    coord = AeccBatteryCoordinator(
        hass,
        mock_tcp_client,
        device_name="Test Battery",
        manufacturer="Sunpura",
        model="S2400",
    )
    coord._WRITE_VERIFY_DELAY_SECONDS = 0
    coord.device_serial = serial_sentinel
    coord.firmware_version = "v2.1.0"
    # Realistic poll response with a serial inside Storage_list...
    # ...plus credential-shaped fields that a future firmware might add.
    # If our redaction list misses any of these paths, the sentinel will
    # appear in the JSON blob and this test fails.
    coord.data = {
        "Storage_list": [
            {
                "StorageSN": storage_sn_sentinel,
                "BatterySoc": 75,
                "wifi_password": wifi_password_sentinel,
            },
        ],
        "SSumInfoList": {
            "AverageBatteryAverageSOC": "75",
            "email": user_email_sentinel,
        },
    }

    hass.data.setdefault(DOMAIN, {})[mock_config_entry.entry_id] = coord
    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    blob = json.dumps(diag)
    for sentinel in (
        serial_sentinel,
        host_sentinel,
        storage_sn_sentinel,
        wifi_password_sentinel,
        user_email_sentinel,
    ):
        assert sentinel not in blob, f"Sensitive value {sentinel!r} leaked into diagnostics output: {blob}"
