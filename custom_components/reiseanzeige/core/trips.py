"""Reiseverwaltung: Zustände, Abfrageplan, Ersetzungsregel, Echtzeit-Gesundheit.

Übernommen aus der bisherigen App (Node.js) und angepasst an den Import über
den geteilten Text: Eine geteilte Reise wird sofort als Entwurf gespeichert
(aus dem Text) und danach im Hintergrund über die Timetables-API angereichert.

Kennt weder Home Assistant noch die API direkt. Speicher, Uhr und Anreicherung
werden übergeben; Benachrichtigungen kommen als Ereignisse zurück.

Zustände:
    upcoming  (Anstehend)      vor der Abfahrt
    running   (Läuft)          zwischen Abfahrt und Ankunft
    completed (Abgeschlossen)  bis zur Nachlaufzeit nach der Ankunft
    expired   → wird entfernt
Maßgeblich sind die prognostizierten Zeiten, sonst die planmäßigen.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import datetime, timedelta
from typing import Callable, Protocol

from .journey import norm_name
from .share_text import SharedJourney

UPCOMING, RUNNING, COMPLETED, EXPIRED = "upcoming", "running", "completed", "expired"

MAX_SEAT_LENGTH = 12
MAX_BACKOFF = timedelta(minutes=15)

# Die Timetables-API liefert den Sollfahrplan nur kurz im Voraus. Reisen, deren
# Bahn-Abschnitte beim Teilen noch nicht im Fahrplan stehen, werden deshalb
# erneut abgeglichen: ab ENRICH_WINDOW vor Abfahrt des Abschnitts alle
# ENRICH_EVERY, in den letzten ENRICH_HOT_WINDOW alle ENRICH_HOT_EVERY.
ENRICH_WINDOW = timedelta(hours=24)
ENRICH_EVERY = timedelta(hours=2)
ENRICH_HOT_WINDOW = timedelta(hours=3)
ENRICH_HOT_EVERY = timedelta(minutes=20)
RAIL_PRODUCTS = {"longdistance", "regional", "suburban"}


class TripError(Exception):
    """code: NOT_FOUND | INVALID_INPUT | IN_PAST"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TripStore(Protocol):
    async def load(self) -> list[dict]: ...
    async def save(self, trips: list[dict]) -> None: ...


class MemoryStore:
    def __init__(self, trips: list[dict] | None = None) -> None:
        self.trips = trips or []
        self.saves = 0

    async def load(self) -> list[dict]:
        return copy.deepcopy(self.trips)

    async def save(self, trips: list[dict]) -> None:
        self.saves += 1
        self.trips = copy.deepcopy(trips)


# --- Zeiten und Zustand ----------------------------------------------------------

def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def _rides(trip: dict) -> list[dict]:
    return [l for l in trip["snapshot"]["legs"] if l.get("kind") == "ride"]


def _leg_rt(trip: dict, leg: dict) -> dict | None:
    return ((trip.get("realtime") or {}).get("legs") or {}).get(str(leg["index"]))


def effective_departure(trip: dict) -> datetime:
    first = _rides(trip)[0]
    rt = (_leg_rt(trip, first) or {}).get("stopovers") or [{}]
    return _dt(rt[0].get("departure", {}).get("prognosed")) or _dt(first["plannedDeparture"])


def effective_arrival(trip: dict) -> datetime:
    last = _rides(trip)[-1]
    rt = (_leg_rt(trip, last) or {}).get("stopovers") or [{}]
    return _dt(rt[-1].get("arrival", {}).get("prognosed")) or _dt(last["plannedArrival"])


def trip_status(trip: dict, now: datetime, grace: timedelta) -> str:
    dep, arr = effective_departure(trip), effective_arrival(trip)
    if now < dep:
        return UPCOMING
    if now < arr:
        return RUNNING
    if now < arr + grace:
        return COMPLETED
    return EXPIRED


def overlaps(a: dict, b: dict) -> bool:
    """Ersetzungsregel: zeitliche Überschneidung; bloßes Aneinanderstoßen zählt nicht."""
    return effective_departure(a) < effective_arrival(b) and effective_departure(b) < effective_arrival(a)


def connection_key(journey: SharedJourney) -> str:
    """Fingerabdruck aus dem Text: gleiche Züge zur gleichen Zeit = dieselbe Reise."""
    return "|".join(f"{l.number or l.line}@{norm_name(l.dep.name)}@{l.dep.when.isoformat()}" for l in journey.legs)


