"""AECC battery TCP protocol client."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import dataclass
from typing import Any

from .const import (
    MODBUS_READ_TIMEOUT,
    MODBUS_UNIT_ID,
    READ_TIMEOUT_SUSPECT_THRESHOLD,
    RECONNECT_BASE_COOLDOWN,
    RECONNECT_MAX_COOLDOWN,
)
from .tcp_manager import TCPClientManager

_LOGGER = logging.getLogger(__name__)

_GET_TIMEOUT = 10

_MODBUS_PROTOCOL_ID = 0
_MODBUS_FC_READ_HOLDING = 0x03
_MODBUS_MBAP_LEN = 7


class _ReadTimeout(Exception):
    """Raised by _read_json when the device accepts the request but never replies."""


@dataclass(frozen=True)
class ModbusException:
    """The device answered a Modbus request with an exception code.

    Code 2 (illegal data address) is how a device says it has no such register,
    which is the signal for "this unit does not expose the Modbus map".
    """

    code: int


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
        self._modbus_tid = 0
        self._connected = False
        self._io_lock = asyncio.Lock()

    async def async_connect(self) -> None:
        # get_reader_writer() reuses a live socket where _connect() replaced it.
        # Setup calls this twice per attempt, so bypassing the guard leaked one
        # socket each time.
        await self._manager.get_reader_writer()
        self._connected = True
        self._manager.note_success()

    async def async_disconnect(self) -> None:
        await self._manager.close()
        self._connected = False

    @property
    def consecutive_failures(self) -> int:
        """Connection-failure streak from the shared manager (0 = healthy)."""
        return self._manager.consecutive_failures

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

    async def get_device_management_info(self) -> dict[str, Any] | None:
        """Read serial, firmware, model and WiFi RSSI from DeviceManagement registers.

        Works on some AECC devices (e.g. Sunpura); times out on others (e.g. Lunergy).
        Uses a short 3-second timeout to avoid blocking setup.

        The register list is a fixed safe set (identity + RSSI). It deliberately
        never includes the device's credential/secret registers exposed on this
        same accessor.
        """
        payload: dict[str, Any] = {
            "Get": "DeviceManagement",
            "SerialNumber": self._next_serial(),
            "CommandSource": "HA",
            "RegDeviceManagementAddr": [2, 8, 9, 20, 21, 76],
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

    async def read_holding_registers(self, start: int, count: int) -> list[int] | ModbusException | None:
        """Read ``count`` holding registers from ``start`` over Modbus TCP.

        Same socket and lock as the JSON commands; the device dispatches per
        frame. Returns the unsigned 16-bit words, a ModbusException when the
        device refused the address, or None when nothing usable came back. In
        that last case the socket is closed so the JSON path never inherits a
        half-read binary reply. Read-only by design: this is the only Modbus
        function code in the integration.
        """
        self._modbus_tid = (self._modbus_tid + 1) & 0xFFFF
        tid = self._modbus_tid
        request = struct.pack(
            ">HHHBBHH", tid, _MODBUS_PROTOCOL_ID, 6, MODBUS_UNIT_ID, _MODBUS_FC_READ_HOLDING, start, count
        )
        async with self._io_lock:
            try:
                reader, writer = await self._manager.get_reader_writer()
                self._connected = True
                writer.write(request)
                await writer.drain()
                async with asyncio.timeout(MODBUS_READ_TIMEOUT):
                    header = await reader.readexactly(_MODBUS_MBAP_LEN)
                    rx_tid, rx_proto, length, rx_unit = struct.unpack(">HHHB", header)
                    if rx_tid != tid or rx_proto != _MODBUS_PROTOCOL_ID or rx_unit != MODBUS_UNIT_ID or length < 2:
                        raise ValueError(f"unexpected MBAP header {header.hex()}")
                    pdu = await reader.readexactly(length - 1)
            except (TimeoutError, ValueError, ConnectionResetError, OSError, asyncio.IncompleteReadError) as exc:
                _LOGGER.debug("Modbus read %d x%d failed: %s - closing socket", start, count, exc)
                await self._manager.close()
                return None

        function = pdu[0]
        if function == _MODBUS_FC_READ_HOLDING | 0x80 and len(pdu) >= 2:
            return ModbusException(pdu[1])
        if function != _MODBUS_FC_READ_HOLDING or len(pdu) < 2 or len(pdu) != 2 + pdu[1] or pdu[1] != 2 * count:
            _LOGGER.debug("Modbus read %d x%d malformed reply %s - closing socket", start, count, pdu.hex())
            await self._manager.close()
            return None
        return list(struct.unpack(f">{count}H", pdu[2:]))

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
                self._manager.reset_read_timeout_streak()
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
                self._manager.reset_read_timeout_streak()
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
        streak = self._manager.note_read_timeout()
        if streak >= READ_TIMEOUT_SUSPECT_THRESHOLD:
            _LOGGER.warning(
                "%s %s: %d consecutive read timeouts - recycling socket",
                op,
                command,
                streak,
            )
            await self._manager.close()
            self._manager.reset_read_timeout_streak()

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
