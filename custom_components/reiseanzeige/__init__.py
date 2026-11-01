"""Reiseanzeige: Bahnreisen aus dem DB Navigator teilen und live verfolgen.

Datenquellen: der geteilte Text aus dem DB Navigator, die offizielle
Timetables-API der DB (Fahrplan und Echtzeit, CC BY 4.0) und für die Karte
OpenStreetMap. Kein Zugriff auf nicht-öffentliche Schnittstellen der Bahn.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CARD_URL, DOMAIN, VERSION
from .map_proxy import MapProxyView
from .runtime import Reiseanzeige
from . import websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARD_FILE = Path(__file__).parent / "frontend" / "reiseanzeige-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Einmalig beim Start: Karte ausliefern, WebSocket-Befehle und Kartenproxy anmelden."""
    websocket.async_register(hass)
    hass.http.register_view(MapProxyView(hass))
    if CARD_FILE.is_file():
        await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_FILE), True)])
        # Karte automatisch laden – keine Ressource von Hand anlegen
        try:
            add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
        except KeyError:  # Frontend nicht geladen (z. B. ohne Oberfläche)
            _LOGGER.warning("Frontend nicht geladen – Karte bitte als Ressource %s eintragen", CARD_URL)
    else:
        _LOGGER.warning("Kartendatei fehlt: %s", CARD_FILE)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    rt = Reiseanzeige(hass, entry)
    entry.runtime_data = rt
    await rt.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload_on_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
    return ok


async def _reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
