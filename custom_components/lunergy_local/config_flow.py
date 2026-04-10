"""Config flow for Lunergy Local Battery integration."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_EXTENDED_POWER,
    DEFAULT_HOST,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
    }
)


class LunergyLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration step."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "LunergyLocalOptionsFlow":
        """Return the options flow so the user can change IP/port later."""
        return LunergyLocalOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            name = user_input[CONF_NAME].strip()

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=name,
                data={
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_NAME: name,
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_SCHEMA,
            description_placeholders={"default_host": DEFAULT_HOST},
        )


class LunergyLocalOptionsFlow(config_entries.OptionsFlow):
    """Allow the user to update host/port/name without removing the entry."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={
                    CONF_HOST: user_input[CONF_HOST].strip(),
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_NAME: user_input[CONF_NAME].strip(),
                },
                options={
                    CONF_EXTENDED_POWER: user_input.get(CONF_EXTENDED_POWER, False),
                },
            )
            await self.hass.config_entries.async_reload(self._entry.entry_id)
            return self.async_create_entry(title="", data={})

        current = self._entry.data
        current_options = self._entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=current.get(CONF_HOST, DEFAULT_HOST)): str,
                vol.Required(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): vol.Coerce(int),
                vol.Required(CONF_NAME, default=current.get(CONF_NAME, DEFAULT_NAME)): str,
                vol.Optional(CONF_EXTENDED_POWER, default=current_options.get(CONF_EXTENDED_POWER, False)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
