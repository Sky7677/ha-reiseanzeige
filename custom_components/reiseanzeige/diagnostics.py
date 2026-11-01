"""Diagnose zum Herunterladen (Integration → ⋮ → Diagnose herunterladen).

Enthält die gespeicherten Reisen samt Abgleich mit dem Fahrplan (Laufwege,
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
        "timetables_calls_since_start": rt.client.calls,
        "station_coordinates": sum(1 for k in rt.stations._coords if not k.startswith("name:")),
        "railnet": rt.railnet.status(),
        "timetable": rt.timetable.status(),
        "phones": {uid: {k: v for k, v in o.items() if k in ("notify", "tracker", "deviceName")} for uid, o in rt.owners.data.items()},
        "trips": rt.trips.trips,
    }