def next_event_at(trip: dict, now: datetime) -> datetime | None:
    """Nächstes Ereignis für die Reisenden: Abfahrt, Umstieg, Ankunft."""
    best = None
    for leg in _rides(trip):
        stops = (_leg_rt(trip, leg) or {}).get("stopovers") or []
        pairs = [(leg["plannedDeparture"], stops[0]["departure"]["prognosed"] if stops else None),
                 (leg["plannedArrival"], stops[-1]["arrival"]["prognosed"] if stops else None)]
        for planned, prognosed in pairs:
            v = _dt(prognosed) or _dt(planned)
            if v and v > now and (best is None or v < best):
                best = v
    return best


# --- Abfrageplan -----------------------------------------------------------------

def hot_interval(until_next: timedelta | None, running: timedelta) -> timedelta:
    if until_next is None or until_next > timedelta(minutes=30):
        return max(running, timedelta(minutes=5))
    if until_next > timedelta(minutes=10):
        return max(running, timedelta(minutes=3))
    return running


def next_refresh_at(trip: dict, now: datetime, grace: timedelta, running: timedelta) -> datetime | None:
    """Wann die nächste Echtzeit-Abfrage fällig ist. Nur Abschnitte, die die API
    kennt, werden überhaupt abgefragt; die Auswahl der Bahnhöfe trifft
    realtime.stations_to_poll."""
    if not any(l.get("matched") for l in _rides(trip)):
        return None
    status = trip_status(trip, now, grace)
    if status in (COMPLETED, EXPIRED):
        return None
    last = _dt(trip.get("lastRefreshAt"))
    dep = effective_departure(trip)
    # Vor der heißen Phase (mehr als 3 h vor Abfahrt) gibt es nichts abzufragen:
    # Änderungen meldet die API erst kurz vorher
    if status == UPCOMING and dep - now > timedelta(hours=3):
        return dep - timedelta(hours=3)
    event = next_event_at(trip, now)
    interval = hot_interval(event - now if event else None, running)
    failures = (trip.get("realtimeHealth") or {}).get("consecutiveFailures", 0)
    if failures:
        interval = min(MAX_BACKOFF, max(interval, running * (2 ** failures)))
    return max(now, last + interval) if last else now


# --- Verwaltung ----------------------------------------------------------------

def _summary(trip: dict) -> dict:
    s = trip["snapshot"]
    return {"id": trip["id"], "originName": s["origin"].get("name"), "destinationName": s["destination"].get("name"),
            "plannedDeparture": s["plannedDeparture"]}


