"""Lunergy battery TCP protocol client."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from .tcp_manager import TCPClientManager

_LOGGER = logging.getLogger(__name__)

_GET_TIMEOUT = 10


class LunergyBatteryClient:

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

    async def get_energy_parameters(self) -> Optional[Dict[str, Any]]:
        return await self._get("EnergyParameter")

    async def get_control_parameters(self, register_addrs: List[int]) -> Optional[Dict[str, Any]]:
        return await self._get("Energycontrolparameters", {"RegControlAddr": register_addrs})

    async def set_control_parameters(self, register_values: Dict[str, str]) -> Optional[Dict[str, Any]]:
        return await self._set("Energycontrolparameters", {"SetControlInfo": register_values})

    async def send_get(self, command: str, extra: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        return await self._get(command, extra)

    async def send_set(self, command: str, extra: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        return await self._set(command, extra)

    async def get_ems_register(self, reg_addr: Any) -> Optional[Dict[str, Any]]:
        return await self._get("DeviceManagement", {"RegDeviceManagementAddr": reg_addr})

    # ── Low-level ──────────────────────────────────────────────────────────

    def _next_serial(self) -> int:
        self._serial += 1
        return self._serial

    async def _get(self, command: str, extra: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "Get": command,
            "SerialNumber": self._next_serial(),
            "CommandSource": "HA",
            **(extra or {}),
        }
        async with self._io_lock:
            try:
                reader, writer = await self._manager.get_reader_writer()
                self._connected = True
                writer.write((json.dumps(payload) + "\n").encode("utf-8"))
                await writer.drain()
                return await self._read_json(reader)
            except (ConnectionResetError, OSError, asyncio.IncompleteReadError) as exc:
                _LOGGER.warning("GET connection error: %s — reconnecting after 2s cooldown", exc)
                self._connected = False
                await asyncio.sleep(2)
                await self._manager.reconnect()
                return None
            except Exception as exc:
                _LOGGER.error("GET error: %s", exc, exc_info=True)
                return None

    async def _set(self, command: str, extra: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """Fire-and-forget: send the command and release the lock immediately."""
        payload: Dict[str, Any] = {
            "Set": command,
            "SerialNumber": self._next_serial(),
            "CommandSource": "HA",
            **(extra or {}),
        }
        async with self._io_lock:
            try:
                reader, writer = await self._manager.get_reader_writer()
                self._connected = True
                writer.write((json.dumps(payload) + "\n").encode("utf-8"))
                await writer.drain()
                return {"sent": True}
            except (ConnectionResetError, OSError, asyncio.IncompleteReadError) as exc:
                _LOGGER.warning("SET connection error: %s — reconnecting after 2s cooldown", exc)
                self._connected = False
                await asyncio.sleep(2)
                await self._manager.reconnect()
                return None
            except Exception as exc:
                _LOGGER.error("SET error: %s", exc, exc_info=True)
                return None

    async def _read_json(self, reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
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
                        _LOGGER.debug("RX ← %s", data)
                        return data
                    except json.JSONDecodeError:
                        await asyncio.sleep(0.05)
        except TimeoutError:
            _LOGGER.warning("GET timed out waiting for response")
            return None
