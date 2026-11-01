"""Offizielle Timetables-API der DB (DB API Marketplace).

Plan "Free": 60 Aufrufe pro Minute, Daten unter CC BY 4.0
(Quelle: Deutsche Bahn AG). Jede Installation nutzt eigene Zugangsdaten.

    /station/{muster}          Bahnhof suchen → EVA-Nummer
    /plan/{eva}/{JJMMTT}/{HH}  Sollfahrplan einer Stunde
    /fchg/{eva}                alle bekannten Änderungen (Verspätung, Gleis, Ausfall)

Sparsam: Jede Fahrplan-Stunde eines Bahnhofs wird nur einmal geholt, die
Änderungen eines Bahnhofs höchstens alle CHANGES_TTL, gleichzeitige Anfragen
für dasselbe teilen sich einen Abruf, und ein Ratenbegrenzer hält die Zahl
der Aufrufe sicher unter dem erlaubten Kontingent.

Ein Halt ("s") hat eine ID der Form "<Fahrt>-<JJMMTTHHmm>-<Nr>": der vordere
Teil bezeichnet die Fahrt eines Tages und ist an allen Bahnhöfen gleich,
die Nummer ist die Position des Halts im Zuglauf.
"""

from __future__ import annotations

import asyncio
import copy
import time as _time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
BASE_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
CHANGES_TTL = 3 * 60            # Sekunden
PLAN_TTL = 6 * 60 * 60
STATION_TTL = 7 * 24 * 60 * 60
RATE_LIMIT = 50                 # Aufrufe pro Minute (Kontingent: 60)
REQUEST_TIMEOUT = 15

LONG_DISTANCE = {"ICE", "IC", "EC", "ECE", "EN", "NJ", "RJ", "RJX", "TGV", "FLX", "D", "EST", "WB"}

# Nachrichtentypen, die Fahrgäste betreffen: h = HIM-Störung, f = Freitext,
# d = Verspätungsgrund, q = Qualitätsinfo
PASSENGER_MESSAGES = {"h", "f", "d", "q"}

Fetch = Callable[[str, dict], Awaitable[tuple[int, str]]]


