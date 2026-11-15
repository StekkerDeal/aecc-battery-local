"""Shared test fixtures for AECC Battery tests."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aecc_battery.const import (
    CONF_HOST,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
)


class SocketTracker:
    """Counting fake for ``asyncio.open_connection``.

    Tracks how many sockets are alive at once, which the other fakes here do
    not. The reader raises TimeoutError on first read: a device that completes
    the handshake and then never answers.
    """

    def __init__(self) -> None:
        # Ordered log, so a test can assert each socket closed before the next.
        self.events: list[str] = []
        self.live = 0
        self.max_live = 0

    @property
    def opened(self) -> int:
        return self.events.count("open")

    @property
    def closed(self) -> int:
        return self.events.count("close")

    async def open_connection(self, host: str, port: int):
        self.events.append("open")
        self.live += 1
        self.max_live = max(self.max_live, self.live)

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(side_effect=TimeoutError)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.is_closing.return_value = False
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()

        def _close() -> None:
            if writer.is_closing():
                return
            writer.is_closing.return_value = True
            self.events.append("close")
            self.live -= 1

        writer.close.side_effect = _close
        return reader, writer


@pytest.fixture
def socket_tracker(monkeypatch) -> SocketTracker:
    """Replace asyncio.open_connection with a tracker that counts sockets."""
    tracker = SocketTracker()
    monkeypatch.setattr(asyncio, "open_connection", tracker.open_connection)
    return tracker


# One word list per Modbus block, in MODBUS_BLOCKS order, as a real unit answered.
TSUN_TELEMETRY = [0] * 23
TSUN_TELEMETRY[65033 - 65030] = 2405
TSUN_TELEMETRY[65035 - 65030] = 2500
TSUN_TELEMETRY[65037 - 65030] = 2500
TSUN_BLOCKS: dict[tuple[int, int], list[int]] = {
    (65030, 23): TSUN_TELEMETRY,
    (30073, 3): [454, 454, 485],
    (52050, 4): [509, 0, 463, 0],
    (52080, 2): [0, 0],
}

ENERGY_SUMMARY = {
    "AverageBatteryAverageSOC": "75",
    "TotalACChargePower": "0",
    "TotalBatteryOutputPower": "100",
    "TotalPVPower": "500",
    "TotalPVChargePower": "400",
    "TotalGridOutputPower": "100",
    "MeterTotalActivePower": "200",
    "TotalBackUpPower": "0",
    "ControlEnableStatus": "1",
}

CONTROL_INFO = {
    "3000": "1",
    "3003": "1,00:00,23:59,0,0,0,0,0,0,100,10",
    "3020": "3",
    "3021": "1",
    "3022": "1",
    "3023": "10",
    "3024": "98",
    "3030": "0",
    "3039": "2400",
}


class FakeConnection:
    """One socket on the fake device and what it saw."""

    def __init__(self, deaf: bool) -> None:
        self.deaf = deaf
        self.protocol: str | None = None
        self.closed = False
        self.buffer = b""
        # (protocol, answered) per frame received.
        self.frames: list[tuple[str, bool]] = []


class FakeDevice(SocketTracker):
    """Protocol-aware fake datalogger behind ``asyncio.open_connection``.

    ``first_frame``: a connection answers only the protocol of its first frame.
    ``both``: JSON and Modbus are answered on one socket. With ``single_client``
    a connection opened while another is live never gets an answer.
    """

    def __init__(self, mode: str = "first_frame", single_client: bool = True) -> None:
        super().__init__()
        assert mode in ("first_frame", "both")
        self.mode = mode
        self.single_client = single_client
        self.blocks: dict[tuple[int, int], list[int]] = dict(TSUN_BLOCKS)
        self.silent_blocks: set[tuple[int, int]] = set()
        self.connect_error: BaseException | None = None
        self.connections: list[FakeConnection] = []
        self.modbus_requests: list[tuple[int, int]] = []

    async def open_connection(self, host: str, port: int):
        if self.connect_error is not None:
            raise self.connect_error
        conn = FakeConnection(deaf=self.single_client and self.live > 0)
        self.connections.append(conn)
        self.events.append("open")
        self.live += 1
        self.max_live = max(self.max_live, self.live)

        reader = asyncio.StreamReader()
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.is_closing.return_value = False
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()

        def _close() -> None:
            if conn.closed:
                return
            conn.closed = True
            writer.is_closing.return_value = True
            self.events.append("close")
            self.live -= 1
            reader.feed_eof()

        def _write(data: bytes) -> None:
            assert not conn.closed, "write on a closed socket"
            conn.buffer += data
            self._drain_frames(conn, reader)

        writer.close.side_effect = _close
        writer.write.side_effect = _write
        return reader, writer

    def _drain_frames(self, conn: FakeConnection, reader: asyncio.StreamReader) -> None:
        while conn.buffer:
            if conn.buffer[:1] == b"{":
                end = conn.buffer.find(b"\n")
                if end < 0:
                    return
                frame, conn.buffer = conn.buffer[:end], conn.buffer[end + 1 :]
                protocol = "json"
            else:
                if len(conn.buffer) < 6:
                    return
                total = 6 + struct.unpack(">H", conn.buffer[4:6])[0]
                if len(conn.buffer) < total:
                    return
                frame, conn.buffer = conn.buffer[:total], conn.buffer[total:]
                protocol = "modbus"
            if conn.protocol is None:
                conn.protocol = protocol
            answers = not conn.deaf and (self.mode == "both" or protocol == conn.protocol)
            reply = None
            if answers:
                reply = self._json_reply(frame) if protocol == "json" else self._modbus_reply(frame)
            else:
                self._record_modbus_request(frame, protocol)
            conn.frames.append((protocol, reply is not None))
            if reply is not None:
                reader.feed_data(reply)

    def _record_modbus_request(self, frame: bytes, protocol: str) -> None:
        if protocol == "modbus" and len(frame) == 12:
            self.modbus_requests.append(struct.unpack(">HH", frame[8:12]))

    def _json_reply(self, frame: bytes) -> bytes:
        request = json.loads(frame)
        serial = request.get("SerialNumber")
        command = request.get("Get")
        reply: dict = {"SerialNumber": serial}
        if command == "EnergyParameter":
            reply["SSumInfoList"] = dict(ENERGY_SUMMARY)
        elif command == "Energycontrolparameters":
            reply["ControlInfo"] = dict(CONTROL_INFO)
        return (json.dumps(reply) + "\n").encode("utf-8")

    def _modbus_reply(self, frame: bytes) -> bytes | None:
        tid, _proto, _length, unit, function, start, count = struct.unpack(">HHHBBHH", frame)
        self.modbus_requests.append((start, count))
        if (start, count) in self.silent_blocks:
            return None
        words = self.blocks.get((start, count)) if function == 0x03 else None
        if words is None:
            pdu = bytes([function | 0x80, 0x02])
        else:
            pdu = bytes([0x03, 2 * count]) + struct.pack(f">{count}H", *words)
        return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def _install_fake_device(monkeypatch, device: FakeDevice) -> FakeDevice:
    from custom_components.aecc_battery.tcp_client import AeccTcpClient

    monkeypatch.setattr(asyncio, "open_connection", device.open_connection)
    # Short budgets so silence costs milliseconds. The JSON timeout is a default
    # argument, bound at definition time, hence the __defaults__ patch.
    monkeypatch.setattr("custom_components.aecc_battery.tcp_client.MODBUS_READ_TIMEOUT", 0.05)
    monkeypatch.setattr("custom_components.aecc_battery.tcp_client._GET_TIMEOUT", 0.2)
    monkeypatch.setattr(AeccTcpClient._read_json, "__defaults__", (0.2, logging.WARNING))
    return device


@pytest.fixture
def lunergy_device(monkeypatch) -> FakeDevice:
    """Answers only a connection's first protocol and serves one client."""
    return _install_fake_device(monkeypatch, FakeDevice(mode="first_frame", single_client=True))


