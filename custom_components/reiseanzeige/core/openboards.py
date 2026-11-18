"""OpenBoards (DB InfraGO, DB API Marketplace): Abfahrts- und Ankunftstafeln, Nachfolger
der Timetables-API. Gleich wie in der Android-App (core/realtime/OpenBoards.kt,
OpenBoardsClient.kt), die gegen dieselben aufgezeichneten Tafeln testet.

Je Bahnhof (bis zu 15 Nummern) und Zeitfenster (höchstens 8 Stunden): Plan- und beste
bekannte Zeit mit Art (Plan, Prognose, tatsächlich), Gleise, die Fahrt-Kennung
(journeyId), Ausfälle, zusätzliche Halte, der restliche Laufweg (Via-Halte, ohne
Zeiten) und Meldungen. Der Plan steht schon Tage vorher bereit (mindestens einen
Monat, geprüft am 08.11.2026).

    POST /departures  bzw.  /arrivals   (JSON-Abfrage, siehe body())

Antworten bleiben JSON (dict); Board liefert, was die Auswertung braucht.
Sparsam: höchstens RATE_LIMIT Abfragen pro Minute und mindestens MIN_GAP Sekunden
zwischen zwei Abfragen (erlaubt sind 10 pro Sekunde; ob je Anwendung oder je Konto,
sagt das Portal nicht – App und Integration zusammen bleiben so darunter). Nach „zu
viele Anfragen“ (429) PAUSE Sekunden lang keine Abfrage. Gleiche Abfragen innerhalb
von CACHE_TTL teilen sich eine Antwort. Werte wie in der App.
"""

from __future__ import annotations

import asyncio
import json
import re
import time as _time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
BASE_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/open-boards/v1"
MAX_WINDOW = timedelta(hours=8)
MAX_STATIONS = 15
MAX_ENTRIES = 50
RATE_LIMIT = 60                 # Abfragen pro Minute, wie in der App
MIN_GAP = 0.25                  # Sekunden zwischen zwei Abfragen: höchstens 4 pro Sekunde
PAUSE = 60.0                    # Sekunden ohne Abfrage nach HTTP 429
TRANSIENT = ("RATE_LIMITED", "UNAVAILABLE", "BAD_RESPONSE")   # vorübergehend: bald neu versuchen
CACHE_TTL = 60                  # Sekunden
REQUEST_TIMEOUT = 30

TIME_SCHEDULE = "SCHEDULE"
TIME_PREVIEW = "PREVIEW"
TIME_REAL = "REAL"

# Bahnhofsnummer, wie OpenBoards sie kennt: 7 Ziffern. Im Gleisnetz stehen bei Bus-
# und Tramhalten auch 6- oder 8-stellige Nummern (geprüft am 08.11.2026).
_EVA_RE = re.compile(r"\d{7}")

# Zugarten der API je Verkehrsmittel, großzügig: die Einteilungen decken sich nicht ganz
_TYPES = {
    "longdistance": ["HIGH_SPEED_TRAIN", "INTERCITY_TRAIN", "INTER_REGIONAL_TRAIN"],
    "regional": ["REGIONAL_TRAIN", "INTER_REGIONAL_TRAIN", "CITY_TRAIN"],
    "suburban": ["CITY_TRAIN", "REGIONAL_TRAIN"],
}


def is_eva(value) -> bool:
    return isinstance(value, str) and _EVA_RE.fullmatch(value) is not None


def transport_types(product: str | None) -> list[str] | None:
    return list(_TYPES[product]) if product in _TYPES else None


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def iso(value: str | None) -> str | None:
    """ISO-Zeit in Berliner Ortszeit wie die Snapshots (2026-11-12T08:01:00+01:00)."""
    t = parse_time(value)
    return t.astimezone(TZ).isoformat() if t else None


class OpenBoardsError(Exception):
    """code: BAD_CREDENTIALS | RATE_LIMITED | UNAVAILABLE | BAD_RESPONSE | BAD_REQUEST"""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class BoardQuery:
    """Ab- oder Ankünfte an diesen Bahnhöfen im Zeitfenster [start, end]."""
    arrivals: bool
    evas: tuple[str, ...]
    start: datetime
    end: datetime
    types: tuple[str, ...] | None = None
    max_via: int = 50
    entries: int = MAX_ENTRIES

    def with_evas(self, evas) -> "BoardQuery":
        return replace(self, evas=tuple(evas))


def query(arrivals: bool, evas, start: datetime, end: datetime, types=None, max_via: int = 50,
          entries: int = MAX_ENTRIES) -> BoardQuery:
    return BoardQuery(arrivals, tuple(evas), start, end, tuple(types) if types else None, max_via, entries)


