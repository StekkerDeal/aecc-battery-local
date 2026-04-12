"""Tests for the AECC Battery config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aecc_battery.const import (
    CONF_EXTENDED_POWER,
    CONF_HOST,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    DOMAIN,
)

from .conftest import MOCK_USER_INPUT


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """Test a successful user-initiated config flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], MOCK_USER_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My Battery"
    assert result["data"][CONF_HOST] == "192.168.1.100"
    assert result["data"][CONF_PORT] == 8080
    assert result["data"][CONF_NAME] == "My Battery"
    assert result["data"][CONF_MANUFACTURER] == "Lunergy"
    assert result["data"][CONF_MODEL] == "Hub 2400 AC"


async def test_user_flow_strips_whitespace(hass: HomeAssistant) -> None:
    """Test that host and name are stripped of leading/trailing whitespace."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "  192.168.1.100  ",
            CONF_PORT: 8080,
            CONF_NAME: "  My Battery  ",
            CONF_MANUFACTURER: "Sunpura",
            CONF_MODEL: "  S2400  ",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.1.100"
    assert result["data"][CONF_NAME] == "My Battery"
    assert result["data"][CONF_MODEL] == "S2400"


async def test_user_flow_duplicate_aborts(hass: HomeAssistant) -> None:
    """Test that configuring the same host:port twice is rejected."""
    # First entry
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], MOCK_USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Second entry with same host:port
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], MOCK_USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_default_manufacturer(hass: HomeAssistant) -> None:
    """Test that manufacturer defaults to AECC if not provided."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    user_input = {
        CONF_HOST: "192.168.1.200",
        CONF_PORT: 8080,
        CONF_NAME: "Other Battery",
        CONF_MANUFACTURER: "Other",
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MANUFACTURER] == "Other"
    assert result["data"][CONF_MODEL] == ""


async def test_options_flow(hass: HomeAssistant) -> None:
    """Test the options flow updates data and options."""
    # Create initial entry
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], MOCK_USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]

    # Open options flow
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    # Submit options
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "192.168.1.200",
            CONF_PORT: 8081,
            CONF_NAME: "Updated Battery",
            CONF_MANUFACTURER: "Sunpura",
            CONF_MODEL: "S2400",
            CONF_EXTENDED_POWER: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Verify data was updated
    assert entry.data[CONF_HOST] == "192.168.1.200"
    assert entry.data[CONF_PORT] == 8081
    assert entry.data[CONF_NAME] == "Updated Battery"
    assert entry.data[CONF_MANUFACTURER] == "Sunpura"
    assert entry.data[CONF_MODEL] == "S2400"
    assert entry.options[CONF_EXTENDED_POWER] is True
