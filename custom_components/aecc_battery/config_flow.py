"""Config flow for AECC Battery (Local TCP) integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_HOST,
    CONF_MANUFACTURER,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    DEFAULT_HOST,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
    KNOWN_BRANDS,
    MAX_BRAND_POWER_W,
    max_power_for_brand,
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
        errors: dict[str, str] = {}
        brand = (user_input or self._entry.data).get(CONF_MANUFACTURER)
        brand_max = max_power_for_brand(brand)

        if user_input is not None:
            # The schema accepts the highest ceiling any brand has, so the brand
            # and its own limit can be changed in one save; the brand's ceiling
            # is enforced here.
            for field in (CONF_MAX_CHARGE_POWER, CONF_MAX_DISCHARGE_POWER):
                if user_input[field] > brand_max:
                    errors[field] = "power_above_brand_max"

        if user_input is not None and not errors:
            # Merge instead of replace: async_create_entry swaps entry.options
            # wholesale, so the legacy extended_power boolean must be carried
            # over explicitly - a rollback to <=1.5.1 then keeps its old
            # behaviour.
            new_options = {
                **self._entry.options,
                CONF_MAX_CHARGE_POWER: user_input[CONF_MAX_CHARGE_POWER],
                CONF_MAX_DISCHARGE_POWER: user_input[CONF_MAX_DISCHARGE_POWER],
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

        # Late import to avoid a module cycle (__init__ imports config_flow's
        # sibling modules at setup).
        from . import resolve_power_limits

        # A rejected submit is shown back with what was typed, not with the
        # stored values.
        current = {**self._entry.data, **(user_input or {})}
        charge_default, discharge_default = resolve_power_limits(self._entry.options)
        charge_default = current.get(CONF_MAX_CHARGE_POWER, charge_default)
        discharge_default = current.get(CONF_MAX_DISCHARGE_POWER, discharge_default)
        power_field = vol.All(vol.Coerce(int), vol.Range(min=100, max=MAX_BRAND_POWER_W))
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=current.get(CONF_HOST, DEFAULT_HOST)): str,
                vol.Required(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): vol.Coerce(int),
                vol.Required(CONF_NAME, default=current.get(CONF_NAME, DEFAULT_NAME)): str,
                vol.Required(CONF_MANUFACTURER, default=current.get(CONF_MANUFACTURER, KNOWN_BRANDS[0])): vol.In(
                    KNOWN_BRANDS
                ),
                vol.Optional(CONF_MODEL, default=current.get(CONF_MODEL, "")): str,
                vol.Required(CONF_MAX_CHARGE_POWER, default=charge_default): power_field,
                vol.Required(CONF_MAX_DISCHARGE_POWER, default=discharge_default): power_field,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"brand_max": str(brand_max)},
        )
