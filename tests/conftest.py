"""Shared test fixtures for AECC Battery tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aecc_battery.const import (
    CONF_HOST,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom_components in all tests."""
    yield


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
