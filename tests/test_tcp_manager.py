"""Tests for the AECC TCP connection manager (backoff + lock-safe reconnect)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.aecc_battery.tcp_manager import TCPClientManager


@pytest.fixture(autouse=True)
def _clear_registry():
    """Keep the class-level singleton registry from leaking across tests."""
    TCPClientManager._connections.clear()
    yield
    TCPClientManager._connections.clear()


def _make_rw():
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.read = AsyncMock(return_value=b"")
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


# ── Backoff formula ───────────────────────────────────────────────────────────


def test_cooldown_formula() -> None:
    m = TCPClientManager("h", 1, base_cooldown=2, max_cooldown=60)
    assert m._cooldown_for(0) == 2
    assert m._cooldown_for(1) == 4
    assert m._cooldown_for(2) == 8
    assert m._cooldown_for(5) == 60  # 2*32=64, capped
    assert m._cooldown_for(10) == 60


def test_current_cooldown_tracks_failures() -> None:
    m = TCPClientManager("h", 1, base_cooldown=2, max_cooldown=60)
    assert m.current_cooldown() == 2
    m.note_failure()
    assert m.current_cooldown() == 4
    m.note_failure()
    assert m.current_cooldown() == 8


def test_note_success_resets_failures() -> None:
    m = TCPClientManager("h", 1)
    m.note_failure()
    m.note_failure()
    assert m._consecutive_failures == 2
    m.note_success()
    assert m._consecutive_failures == 0
    assert m.current_cooldown() == m._base_cooldown


# ── Singleton registry ─────────────────────────────────────────────────────────


def test_get_instance_is_singleton_per_host_port() -> None:
    a = TCPClientManager.get_instance("h", 1)
    b = TCPClientManager.get_instance("h", 1)
    c = TCPClientManager.get_instance("h", 2)
    assert a is b
    assert c is not a


def test_get_instance_does_not_mutate_existing_cooldown() -> None:
    a = TCPClientManager.get_instance("h", 1, base_cooldown=2, max_cooldown=60)
    b = TCPClientManager.get_instance("h", 1, base_cooldown=5, max_cooldown=99)
    assert b is a
    assert a._base_cooldown == 2
    assert a._max_cooldown == 60


# ── Lock-safe reconnect ─────────────────────────────────────────────────────────


async def test_reconnect_connects_without_deadlock(monkeypatch) -> None:
    reader, writer = _make_rw()
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(return_value=(reader, writer)))
    m = TCPClientManager("h", 1)
    # A prior get_reader_writer takes and releases the lock; reconnect must be
    # able to take it again without deadlocking.
    await m.get_reader_writer()
    await m.reconnect()
    assert m.reader is reader
    assert m.writer is writer
