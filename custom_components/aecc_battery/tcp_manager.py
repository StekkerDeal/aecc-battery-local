"""Persistent TCP connection manager for AECC battery devices."""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)


class TCPClientManager:
    """Manages a single persistent TCP connection per (host, port) pair.

    Uses a class-level registry so that multiple callers sharing the same
    host/port always reuse the same underlying socket.
    """

    _connections: dict[tuple[str, int], TCPClientManager] = {}

    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def get_instance(
        cls, host: str, port: int, timeout: float = 5.0
    ) -> TCPClientManager:
        key = (host, port)
        if key not in cls._connections:
            cls._connections[key] = TCPClientManager(host, port, timeout)
        return cls._connections[key]

    @classmethod
    def remove_instance(cls, host: str, port: int) -> None:
        cls._connections.pop((host, port), None)

    # ── Connection helpers ────────────────────────────────────────────────────

    async def get_reader_writer(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        async with self._lock:
            if not self.writer or self.writer.is_closing():
                await self._connect()
            return self.reader, self.writer

    async def _connect(self) -> None:
        try:
            _LOGGER.info("Connecting to %s:%s (timeout=%ss)", self.host, self.port, self.timeout)
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
            _LOGGER.info("Connected to %s:%s", self.host, self.port)
        except asyncio.TimeoutError:
            _LOGGER.error("Connection timed out: %s:%s", self.host, self.port)
            raise
        except OSError as exc:
            _LOGGER.error("Connection failed: %s:%s – %s", self.host, self.port, exc)
            raise

    async def close(self) -> None:
        if self.writer and not self.writer.is_closing():
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
            _LOGGER.info("Closed connection to %s:%s", self.host, self.port)
        self.reader = None
        self.writer = None

    async def reconnect(self) -> None:
        _LOGGER.info("Reconnecting to %s:%s", self.host, self.port)
        await self.close()
        await self._connect()