def body(q: BoardQuery) -> dict:
    end = min(q.end, q.start + MAX_WINDOW)
    out = {
        "stopPlaceOrStation": {"evaNumbers": list(q.evas[:MAX_STATIONS])},
        "timeWindow": {"timeStart": q.start.isoformat(), "timeEnd": end.isoformat()},
    }
    if q.types:
        out["transportTypes"] = list(q.types)
    out["maxViaStops"] = q.max_via
    out["maxEntriesPerTransportType"] = min(q.entries, MAX_ENTRIES)
    out["languages"] = ["deu"]
    return out


# --- Einträge ------------------------------------------------------------------

def prognosed(e: dict) -> str | None:
    """Zeit mit Prognose bzw. tatsächlich; None, solange nur der Plan bekannt ist."""
    tt = e.get("timeType")
    return None if tt in (None, TIME_SCHEDULE) else e.get("time")


def numbers(e: dict) -> set[int]:
    """Zugnummern, unter denen die Fahrt hier und an ihrem Start läuft."""
    info = e.get("journeyInfo") or {}
    out = {(e.get("transport") or {}).get("journeyNumber"), (info.get("transportAtStart") or {}).get("journeyNumber"),
           info.get("headerJourneyNumber")}
    return {n for n in out if isinstance(n, int)}


def journey_cancelled(e: dict) -> bool:
    return bool((e.get("journeyInfo") or {}).get("journeyCancelled"))


def platform(e: dict, key: str = "platform") -> str | None:
    return (e.get(key) or {}).get("name")


def via(e: dict) -> list[dict]:
    return list((e.get("transport") or {}).get("via") or [])


def destination(e: dict) -> dict | None:
    return (e.get("journeyInfo") or {}).get("destination")


def line_name(e: dict) -> str:
    """Name der Fahrt wie auf der Tafel: Linie (RE9, S3), sonst Gattung und Nummer (ICE 612)."""
    t = e.get("transport") or {}
    name = ((t.get("line") or {}).get("name") or "").strip()
    if name:
        return name
    desc = (t.get("journeyDescription") or "").strip()
    if desc:
        return desc
    joined = " ".join(str(p) for p in (t.get("category"), t.get("journeyNumber")) if p is not None).strip()
    return joined or "?"


# Meldungen: Art je Liste der Antwort
DISRUPTION, NOTE, CAUSE, QUALITY = "disruption", "note", "cause", "quality"
_MESSAGE_LISTS = ((DISRUPTION, "disruptions"), (NOTE, "notes"), (CAUSE, "risCauseCodes"), (QUALITY, "risQualityDeviations"))


def _text(msg: dict) -> str | None:
    descs = msg.get("descriptions") or []
    d = next((x for x in descs if x.get("language") == "deu"), descs[0] if descs else None)
    if not d:
        return None
    t = d.get("text") or d.get("textShort")
    return t if t and t.strip() else None


class Board:
    """Antwort einer Abfrage."""

    def __init__(self, data: dict | None = None) -> None:
        data = data or {}
        self.data = data
        self.departures: list[dict] = list(data.get("departures") or [])
        self.arrivals: list[dict] = list(data.get("arrivals") or [])
        self._names: dict[str, str] = {}
        for sp in data.get("stopPlaces") or []:
            names = sp.get("localizedNames") or []
            n = next((x for x in names if x.get("language") == "deu"), names[0] if names else None)
            if sp.get("evaNumber") and n and n.get("name"):
                self._names.setdefault(sp["evaNumber"], n["name"])

    @property
    def entries(self) -> list[dict]:
        return self.departures + self.arrivals

    def name(self, eva: str | None) -> str | None:
        return self._names.get(eva) if eva else None

    def texts(self, ids: list[int] | None) -> list[tuple[str, str | None, str]]:
        """Meldungen zu den IDs eines Eintrags: (Art, Code, Text)."""
        m = self.data.get("messages") or {}
        out = []
        for i in ids or []:
            for kind, key in _MESSAGE_LISTS:
                msg = next((x for x in m.get(key) or [] if x.get("messageID") == i), None)
                if msg is None:
                    continue
                t = _text(msg)
                if t:
                    out.append((kind, msg.get("code"), t))
        return out

    def same_journey(self, journey_id: str) -> dict | None:
        return next((e for e in self.entries if e.get("journeyId") == journey_id), None)


def parse(text: str) -> Board:
    try:
        data = json.loads(text.lstrip("﻿"))
    except ValueError as err:
        raise OpenBoardsError("BAD_RESPONSE", f"Antwort von OpenBoards unlesbar: {err}") from err
    if not isinstance(data, dict):
        raise OpenBoardsError("BAD_RESPONSE", "Antwort von OpenBoards ohne Tafel")
    return Board(data)


# --- Abfragen -------------------------------------------------------------------

class BoardSource(Protocol):
    async def board(self, q: BoardQuery) -> Board: ...