@pytest.fixture
def tsun_device(monkeypatch) -> FakeDevice:
    """Answers JSON and Modbus on one socket and serves one client."""
    return _install_fake_device(monkeypatch, FakeDevice(mode="both", single_client=True))


@pytest.fixture
def bypass_integration_setup():
    """Stop a created entry from setting the integration up for real.

    The config flow does not talk to the battery, but Home Assistant sets the
    new entry up as soon as the flow finishes, and that opens a TCP socket.
    Tests that only exercise the flow must not reach the network: pytest-socket
    blocks it and fails the test in teardown.
    """
    with patch("custom_components.aecc_battery.async_setup_entry", return_value=True) as mock_setup:
        yield mock_setup


@pytest.fixture
def mock_tcp_client():
    """Return a mocked AeccTcpClient."""
    with patch("custom_components.aecc_battery.tcp_client.AeccTcpClient", autospec=True) as mock_cls:
        client = mock_cls.return_value
        client.host = "192.168.1.100"
        client.port = 8080
        client.consecutive_failures = 0
        client.async_connect = AsyncMock()
        client.async_disconnect = AsyncMock()
        client.get_energy_parameters = AsyncMock(
            return_value={
                "SSumInfoList": {
                    "AverageBatteryAverageSOC": "75",
                    "TotalACChargePower": "0",
                    "TotalBatteryOutputPower": "100",
                    "TotalPVPower": "500",
                    "TotalPVChargePower": "400",
                    "TotalGridOutputPower": "100",
                    "MeterTotalActivePower": "200",
                    "TotalBackUpPower": "0",
                    "ControlEnableStatus": "1",
                },
            }
        )
        client.get_control_parameters = AsyncMock(
            return_value={
                "ControlInfo": {
                    "3000": "1",
                    "3003": "1,00:00,23:59,0,0,0,0,0,0,100,10",
                    "3020": "3",
                    "3021": "1",
                    "3022": "1",
                    "3023": "10",
                    "3024": "98",
                    "3030": "0",
                    "3039": "2400",
                },
            }
        )
        client.set_control_parameters = AsyncMock(return_value={"result": "ok"})
        client.get_device_management_info = AsyncMock(return_value=None)
        client.read_holding_registers = AsyncMock(return_value=None)
        client.read_register_blocks_separately = AsyncMock(return_value=None)
        yield client


@pytest.fixture
def mock_config_entry():
    """Return a mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.data = {
        CONF_HOST: "192.168.1.100",
        CONF_PORT: 8080,
        CONF_NAME: "Test Battery",
        CONF_MANUFACTURER: "Sunpura",
        CONF_MODEL: "S2400",
    }
    entry.options = {}
    entry.unique_id = "192.168.1.100:8080"
    return entry


MOCK_USER_INPUT = {
    CONF_HOST: "192.168.1.100",
    CONF_PORT: 8080,
    CONF_NAME: "My Battery",
    CONF_MANUFACTURER: "Lunergy",
    CONF_MODEL: "Hub 2400 AC",
}
