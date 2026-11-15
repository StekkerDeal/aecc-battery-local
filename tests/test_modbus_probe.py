"""Modbus probe: choosing the shared or the separate connection, and refreshing over it."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aecc_battery.const import (
    MB_AVAILABLE_CHARGE_POWER,
    MB_ENERGY_CHARGED,
    MB_TEMP_1,
    MB_TEMP_3,
    MODBUS_BLOCKS,
    MODBUS_REFRESH_INTERVAL,
    MODBUS_TRANSPORT_SEPARATE,
    MODBUS_TRANSPORT_SHARED,
)
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator
from custom_components.aecc_battery.tcp_client import ModbusException

from .test_modbus import _TSUN_BLOCKS, _answering

_MONOTONIC = "custom_components.aecc_battery.coordinator.time.monotonic"


@pytest.fixture
def coordinator(hass: HomeAssistant) -> AeccBatteryCoordinator:
    client = AsyncMock()
    client.host = "192.168.1.100"
    client.port = 8080
    client.consecutive_failures = 0
    client.read_register_blocks_separately = AsyncMock(return_value=None)
    coord = AeccBatteryCoordinator(hass, client, device_name="Test", manufacturer="TSUN")
    coord.data = {"SSumInfoList": {"AverageBatteryAverageSOC": "70"}}
    return coord


def _separate_results() -> list[list[int]]:
    """One word list per block, in MODBUS_BLOCKS order."""
    return [list(_TSUN_BLOCKS[block]) for block in MODBUS_BLOCKS]


async def _probe_separate(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.read_holding_registers = AsyncMock(return_value=None)
    coordinator.client.read_register_blocks_separately = AsyncMock(return_value=_separate_results())
    await coordinator.async_probe_modbus()


def _advance(monkeypatch, seconds: float) -> None:
    later = time.monotonic() + seconds
    monkeypatch.setattr(_MONOTONIC, lambda: later)


async def test_shared_answers_stays_shared(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.read_holding_registers = _answering()

    await coordinator.async_probe_modbus()

    assert coordinator.modbus_transport == MODBUS_TRANSPORT_SHARED
    assert coordinator.modbus_supported is True
    assert coordinator.client.read_holding_registers.await_count == len(MODBUS_BLOCKS)
    coordinator.client.read_register_blocks_separately.assert_not_awaited()


async def test_silent_shared_falls_back_to_separate(coordinator: AeccBatteryCoordinator) -> None:
    await _probe_separate(coordinator)

    assert coordinator.modbus_transport == MODBUS_TRANSPORT_SEPARATE
    assert coordinator.modbus_supported is True
    assert coordinator.client.read_holding_registers.await_count == 1
    coordinator.client.read_register_blocks_separately.assert_awaited_once_with(MODBUS_BLOCKS)
    assert coordinator.modbus[MB_AVAILABLE_CHARGE_POWER] == 2405
    assert coordinator.modbus[MB_TEMP_1] == 45.4
    assert coordinator.modbus[MB_TEMP_3] == 48.5
    assert coordinator.modbus[MB_ENERGY_CHARGED] == 50.9
    assert coordinator._last_modbus_error is None


async def test_refused_shared_does_not_fall_back(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.read_holding_registers = AsyncMock(return_value=ModbusException(2))
    coordinator.client.read_register_blocks_separately = AsyncMock(return_value=_separate_results())

    await coordinator.async_probe_modbus()

    coordinator.client.read_register_blocks_separately.assert_not_awaited()
    assert coordinator.modbus_supported is False
    assert coordinator.modbus_transport is None
    assert coordinator.modbus == {}


async def test_both_silent_is_unsupported(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.read_holding_registers = AsyncMock(return_value=None)
    coordinator.client.read_register_blocks_separately = AsyncMock(return_value=[None])

    await coordinator.async_probe_modbus()

    coordinator.client.read_register_blocks_separately.assert_awaited_once()
    assert coordinator.modbus_supported is False
    assert coordinator.modbus_transport is None
    assert coordinator.modbus == {}
    assert coordinator._last_modbus_error == "no reply for 65030 x23"


async def test_separate_connection_failure_is_unsupported(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.read_holding_registers = AsyncMock(return_value=None)
    coordinator.client.read_register_blocks_separately = AsyncMock(return_value=None)

    await coordinator.async_probe_modbus()

    coordinator.client.read_register_blocks_separately.assert_awaited_once()
    assert coordinator.modbus_supported is False
    assert coordinator.modbus_transport is None
    assert coordinator._last_modbus_error == "separate connection failed"


async def test_separate_refresh_rounds(coordinator: AeccBatteryCoordinator, monkeypatch) -> None:
    await _probe_separate(coordinator)
    client = coordinator.client
    client.read_holding_registers.reset_mock()
    client.read_register_blocks_separately.reset_mock()

    # Inside the interval: nothing is sent.
    await coordinator._async_maybe_refresh_modbus()
    client.read_register_blocks_separately.assert_not_awaited()

    # A due round: one separate call, no shared reads.
    updated = _separate_results()
    updated[1] = [500, 454, 485]
    client.read_register_blocks_separately.return_value = updated
    _advance(monkeypatch, MODBUS_REFRESH_INTERVAL + 1)
    await coordinator._async_maybe_refresh_modbus()
    client.read_register_blocks_separately.assert_awaited_once_with(MODBUS_BLOCKS)
    assert coordinator.modbus[MB_TEMP_1] == 50.0

    # Same clock: throttled again.
    await coordinator._async_maybe_refresh_modbus()
    assert client.read_register_blocks_separately.await_count == 1

    # A failed round keeps the values and the mode.
    client.read_register_blocks_separately.return_value = None
    _advance(monkeypatch, 2 * (MODBUS_REFRESH_INTERVAL + 1))
    await coordinator._async_maybe_refresh_modbus()
    assert client.read_register_blocks_separately.await_count == 2
    assert coordinator.modbus[MB_TEMP_1] == 50.0
    assert coordinator.modbus[MB_ENERGY_CHARGED] == 50.9
    assert coordinator.modbus_transport == MODBUS_TRANSPORT_SEPARATE
    assert coordinator.modbus_supported is True
    assert coordinator._last_modbus_error == "separate connection failed"

    client.read_holding_registers.assert_not_awaited()


async def test_shared_refresh_never_uses_separate(coordinator: AeccBatteryCoordinator, monkeypatch) -> None:
    coordinator.client.read_holding_registers = _answering()
    await coordinator.async_probe_modbus()
    coordinator.client.read_holding_registers.reset_mock()

    _advance(monkeypatch, MODBUS_REFRESH_INTERVAL + 1)
    await coordinator._async_maybe_refresh_modbus()

    assert coordinator.client.read_holding_registers.await_count == len(MODBUS_BLOCKS)
    coordinator.client.read_register_blocks_separately.assert_not_awaited()
    assert coordinator.modbus_transport == MODBUS_TRANSPORT_SHARED


@pytest.mark.parametrize("failed_block", [1, 2, 3])
async def test_later_shared_block_silent_stays_shared(coordinator: AeccBatteryCoordinator, failed_block: int) -> None:
    blocks = dict(_TSUN_BLOCKS)
    blocks[MODBUS_BLOCKS[failed_block]] = None
    coordinator.client.read_holding_registers = _answering(blocks)

    await coordinator.async_probe_modbus()

    assert coordinator.modbus_transport == MODBUS_TRANSPORT_SHARED
    assert coordinator.modbus_supported is True
    coordinator.client.read_register_blocks_separately.assert_not_awaited()
    start, count = MODBUS_BLOCKS[failed_block]
    assert coordinator._last_modbus_error == f"no reply for {start} x{count}"
