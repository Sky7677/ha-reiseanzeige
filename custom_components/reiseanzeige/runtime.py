"""Laufzeit der Integration: verbindet Teilen-Ereignis, Reiseverwaltung,
Timetables-API, Benachrichtigungen und die Karte.

  mobile_app.share  → Text auslesen, Entwurf speichern, Bestätigung ans Handy,
                      im Hintergrund Züge im Fahrplan suchen
  Reise gespeichert/abgeglichen → Lage der Halte und Streckenverlauf aus dem
                      Gleisnetz (im Executor, siehe core/geometry.py)
  alle 15 s         → Abgelaufene entfernen, fällige Echtzeit holen; Gleisnetz
                      wöchentlich auf neue Fassung prüfen
  Echtzeit fehlt    → einmalige Nachricht, Ereignis "reiseanzeige"
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import time
from datetime import timedelta

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_KEY, CONF_CLIENT_ID, CONF_GRACE_MINUTES, CONF_RAILNET, CONF_REFRESH_SECONDS, CONF_STATION,
    CONF_TIMETABLE, DEFAULT_GRACE_MINUTES, DEFAULT_RAILNET, DEFAULT_REFRESH_SECONDS, DEFAULT_TIMETABLE, DOMAIN,
    NOTIFY_TITLE, SHARE_EVENT,
    SIGNAL_NEW_OWNER, SIGNAL_TRIPS_CHANGED, STORAGE_KEY_TRIPS, STORAGE_VERSION, TICK_SECONDS,
)
from .core.geometry import apply_geometry
from .core.journey import build_snapshot, draft_snapshot
from .core.realtime import leg_realtime, stations_to_poll
from .core.share_text import ShareTextError, looks_like_share, parse_share_text
from .core.timetables import TimetablesClient, TimetablesError, aiohttp_fetch
from .core.trips import (
    ENRICH_WINDOW, TripError, TripManager, realtime_unavailable, share_confirmation, share_error,
)
from .owners import Owners
from .railnet_store import DataStore, RailNetStore, TimetableStore
from .stations import StationDirectory

_LOGGER = logging.getLogger(__name__)

LOCATE_COOLDOWN = 30  # Sekunden je Person (Companion-App rät von häufigen Anforderungen ab)


class HaTripStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY_TRIPS)

    async def load(self) -> list[dict]:
        data = await self._store.async_load() or {}
        return data.get("trips") or []

    async def save(self, trips: list[dict]) -> None:
        await self._store.async_save({"trips": trips})


class Coordinates:
    """Lage eines Bahnhofs über seine Nummer: Gleisnetz (OSM), sonst StaDa.
    Suche über den Namen übernimmt core/geometry.py mit Anker."""

    def __init__(self, railnet: RailNetStore, stations: StationDirectory) -> None:
        self.railnet, self.stations = railnet, stations

    def coords(self, eva: str | None, name: str) -> tuple[float, float] | None:
        places = self.railnet.places
        return (places.by_eva(eva) if places else None) or self.stations.coords(eva, name)


class Reiseanzeige:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        opts = {**entry.data, **entry.options}
        self.default_station: str | None = opts.get(CONF_STATION) or None
        session = async_get_clientsession(hass)
        self.client = TimetablesClient(aiohttp_fetch(session), entry.data[CONF_CLIENT_ID], entry.data[CONF_API_KEY])
        self.stations = StationDirectory(hass, entry.data[CONF_CLIENT_ID], entry.data[CONF_API_KEY])
        self.railnet = RailNetStore(hass, enabled=opts.get(CONF_RAILNET, DEFAULT_RAILNET))
        self.timetable = TimetableStore(hass, enabled=opts.get(CONF_TIMETABLE, DEFAULT_TIMETABLE))
        self.coords = Coordinates(self.railnet, self.stations)
        self.trips = TripManager(
            HaTripStore(hass),
            clock=dt_util.now,
            grace=timedelta(minutes=opts.get(CONF_GRACE_MINUTES, DEFAULT_GRACE_MINUTES)),
            running=timedelta(seconds=opts.get(CONF_REFRESH_SECONDS, DEFAULT_REFRESH_SECONDS)),
        )
        self.owners = Owners(hass)
        # Zufälliger Schlüssel für die Kartenkacheln (wie das access_token bei Kamerabildern):
        # die Karte bekommt ihn über die angemeldete WebSocket-Verbindung
        self.map_token = secrets.token_urlsafe(24)
        self._names: dict[str, str] = {}
        self._locate_last: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._unsubs: list = []
        self._ticking = False
        self._busy: set[str] = set()   # gerade ladende Datenquellen
        self._geo_lock = asyncio.Lock()
        self.sensor_owners: set[str] = set()

    # --- Start/Stopp ---

    async def async_start(self) -> None:
        await self.owners.load()
        await self.trips.init()
        await self.stations.load()
        for store in (self.railnet, self.timetable):
            await store.load()
            store.listeners.append(self.update_geometry)
        await self._load_names()
        self.trips.listeners.append(self._trips_changed)
        self._unsubs.append(self.hass.bus.async_listen(SHARE_EVENT, self._on_share))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, timedelta(seconds=TICK_SECONDS)))
        self._spawn(self.stations.refresh_if_due())
        self._spawn(self.update_geometry())  # gespeicherte Reisen, z. B. nach einem Update
        self._spawn(self.tick())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in list(self._tasks):
            task.cancel()

    def _spawn(self, coro) -> asyncio.Task:
        task = self.entry.async_create_background_task(self.hass, coro, f"{DOMAIN} Hintergrund")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @callback
    def _trips_changed(self) -> None:
        owners = {t["ownerId"] for t in self.trips.trips}
        if owners - self.sensor_owners:
            async_dispatcher_send(self.hass, SIGNAL_NEW_OWNER)
        async_dispatcher_send(self.hass, SIGNAL_TRIPS_CHANGED)

    # --- Namen der Benutzer ---

    async def _load_names(self) -> None:
        self._names = {u.id: u.name or u.id for u in await self.hass.auth.async_get_users() if not u.system_generated}

    def name_of(self, user_id: str | None) -> str | None:
        return self._names.get(user_id) if user_id else None

    # --- Sicht für die Karte ---

    def _with_coords(self, snapshot: dict) -> None:
        for leg in snapshot.get("legs", []):
            for s in [leg.get("origin"), leg.get("destination")] + [so.get("station") for so in leg.get("stopovers", [])]:
                if s and s.get("lat") is None:
                    c = self.coords.coords(s.get("id"), s.get("name") or "")
                    if c:
                        s["lat"], s["lon"] = c

    def trip_view(self, trip: dict, *, light: bool = False) -> dict:
        t = copy.deepcopy(trip)
        self._with_coords(t["snapshot"])
        owner = self.owners.get(t["ownerId"]) or {}
        t["ownerName"] = self.name_of(t["ownerId"])
        t["ownerLocation"] = owner.get("tracker")
        t["ownerCanLocate"] = bool(owner.get("tracker") and owner.get("notify"))
        if light:
            for leg in t["snapshot"]["legs"]:
                if leg.get("polyline"):
                    leg["hasPolyline"], leg["polyline"] = True, None
        return t

    # --- Teilen ---

    async def _on_share(self, event: Event) -> None:
        data = event.data or {}
        text = data.get("text") or data.get("url") or ""
        user_id = event.context.user_id
        if not user_id or not looks_like_share(text):
            return  # Teilen aus anderen Apps (YouTube …) stillschweigend ignorieren
        if user_id not in self._names:
            await self._load_names()  # neuer Benutzer seit dem Start
        await self.owners.learn(user_id, data)
        result = await self.add_text(user_id, text)
        await self.notify(user_id, result["message"])

    async def add_text(self, user_id: str, text: str) -> dict:
        """Geteilten Text speichern. Ergebnis: {ok, message, trip?}."""
        try:
            journey = parse_share_text(text)
        except ShareTextError as err:
            _LOGGER.info("Geteilter Text nicht lesbar (%s): %s", err.code, err)
            return {"ok": False, "code": err.code, "message": share_error(err.code)}
        now = dt_util.now()
        try:
            result = await self.trips.add_from_share(user_id, journey, draft_snapshot(journey, now, self.coords), text)
        except TripError as err:
            return {"ok": False, "code": err.code, "message": share_error(err.code)}
        message = share_confirmation(result)
        _LOGGER.info("%s: %s", self.name_of(user_id) or user_id, message)
        trip_id = result["trip"]["id"]
        # Erst Lage und Verlauf aus dem Text, dann (auch bei erneutem Teilen) Fahrplan abgleichen
        self._spawn(self.update_geometry([trip_id]))
        self._spawn(self._enrich(trip_id, journey))
        return {"ok": True, "status": result["status"], "message": message, "trip": self.trip_view(self.trips.get(trip_id))}

    async def _enrich(self, trip_id: str, journey) -> None:
        await self.trips.mark_enrich_attempt(trip_id)
        try:
            snapshot = await build_snapshot(journey, self.client, dt_util.now(), self.coords, horizon=ENRICH_WINDOW)
        except TimetablesError as err:
            _LOGGER.warning("Reise %s: Züge nicht im Fahrplan gesucht: %s", trip_id, err)
            return
        matched = sum(1 for l in snapshot["legs"] if l.get("matched"))
        rail = sum(1 for l in journey.legs if l.rail)
        _LOGGER.info("Reise %s: %s von %s Bahn-Abschnitten im Fahrplan gefunden (%s Abfragen bisher)",
                     trip_id, matched, rail, self.client.calls)
        if await self.trips.set_snapshot(trip_id, snapshot):
            await self.update_geometry([trip_id])
            await self.tick()

    # --- Lage der Halte und Streckenverlauf ---

    async def update_geometry(self, trip_ids: list[str] | None = None) -> None:
        """Koordinaten und Verlauf ergänzen – im Executor, nacheinander."""
        async with self._geo_lock:
            for trip in list(self.trips.trips):
                if trip_ids is not None and trip["id"] not in trip_ids:
                    continue
                snapshot = copy.deepcopy(trip["snapshot"])
                created = snapshot.get("createdAt")
                try:
                    changed = await self.hass.async_add_executor_job(
                        apply_geometry, snapshot, self.railnet.net, self.railnet.places,
                        self.stations.coords, self.railnet.version or "", self.timetable.timetable)
                except Exception:  # noqa: BLE001 – ohne Verlauf geht es auch
                    _LOGGER.exception("Streckenverlauf für Reise %s nicht berechnet", trip["id"])
                    continue
                if changed and await self.trips.set_geometry(trip["id"], created, snapshot):
                    legs = snapshot.get("legs", [])
                    _LOGGER.debug("Reise %s: Verlauf für %s von %s Abschnitten", trip["id"],
                                  sum(1 for l in legs if l.get("polyline")), len(legs))

    async def _refresh_store(self, store: DataStore) -> None:
        try:
            await store.refresh_if_due()
        finally:
            self._busy.discard(store.label)

    # --- Echtzeit ---

    async def _on_tick(self, _now) -> None:
        await self.tick()

    async def tick(self) -> None:
        if self._ticking:
            return
        self._ticking = True
        try:
            for removed in await self.trips.cleanup():
                _LOGGER.info("Reise %s → %s entfernt", removed["originName"], removed["destinationName"])
            for trip in self.trips.due_for_enrichment():
                try:
                    await self._enrich(trip["id"], parse_share_text(trip["shareText"]))
                except ShareTextError:
                    pass
            for trip in self.trips.due_for_refresh():
                await self._refresh(trip)
            await self.stations.refresh_if_due()  # fragt höchstens alle 7 Tage wirklich ab
            for store in (self.railnet, self.timetable):
                if store.label not in self._busy and store.due(dt_util.now()):
                    # Download kann dauern: eigene Aufgabe, damit die Echtzeit weiterläuft
                    self._busy.add(store.label)
                    self._spawn(self._refresh_store(store))
        except Exception:  # noqa: BLE001 – ein Durchlauf darf die Integration nicht stoppen
            _LOGGER.exception("Durchlauf fehlgeschlagen")
        finally:
            self._ticking = False

    async def _refresh(self, trip: dict) -> None:
        now = dt_util.now()
        polls = stations_to_poll(trip["snapshot"], trip.get("realtime"), now)
        results = []
        for index, evas in polls.items():
            leg = trip["snapshot"]["legs"][index]
            try:
                rt = await leg_realtime(self.client, leg, evas, now)
                results.append({"legIndex": index, "status": "ok", "realtime": rt})
            except TimetablesError as err:
                _LOGGER.warning("Echtzeit Abschnitt %s (%s): %s", index, leg["line"]["name"], err)
                results.append({"legIndex": index, "status": "error", "error": err.code})
        for ev in await self.trips.apply_realtime(trip["id"], results):
            if ev["type"] == "realtime_unavailable":
                self.hass.bus.async_fire(DOMAIN, {"type": ev["type"], "trip_id": ev["tripId"], "user_id": ev["ownerId"]})
                await self.notify(ev["ownerId"], realtime_unavailable(ev))

    # --- Benachrichtigungen ---

    async def notify(self, user_id: str, message: str) -> None:
        service = self.owners.notify_service(user_id)
        try:
            if service:
                await self.hass.services.async_call("notify", service, {"title": NOTIFY_TITLE, "message": message})
            else:
                # Noch kein Handy bekannt (z. B. Text in der Karte eingefügt): Hinweis in Home Assistant
                persistent_notification.async_create(
                    self.hass, f"{self.name_of(user_id) or 'Unbekannt'}: {message}", title=NOTIFY_TITLE)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Benachrichtigung an %s fehlgeschlagen: %s", self.name_of(user_id) or user_id, err)

    async def request_location(self, trip_id: str) -> dict:
        """Handy der Reisenden um eine sofortige Standortmeldung bitten (unsichtbarer
        Befehl der Companion-App). {status: sent|cooldown|unavailable, retryInSec?}"""
        trip = self.trips.get(trip_id)
        owner = self.owners.get(trip["ownerId"]) or {}
        if trip["status"] != "running" or not (owner.get("notify") and owner.get("tracker")):
            return {"status": "unavailable"}
        now = time.monotonic()
        last = self._locate_last.get(trip["ownerId"])
        if last is not None and now - last < LOCATE_COOLDOWN:
            return {"status": "cooldown", "retryInSec": int(LOCATE_COOLDOWN - (now - last)) + 1}
        self._locate_last[trip["ownerId"]] = now
        try:
            await self.hass.services.async_call("notify", owner["notify"], {"message": "request_location_update"})
        except Exception:
            self._locate_last.pop(trip["ownerId"], None)
            raise
        return {"status": "sent"}
