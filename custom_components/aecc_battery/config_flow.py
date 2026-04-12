"""Config flow for AECC Battery (Local TCP) integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_EXTENDED_POWER,
    CONF_HOST,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    DEFAULT_HOST,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
    KNOWN_BRANDS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_MANUFACTURER, default=KNOWN_BRANDS[0]): vol.In(KNOWN_BRANDS),
        vol.Optional(CONF_MODEL, default=""): str,
    }
)


class AeccBatteryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration step."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AeccBatteryOptionsFlow:
        return AeccBatteryOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            name = user_input[CONF_NAME].strip()
            manufacturer = user_input.get(CONF_MANUFACTURER, "AECC")
            model = user_input.get(CONF_MODEL, "").strip()

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=name,
                data={
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_NAME: name,
                    CONF_MANUFACTURER: manufacturer,
                    CONF_MODEL: model,
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            description_placeholders={"default_host": DEFAULT_HOST},
        )


class AeccBatteryOptionsFlow(config_entries.OptionsFlow):
    """Allow the user to update host/port/name without removing the entry."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            new_options = {
                CONF_EXTENDED_POWER: user_input.get(CONF_EXTENDED_POWER, False),
            }
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={
                    CONF_HOST: user_input[CONF_HOST].strip(),
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_NAME: user_input[CONF_NAME].strip(),
                    CONF_MANUFACTURER: user_input.get(
                        CONF_MANUFACTURER,
                        self._entry.data.get(CONF_MANUFACTURER, "AECC"),
                    ),
                    CONF_MODEL: user_input.get(CONF_MODEL, "").strip(),
                },
            )
            return self.async_create_entry(title="", data=new_options)

        current = self._entry.data
        current_options = self._entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=current.get(CONF_HOST, DEFAULT_HOST)): str,
                vol.Required(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): vol.Coerce(int),
                vol.Required(CONF_NAME, default=current.get(CONF_NAME, DEFAULT_NAME)): str,
                vol.Required(CONF_MANUFACTURER, default=current.get(CONF_MANUFACTURER, KNOWN_BRANDS[0])): vol.In(
                    KNOWN_BRANDS
                ),
                vol.Optional(CONF_MODEL, default=current.get(CONF_MODEL, "")): str,
                vol.Optional(CONF_EXTENDED_POWER, default=current_options.get(CONF_EXTENDED_POWER, False)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
