"""Laufzeit der Integration: verbindet Teilen-Ereignis, Reiseverwaltung,
OpenBoards, Benachrichtigungen und die Karte.

  mobile_app.share  → Text auslesen, Entwurf speichern, Bestätigung ans Handy;
                      Lage und Verlauf, dann im Hintergrund die Züge in OpenBoards
                      zuordnen (core/boardmatch.py)
  Reise gespeichert/zugeordnet → Lage der Halte und Streckenverlauf aus dem
                      Gleisnetz (im Executor, siehe core/geometry.py)
  alle 15 s         → Abgelaufene entfernen, nicht gefundene Züge erneut suchen,
                      fällige Echtzeit holen; Gleisnetz, Fahrplan und Bahnhofsnummern
                      auf neue Fassung prüfen
  Echtzeit fehlt    → einmalige Nachricht, Ereignis "reiseanzeige"
  Android-App       → Reisen, Standort und Umleitungen per Webhook (links.py,
                      Format v1); für verbundene Personen ist die App die Quelle

Ohne Zugangsdaten der Bahn (client None) läuft alles nach Fahrplan: keine
Zuordnung in OpenBoards, keine Echtzeit, keine Abfahrtstafel.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_KEY, CONF_APP_LOCAL_ONLY, CONF_APP_URL, CONF_CLIENT_ID, DEFAULT_APP_LOCAL_ONLY, CONF_GRACE_MINUTES, CONF_RAILNET, CONF_REFRESH_SECONDS, CONF_STATION,
    CONF_GPS_ROUTE, CONF_TIMETABLE, DEFAULT_GPS_ROUTE, DEFAULT_GRACE_MINUTES, DEFAULT_RAILNET, DEFAULT_REFRESH_SECONDS,
    DEFAULT_TIMETABLE, DOMAIN,
    NOTIFY_TITLE, SHARE_EVENT,
    SIGNAL_NEW_OWNER, SIGNAL_TRIPS_CHANGED, STORAGE_KEY_TRIPS, STORAGE_VERSION, TICK_SECONDS,
)
from .core.geometry import apply_geometry
from .core.gpsroute import Watch, apply_route, read_fix, step as gps_step
from .core.board import Station as BoardStation
from .core.boardmatch import match_snapshot, pending
from .core.journey import draft_snapshot, norm_line
from .core.link_format import IGNORED, OK, UNKNOWN, deep_link, parse_pairing
from .core.openboards import TRANSIENT, OpenBoardsClient, OpenBoardsError, aiohttp_post, is_eva
from .core.realtime import leg_realtime, stops_to_poll
from .core.share_text import ShareTextError, looks_like_share, parse_share_text
from .core.trips import (
    RUNNING, TripError, TripManager, realtime_unavailable, share_confirmation, share_error, trip_status,
)
from .links import Links
from .owners import Owners
from .railnet_store import DataStore, RailNetStore, StationListStore, TimetableStore

_LOGGER = logging.getLogger(__name__)

LOCATE_COOLDOWN = 30  # Sekunden je Person (Companion-App rät von häufigen Anforderungen ab)
LINKED_HINT = ("Deine Reiseanzeige-App ist mit Home Assistant verbunden. Bitte teile Reisen dort; "
               "Home Assistant übernimmt sie von selbst.")
PAIRING_MESSAGE = "Tippen, um die Reiseanzeige-App mit Home Assistant zu verbinden (gilt 10 Minuten)."
REPLACEMENT_RADIUS_KM = 0.5   # Ersatzbus-Haltestellen so nah am Bahnhof gehören zur Tafel
STOP_ENRICHING = ("BAD_CREDENTIALS", "RATE_LIMITED")   # danach in diesem Durchlauf keine weiteren Abfragen
REJECTED_MESSAGE = (
    "OpenBoards lehnt die Zugangsdaten der Bahn ab. Seit Version 0.9.0 holt die Reiseanzeige Zuordnung, "
    "Echtzeit und Abfahrtstafel aus OpenBoards statt aus der Timetables-API. Bitte im DB API Marketplace "
    "die Anwendung zusätzlich für OpenBoards abonnieren (Plan „Free“): "
    "https://developers.deutschebahn.com/db-api-marketplace/apis/product/open-boards – oder unter "
    "Geräte & Dienste → Reiseanzeige → ⋮ → Neu konfigurieren andere Zugangsdaten eintragen.")


class HaTripStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY_TRIPS)

    async def load(self) -> list[dict]:
        data = await self._store.async_load() or {}
        return data.get("trips") or []

    async def save(self, trips: list[dict]) -> None:
        await self._store.async_save({"trips": trips})


class Coordinates:
    """Lage eines Bahnhofs über seine Nummer: Gleisnetz (OSM), sonst Bahnhofsliste (auch
    über einen eindeutigen Namen). Suche über den Namen mit Anker übernimmt core/geometry.py."""

    def __init__(self, railnet: RailNetStore, stations: StationListStore) -> None:
        self.railnet, self.stations = railnet, stations

    def coords(self, eva: str | None, name: str) -> tuple[float, float] | None:
        places, stations = self.railnet.places, self.stations.stations
        return (places.by_eva(eva) if places else None) or (stations.coords(eva, name) if stations else None)


class Reiseanzeige:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        opts = {**entry.data, **entry.options}
        self.default_station: str | None = opts.get(CONF_STATION) or None
        session = async_get_clientsession(hass)
        client_id, api_key = entry.data.get(CONF_CLIENT_ID), entry.data.get(CONF_API_KEY)
        # Ohne Zugangsdaten: Fahrplanmodus (siehe oben)
        self.client: OpenBoardsClient | None = (
            OpenBoardsClient(aiohttp_post(session), client_id, api_key) if client_id and api_key else None)
        self.railnet = RailNetStore(hass, enabled=opts.get(CONF_RAILNET, DEFAULT_RAILNET))
        self.timetable = TimetableStore(hass, enabled=opts.get(CONF_TIMETABLE, DEFAULT_TIMETABLE))
        self.stations = StationListStore(hass)   # klein, immer: Nummern für OpenBoards, Lage als Rückfall
        self.gps_route: bool = opts.get(CONF_GPS_ROUTE, DEFAULT_GPS_ROUTE)
        self.coords = Coordinates(self.railnet, self.stations)
        self.trips = TripManager(
            HaTripStore(hass),
            clock=dt_util.now,
            grace=timedelta(minutes=opts.get(CONF_GRACE_MINUTES, DEFAULT_GRACE_MINUTES)),
            running=timedelta(seconds=opts.get(CONF_REFRESH_SECONDS, DEFAULT_REFRESH_SECONDS)),
        )
        self.owners = Owners(hass)
        self.links = Links(hass, self, local_only=opts.get(CONF_APP_LOCAL_ONLY, DEFAULT_APP_LOCAL_ONLY),
                           app_url=opts.get(CONF_APP_URL))
        # Zufälliger Schlüssel für die Kartenkacheln (wie das access_token bei Kamerabildern):
        # die Karte bekommt ihn über die angemeldete WebSocket-Verbindung
        self.map_token = secrets.token_urlsafe(24)
        self._names: dict[str, str] = {}
        self._locate_last: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._unsubs: list = []
        self._ticking = False
        self._busy: set[str] = set()   # gerade ladende Datenquellen
        self._rejected_shown = False
        self._geo_lock = asyncio.Lock()
        self._gps: dict[str, Watch] = {}   # je laufender Reise: Stand der GPS-Auswertung
        self.sensor_owners: set[str] = set()

    # --- Start/Stopp ---

    async def async_start(self) -> None:
        await self.owners.load()
        await self.links.load()
        await self.trips.init()
        for store in (self.railnet, self.timetable):
            await store.load()
            store.listeners.append(self.update_geometry)
        await self.stations.load()
        self.stations.listeners.append(self._stations_loaded)
        await self._load_names()
        self.trips.listeners.append(self._trips_changed)
        self.links.start()
        self._unsubs.append(self.hass.bus.async_listen(SHARE_EVENT, self._on_share))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, timedelta(seconds=TICK_SECONDS)))
        if self.client is None:
            _LOGGER.info("Ohne Zugangsdaten der Bahn: Reisen nach Fahrplan, ohne Echtzeit und Abfahrtstafel")
        self._spawn(self.update_geometry())  # gespeicherte Reisen, z. B. nach einem Update
        self._spawn(self.tick())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.links.stop()
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
        app = t.get("app")
        t["ownerName"] = self.name_of(t["ownerId"])
        t["fromApp"] = bool(app)
        if self.links.linked(t["ownerId"]):
            # Verbundene Person: Standort nur von der App, kein Anfordern über die Companion-App
            t["ownerLocation"] = (self.links.tracker_entity(app["linkId"]) if app
                                  else self.links.tracker_of_user(t["ownerId"]))
            t["ownerCanLocate"] = False
        else:
            t["ownerLocation"] = owner.get("tracker")
            t["ownerCanLocate"] = bool(owner.get("tracker") and owner.get("notify"))
        t.pop("connection", None)
        if light:
            for leg in t["snapshot"]["legs"]:
                for part in (leg, leg.get("diversion")):
                    if part and part.get("polyline"):
                        part["hasPolyline"], part["polyline"] = True, None
        for leg in t["snapshot"]["legs"]:
            if (leg.get("diversion") or {}).get("gps"):
                leg["diversion"]["gps"] = {"active": True}   # Standortpunkte bleiben in Home Assistant
        return t

    # --- Teilen ---

    async def _on_share(self, event: Event) -> None:
        data = event.data or {}
        text = data.get("text") or data.get("url") or ""
        user_id = event.context.user_id
        pairing = parse_pairing(text)
        if not user_id or not (pairing or looks_like_share(text)):
            return  # Teilen aus anderen Apps (YouTube …) stillschweigend ignorieren
        if user_id not in self._names:
            await self._load_names()  # neuer Benutzer seit dem Start
        await self.owners.learn(user_id, data)
        if pairing:
            await self._pair_from_share(user_id, *pairing)
            return
        result = await self.add_text(user_id, text)
        # Rückmeldung auf das eigene Teilen, auch an verbundene Personen (Hinweis auf die App)
        await self.notify(user_id, result["message"], force=True)

    async def _pair_from_share(self, user_id: str, pair_id: str, token: str) -> None:
        """Kopplungsanfrage der App, geteilt an die Companion-App: Einmal-Schlüssel
        anmelden und dem Handy, von dem sie kam, den Deep Link zum Abschließen schicken."""
        base = self.links.base_url()
        if base is None:
            await self.notify(user_id, "Die Adresse von Home Assistant ist unbekannt. Bitte in den Optionen der "
                                       "Reiseanzeige „Adresse für die Android-App“ eintragen.", force=True)
            return
        try:
            self.links.begin(user_id, token=token, pair_id=pair_id)
        except ValueError:
            _LOGGER.warning("Kopplungsanfrage mit fremdem Schlüssel ignoriert")
            return
        link = f"deep-link://{deep_link(base, name=self.hass.config.location_name, user=self.name_of(user_id) or '', pair=pair_id)}"
        service = self.owners.notify_service(user_id)
        if service:
            await self.hass.services.async_call("notify", service, {
                "title": NOTIFY_TITLE, "message": PAIRING_MESSAGE,
                "data": {"clickAction": link, "tag": "reiseanzeige-verbinden",
                         "actions": [{"action": "URI", "title": "Verbinden", "uri": link}]}})
        else:
            persistent_notification.async_create(self.hass, f"{PAIRING_MESSAGE}\n\n{link}", title=NOTIFY_TITLE)

    async def add_text(self, user_id: str, text: str) -> dict:
        """Geteilten Text speichern. Ergebnis: {ok, message, trip?}."""
        if self.links.linked(user_id):
            return {"ok": False, "code": "LINKED", "message": LINKED_HINT}
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
        # Erst Lage und Verlauf aus dem Text, dann (auch bei erneutem Teilen) Züge zuordnen
        self._spawn(self._prepare(trip_id))
        return {"ok": True, "status": result["status"], "message": message, "trip": self.trip_view(self.trips.get(trip_id))}

    async def delete_trip(self, trip_id: str) -> dict:
        """Reise löschen (Karte). Reisen aus der Android-App löscht man nur dort; ist das Handy
        weg, entfernt das Aufheben der Verbindung alle seine Reisen (Entscheidung 18.11.2026)."""
        trip = self.trips.get(trip_id)
        if trip is not None and trip.get("app"):
            raise TripError("FROM_APP", "Diese Reise kommt aus der Android-App und wird dort gelöscht")
        return await self.trips.delete_trip(trip_id)

    # --- Android-App (links.py, Format v1) ---

    async def on_linked(self, link: dict) -> None:
        """Neue Verbindung: Sensor der Person anlegen; die App schickt danach alle
        laufenden und kommenden Reisen."""
        self._trips_changed()

    async def app_message(self, link: dict, msg: dict) -> str:
        kind = msg["type"]
        try:
            if kind == "trip.upsert":
                return await self._app_upsert(link, msg)
            if kind == "trip.delete":
                await self.trips.delete_app(link["id"], msg["tripId"])
                return OK
            trip = self.trips.find_app(link["id"], msg["tripId"])
            if trip is None:
                return UNKNOWN
            if kind == "location":
                if msg["fixes"]:
                    self.links.set_location(link["id"], msg["fixes"][-1], trip["id"])
                return OK
            if kind == "sharing.stop":
                self.links.clear_location(link["id"])
                return OK
            if kind == "route.gps":
                return await self._app_route(trip["id"], msg)
        except Exception:  # noqa: BLE001 – eine Nachricht darf die anderen nicht aufhalten
            _LOGGER.exception("Nachricht %s der Android-App nicht verarbeitet", kind)
        return IGNORED

    async def _app_upsert(self, link: dict, msg: dict) -> str:
        journey = msg["journey"]
        draft = draft_snapshot(journey, dt_util.now(), self.coords)
        res = await self.trips.upsert_from_app(link["userId"], link["id"], msg["tripId"], msg["revision"], journey,
                                               draft, msg["connection"], msg["seats"])
        if res["replaced"]:
            _LOGGER.info("Reise aus der App ersetzt %s anders geteilte Reise(n)", len(res["replaced"]))
        if res["change"] in ("new", "connection"):
            self._spawn(self._prepare(res["tripId"]))
        return OK

    async def _prepare(self, trip_id: str) -> None:
        """Lage und Verlauf aus der Verbindung, danach Zuordnung in OpenBoards –
        nacheinander: Die Zuordnung braucht die Lage der Halte (Bahnhofsnummern rundum)."""
        await self.update_geometry([trip_id])
        if self.client is not None:
            await self._enrich(trip_id)

    async def _app_route(self, trip_id: str, msg: dict) -> str:
        async with self._geo_lock:
            trip = next((t for t in self.trips.trips if t["id"] == trip_id), None)
            if trip is None:
                return UNKNOWN
            legs = trip["snapshot"].get("legs") or []
            i, key = msg["leg"], msg["legKey"]
            if not 0 <= i < len(legs) or (key["line"] and norm_line(key["line"]) != norm_line(legs[i]["line"].get("name"))) \
                    or (key["dep"] and legs[i].get("plannedDeparture")
                        and datetime.fromisoformat(legs[i]["plannedDeparture"]) != key["dep"]):
                return IGNORED
            snapshot, created = copy.deepcopy(trip["snapshot"]), trip["snapshot"].get("createdAt")
            changed = await self.hass.async_add_executor_job(
                apply_route, snapshot, i, msg["gps"], copy.deepcopy(trip.get("realtime")), self.railnet.net,
                self.railnet.places, self.coords.coords, self.railnet.version or "")
            if changed:
                await self.trips.set_geometry(trip_id, created, snapshot)
        return OK

    async def _enrich(self, trip_id: str) -> str | None:
        """Bahn-Abschnitte der gespeicherten Reise in OpenBoards zuordnen (mit der Lage der
        Halte aus dem Gleisnetz). Liefert den Fehlercode, falls OpenBoards ablehnt."""
        trip = next((t for t in self.trips.trips if t["id"] == trip_id), None)
        if trip is None or not pending(trip["snapshot"]):
            return None
        await self.trips.mark_enrich_attempt(trip_id)
        snapshot = copy.deepcopy(trip["snapshot"])
        created = snapshot.get("createdAt")
        notes: list[str] = []
        errors: list[OpenBoardsError] = []
        try:
            matched = await match_snapshot(snapshot, self.client, self.railnet.places, self.stations.stations,
                                           log=notes.append, on_error=lambda _leg, err: errors.append(err))
        except OpenBoardsError as err:
            await self.trips.mark_enrich_result(trip_id, err.code in TRANSIENT)
            trip = next((t for t in self.trips.trips if t["id"] == trip_id), None)
            _LOGGER.warning("Reise %s: Züge nicht in OpenBoards gesucht: %s%s", trip_id, err,
                            f"; neuer Versuch um {trip['enrichRetryAt'][11:16]}" if trip and trip.get("enrichRetryAt") else "")
            self.rejected(err)
            return err.code
        await self.trips.mark_enrich_result(trip_id, any(e.code in TRANSIENT for e in errors))
        for note in notes:
            _LOGGER.info("Reise %s: %s", trip_id, note)
        rides = [l for l in (matched or snapshot)["legs"] if l.get("kind") == "ride"]
        _LOGGER.info("Reise %s: %s von %s Fahrten in OpenBoards zugeordnet (%s Abfragen bisher)", trip_id,
                     sum(1 for l in rides if l.get("source") == "openboards"), len(rides), self.client.calls)
        if matched is None:
            return None
        # Neue Fassung: Verlauf und GPS-Auswertung der alten dürfen sie nicht mehr überschreiben
        matched["createdAt"] = dt_util.now().isoformat()
        if await self.trips.set_snapshot(trip_id, matched, created):
            await self.update_geometry([trip_id])
            await self.tick()
        return None

    def rejected(self, err: OpenBoardsError) -> None:
        """Zugangsdaten abgelehnt – etwa für die Timetables-API, aber nicht für OpenBoards
        abonniert (Umstieg in 0.9.0): einmal je Start in Home Assistant melden."""
        if err.code != "BAD_CREDENTIALS" or self._rejected_shown:
            return
        self._rejected_shown = True
        _LOGGER.error("OpenBoards lehnt die Zugangsdaten ab: %s", err)
        persistent_notification.async_create(self.hass, REJECTED_MESSAGE, title=NOTIFY_TITLE,
                                             notification_id=f"{DOMAIN}_openboards")

    async def _stations_loaded(self) -> None:
        """Bahnhofsliste neu: nicht gefundene Züge gleich noch einmal suchen."""
        self.trips.retry_enrichment()
        self._spawn(self.tick())

    # --- Lage der Halte und Streckenverlauf ---

    async def update_geometry(self, trip_ids: list[str] | None = None) -> None:
        """Koordinaten und Verlauf ergänzen – im Executor, nacheinander."""
        async with self._geo_lock:
            for trip in list(self.trips.trips):
                if trip_ids is not None and trip["id"] not in trip_ids:
                    continue
                snapshot = copy.deepcopy(trip["snapshot"])
                created = snapshot.get("createdAt")
                # Umleitungen laut letzter Echtzeit je Abschnitt (fehlt ein Abschnitt, bleibt es, wie es ist)
                diversions = {int(k): copy.deepcopy(v.get("diversion"))
                              for k, v in ((trip.get("realtime") or {}).get("legs") or {}).items()}
                try:
                    changed = await self.hass.async_add_executor_job(
                        apply_geometry, snapshot, self.railnet.net, self.railnet.places,
                        self.coords.coords, self.railnet.version or "", self.timetable.timetable, diversions,
                        copy.deepcopy(trip.get("realtime")))
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
            for store in (self.railnet, self.timetable, self.stations):
                if store.label not in self._busy and store.due(dt_util.now()):
                    # Download kann dauern: eigene Aufgabe, damit die Echtzeit weiterläuft
                    self._busy.add(store.label)
                    self._spawn(self._refresh_store(store))
            # Beim ersten Start die Bahnhofsliste abwarten (wenige Sekunden): ohne sie fehlen
            # bei großen Bahnhöfen oft die richtigen Nummern
            if self.client is not None and (self.stations.loaded or self.stations.label not in self._busy):
                for trip in self.trips.due_for_enrichment():
                    if await self._enrich(trip["id"]) in STOP_ENRICHING:
                        break
            if self.client is not None:
                for trip in self.trips.due_for_refresh():
                    await self._refresh(trip)
            await self._gps_tick()
            await self.links.cleanup(dt_util.now())
        except Exception:  # noqa: BLE001 – ein Durchlauf darf die Integration nicht stoppen
            _LOGGER.exception("Durchlauf fehlgeschlagen")
        finally:
            self._ticking = False

    async def _refresh(self, trip: dict) -> None:
        now = dt_util.now()
        polls = stops_to_poll(trip["snapshot"], trip.get("realtime"), now)
        known = (trip.get("realtime") or {}).get("legs") or {}
        results = []
        for index, stops in polls.items():
            leg = next(l for l in trip["snapshot"]["legs"] if l.get("index") == index)
            try:
                rt = await leg_realtime(leg, stops, known.get(str(index)), self.client, now)
                results.append({"legIndex": index, "status": "ok", "realtime": rt})
            except OpenBoardsError as err:
                _LOGGER.warning("Echtzeit Abschnitt %s (%s): %s", index, leg["line"]["name"], err)
                self.rejected(err)
                results.append({"legIndex": index, "status": "error", "error": err.code})
        events = await self.trips.apply_realtime(trip["id"], results)
        # Laufweg geändert (Umleitung, Zug endet früher) oder wieder wie geplant: Halte und Verlauf neu
        legs = trip["snapshot"].get("legs", [])
        for r in results:
            if r["status"] != "ok" or not 0 <= r["legIndex"] < len(legs):
                continue
            new = (r["realtime"].get("diversion") or {}).get("path")
            if new != (legs[r["legIndex"]].get("diversion") or {}).get("path"):
                _LOGGER.info("Reise %s, Abschnitt %s: %s", trip["id"], r["legIndex"],
                             f"geänderter Laufweg {' – '.join(new)}" if new else "Laufweg wieder wie geplant")
                self._spawn(self.update_geometry([trip["id"]]))
                break
        for ev in events:
            if ev["type"] == "realtime_unavailable":
                self.hass.bus.async_fire(DOMAIN, {"type": ev["type"], "trip_id": ev["tripId"], "user_id": ev["ownerId"]})
                await self.notify(ev["ownerId"], realtime_unavailable(ev))

    # --- Abfahrtstafel (core/board.py) ---

    def board_station(self, query: str) -> BoardStation | None:
        """Bahnhof der Tafel aus Name oder Nummer: über die Bahnhofsliste, sonst eine
        Nummer direkt oder die nächste Nummer aus dem Gleisnetz."""
        q = (query or "").strip()
        stations = self.stations.stations
        if stations is not None:
            hit = stations.resolve(q)
            return BoardStation(*hit) if hit else None
        if is_eva(q):
            return BoardStation(q, q)
        places = self.railnet.places
        c = places.find(q) if places else None
        evas = places.eva_near(c) if c else []
        return BoardStation(evas[0], q) if evas else None

    def board_replacements(self, station, now, minutes: int) -> list[dict]:
        """Ersatzbusse an Haltestellen rund um den Bahnhof laut Fahrplan (GTFS)."""
        tt = self.timetable.timetable
        c = self.coords.coords(station.eva, station.name) if tt else None
        if c is None:
            return []
        tz = ZoneInfo("Europe/Berlin")
        local = now.astimezone(tz)
        day = local.date()
        start = local.hour * 60 + local.minute
        deps = tt.departures(tt.near(c[0], c[1], REPLACEMENT_RADIUS_KM), day, start - 1, minutes + 1, modes={"replacement"})
        midnight = datetime.combine(day, dtime(0), tz)
        return [{"line": d.line, "planned": midnight + timedelta(minutes=d.minute), "destination": d.to,
                 "via": d.via, "stop": d.stop, "to_at": d.to_at} for d in deps]

    def locate_path(self, names: list[str], station) -> list:
        """Lage der weiteren Halte eines Zuges ab dem Bahnhof (Namen der Bahn); jeder
        Halt wird nahe dem vorigen gesucht. Unbekannte Halte: None."""
        places = self.railnet.places
        anchor = self.coords.coords(station.eva, station.name)
        out = []
        for name in names:
            c = (places.find(name, near=anchor) if places else None) or self.coords.coords(None, name)
            if c:
                anchor = c
            out.append(c)
        return out

    # --- Umleitungen per GPS (core/gpsroute.py) ---

    async def _gps_tick(self) -> None:
        """Neue Standorte der Reisenden auswerten – nur während laufender Reisen, mit
        Gleisnetz und eingetragenem Handy. Rechnet im Executor."""
        now = dt_util.now()
        running = {t["id"] for t in self.trips.trips if trip_status(t, now, self.trips.grace) == RUNNING}
        for trip_id in list(self._gps):
            if trip_id not in running:
                del self._gps[trip_id]
        if not self.gps_route or self.railnet.net is None:
            return
        for trip_id in running:
            trip = next((t for t in self.trips.trips if t["id"] == trip_id), None)
            if trip is None or self.links.linked(trip["ownerId"]):
                continue   # Umleitungen verbundener Personen wertet die App aus (route.gps)
            tracker = (self.owners.get(trip["ownerId"]) or {}).get("tracker") if trip else None
            fix = read_fix(self.hass.states.get(tracker), now) if tracker else None
            if fix is None:
                continue
            watch = self._gps.setdefault(trip_id, Watch())
            if watch.last_at and fix.at <= watch.last_at:
                continue
            async with self._geo_lock:
                trip = next((t for t in self.trips.trips if t["id"] == trip_id), None)
                if trip is None:
                    continue
                snapshot, created = copy.deepcopy(trip["snapshot"]), trip["snapshot"].get("createdAt")
                had = {l["index"] for l in snapshot["legs"] if (l.get("diversion") or {}).get("gps")}
                try:
                    changed = await self.hass.async_add_executor_job(
                        gps_step, snapshot, copy.deepcopy(trip.get("realtime")), watch, fix, self.railnet.net,
                        self.railnet.places, self.coords.coords, self.railnet.version or "")
                except Exception:  # noqa: BLE001 – ohne GPS-Auswertung geht es auch
                    _LOGGER.exception("Standort für Reise %s nicht ausgewertet", trip_id)
                    continue
                if changed and await self.trips.set_geometry(trip_id, created, snapshot):
                    for leg in snapshot["legs"]:
                        if (leg.get("diversion") or {}).get("gps") and leg["index"] not in had:
                            _LOGGER.info("Reise %s, Abschnitt %s (%s): Zug weicht von der Strecke ab – Linie folgt den Standorten",
                                         trip_id, leg["index"], leg["line"].get("name"))

    # --- Benachrichtigungen ---

    async def notify(self, user_id: str, message: str, *, force: bool = False) -> None:
        """Nachricht an das Handy der Person. Verbundene Personen bekommen ihre
        Meldungen von der App – außer der direkten Rückmeldung auf ihr Teilen (force)."""
        if not force and self.links.linked(user_id):
            _LOGGER.debug("Keine Nachricht an %s (App verbunden): %s", user_id, message)
            return
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
        if trip["status"] != "running" or not (owner.get("notify") and owner.get("tracker")) \
                or self.links.linked(trip["ownerId"]):
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
