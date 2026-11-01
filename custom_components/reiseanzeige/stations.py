"""Koordinaten der Bahnhöfe für die Karte (Übergangslösung bis zum eigenen Gleisnetz).

Quelle: StaDa – Station Data API der DB (DB API Marketplace, Plan "Free4All",
CC BY 4.0). Laut deren Bedingungen darf ein Schlüssel den Dienst höchstens
einmal pro Tag aufrufen; die Integration holt die Gesamtliste daher nur alle
REFRESH_DAYS Tage, und nach einem Fehlschlag frühestens nach einem Tag erneut.
Ist die API nicht abonniert, gibt es eben keine Koordinaten – die Karte zeigt
dann keine Linie, alles andere funktioniert.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_VERSION
from .core.journey import norm_name

_LOGGER = logging.getLogger(__name__)

STADA_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/station-data/v2/stations?limit=10000"
REFRESH_DAYS = 7
MIN_RETRY = timedelta(days=1)


def parse_stada(data: dict) -> dict[str, list[float]]:
    """StaDa-Antwort → {EVA: [lat, lon]} plus {"name:<normalisiert>": [lat, lon]}."""
    out: dict[str, list[float]] = {}
    for st in data.get("result") or []:
        name_key = None
        for eva in st.get("evaNumbers") or []:
            coords = ((eva.get("geographicCoordinates") or {}).get("coordinates")) or []
            if len(coords) == 2 and eva.get("number") is not None:
                lat, lon = float(coords[1]), float(coords[0])
                out[str(eva["number"])] = [lat, lon]
                if eva.get("isMain") or name_key is None:
                    name_key = [lat, lon]
        if name_key and st.get("name"):
            out[f"name:{norm_name(st['name'])}"] = name_key
    return out


class StationDirectory:
    def __init__(self, hass: HomeAssistant, client_id: str, api_key: str) -> None:
        self.hass = hass
        self._headers = {"DB-Client-Id": client_id, "DB-Api-Key": api_key, "Accept": "application/json"}
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, "reiseanzeige.stations")
        self._coords: dict[str, list[float]] = {}
        self._fetched_at: datetime | None = None
        self._attempt_at: datetime | None = None

    async def load(self) -> None:
        data = await self._store.async_load() or {}
        self._coords = data.get("coords") or {}
        self._fetched_at = dt_util.parse_datetime(data["fetchedAt"]) if data.get("fetchedAt") else None
        self._attempt_at = dt_util.parse_datetime(data["attemptAt"]) if data.get("attemptAt") else None
        if not self._coords:
            # Noch keine Liste (z. B. StaDa gerade erst abonniert): beim Start einmal
            # versuchen, statt einen Tag zu warten
            self._attempt_at = None

    async def refresh_if_due(self) -> None:
        now = dt_util.now()
        if self._fetched_at and now - self._fetched_at < timedelta(days=REFRESH_DAYS):
            return
        if self._attempt_at and now - self._attempt_at < MIN_RETRY:
            return
        self._attempt_at = now
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(STADA_URL, headers=self._headers, timeout=aiohttp.ClientTimeout(total=60)) as res:
                if res.status in (401, 403):
                    _LOGGER.info("Stationsdaten-API nicht freigeschaltet (HTTP %s) – Karte ohne Linien", res.status)
                elif res.status >= 400:
                    _LOGGER.warning("Stationsdaten-API: HTTP %s", res.status)
                else:
                    coords = parse_stada(await res.json(content_type=None))
                    if coords:
                        self._coords, self._fetched_at = coords, now
                        _LOGGER.info("Koordinaten für %s Bahnhöfe geladen", sum(1 for k in coords if not k.startswith("name:")))
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Stationsdaten-API nicht erreichbar: %s", err)
        await self._store.async_save({
            "coords": self._coords,
            "fetchedAt": self._fetched_at.isoformat() if self._fetched_at else None,
            "attemptAt": self._attempt_at.isoformat(),
        })

    def coords(self, eva: str | None, name: str) -> tuple[float, float] | None:
        c = (self._coords.get(str(eva)) if eva else None) or self._coords.get(f"name:{norm_name(name)}")
        return (c[0], c[1]) if c else None
