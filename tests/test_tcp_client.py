"""Tests for AeccTcpClient backoff, guarded reconnect, and half-open recycle."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.aecc_battery.tcp_client import AeccTcpClient
from custom_components.aecc_battery.tcp_manager import TCPClientManager


@pytest.fixture(autouse=True)
def _clear_registry():
    TCPClientManager._connections.clear()
    yield
    TCPClientManager._connections.clear()


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Replace tcp_client's asyncio.sleep so backoff is recorded, not waited."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("custom_components.aecc_battery.tcp_client.asyncio.sleep", fake_sleep)
    return sleeps


def _make_rw(read_side=None, read_return=b""):
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.read = AsyncMock(side_effect=read_side) if read_side is not None else AsyncMock(return_value=read_return)
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


def _patch_open(monkeypatch, *, return_value=None, side_effect=None):
    mock = AsyncMock(return_value=return_value) if side_effect is None else AsyncMock(side_effect=side_effect)
    monkeypatch.setattr(asyncio, "open_connection", mock)
    return mock


# ── Connection-error backoff ───────────────────────────────────────────────────


async def test_single_blip_sleeps_base_then_reconnects(monkeypatch, recorded_sleeps) -> None:
    # Reader always reports a closed connection (empty chunk -> ConnectionResetError).
    reader, writer = _make_rw(read_return=b"")
    _patch_open(monkeypatch, return_value=(reader, writer))
    client = AeccTcpClient("h", 1, base_cooldown=2, max_cooldown=60)

    result = await client.send_get("EnergyParameter")

    assert result is None
    assert recorded_sleeps == [2]  # first failure uses base, parity with old flat 2s
    assert client._manager._consecutive_failures == 1


async def test_sustained_outage_escalates_and_caps(monkeypatch, recorded_sleeps) -> None:
    reader, writer = _make_rw(read_return=b"")
    _patch_open(monkeypatch, return_value=(reader, writer))
    client = AeccTcpClient("h", 1, base_cooldown=2, max_cooldown=60)

    for _ in range(7):
        assert await client.send_get("EnergyParameter") is None

    assert recorded_sleeps == [2, 4, 8, 16, 32, 60, 60]


async def test_reconnect_failure_is_swallowed(monkeypatch, recorded_sleeps) -> None:
    reader, writer = _make_rw(read_return=b"")
    # Initial connect succeeds; the reconnect attempt raises.
    _patch_open(monkeypatch, side_effect=[(reader, writer), OSError("refused")])
    client = AeccTcpClient("h", 1)

    # Must not raise out of the request path.
    assert await client.send_get("EnergyParameter") is None


async def test_recovery_resets_failures(monkeypatch, recorded_sleeps) -> None:
    reader, writer = _make_rw(read_side=[b"", b"", b'{"ok": 1}'])
    _patch_open(monkeypatch, return_value=(reader, writer))
    client = AeccTcpClient("h", 1, base_cooldown=2, max_cooldown=60)

    assert await client.send_get("EnergyParameter") is None
    assert await client.send_get("EnergyParameter") is None
    assert await client.send_get("EnergyParameter") == {"ok": 1}

    assert recorded_sleeps == [2, 4]
    assert client._manager._consecutive_failures == 0


async def test_concurrent_get_and_set_failure_single_counter(monkeypatch, recorded_sleeps) -> None:
    reader, writer = _make_rw(read_return=b"")
    _patch_open(monkeypatch, return_value=(reader, writer))
    client = AeccTcpClient("h", 1)

    await asyncio.gather(client.send_get("EnergyParameter"), client.send_set("Energycontrolparameters"))

    # _io_lock serializes the two failing requests: exactly two recorded failures.
    assert client._manager._consecutive_failures == 2


# ── Half-open socket guard ─────────────────────────────────────────────────────


async def test_read_timeout_below_threshold_no_recycle(monkeypatch, recorded_sleeps) -> None:
    reader, writer = _make_rw(read_side=[TimeoutError, TimeoutError])
    _patch_open(monkeypatch, return_value=(reader, writer))
    client = AeccTcpClient("h", 1)
    client._manager.close = AsyncMock()

    assert await client.send_get("EnergyParameter") is None
    assert await client.send_get("EnergyParameter") is None

    client._manager.close.assert_not_called()
    assert client._read_timeout_streak == 2
    assert client._manager._consecutive_failures == 0  # timeouts are not connection errors


async def test_read_timeout_at_threshold_recycles(monkeypatch, recorded_sleeps) -> None:
    reader, writer = _make_rw(read_side=[TimeoutError, TimeoutError, TimeoutError])
    _patch_open(monkeypatch, return_value=(reader, writer))
    client = AeccTcpClient("h", 1)
    client._manager.close = AsyncMock()

    for _ in range(3):
        assert await client.send_get("EnergyParameter") is None

    client._manager.close.assert_awaited_once()
    assert client._read_timeout_streak == 0  # reset after recycle


async def test_slow_but_healthy_never_recycles(monkeypatch, recorded_sleeps) -> None:
    # Alternating timeout / good response: the streak resets on every success
    # and never reaches the recycle threshold.
    good = b'{"ok": 1}'
    reader, writer = _make_rw(read_side=[TimeoutError, good, TimeoutError, good, TimeoutError, good])
    _patch_open(monkeypatch, return_value=(reader, writer))
    client = AeccTcpClient("h", 1)
    client._manager.close = AsyncMock()

    for _ in range(6):
        await client.send_get("EnergyParameter")

    client._manager.close.assert_not_called()
    assert client._read_timeout_streak == 0


# ── DeviceManagement hardening ─────────────────────────────────────────────────


def test_no_arbitrary_register_reader() -> None:
    """The get_ems_register foot-gun is gone: no path can read an arbitrary reg."""
    assert not hasattr(AeccTcpClient, "get_ems_register")


async def test_device_management_request_is_a_safe_fixed_list(monkeypatch) -> None:
    """The DeviceManagement read requests only the fixed identity + RSSI set.

    Asserting the exact list guarantees no other (incl. credential/secret)
    register can ever be requested by this path.
    """
    reader, writer = _make_rw(read_return=b'{"Response":"DeviceManagement","ControlInfo":{}}')
    _patch_open(monkeypatch, return_value=(reader, writer))
    client = AeccTcpClient("h", 1)

    await client.get_device_management_info()

    payload = json.loads(writer.write.call_args[0][0].decode("utf-8"))
    assert set(payload["RegDeviceManagementAddr"]) == {2, 8, 9, 20, 21, 76}
