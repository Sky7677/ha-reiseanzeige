"""Einrichtung über die Oberfläche: Zugangsdaten der Timetables-API und Optionen."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import (
    CONF_API_KEY, CONF_CLIENT_ID, CONF_GRACE_MINUTES, CONF_RAILNET, CONF_REFRESH_SECONDS, CONF_STATION,
    DEFAULT_GRACE_MINUTES, DEFAULT_RAILNET, DEFAULT_REFRESH_SECONDS, DOMAIN,
)
from .core.timetables import RateLimiter, TimetablesClient, TimetablesError, aiohttp_fetch

TEST_QUERY = "Frankfurt(Main)Hbf"


async def validate(hass, client_id: str, api_key: str) -> str | None:
    """Eine einzige Testanfrage. Liefert einen Fehlerschlüssel oder None."""
    client = TimetablesClient(aiohttp_fetch(async_get_clientsession(hass)), client_id, api_key, limiter=RateLimiter(limit=5))
    try:
        await client.stations(TEST_QUERY)
    except TimetablesError as err:
        return "invalid_auth" if err.code == "BAD_CREDENTIALS" else "cannot_connect"
    return None


class ReiseanzeigeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            client_id = user_input[CONF_CLIENT_ID].strip()
            api_key = user_input[CONF_API_KEY].strip()
            error = await validate(self.hass, client_id, api_key)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Reiseanzeige",
                    data={CONF_CLIENT_ID: client_id, CONF_API_KEY: api_key},
                    options={CONF_STATION: (user_input.get(CONF_STATION) or "").strip()},
                )
        schema = vol.Schema({
            vol.Required(CONF_CLIENT_ID): str,
            vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional(CONF_STATION, default=""): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            client_id, api_key = user_input[CONF_CLIENT_ID].strip(), user_input[CONF_API_KEY].strip()
            error = await validate(self.hass, client_id, api_key)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data={CONF_CLIENT_ID: client_id, CONF_API_KEY: api_key})
        schema = vol.Schema({
            vol.Required(CONF_CLIENT_ID, default=entry.data.get(CONF_CLIENT_ID, "")): str,
            vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ReiseanzeigeOptionsFlow()


class ReiseanzeigeOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            user_input[CONF_STATION] = (user_input.get(CONF_STATION) or "").strip()
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        schema = vol.Schema({
            vol.Optional(CONF_STATION, default=o.get(CONF_STATION, "")): str,
            vol.Required(CONF_GRACE_MINUTES, default=o.get(CONF_GRACE_MINUTES, DEFAULT_GRACE_MINUTES)): vol.All(vol.Coerce(int), vol.Range(min=10, max=240)),
            vol.Required(CONF_REFRESH_SECONDS, default=o.get(CONF_REFRESH_SECONDS, DEFAULT_REFRESH_SECONDS)): vol.All(vol.Coerce(int), vol.Range(min=60, max=600)),
            vol.Required(CONF_RAILNET, default=o.get(CONF_RAILNET, DEFAULT_RAILNET)): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