class TripManager:
    def __init__(self, store: TripStore, *, clock: Callable[[], datetime],
                 grace: timedelta = timedelta(hours=1), running: timedelta = timedelta(seconds=90),
                 failure_threshold: int = 2) -> None:
        self.store = store
        self.clock = clock
        self.grace = grace
        self.running = running
        self.failure_threshold = failure_threshold
        self.trips: list[dict] = []
        self._lock = asyncio.Lock()
        self.listeners: list[Callable[[], None]] = []

    async def init(self) -> None:
        self.trips = await self.store.load()

    def _changed(self) -> None:
        for fn in list(self.listeners):
            fn()

    async def _mutate(self, fn: Callable[[], object]):
        async with self._lock:
            result = fn()
            await self.store.save(self.trips)
        self._changed()
        return result

    def _status(self, trip: dict, now: datetime) -> str:
        return trip_status(trip, now, self.grace)

    def _find(self, trip_id: str) -> dict:
        for t in self.trips:
            if t["id"] == trip_id:
                return t
        raise TripError("NOT_FOUND", "Reise nicht gefunden")

    def view(self, trip: dict, now: datetime | None = None) -> dict:
        now = now or self.clock()
        out = copy.deepcopy(trip)
        out.update(status=self._status(trip, now), departure=effective_departure(trip).isoformat(),
                   arrival=effective_arrival(trip).isoformat())
        return out

    # --- Anlegen aus geteiltem Text ---

    async def add_from_share(self, owner_id: str, journey: SharedJourney, draft: dict, text: str | None = None) -> dict:
        """Entwurf speichern. Ergebnis: {status: saved|duplicate, trip, replaced}."""
        if not owner_id:
            raise TripError("INVALID_INPUT", "Kein Benutzer angegeben")
        key = connection_key(journey)

        def run():
            now = self.clock()
            candidate = {"snapshot": draft, "realtime": None}
            if effective_arrival(candidate) <= now:
                raise TripError("IN_PAST", "Diese Reise ist bereits vorbei")
            own = [t for t in self.trips if t["ownerId"] == owner_id and self._status(t, now) != EXPIRED]
            same = next((t for t in own if t.get("connectionKey") == key), None)
            if same:
                if text:
                    same["shareText"] = text
                return {"status": "duplicate", "trip": self.view(same, now), "replaced": []}
            replaced = [t for t in own if overlaps(candidate, t)]
            self.trips = [t for t in self.trips if t not in replaced]
            trip = {
                "id": uuid.uuid4().hex,
                "ownerId": owner_id,
                "createdAt": now.isoformat(),
                "connectionKey": key,
                "snapshot": draft,
                "enriched": False,
                "shareText": text,          # für den erneuten Abgleich mit dem Fahrplan
                "enrichAttemptAt": None,
                "seats": {},
                "realtime": None,
                "realtimeHealth": {"consecutiveFailures": 0, "failing": False, "lastSuccessAt": None,
                                   "notifiedAt": None, "lastError": None},
                "lastRefreshAt": None,
            }
            self.trips.append(trip)
            return {"status": "saved", "trip": self.view(trip, now), "replaced": [_summary(t) for t in replaced]}
        return await self._mutate(run)

    async def set_snapshot(self, trip_id: str, snapshot: dict) -> bool:
        """Angereicherte Fassung übernehmen (Sitzplätze bleiben). False, wenn die
        Reise inzwischen gelöscht oder ersetzt wurde."""
        def run():
            trip = next((t for t in self.trips if t["id"] == trip_id), None)
            if trip is None:
                return False
            # Schon gefundene Abschnitte behalten, falls der neue Abgleich sie nicht findet
            old = {l["index"]: l for l in trip["snapshot"].get("legs", [])}
            for i, leg in enumerate(snapshot.get("legs", [])):
                prev = old.get(leg.get("index"))
                if prev and prev.get("matched") and not leg.get("matched"):
                    snapshot["legs"][i] = prev
            trip["snapshot"] = snapshot
            trip["enriched"] = True
            trip["realtime"] = None
            trip["lastRefreshAt"] = None
            return True
        return await self._mutate(run)

    # --- Bearbeiten (jeder Benutzer darf, Entscheidung 02.10.2026) ---

    async def delete_trip(self, trip_id: str) -> dict:
        def run():
            trip = self._find(trip_id)
            self.trips = [t for t in self.trips if t is not trip]
            return _summary(trip)
        return await self._mutate(run)

    async def set_seat(self, trip_id: str, leg_index: int, coach: str = "", seat: str = "") -> dict:
        def run():
            trip = self._find(trip_id)
            legs = trip["snapshot"]["legs"]
            if not (0 <= leg_index < len(legs)) or legs[leg_index].get("kind") != "ride":
                raise TripError("INVALID_INPUT", "Kein Fahrtabschnitt mit diesem Index")
            c, s = str(coach or "").strip(), str(seat or "").strip()
            if len(c) > MAX_SEAT_LENGTH or len(s) > MAX_SEAT_LENGTH:
                raise TripError("INVALID_INPUT", f"Wagen und Sitzplatz höchstens {MAX_SEAT_LENGTH} Zeichen")
            if not c and not s:
                trip["seats"].pop(str(leg_index), None)
            else:
                trip["seats"][str(leg_index)] = {"coach": c or None, "seat": s or None}
            return self.view(trip)
        return await self._mutate(run)

    # --- Lesen ---

    def list(self) -> list[dict]:
        now = self.clock()
        live = [t for t in self.trips if self._status(t, now) != EXPIRED]
        return [self.view(t, now) for t in sorted(live, key=effective_departure)]

    def get(self, trip_id: str) -> dict:
        return self.view(self._find(trip_id))

    # --- Echtzeit ---

    def due_for_enrichment(self) -> list[dict]:
        """Reisen mit Bahn-Abschnitten, die noch nicht im Fahrplan gefunden wurden
        und deren Abfahrt jetzt in Reichweite der API liegt."""
        now = self.clock()
        out = []
        for t in self.trips:
            if not t.get("shareText"):
                continue
            pending = [l for l in _rides(t) if not l.get("matched") and l["line"].get("product") in RAIL_PRODUCTS]
            deps = [_dt(l["plannedDeparture"]) for l in pending]
            deps = [d for d in deps if d and d > now]
            if not deps:
                continue
            until = min(deps) - now
            if until > ENRICH_WINDOW:
                continue
            every = ENRICH_HOT_EVERY if until <= ENRICH_HOT_WINDOW else ENRICH_EVERY
            last = _dt(t.get("enrichAttemptAt"))
            if last is None or now - last >= every:
                out.append(copy.deepcopy(t))
        return out

    async def mark_enrich_attempt(self, trip_id: str) -> None:
        def run():
            trip = next((t for t in self.trips if t["id"] == trip_id), None)
            if trip is not None:
                trip["enrichAttemptAt"] = self.clock().isoformat()
        await self._mutate(run)

    def due_for_refresh(self) -> list[dict]:
        now = self.clock()
        out = []
        for t in self.trips:
            if not t.get("enriched"):
                continue
            nxt = next_refresh_at(t, now, self.grace, self.running)
            if nxt is not None and nxt <= now:
                out.append(copy.deepcopy(t))
        return out

    async def apply_realtime(self, trip_id: str, results: list[dict]) -> list[dict]:
        """results: [{legIndex, status: ok, realtime} | {legIndex, status: error, error}].
        Liefert Ereignisse (einmalige Meldung "keine Echtzeit")."""
        def run():
            trip = next((t for t in self.trips if t["id"] == trip_id), None)
            if trip is None:
                return []
            now = self.clock()
            events = []
            trip["realtime"] = trip.get("realtime") or {"legs": {}}
            failed = []
            for r in results:
                if r["status"] == "ok":
                    trip["realtime"]["legs"][str(r["legIndex"])] = r["realtime"]
                else:
                    failed.append(r)
            trip["lastRefreshAt"] = now.isoformat()
            h = trip["realtimeHealth"]
            if not failed:
                h.update(consecutiveFailures=0, failing=False, lastSuccessAt=now.isoformat(), lastError=None)
                return events
            h["consecutiveFailures"] += 1
            h["lastError"] = failed[0].get("error")
            if h["consecutiveFailures"] >= self.failure_threshold:
                h["failing"] = True
                for r in failed:  # veraltete Prognosen nicht weiter anzeigen
                    trip["realtime"]["legs"].pop(str(r["legIndex"]), None)
                if not h.get("notifiedAt"):
                    h["notifiedAt"] = now.isoformat()
                    events.append({"type": "realtime_unavailable", "tripId": trip["id"], "ownerId": trip["ownerId"],
                                   "destinationName": trip["snapshot"]["destination"].get("name")})
            return events
        return await self._mutate(run)

    async def cleanup(self) -> list[dict]:
        now = self.clock()
        if not any(self._status(t, now) == EXPIRED for t in self.trips):
            return []

        def run():
            n = self.clock()
            expired = [t for t in self.trips if self._status(t, n) == EXPIRED]
            self.trips = [t for t in self.trips if t not in expired]
            return [_summary(t) for t in expired]
        return await self._mutate(run)


