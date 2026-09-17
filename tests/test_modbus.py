"""Modbus telemetry: framing, decoding, the coordinator refresh and the entities."""

from __future__ import annotations

import asyncio
import struct
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aecc_battery import binary_sensor as binary_sensor_module
from custom_components.aecc_battery import sensor as sensor_module
from custom_components.aecc_battery.binary_sensor import AeccFaultSensor
from custom_components.aecc_battery.const import (
    DOMAIN,
    MB_ALARM_FLAGS,
    MB_AVAILABLE_CHARGE_POWER,
    MB_DEVICE_STATUS,
    MB_ENERGY_CHARGED,
    MB_ENERGY_DISCHARGED,
    MB_ENERGY_TO_GRID,
    MB_NOMINAL_BATTERY_POWER,
    MB_NOMINAL_POWER,
    MB_TEMP_1,
    MB_TEMP_3,
    MODBUS_BLOCKS,
    MODBUS_REFRESH_INTERVAL,
)
from custom_components.aecc_battery.coordinator import AeccBatteryCoordinator, _i16, _u32_low_first
from custom_components.aecc_battery.sensor import _MODBUS_SENSORS, AeccModbusSensor
from custom_components.aecc_battery.tcp_client import AeccTcpClient, ModbusException
from custom_components.aecc_battery.tcp_manager import TCPClientManager

# The real TSUN reply to "read 1 register at 65030": SOC 70.
SOC_REPLY = bytes.fromhex("0001000000050103020046")


@pytest.fixture(autouse=True)
def _clear_registry():
    TCPClientManager._connections.clear()
    yield
    TCPClientManager._connections.clear()


def _make_rw(chunks):
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.readexactly = AsyncMock(side_effect=chunks)
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


def _client(monkeypatch, chunks) -> tuple[AeccTcpClient, MagicMock]:
    reader, writer = _make_rw(chunks)
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(return_value=(reader, writer)))
    return AeccTcpClient("h", 1), writer


def _reply(tid: int, pdu: bytes) -> list[bytes]:
    """Header and PDU chunks the way readexactly hands them back."""
    return [struct.pack(">HHHB", tid, 0, len(pdu) + 1, 1), pdu]


# ── Transport ──────────────────────────────────────────────────────────────────


async def test_request_frame_is_read_holding_registers(monkeypatch) -> None:
    pdu = bytes([0x03, 46]) + bytes(46)
    client, writer = _client(monkeypatch, _reply(1, pdu) + _reply(2, pdu))

    assert await client.read_holding_registers(65030, 23) == [0] * 23
    assert writer.write.call_args[0][0] == bytes.fromhex("000100000006 01 03 fe06 0017".replace(" ", ""))

    await client.read_holding_registers(65030, 23)
    assert writer.write.call_args[0][0][:2] == b"\x00\x02"  # transaction id increments


async def test_real_soc_reply_decodes(monkeypatch) -> None:
    client, writer = _client(monkeypatch, [SOC_REPLY[:7], SOC_REPLY[7:]])

    assert await client.read_holding_registers(65030, 1) == [70]
    writer.close.assert_not_called()


async def test_exception_reply_is_returned_not_fatal(monkeypatch) -> None:
    client, writer = _client(monkeypatch, _reply(1, bytes([0x83, 0x02])))

    assert await client.read_holding_registers(3000, 1) == ModbusException(2)
    writer.close.assert_not_called()


@pytest.mark.parametrize(
    "chunks",
    [
        pytest.param(_reply(9, bytes([0x03, 0x02, 0x00, 0x46])), id="transaction id mismatch"),
        pytest.param(_reply(1, bytes([0x04, 0x02, 0x00, 0x46])), id="wrong function"),
        pytest.param(_reply(1, bytes([0x03, 0x04, 0x00, 0x46])), id="byte count does not match"),
        pytest.param([asyncio.IncompleteReadError(b"", 7)], id="connection dropped mid-frame"),
    ],
)
async def test_malformed_reply_closes_socket_and_returns_none(monkeypatch, chunks) -> None:
    client, writer = _client(monkeypatch, chunks)

    assert await client.read_holding_registers(65030, 1) is None

    writer.close.assert_called_once()
    assert client.consecutive_failures == 0
    assert client._manager.read_timeout_streak == 0