class TimetablesError(Exception):
    """code: BAD_CREDENTIALS | RATE_LIMITED | UNAVAILABLE | NOT_FOUND | UNEXPECTED"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- Zeit: IRIS liefert JJMMTTHHmm in deutscher Ortszeit -----------------------

def parse_iris_time(value: str | None) -> datetime | None:
    if not value or len(value) != 10 or not value.isdigit():
        return None
    try:
        return datetime(2000 + int(value[0:2]), int(value[2:4]), int(value[4:6]),
                        int(value[6:8]), int(value[8:10]), tzinfo=TZ)
    except ValueError:
        return None


def iris_time(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%y%m%d%H%M")


def berlin_slice(dt: datetime) -> tuple[str, str]:
    """Stunde in Ortszeit für /plan: ("261008", "07")."""
    local = dt.astimezone(TZ)
    return local.strftime("%y%m%d"), local.strftime("%H")


# --- Auswertung ----------------------------------------------------------------

@dataclass
class Event:
    """Ankunft (ar) oder Abfahrt (dp) an einem Halt."""
    planned: datetime | None = None
    changed: datetime | None = None
    planned_platform: str | None = None
    changed_platform: str | None = None
    line: str | None = None
    planned_path: list[str] = field(default_factory=list)
    changed_path: list[str] | None = None
    planned_status: str | None = None   # "p" geplant, "a" zusätzlich, "c" entfällt
    changed_status: str | None = None
    hidden: bool = False
    messages: list[dict] = field(default_factory=list)

    @property
    def cancelled(self) -> bool:
        return self.changed_status == "c" or self.planned_status == "c"

    @property
    def path(self) -> list[str]:
        return self.changed_path if self.changed_path is not None else self.planned_path


@dataclass
class Stop:
    """Ein Halt einer Fahrt an einem Bahnhof (Element "s")."""
    id: str
    eva: str | None = None
    category: str | None = None   # tl/@c: ICE, RE, S …
    number: str | None = None     # tl/@n: Zugnummer
    operator: str | None = None   # tl/@o
    filter: str | None = None     # tl/@f: F Fern, D Regio, S S-Bahn
    arrival: Event | None = None
    departure: Event | None = None
    messages: list[dict] = field(default_factory=list)

    @property
    def trip_key(self) -> str:
        """Fahrt-Kennung (ohne Haltnummer), an allen Bahnhöfen gleich."""
        return self.id.rsplit("-", 1)[0] if "-" in self.id.lstrip("-") else self.id

    @property
    def index(self) -> int | None:
        tail = self.id.rsplit("-", 1)[-1]
        return int(tail) if tail.isdigit() else None


def _split_path(value: str | None) -> list[str]:
    return [p for p in (value or "").split("|") if p]


def _messages(el: ET.Element) -> list[dict]:
    out = []
    for m in el.findall("m"):
        out.append({
            "id": m.get("id"), "type": m.get("t"), "code": m.get("c"),
            "text": m.get("ext") or m.get("txt"), "from": m.get("from"), "to": m.get("to"),
            "category": m.get("cat"), "priority": m.get("pr"),
        })
    return out


def _event(el: ET.Element | None) -> Event | None:
    if el is None:
        return None
    cpth = el.get("cpth")
    return Event(
        planned=parse_iris_time(el.get("pt")),
        changed=parse_iris_time(el.get("ct")),
        planned_platform=el.get("pp"),
        changed_platform=el.get("cp"),
        line=el.get("l"),
        planned_path=_split_path(el.get("ppth")),
        changed_path=_split_path(cpth) if cpth is not None else None,
        planned_status=el.get("ps"),
        changed_status=el.get("cs"),
        hidden=el.get("hi") == "1",
        messages=_messages(el),
    )


def parse_timetable(xml: str) -> tuple[str | None, list[Stop]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as err:
        raise TimetablesError("UNEXPECTED", f"Antwort der Timetables-API unlesbar: {err}") from err
    if root.tag != "timetable":
        raise TimetablesError("UNEXPECTED", "Antwort der Timetables-API ohne timetable")
    stops = []
    for s in root.findall("s"):
        tl = s.find("tl")
        stops.append(Stop(
            id=s.get("id", ""),
            eva=s.get("eva") or root.get("eva"),
            category=tl.get("c") if tl is not None else None,
            number=tl.get("n") if tl is not None else None,
            operator=tl.get("o") if tl is not None else None,
            filter=tl.get("f") if tl is not None else None,
            arrival=_event(s.find("ar")),
            departure=_event(s.find("dp")),
            messages=_messages(s),
        ))
    return root.get("station"), stops


@dataclass
class Station:
    eva: str
    name: str
    ds100: str | None = None
    meta: list[str] = field(default_factory=list)  # weitere EVA-Nummern desselben Bahnhofs


def parse_stations(xml: str) -> list[Station]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as err:
        raise TimetablesError("UNEXPECTED", f"Antwort der Timetables-API unlesbar: {err}") from err
    return [
        Station(eva=s.get("eva", ""), name=s.get("name", ""), ds100=s.get("ds100"),
                meta=_split_path(s.get("meta")))
        for s in root.iter("station") if s.get("eva")
    ]


def line_name(stop: Stop, event: Event | None = None) -> str:
    """Anzeigename: Fernverkehr mit Zugnummer ("ICE 1604"), sonst Linie ("RE16", "S1").

    Die API liefert die Linie mal ohne ("16"), mal mit Gattung ("RE16").
    """
    c = stop.category or ""
    ev = event or stop.departure or stop.arrival
    l = (ev.line if ev else None) or ""
    if l and c not in LONG_DISTANCE:
        if c and l.upper().startswith(c.upper()):
            return l
        return f"{c}{l}" if l[:1].isdigit() else f"{c} {l}".strip()
    return f"{c} {stop.number or ''}".strip()


def merge_changes(plan: list[Stop], changes: list[Stop]) -> dict[str, Stop]:
    """Änderungen in die Planhalte übernehmen (nach ID). Zusatzhalte (nur in den
    Änderungen, mit tl) kommen dazu. Ergebnis: ID → Halt mit geplanten und
    geänderten Werten."""
    # Kopien: die Planhalte liegen im Zwischenspeicher und dürfen nicht verändert werden
    out: dict[str, Stop] = {s.id: copy.deepcopy(s) for s in plan}
    for c in changes:
        base = out.get(c.id)
        if base is None:
            if c.category is None and c.number is None:
                continue  # Änderung ohne bekannten Plan und ohne Zugangaben
            out[c.id] = copy.deepcopy(c)
            continue
        for attr in ("arrival", "departure"):
            pe, ce = getattr(base, attr), getattr(c, attr)
            if ce is None:
                continue
            if pe is None:
                setattr(base, attr, ce)
                continue
            pe.changed = ce.changed or pe.changed
            pe.changed_platform = ce.changed_platform or pe.changed_platform
            pe.changed_status = ce.changed_status or pe.changed_status
            if ce.changed_path is not None:
                pe.changed_path = ce.changed_path
            pe.messages = ce.messages or pe.messages
        base.messages = c.messages or base.messages
    return out


# --- Ratenbegrenzer ------------------------------------------------------------

class RateLimiter:
    """Höchstens `limit` Aufrufe je `window` Sekunden (gleitendes Fenster)."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = 60.0,
                 clock: Callable[[], float] = _time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.limit, self.window, self.clock, self.sleep = limit, window, clock, sleep
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self.clock()
                while self._stamps and now - self._stamps[0] >= self.window:
                    self._stamps.popleft()
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    return
                await self.sleep(self.window - (now - self._stamps[0]) + 0.01)


