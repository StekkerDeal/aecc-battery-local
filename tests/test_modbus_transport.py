"""Modbus over a connection of its own, against a protocol-aware fake device."""

from __future__ import annotations

import asyncio
import time
import types
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.aecc_battery.const import (
    CONF_HOST,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    DOMAIN,
    MB_ENERGY_CHARGED,
    MB_TEMP_1,
    MODBUS_BLOCKS,
    MODBUS_REFRESH_INTERVAL,
    MODBUS_TRANSPORT_SEPARATE,
    MODBUS_TRANSPORT_SHARED,
)
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator
from custom_components.aecc_battery.diagnostics import async_get_config_entry_diagnostics
from custom_components.aecc_battery.sensor import _MODBUS_SENSORS
from custom_components.aecc_battery.tcp_client import AeccTcpClient
from custom_components.aecc_battery.tcp_manager import TCPClientManager
from tests.conftest import TSUN_BLOCKS, FakeDevice

_HOST = "192.168.1.60"
_PORT = 8080

_ENTRY_DATA = {
    CONF_HOST: _HOST,
    CONF_PORT: _PORT,
    CONF_NAME: "Test Battery",
    CONF_MANUFACTURER: "Lunergy",
    CONF_MODEL: "Hub 2400 AC",
}

_EXPECTED_ROUND = [TSUN_BLOCKS[block] for block in MODBUS_BLOCKS]


@pytest.fixture(autouse=True)
def _clear_registry():
    TCPClientManager._connections.clear()
    yield
    TCPClientManager._connections.clear()


def _client() -> AeccTcpClient:
    return AeccTcpClient(_HOST, _PORT)


# ── Client: separate round ─────────────────────────────────────────────────────


async def test_separate_round_after_json(lunergy_device: FakeDevice) -> None:
    client = _client()
    assert await client.send_get("Foo") == {"SerialNumber": 1}

    assert await client.read_register_blocks_separately(MODBUS_BLOCKS) == _EXPECTED_ROUND

    # JSON socket closed before the Modbus one opened, Modbus closed afterwards.
    assert lunergy_device.events == ["open", "close", "open", "close"]
    assert lunergy_device.max_live == 1
    assert lunergy_device.live == 0
    assert client._manager.writer is None
    assert [c.protocol for c in lunergy_device.connections] == ["json", "modbus"]

    assert await client.send_get("Bar") == {"SerialNumber": 2}
    assert lunergy_device.events == ["open", "close", "open", "close", "open"]
    assert lunergy_device.max_live == 1
    assert client.consecutive_failures == 0


@pytest.mark.parametrize("error", [OSError("refused"), TimeoutError()], ids=["oserror", "timeout"])
async def test_separate_connect_failure(lunergy_device: FakeDevice, monkeypatch, error) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    client = _client()
    assert await client.send_get("Foo") is not None
    lunergy_device.connect_error = error

    assert await client.read_register_blocks_separately(MODBUS_BLOCKS) is None

    assert lunergy_device.live == 0
    assert client._manager.writer is None
    assert client.consecutive_failures == 0
    assert client._manager.read_timeout_streak == 0
    sleep.assert_not_awaited()


async def test_silent_block_stops_the_round(lunergy_device: FakeDevice) -> None:
    lunergy_device.silent_blocks = {MODBUS_BLOCKS[1]}
    client = _client()

    results = await client.read_register_blocks_separately(MODBUS_BLOCKS)

    assert results == [_EXPECTED_ROUND[0], None]
    assert lunergy_device.modbus_requests == list(MODBUS_BLOCKS[:2])
    assert lunergy_device.live == 0
    assert client._manager.writer is None
    assert client.consecutive_failures == 0
    assert client._manager.read_timeout_streak == 0


async def test_cancelled_round_leaves_no_socket(lunergy_device: FakeDevice, monkeypatch) -> None:
    monkeypatch.setattr("custom_components.aecc_battery.tcp_client.MODBUS_READ_TIMEOUT", 30)
    lunergy_device.silent_blocks = {MODBUS_BLOCKS[0]}
    client = _client()

    task = asyncio.ensure_future(client.read_register_blocks_separately(MODBUS_BLOCKS))
    while not lunergy_device.modbus_requests:
        await asyncio.sleep(0)
    assert lunergy_device.live == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lunergy_device.live == 0
    assert client._manager.writer is None
    assert not client._io_lock.locked()


@pytest.mark.parametrize("round_first", [True, False], ids=["round-first", "json-first"])
async def test_round_concurrent_with_json(lunergy_device: FakeDevice, round_first: bool) -> None:
    client = _client()
    assert await client.send_get("Warmup") is not None

    round_coro = client.read_register_blocks_separately(MODBUS_BLOCKS)
    json_coro = client.send_get("Foo")
    if round_first:
        results, reply = await asyncio.gather(round_coro, json_coro)
    else:
        reply, results = await asyncio.gather(json_coro, round_coro)

    assert results == _EXPECTED_ROUND
    assert reply is not None and reply["SerialNumber"] == 2
    assert lunergy_device.max_live == 1
    assert not any(c.deaf for c in lunergy_device.connections)


# ── Client: shared socket ──────────────────────────────────────────────────────