async def test_timeout_closes_socket_and_returns_none(monkeypatch) -> None:
    async def never(_n):
        await asyncio.sleep(1)

    monkeypatch.setattr("custom_components.aecc_battery.tcp_client.MODBUS_READ_TIMEOUT", 0.01)
    client, writer = _client(monkeypatch, never)

    assert await client.read_holding_registers(65030, 1) is None
    writer.close.assert_called_once()
    assert client.consecutive_failures == 0


def test_decode_helpers() -> None:
    assert _i16(0xFFFF) == -1
    assert _i16(454) == 454
    assert _u32_low_first(0x01FD, 0x0000) == 509  # 50.9 kWh on the TSUN
    assert _u32_low_first(0x0001, 0x0001) == 0x10001


# ── Coordinator ────────────────────────────────────────────────────────────────

# One word list per block, in MODBUS_BLOCKS order, as the TSUN answered on 2026-09-17.
_TELEMETRY = [0] * 23
_TELEMETRY[65033 - 65030] = 2405
_TELEMETRY[65035 - 65030] = 2500
_TELEMETRY[65037 - 65030] = 2500
_TSUN_BLOCKS = {
    (65030, 23): _TELEMETRY,
    (30073, 3): [454, 454, 485],
    (52050, 4): [509, 0, 463, 0],
    (52080, 2): [0, 0],
}


def _answering(blocks=_TSUN_BLOCKS):
    async def read(start, count):
        return blocks[(start, count)]

    return AsyncMock(side_effect=read)


@pytest.fixture
def coordinator(hass: HomeAssistant) -> AeccBatteryCoordinator:
    client = AsyncMock()
    client.host = "192.168.1.100"
    client.port = 8080
    client.consecutive_failures = 0
    coord = AeccBatteryCoordinator(hass, client, device_name="Test", manufacturer="TSUN")
    coord.data = {"SSumInfoList": {"AverageBatteryAverageSOC": "70"}}
    return coord


@pytest.fixture
def config_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