# --- Client --------------------------------------------------------------------

def aiohttp_fetch(session) -> Fetch:
    """Abruf über eine aiohttp-Sitzung (in Home Assistant: async_get_clientsession)."""
    import aiohttp

    async def fetch(url: str, headers: dict) -> tuple[int, str]:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as res:
            return res.status, await res.text()
    return fetch


class TimetablesClient:
    def __init__(self, fetch: Fetch, client_id: str, api_key: str, *,
                 base_url: str = BASE_URL, clock: Callable[[], float] = _time.time,
                 limiter: RateLimiter | None = None) -> None:
        self._fetch = fetch
        self._headers = {"DB-Client-Id": client_id, "DB-Api-Key": api_key, "Accept": "application/xml"}
        self._base = base_url
        self._clock = clock
        self._limiter = limiter or RateLimiter()
        self._plans: dict[str, tuple[float, list[Stop]]] = {}
        self._changes: dict[str, tuple[float, list[Stop]]] = {}
        self._stations: dict[str, tuple[float, list[Station]]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self.calls = 0  # Zähler für Diagnose

    async def _get(self, path: str) -> str | None:
        await self._limiter.acquire()
        self.calls += 1
        try:
            status, body = await self._fetch(self._base + path, self._headers)
        except Exception as err:  # noqa: BLE001 – Netzfehler jeder Art
            raise TimetablesError("UNAVAILABLE", f"Timetables-API nicht erreichbar: {err}") from err
        if status in (401, 403):
            raise TimetablesError("BAD_CREDENTIALS", "Zugangsdaten der Timetables-API werden nicht akzeptiert")
        if status == 404:
            return None
        if status == 429:
            raise TimetablesError("RATE_LIMITED", "Timetables-API: zu viele Anfragen")
        if status >= 400:
            raise TimetablesError("UNAVAILABLE", f"Timetables-API: HTTP {status}")
        return body

    async def _once(self, key: str, factory: Callable[[], Awaitable]):
        """Gleichzeitige Anfragen für dasselbe teilen sich einen Abruf."""
        fut = self._pending.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[key] = fut
        try:
            result = await factory()
            fut.set_result(result)
            return result
        except BaseException as err:
            fut.set_exception(err)
            fut.exception()  # als abgerufen markieren, falls niemand wartet
            raise
        finally:
            self._pending.pop(key, None)

    async def stations(self, query: str) -> list[Station]:
        q = query.strip()
        hit = self._stations.get(q.lower())
        if hit and self._clock() - hit[0] < STATION_TTL:
            return hit[1]

        async def load():
            xml = await self._get(f"/station/{quote(q, safe='')}")
            return parse_stations(xml) if xml else []
        found = await self._once(f"station|{q.lower()}", load)
        self._stations[q.lower()] = (self._clock(), found)
        return found

    async def plan(self, eva: str, when: datetime) -> list[Stop]:
        day, hour = berlin_slice(when)
        key = f"{eva}|{day}|{hour}"
        now = self._clock()
        hit = self._plans.get(key)
        if hit and now - hit[0] < PLAN_TTL:
            return hit[1]

        async def load():
            xml = await self._get(f"/plan/{eva}/{day}/{hour}")
            return parse_timetable(xml)[1] if xml else []
        stops = await self._once(f"plan|{key}", load)
        self._plans[key] = (now, stops)
        for k in [k for k, v in self._plans.items() if now - v[0] > PLAN_TTL]:
            del self._plans[k]
        return stops

    async def changes(self, eva: str, *, max_age: float = CHANGES_TTL) -> tuple[float, list[Stop]]:
        """Änderungen eines Bahnhofs: (Abrufzeitpunkt, Halte)."""
        now = self._clock()
        hit = self._changes.get(eva)
        if hit and now - hit[0] < max_age:
            return hit

        async def load():
            xml = await self._get(f"/fchg/{eva}")
            return parse_timetable(xml)[1] if xml else []
        stops = await self._once(f"fchg|{eva}", load)
        entry = (self._clock(), stops)
        self._changes[eva] = entry
        return entry

    def clear_changes(self) -> None:
        self._changes.clear()
