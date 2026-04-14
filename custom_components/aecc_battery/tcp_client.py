"""AECC battery TCP protocol client."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .tcp_manager import TCPClientManager

_LOGGER = logging.getLogger(__name__)

_GET_TIMEOUT = 10


class AeccTcpClient:
    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self._manager = TCPClientManager.get_instance(host, port, timeout)
        self._serial = 0
        self._connected = False
        self._io_lock = asyncio.Lock()

    async def async_connect(self) -> None:
        await self._manager._connect()
        self._connected = True

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
                if result is None:
                    _LOGGER.warning("GET %s returned no data", command)
                return result
            except (ConnectionResetError, OSError, asyncio.IncompleteReadError) as exc:
                _LOGGER.warning("GET %s connection error: %s - reconnecting after 2s cooldown", command, exc)
                self._connected = False
                await asyncio.sleep(2)
                await self._manager.reconnect()
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
                return response
            except (ConnectionResetError, OSError, asyncio.IncompleteReadError) as exc:
                _LOGGER.warning("SET %s connection error: %s - reconnecting after 2s cooldown", command, exc)
                self._connected = False
                await asyncio.sleep(2)
                await self._manager.reconnect()
                return None
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError) as exc:
                _LOGGER.error("SET %s error: %s", command, exc, exc_info=True)
                return None

    async def _read_json(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
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
            return None