async def test_probe_marks_unsupported_on_exception(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.read_holding_registers = AsyncMock(return_value=ModbusException(2))

    await coordinator.async_probe_modbus()

    assert coordinator.modbus_supported is False
    assert coordinator.modbus == {}


async def test_probe_marks_unsupported_on_silence(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.read_holding_registers = AsyncMock(return_value=None)

    await coordinator.async_probe_modbus()

    assert coordinator.modbus_supported is False
    assert coordinator.client.read_holding_registers.await_count == 1  # no point sending more


async def test_probe_decodes_every_block(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.read_holding_registers = _answering()

    await coordinator.async_probe_modbus()

    assert coordinator.modbus_supported is True
    assert coordinator.modbus[MB_TEMP_1] == 45.4
    assert coordinator.modbus[MB_TEMP_3] == 48.5
    assert coordinator.modbus[MB_ENERGY_CHARGED] == 50.9
    assert coordinator.modbus[MB_ENERGY_DISCHARGED] == 46.3
    assert coordinator.modbus[MB_ENERGY_TO_GRID] == 0
    assert coordinator.modbus[MB_AVAILABLE_CHARGE_POWER] == 2405
    assert coordinator.modbus[MB_NOMINAL_POWER] == 2500
    assert coordinator.modbus[MB_NOMINAL_BATTERY_POWER] == 2500
    assert coordinator.modbus[MB_DEVICE_STATUS] == 0
    assert coordinator.modbus[MB_ALARM_FLAGS] == 0


async def test_failed_block_keeps_earlier_values(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.client.read_holding_registers = AsyncMock(side_effect=[_TELEMETRY, None])

    await coordinator.async_probe_modbus()

    assert coordinator.modbus_supported is True
    assert coordinator.modbus[MB_NOMINAL_POWER] == 2500
    assert MB_TEMP_1 not in coordinator.modbus
    assert coordinator._last_modbus_error == "no reply for 30073 x3"


async def test_refused_block_is_skipped(coordinator: AeccBatteryCoordinator) -> None:
    blocks = dict(_TSUN_BLOCKS)
    blocks[(30073, 3)] = ModbusException(2)
    coordinator.client.read_holding_registers = _answering(blocks)

    await coordinator.async_probe_modbus()

    assert coordinator.modbus_supported is True
    assert MB_TEMP_1 not in coordinator.modbus
    assert coordinator.modbus[MB_ENERGY_CHARGED] == 50.9


async def test_refresh_is_throttled(coordinator: AeccBatteryCoordinator, monkeypatch) -> None:
    coordinator.client.read_holding_registers = _answering()
    await coordinator.async_probe_modbus()
    coordinator.client.read_holding_registers.reset_mock()

    await coordinator._async_maybe_refresh_modbus()
    assert coordinator.client.read_holding_registers.await_count == 0

    later = time.monotonic() + MODBUS_REFRESH_INTERVAL + 1
    monkeypatch.setattr("custom_components.aecc_battery.coordinator.time.monotonic", lambda: later)
    await coordinator._async_maybe_refresh_modbus()
    assert coordinator.client.read_holding_registers.await_count == len(MODBUS_BLOCKS)


async def test_refresh_never_runs_when_unsupported(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.modbus_supported = False
    coordinator.client.read_holding_registers = _answering()

    await coordinator._async_maybe_refresh_modbus()

    coordinator.client.read_holding_registers.assert_not_awaited()


async def test_raising_client_never_fails_the_poll(coordinator: AeccBatteryCoordinator) -> None:
    coordinator.modbus_supported = True
    coordinator.client.get_energy_parameters = AsyncMock(return_value=coordinator.data)
    coordinator.client.read_holding_registers = AsyncMock(side_effect=RuntimeError("boom"))

    assert await coordinator._async_update_data() == coordinator.data
    assert coordinator._last_modbus_error == "boom"


# ── Entities ───────────────────────────────────────────────────────────────────


def _sensor(coordinator, config_entry, key: str) -> AeccModbusSensor:
    spec = next(s for s in _MODBUS_SENSORS if s[0] == key)
    return AeccModbusSensor(coordinator, config_entry, *spec)


async def test_sensor_values_and_availability(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    coordinator.modbus = {MB_TEMP_1: 45.4, MB_ENERGY_CHARGED: 50.9, MB_ALARM_FLAGS: 5}

    temperature = _sensor(coordinator, config_entry, "temperature_1")
    assert temperature.native_value == 45.4
    assert temperature.available is True
    assert temperature.unique_id == "test_entry_temperature_1"
    assert temperature.suggested_display_precision == 1

    assert _sensor(coordinator, config_entry, "lifetime_energy_charged").native_value == 50.9
    assert _sensor(coordinator, config_entry, "alarm_flags").native_value == 5

    missing = _sensor(coordinator, config_entry, "nominal_power")
    assert missing.native_value is None
    assert missing.available is False


async def test_fault_sensor(coordinator: AeccBatteryCoordinator, config_entry) -> None:
    fault = AeccFaultSensor(coordinator, config_entry)
    assert fault.available is False

    coordinator.modbus = {MB_DEVICE_STATUS: 0}
    assert fault.is_on is False
    assert fault.available is True

    coordinator.modbus = {MB_DEVICE_STATUS: 1}
    assert fault.is_on is True


async def _run_setup(hass: HomeAssistant, module, coordinator, config_entry) -> list:
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator
    added: list = []

    def _add_entities(entities, update_before_add=False):
        added.extend(entities)

    await module.async_setup_entry(hass, config_entry, _add_entities)
    return added


async def test_setup_creates_nothing_when_unsupported(
    hass: HomeAssistant, coordinator: AeccBatteryCoordinator, config_entry
) -> None:
    coordinator.modbus_supported = False

    sensors = await _run_setup(hass, sensor_module, coordinator, config_entry)
    assert not [e for e in sensors if isinstance(e, AeccModbusSensor)]
    assert await _run_setup(hass, binary_sensor_module, coordinator, config_entry) == []


async def test_setup_creates_every_modbus_entity_when_supported(
    hass: HomeAssistant, coordinator: AeccBatteryCoordinator, config_entry
) -> None:
    coordinator.modbus_supported = True

    sensors = await _run_setup(hass, sensor_module, coordinator, config_entry)
    assert len([e for e in sensors if isinstance(e, AeccModbusSensor)]) == len(_MODBUS_SENSORS) == 10
    binary = await _run_setup(hass, binary_sensor_module, coordinator, config_entry)
    assert [e.unique_id for e in binary] == ["test_entry_fault"]
