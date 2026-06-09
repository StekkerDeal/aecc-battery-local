"""AECC battery TCP protocol client."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .const import (
    READ_TIMEOUT_SUSPECT_THRESHOLD,
    RECONNECT_BASE_COOLDOWN,
    RECONNECT_MAX_COOLDOWN,
)
from .tcp_manager import TCPClientManager

_LOGGER = logging.getLogger(__name__)

_GET_TIMEOUT = 10


class _ReadTimeout(Exception):
    """Raised by _read_json when the device accepts the request but never replies."""


class AeccTcpClient:
    def __init__(
        self,
        host: str,
        port: int,
        timeout: float = 5.0,
        base_cooldown: float = RECONNECT_BASE_COOLDOWN,
        max_cooldown: float = RECONNECT_MAX_COOLDOWN,
    ) -> None:
        self.host = host
        self.port = port
        self._manager = TCPClientManager.get_instance(host, port, timeout, base_cooldown, max_cooldown)
        self._serial = 0
        self._connected = False
        self._io_lock = asyncio.Lock()
        # Consecutive GET read timeouts (device connected but silent). After
        # READ_TIMEOUT_SUSPECT_THRESHOLD we recycle the possibly half-open socket.
        self._read_timeout_streak = 0

    async def async_connect(self) -> None:
        await self._manager._connect()
        self._connected = True
        self._manager.note_success()

    async def async_disconnect(self) -> None:
        await self._manager.close()
        self._connected = False

    # ── Public API ─────────────────────────────────────────────────────────

    async def get_energy_parameters(self) -> dict[str, Any] | None:
        return await self._get("EnergyParameter")

    async def get_control_parameters(self, register_addrs: list[int]) -> dict[str, Any] | None:
        return await self._get("Energycontrolparameters", {"RegControlAddr": register_addrs})

    async def set_control_parameters(self, register_values: dict[str, str]) -> dict[str, Any] | None:
        return await self._set("Energycontrolparameters", {"SetControlInfo": register_values})

    async def send_get(self, command: str, extra: dict | None = None) -> dict[str, Any] | None:
        return await self._get(command, extra)

    async def send_set(self, command: str, extra: dict | None = None) -> dict[str, Any] | None:
        return await self._set(command, extra)

    async def get_ems_register(self, reg_addr: Any) -> dict[str, Any] | None:
        return await self._get("DeviceManagement", {"RegDeviceManagementAddr": reg_addr})

    async def get_device_management_info(self) -> dict[str, Any] | None:
        """Read serial, firmware, model from DeviceManagement registers.

        Works on some AECC devices (e.g. Sunpura); times out on others (e.g. Lunergy).
        Uses a short 3-second timeout to avoid blocking setup.
        """
        payload: dict[str, Any] = {
            "Get": "DeviceManagement",
            "SerialNumber": self._next_serial(),
            "CommandSource": "HA",
            "RegDeviceManagementAddr": [2, 8, 9, 20, 21],
        }
        async with self._io_lock:
            try:
                reader, writer = await self._manager.get_reader_writer()
                self._connected = True
                writer.write((json.dumps(payload) + "\n").encode("utf-8"))
                await writer.drain()
                buffer = b""
                async with asyncio.timeout(3):
                    while True:
                        chunk = await reader.read(4096)
                        if not chunk:
                            return None
                        buffer += chunk
                        try:
                            return json.loads(buffer.decode("utf-8"))
                        except json.JSONDecodeError:
                            await asyncio.sleep(0.05)
            except TimeoutError:
                _LOGGER.debug(
                    "DeviceManagement probe timed out (%d bytes received): %.200s",
                    len(buffer),
                    buffer.decode("utf-8", errors="replace") if buffer else "(empty)",
                )
                return None
            except (ConnectionResetError, OSError, asyncio.IncompleteReadError) as exc:
                _LOGGER.debug("DeviceManagement probe connection error: %s", exc)
                return None
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError) as exc:
                _LOGGER.debug("DeviceManagement probe error: %s", exc)
                return None

    # ── Low-level ──────────────────────────────────────────────────────────

    def _next_serial(self) -> int:
        self._serial += 1
        return self._serial

    async def _get(self, command: str, extra: dict | None = None) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "Get": command,
            "SerialNumber": self._next_serial(),
            "CommandSource": "HA",
            **(extra or {}),
        }
        _LOGGER.debug("TX GET -> %s", command)
        async with self._io_lock:
            try:
                reader, writer = await self._manager.get_reader_writer()
                self._connected = True
                writer.write((json.dumps(payload) + "\n").encode("utf-8"))
                await writer.drain()
                result = await self._read_json(reader)
                self._manager.note_success()
                self._read_timeout_streak = 0
                return result
            except _ReadTimeout:
                await self._handle_read_timeout("GET", command)
                return None
            except (ConnectionResetError, OSError, asyncio.IncompleteReadError) as exc:
                await self._handle_connection_error("GET", command, exc)
                return None
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError) as exc:
                _LOGGER.error("GET %s error: %s", command, exc, exc_info=True)
                return None

    async def _set(self, command: str, extra: dict | None = None) -> dict[str, Any] | None:
        """Send SET command and wait for acknowledgement from the battery."""
        payload: dict[str, Any] = {
            "Set": command,
            "SerialNumber": self._next_serial(),
            "CommandSource": "HA",
            **(extra or {}),
        }
        _LOGGER.debug("TX SET -> %s", json.dumps(payload))
        async with self._io_lock:
            try:
                reader, writer = await self._manager.get_reader_writer()
                self._connected = True
                writer.write((json.dumps(payload) + "\n").encode("utf-8"))
                await writer.drain()
                response = await self._read_json(reader)
                _LOGGER.debug("RX SET <- %s", response)
                self._manager.note_success()
                self._read_timeout_streak = 0
                return response
            except _ReadTimeout:
                await self._handle_read_timeout("SET", command)
                return None
            except (ConnectionResetError, OSError, asyncio.IncompleteReadError) as exc:
                await self._handle_connection_error("SET", command, exc)
                return None
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError) as exc:
                _LOGGER.error("SET %s error: %s", command, exc, exc_info=True)
                return None

    # ── Failure handling ─────────────────────────────────────────────────────

    async def _handle_connection_error(self, op: str, command: str, exc: Exception) -> None:
        """Log, back off, and reconnect after a connection-level error.

        The cooldown is read before recording the failure, so the first error
        of an outage waits ``base_cooldown`` (matching the previous flat delay)
        and only sustained failures escalate. A failing reconnect is swallowed
        here so it can never propagate out of the calling _get/_set.
        """
        self._connected = False
        cooldown = self._manager.current_cooldown()
        _LOGGER.warning(
            "%s %s connection error: %s - reconnecting after %.0fs cooldown",
            op,
            command,
            exc,
            cooldown,
        )
        self._manager.note_failure()
        await asyncio.sleep(cooldown)
        try:
            await self._manager.reconnect()
        except (TimeoutError, OSError) as reconnect_exc:
            _LOGGER.debug("%s %s reconnect failed: %s", op, command, reconnect_exc)

    async def _handle_read_timeout(self, op: str, command: str) -> None:
        """Recycle a likely half-open socket after repeated silent reads.

        A single slow response is tolerated; only after
        ``READ_TIMEOUT_SUSPECT_THRESHOLD`` consecutive timeouts do we close the
        socket so the next request reconnects lazily.
        """
        self._read_timeout_streak += 1
        if self._read_timeout_streak >= READ_TIMEOUT_SUSPECT_THRESHOLD:
            _LOGGER.warning(
                "%s %s: %d consecutive read timeouts - recycling socket",
                op,
                command,
                self._read_timeout_streak,
            )
            await self._manager.close()
            self._read_timeout_streak = 0

    async def _read_json(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        buffer = b""
        try:
            async with asyncio.timeout(_GET_TIMEOUT):
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        raise ConnectionResetError("Battery closed connection")
                    buffer += chunk
                    try:
                        data = json.loads(buffer.decode("utf-8"))
                        _LOGGER.debug("RX <- %s", data)
                        return data
                    except json.JSONDecodeError:
                        await asyncio.sleep(0.05)
        except TimeoutError:
            _LOGGER.warning(
                "GET timed out waiting for response (%d bytes received): %.300s",
                len(buffer),
                buffer.decode("utf-8", errors="replace") if buffer else "(empty)",
            )
            raise _ReadTimeout from None
