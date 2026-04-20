"""Shared test fixtures for AECC Battery tests."""

from __future__ import annotations

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


@pytest.fixture
def mock_tcp_client():
    """Return a mocked AeccTcpClient."""
    with patch("custom_components.aecc_battery.tcp_client.AeccTcpClient", autospec=True) as mock_cls:
        client = mock_cls.return_value
        client.host = "192.168.1.100"
        client.port = 8080
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
        client.get_control_parameters = AsyncMock(return_value=None)
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
