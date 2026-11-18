"""Zug eines Bahn-Abschnitts in OpenBoards finden – wie BoardMatch der Android-App.

OpenBoards kennt den Plan schon Tage vorher, deshalb geht das gleich beim Teilen:

 1. Abfahrtstafel am Einstieg zur Abfahrtsminute (Bahnhofsnummern aus der
    Bahnhofsliste und dem Gleisnetz rund um den Halt); der Zug über die Nummer aus dem
    Text, sonst über die Linie.
 2. Laufweg bis zum Ausstieg aus den Via-Halten (ganzer restlicher Laufweg in
    Fahrtreihenfolge, ohne Zeiten).
 3. Zeiten der Zwischenhalte: aus dem gtfs.de-Fahrplan, sonst aus der Tafel des Halts
    (höchstens MAX_LOOKUPS_PER_LEG Abfragen), sonst geschätzt.
 4. Ankunft und Gleis am Ausstieg aus dessen Ankunftstafel.

Ergebnis: Abschnitt mit Bahnhofsnummern an den Halten, tripId = Fahrt-Kennung (journeyId),
source „openboards“. Gelingt es nicht, bleibt der Abschnitt, wie er ist.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from . import openboards as ob
from .journey import name_variants, norm_line, norm_name
from .openboards import Board, BoardSource, OpenBoardsError, iso, parse_time
from .places import Places, km
from .share_text import RAIL_MODES, classify
from .stationlist import StationList, station_point

_LOGGER = logging.getLogger(__name__)

SOURCE = "openboards"
MAX_TIME_DIFF_MIN = 3.0          # Abweichung Text ↔ Plan ohne Zugnummer
MAX_TIME_DIFF_NUMBER_MIN = 60.0  # mit passender Zugnummer (Text zeigt evtl. die Prognose)
MAX_LOOKUPS_PER_LEG = 30
SAME_STOP_KM = 1.0               # Zwischenhalt aus gtfs.de gehört zum Halt der Bahn, wenn so nah

NARROW = timedelta(minutes=10)
LOOKUP = timedelta(minutes=45)
ARRIVAL = timedelta(minutes=60)
LATE = timedelta(hours=3)

_LONG_NUMBER_RE = re.compile(r"[A-Za-z]+\s*(\d+)")

Log = Callable[[str], None]


@dataclass
class Found:
    entry: dict
    board: Board
    score: float


def line_parts(name: str | None) -> list[str]:
    """Der DB Navigator schreibt Züge mit zwei Nummern als „ICE 700 / ICE 29000“."""
    return [p.strip() for p in (name or "").split("/") if p.strip()]


def train_numbers(leg: dict) -> set[int]:
    """Zugnummern eines Abschnitts: aus dem Text und aus jedem Fernverkehrs-Teil des Namens."""
    line = leg.get("line") or {}
    out: list[int] = []
    tn = str(line.get("trainNumber") or "").strip()
    if tn.isdigit():
        out.append(int(tn))
    for part in line_parts(line.get("name")):
        if classify(part)[1] != "longdistance":
            continue
        m = _LONG_NUMBER_RE.fullmatch(part)
        if m:
            out.append(int(m.group(1)))
    return set(out)


def score(e: dict, leg: dict, when: datetime) -> float | None:
    """Wie gut passt ein Eintrag zum Abschnitt (None: gar nicht)."""
    planned = parse_time(e.get("timeSchedule"))
    if planned is None:
        return None
    line = leg.get("line") or {}
    number_ok = bool(train_numbers(leg) & ob.numbers(e))
    wanted = {norm_line(x) for x in [line.get("name"), *line_parts(line.get("name"))]} - {""}
    t = e.get("transport") or {}
    line_ok = bool(wanted) and (norm_line((t.get("line") or {}).get("name")) in wanted
                                or norm_line(t.get("journeyDescription")) in wanted)
    if not number_ok and not line_ok:
        return None
    diff = abs((planned - when).total_seconds()) / 60
    if diff > (MAX_TIME_DIFF_NUMBER_MIN if number_ok else MAX_TIME_DIFF_MIN):
        return None
    sc = (100.0 if number_ok else 0.0) + (50.0 if line_ok else 0.0) - diff
    cat = t.get("category")
    if cat and cat.lower() == (line.get("productName") or "").lower():
        sc += 10.0
    return sc


def evas_of(station: dict, places: Places | None, directory: StationList | None = None) -> list[str]:
    """Bahnhofsnummern eines Halts für die Abfrage: die eigene, dann aus der Bahnhofsliste
    (Name, Umgebung), zuletzt uic_ref aus dem Gleisnetz – das ist nur bei einem Teil der
    Bahnhöfe die EVA."""
    out: list[str] = []

    def add(items):
        for e in items:
            if e and e not in out:
                out.append(e)
    if station.get("id"):
        add([station["id"]])
    if directory is not None:
        add(directory.candidates(station))
    p = station_point(station)
    if p is not None and places is not None:
        add(places.eva_near(p))
    return [e for e in out if ob.is_eva(e)][:ob.MAX_STATIONS]


async def board_of(source: BoardSource, q: ob.BoardQuery) -> Board:
    """Tafel abfragen; weist OpenBoards eine Abfrage mit mehreren Bahnhöfen ab (HTTP 400,
    etwa eine Nummer, die es nicht kennt), noch einmal nur mit dem ersten."""
    try:
        return await source.board(q)
    except OpenBoardsError as err:
        if err.code != "BAD_REQUEST" or len(q.evas) <= 1:
            raise
        return await source.board(q.with_evas(q.evas[:1]))


async def find_departure(source: BoardSource, evas: list[str], leg: dict, when: datetime) -> Found | None:
    """Zug am Einstieg suchen: erst ±10 Minuten, mit Zugnummer auch bis 60 Minuten daneben,
    zuletzt bis 3 Stunden später. Das Zeitfenster der API gilt für die beste bekannte
    Zeit, nicht für den Plan: ein verspäteter Zug steht erst später in der Tafel (geprüft
    am 08.11.2026, ICE 515 geplant 13:58, in der Tafel ab 15:00). Bewertet wird nach der Planzeit."""
    types = ob.transport_types((leg.get("line") or {}).get("product"))
    number = bool(train_numbers(leg))
    wide = timedelta(minutes=MAX_TIME_DIFF_NUMBER_MIN)
    windows = [(when - NARROW, when + NARROW)]
    if number:
        windows += [(when - wide, when - NARROW), (when + NARROW, when + wide)]
    windows.append((when + (wide if number else NARROW), when + LATE))
    for start, end in windows:
        board = await board_of(source, ob.query(False, evas, start, end, types))
        best: Found | None = None
        for e in board.departures:
            if not e.get("journeyId"):
                continue
            sc = score(e, leg, when)
            if sc is not None and (best is None or sc > best.score):
                best = Found(e, board, sc)
        if best is not None:
            return best
    return None


def _keys(name: str | None) -> set[str]:
    if not name or not name.strip():
        return set()
    return {k for k in (norm_name(v) for v in name_variants(name)) if k}


def _station(eva: str, name: str | None, directory: StationList | None) -> dict:
    """Lage aus der Bahnhofsliste: im Gleisnetz kann dieselbe Nummer einen anderen Halt meinen."""
    p = directory.point(eva) if directory is not None else None
    return {"id": eva, "name": name, "lat": p[0] if p else None, "lon": p[1] if p else None}


async def match_leg(leg: dict, source: BoardSource, places: Places | None, directory: StationList | None = None,
                    log: Log | None = None) -> dict | None:
    """Abschnitt über OpenBoards anreichern; None, wenn der Zug nicht gefunden wird oder der
    Abschnitt nicht passt (keine Bahn, schon zugeordnet)."""
    log = log or (lambda _m: None)
    line = leg.get("line") or {}
    sos = leg.get("stopovers") or []
    if leg.get("kind") != "ride" or line.get("product") not in RAIL_MODES or leg.get("source") == SOURCE or len(sos) < 2:
        return None
    dep, arr = sos[0], sos[-1]
    dep_at = parse_time(dep.get("plannedDeparture") or leg.get("plannedDeparture"))
    arr_at = parse_time(arr.get("plannedArrival") or leg.get("plannedArrival"))
    if dep_at is None or arr_at is None:
        return None
    dep_evas = evas_of(dep["station"], places, directory)
    if not dep_evas:
        log(f"{line.get('name')}: keine Bahnhofsnummer für {dep['station'].get('name')}")
        return None
    found = await find_departure(source, dep_evas, leg, dep_at)
    if found is None:
        log(f"{line.get('name')}: nicht in der Tafel von {dep['station'].get('name')} ({', '.join(dep_evas)}) "
            f"um {dep_at.astimezone(ob.TZ).strftime('%H:%M')}")
        return None
    entry = found.entry
    journey_id = entry["journeyId"]
    types = ob.transport_types(line.get("product"))
    lookups = 0

    # Laufweg nach dem Einstieg (ohne zusätzliche Halte, die gehören nicht zum Plan)
    route: list[str] = []
    for v in ob.via(entry):
        if not v.get("additional") and v.get("evaNumber") and v["evaNumber"] not in route:
            route.append(v["evaNumber"])
    dest = (ob.destination(entry) or {}).get("evaNumber")
    if dest and dest not in route:
        route.append(dest)
    if entry.get("evaNumber") in route:
        route.remove(entry["evaNumber"])

    # Ausstieg: über die Bahnhofsnummer, sonst über den Namen
    arr_evas = evas_of(arr["station"], places, directory)
    if not arr_evas:
        log(f"{line.get('name')}: keine Bahnhofsnummer für {arr['station'].get('name')}, Ausstieg nur über den Namen")
    arr_keys = _keys(arr["station"].get("name"))
    exit_i = next((i for i, e in enumerate(route) if e in arr_evas), -1)
    if exit_i < 0:
        exit_i = next((i for i, e in enumerate(route) if norm_name(found.board.name(e)) in arr_keys), -1)
    middle_evas = route[:exit_i] if exit_i >= 0 else []

    # Ankunft am Ausstieg aus dessen Ankunftstafel
    exit_evas = [route[exit_i]] if exit_i >= 0 else list(arr_evas)
    arr_entry = None
    if exit_evas:
        lookups += 1
        board = await board_of(source, ob.query(True, exit_evas, arr_at - ARRIVAL, arr_at + ARRIVAL, types, max_via=0))
        arr_entry = board.same_journey(journey_id)
    t0 = parse_time(entry.get("timeSchedule")) or dep_at
    t1 = parse_time((arr_entry or {}).get("timeSchedule")) or arr_at

    # Zwischenhalte: Zeiten aus gtfs.de, sonst aus der Tafel des Halts, sonst geschätzt
    known = sos[1:-1]
    middle = []
    for k, eva in enumerate(middle_evas):
        name = found.board.name(eva)
        at = places.by_eva(eva) if places is not None else None
        name_keys = _keys(name)
        same = None
        for so in known:
            p = station_point(so["station"])
            if norm_name(so["station"].get("name")) in name_keys or (_keys(so["station"].get("name")) & name_keys) \
                    or (p is not None and at is not None and km(p, at) <= SAME_STOP_KM):
                same = so
                break
        if same is not None:
            s = copy.deepcopy(same)
            s["station"] = {**s["station"], "id": eva, "name": name or s["station"].get("name")}
            middle.append(s)
            continue
        # auf Millisekunden wie in der App
        span_ms = (t1 - t0) // timedelta(milliseconds=1)
        estimate = t0 + timedelta(milliseconds=int(span_ms * (k + 1.0) / (len(middle_evas) + 1)))
        est = estimate.astimezone(ob.TZ).isoformat()
        hit = None
        if lookups < MAX_LOOKUPS_PER_LEG:
            lookups += 1
            board = await source.board(ob.query(False, [eva], estimate - LOOKUP, estimate + LOOKUP, types, max_via=0))
            hit = board.same_journey(journey_id)
        if hit is not None:
            middle.append({"station": _station(eva, name, directory), "plannedArrival": iso(hit.get("timeSchedule")),
                           "plannedDeparture": iso(hit.get("timeSchedule")), "plannedPlatform": ob.platform(hit, "platformSchedule"),
                           "estimated": False})
        else:
            middle.append({"station": _station(eva, name, directory), "plannedArrival": est, "plannedDeparture": est,
                           "plannedPlatform": None, "estimated": True})

    first = {
        "station": {**dep["station"], "id": entry.get("evaNumber") or dep_evas[0]},
        "plannedArrival": None,
        "plannedDeparture": iso(entry.get("timeSchedule")) or dep.get("plannedDeparture"),
        "plannedPlatform": ob.platform(entry, "platformSchedule") or dep.get("plannedPlatform"),
    }
    last = {
        "station": {**arr["station"], "id": (arr_entry or {}).get("evaNumber") or (exit_evas[0] if exit_evas else None)
                    or arr["station"].get("id")},
        "plannedArrival": iso((arr_entry or {}).get("timeSchedule")) or arr.get("plannedArrival"),
        "plannedDeparture": None,
        "plannedPlatform": ob.platform(arr_entry or {}, "platformSchedule") or arr.get("plannedPlatform"),
    }
    stopovers = [first, *middle, last]
    number = (entry.get("transport") or {}).get("journeyNumber")
    out = copy.deepcopy(leg)
    out.pop("iris", None)
    out.update({
        "tripId": journey_id,
        "line": {**line, "trainNumber": line.get("trainNumber") or (str(number) if number is not None else None)},
        "origin": first["station"],
        "destination": last["station"],
        "plannedDeparture": first["plannedDeparture"],
        "plannedArrival": last["plannedArrival"],
        "plannedDeparturePlatform": first["plannedPlatform"],
        "plannedArrivalPlatform": last["plannedPlatform"],
        "stopovers": stopovers,
        "matched": True,
        "source": SOURCE,
    })
    return out


def pending(snapshot: dict) -> bool:
    """Hat die Reise Bahn-Abschnitte, die noch zugeordnet werden können?"""
    return any(l.get("kind") == "ride" and (l.get("line") or {}).get("product") in RAIL_MODES and l.get("source") != SOURCE
               for l in snapshot.get("legs") or [])


async def match_snapshot(snapshot: dict, source: BoardSource, places: Places | None, directory: StationList | None = None,
                         log: Log | None = None) -> dict | None:
    """Alle noch nicht zugeordneten Bahn-Abschnitte einer Reise; None, wenn sich nichts
    geändert hat. Zugangs- und Kontingentfehler gehen weiter, andere Fehler lassen nur
    den einen Abschnitt, wie er ist."""
    log = log or (lambda m: _LOGGER.info("%s", m))
    changed = False
    legs = []
    for leg in snapshot.get("legs") or []:
        try:
            m = await match_leg(leg, source, places, directory, log)
        except OpenBoardsError as err:
            if err.code in ("BAD_CREDENTIALS", "RATE_LIMITED"):
                raise
            log(f"{(leg.get('line') or {}).get('name')}: {err}")
            m = None
        if m is not None:
            changed = True
        legs.append(m if m is not None else leg)
    if not changed:
        return None
    rides = [l for l in legs if l.get("kind") == "ride"]
    out = {**snapshot, "legs": legs}
    out["plannedDeparture"] = (rides[0].get("plannedDeparture") if rides else None) or snapshot.get("plannedDeparture")
    out["plannedArrival"] = (rides[-1].get("plannedArrival") if rides else None) or snapshot.get("plannedArrival")
    return out