async def test_shared_read_after_json_is_ignored_on_first_frame_firmware(lunergy_device: FakeDevice) -> None:
    client = _client()
    assert await client.send_get("Foo") is not None

    assert await client.read_holding_registers(*MODBUS_BLOCKS[0]) is None

    assert lunergy_device.connections[0].frames == [("json", True), ("modbus", False)]
    assert lunergy_device.live == 0  # the silent socket is closed
    assert client.consecutive_failures == 0


async def test_shared_read_after_json_is_answered_on_both_firmware(tsun_device: FakeDevice) -> None:
    client = _client()
    assert await client.send_get("Foo") is not None

    assert await client.read_holding_registers(*MODBUS_BLOCKS[0]) == _EXPECTED_ROUND[0]
    assert await client.send_get("Bar") is not None

    assert tsun_device.opened == 1
    assert tsun_device.live == 1
    assert tsun_device.connections[0].frames == [("json", True), ("modbus", True), ("json", True)]


@pytest.mark.parametrize("before_probe", ["closed", "connected_unused"])
async def test_probe_never_starts_a_socket_with_modbus(
    hass: HomeAssistant, lunergy_device: FakeDevice, before_probe: str
) -> None:
    """A socket closed or opened without JSON before the probe must not end up Modbus-first."""
    client = _client()
    assert await client.send_get("Foo") is not None
    await client._manager.close()
    if before_probe == "connected_unused":
        await client.async_connect()

    coord = AeccBatteryCoordinator(hass, client, device_name="T", manufacturer="Lunergy")
    await coord.async_probe_modbus()

    assert coord.modbus_transport == MODBUS_TRANSPORT_SEPARATE
    assert coord.modbus_supported is True
    assert await client.send_get("Bar") is not None
    assert lunergy_device.max_live == 1


async def test_probe_on_an_open_json_socket_sends_no_extra_json(hass: HomeAssistant, tsun_device: FakeDevice) -> None:
    client = _client()
    assert await client.send_get("Foo") is not None

    coord = AeccBatteryCoordinator(hass, client, device_name="T", manufacturer="TSUN")
    await coord.async_probe_modbus()

    assert coord.modbus_transport == MODBUS_TRANSPORT_SHARED
    assert [protocol for protocol, _ in tsun_device.connections[0].frames].count("json") == 1
    assert tsun_device.opened == 1


# ── End to end ─────────────────────────────────────────────────────────────────


async def _set_up(hass: HomeAssistant) -> tuple[MockConfigEntry, AeccBatteryCoordinator]:
    entry = MockConfigEntry(domain=DOMAIN, data=_ENTRY_DATA, unique_id=f"{_HOST}:{_PORT}")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, hass.data[DOMAIN][entry.entry_id]


def _unique_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def test_setup_on_first_frame_firmware_uses_separate(
    hass: HomeAssistant, lunergy_device: FakeDevice, monkeypatch
) -> None:
    entry, coordinator = await _set_up(hass)

    assert coordinator.modbus_supported is True
    assert coordinator.modbus_transport == MODBUS_TRANSPORT_SEPARATE
    assert coordinator.modbus[MB_TEMP_1] == 45.4
    unique_ids = _unique_ids(hass, entry)
    assert {f"{entry.entry_id}_{spec[0]}" for spec in _MODBUS_SENSORS} <= unique_ids
    assert f"{entry.entry_id}_fault" in unique_ids
    assert lunergy_device.max_live == 1
    assert not any(c.deaf for c in lunergy_device.connections)

    # One throttled refresh round later, still one socket at a time.
    opened = lunergy_device.opened
    coordinator.modbus.clear()
    later = time.monotonic() + MODBUS_REFRESH_INTERVAL + 1
    # Only the coordinator's clock moves; the event loop keeps real time.
    clock = types.SimpleNamespace(monotonic=lambda: later, time=time.time)
    monkeypatch.setattr("custom_components.aecc_battery.coordinator.time", clock)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.modbus[MB_ENERGY_CHARGED] == 50.9
    assert lunergy_device.opened == opened + 2  # JSON reconnect, then the Modbus connection
    assert lunergy_device.max_live == 1
    assert coordinator.client.consecutive_failures == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert lunergy_device.live == 0


async def test_setup_on_both_firmware_uses_shared(hass: HomeAssistant, tsun_device: FakeDevice) -> None:
    entry, coordinator = await _set_up(hass)

    assert coordinator.modbus_supported is True
    assert coordinator.modbus_transport == MODBUS_TRANSPORT_SHARED
    assert f"{entry.entry_id}_fault" in _unique_ids(hass, entry)
    assert tsun_device.opened == 1
    assert tsun_device.max_live == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert tsun_device.live == 0


# ── Diagnostics ────────────────────────────────────────────────────────────────


async def test_diagnostics_reports_separate_transport(hass: HomeAssistant, mock_tcp_client, mock_config_entry) -> None:
    coordinator = AeccBatteryCoordinator(hass, mock_tcp_client, device_name="Test Battery", manufacturer="Lunergy")
    coordinator.data = {"SSumInfoList": {"AverageBatteryAverageSOC": "75"}}
    coordinator.modbus_supported = True
    coordinator.modbus_transport = MODBUS_TRANSPORT_SEPARATE
    hass.data.setdefault(DOMAIN, {})[mock_config_entry.entry_id] = coordinator

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diag["modbus"]["transport"] == "separate"
