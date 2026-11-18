"""Diagnose zum Herunterladen (Integration → ⋮ → Diagnose herunterladen).

Enthält die gespeicherten Reisen samt Zuordnung in OpenBoards (Laufwege,
Fahrt-Kennungen) für die Fehlersuche. Zugangsdaten werden entfernt.
"""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, CONF_CLIENT_ID, VERSION

TO_REDACT = {CONF_CLIENT_ID, CONF_API_KEY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    rt = entry.runtime_data
    return {
        "version": VERSION,
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "credentials": rt.client is not None,
        "openboards_calls_since_start": rt.client.calls if rt.client else 0,
        "railnet": rt.railnet.status(),
        "timetable": rt.timetable.status(),
        "stations": rt.stations.status(),
        "phones": {uid: {k: v for k, v in o.items() if k in ("notify", "tracker", "deviceName")} for uid, o in rt.owners.data.items()},
        # Verbundene Android-Apps ohne Webhook-Adressen
        "app_links": [rt.links.public(l) | {"user": rt.name_of(l["userId"])} for l in rt.links.links.values()],
        "app_local_only": rt.links.local_only,
        "trips": rt.trips.trips,
    }