# --- Texte für die Person ----------------------------------------------------------

def _day(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.")


def _route(s: dict) -> str:
    return f"{s['originName']} → {s['destinationName']}"


def share_confirmation(result: dict) -> str:
    t = result["trip"]["snapshot"]
    s = {"originName": t["origin"].get("name"), "destinationName": t["destination"].get("name")}
    if result["status"] == "duplicate":
        return f"Die Reise {_route(s)} am {_day(t['plannedDeparture'])} ist bereits gespeichert."
    text = f"Reise {_route(s)} am {_day(t['plannedDeparture'])} gespeichert."
    rep = result["replaced"]
    if len(rep) == 1:
        r = rep[0]
        when = datetime.fromisoformat(r["plannedDeparture"]).strftime("%d.%m., %H:%M")
        text += f" Sie ersetzt die Reise {_route(r)} ({when})."
    elif len(rep) > 1:
        text += f" Sie ersetzt {len(rep)} Reisen im selben Zeitraum."
    return text


def share_error(code: str | None) -> str:
    return {
        "NO_CONNECTION": "Es kam nur ein Link an. Bitte im DB Navigator die Verbindung über „Teilen“ an Home Assistant senden – mit dem Text, nicht nur dem Link.",
        "NO_LEGS": "Im geteilten Text wurde keine Verbindung gefunden. Bitte die Verbindung im DB Navigator über „Teilen“ senden.",
        "NO_DATE": "Im geteilten Text fehlt das Reisedatum. Bitte die Verbindung im DB Navigator über „Teilen“ senden.",
        "BAD_LEG": "Der geteilte Text ließ sich nicht vollständig lesen. Bitte die Verbindung erneut teilen.",
        "IN_PAST": "Diese Reise ist bereits vorbei und wurde nicht gespeichert.",
    }.get(code or "", "Die Reise konnte nicht gespeichert werden. Details stehen im Protokoll von Home Assistant.")


def realtime_unavailable(event: dict) -> str:
    return f"Für die Reise nach {event.get('destinationName') or 'deinem Ziel'} sind in Home Assistant keine Echtzeitdaten verfügbar."