class RateLimiter:
    """Höchstens `limit` Aufrufe je `window` Sekunden (gleitendes Fenster) und mindestens
    `min_gap` Sekunden zwischen zwei Aufrufen (Standard: MIN_GAP)."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = 60.0,
                 clock: Callable[[], float] = _time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 min_gap: float | None = None) -> None:
        self.limit, self.window, self.clock, self.sleep = limit, window, clock, sleep
        self.min_gap = MIN_GAP if min_gap is None else min_gap
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self.clock()
                while self._stamps and now - self._stamps[0] >= self.window:
                    self._stamps.popleft()
                if len(self._stamps) >= self.limit:
                    wait = self.window - (now - self._stamps[0]) + 0.01
                elif self._stamps and now - self._stamps[-1] < self.min_gap:
                    wait = self.min_gap - (now - self._stamps[-1])
                else:
                    self._stamps.append(now)
                    return
                await self.sleep(wait)


# (Adresse, Kopfzeilen, Inhalt) → (HTTP-Status, Text)
Post = Callable[[str, dict, dict], Awaitable[tuple[int, str]]]


def aiohttp_post(session) -> Post:
    """Abfrage über eine aiohttp-Sitzung (in Home Assistant: async_get_clientsession)."""
    import aiohttp

    async def post(url: str, headers: dict, payload: dict) -> tuple[int, str]:
        async with session.post(url, headers=headers, json=payload,
                                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as res:
            return res.status, await res.text()
    return post


def _reason(text: str) -> str:
    t = " ".join((text or "").split())
    return f": {t[:160]}" if t else ""


class OpenBoardsClient:
    """Fehler: 401/403 → BAD_CREDENTIALS (falsche Daten oder OpenBoards nicht abonniert),
    429 → RATE_LIMITED, 400 → BAD_REQUEST (mit Begründung der API), 404 → leere Tafel,
    Netz und 5xx → UNAVAILABLE."""

    def __init__(self, post: Post, client_id: str, api_key: str, *, base_url: str = BASE_URL,
                 limiter: RateLimiter | None = None, clock: Callable[[], float] = _time.monotonic) -> None:
        self._post = post
        self._headers = {"DB-Client-ID": client_id, "DB-Api-Key": api_key, "Accept": "application/json"}
        self._base = base_url
        self._limiter = limiter or RateLimiter()
        self._clock = clock
        self._cache: dict[str, tuple[float, Board]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._paused_until: float | None = None
        self.calls = 0  # für die Diagnose

    async def board(self, q: BoardQuery) -> Board:
        payload = body(q)
        key = ("an|" if q.arrivals else "ab|") + json.dumps(payload, sort_keys=True)
        now = self._clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
        fut = self._pending.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        try:
            result = await self._request(q.arrivals, payload)
            fut.set_result(result)
        except BaseException as err:
            fut.set_exception(err)
            fut.exception()   # als abgerufen markieren, falls niemand wartet
            raise
        finally:
            self._pending.pop(key, None)
        self._cache[key] = (self._clock(), result)
        for k in [k for k, v in self._cache.items() if self._clock() - v[0] >= CACHE_TTL]:
            del self._cache[k]
        return result

    def paused(self) -> float:
        """Sekunden, die die Pause nach HTTP 429 noch dauert (0: keine)."""
        return max(0.0, self._paused_until - self._clock()) if self._paused_until is not None else 0.0

    async def _request(self, arrivals: bool, payload: dict) -> Board:
        if (left := self.paused()) > 0:
            raise OpenBoardsError("RATE_LIMITED", f"OpenBoards: Pause nach zu vielen Anfragen, noch {left:.0f} s", 429)
        await self._limiter.acquire()
        self.calls += 1
        url = f"{self._base}/{'arrivals' if arrivals else 'departures'}"
        try:
            status, text = await self._post(url, self._headers, payload)
        except Exception as err:  # noqa: BLE001 – Netzfehler jeder Art
            raise OpenBoardsError("UNAVAILABLE", f"OpenBoards nicht erreichbar: {err or type(err).__name__}") from err
        if status == 200:
            return parse(text)
        if status == 404:
            return Board()
        if status in (401, 403):
            raise OpenBoardsError("BAD_CREDENTIALS", "Zugangsdaten werden von OpenBoards nicht akzeptiert", status)
        if status == 429:
            self._paused_until = self._clock() + PAUSE
            raise OpenBoardsError("RATE_LIMITED", "OpenBoards: zu viele Anfragen", status)
        if status == 400:
            raise OpenBoardsError("BAD_REQUEST", "OpenBoards: HTTP 400" + _reason(text), status)
        raise OpenBoardsError("UNAVAILABLE", f"OpenBoards: HTTP {status}", status)

    async def check(self, eva: str, now: datetime | None = None) -> None:
        """Zugangsdaten mit einer einzigen Abfrage prüfen: nächste Abfahrt an eva."""
        now = now or datetime.now(TZ)
        payload = body(query(False, [eva], now, now + timedelta(minutes=30), max_via=0, entries=1))
        await self._request(False, payload)

    def clear(self) -> None:
        self._cache.clear()
