"""Reise aus dem geteilten Text und dem Fahrplan der Timetables-API zusammensetzen.

Zwei Stufen:
  1. draft_snapshot(): sofort aus dem Text – Ein- und Ausstieg je Abschnitt,
     ohne Bahnhofsnummern. Damit kann die Reise gespeichert und bestätigt
     werden, bevor irgendeine Anfrage läuft.
  2. build_snapshot(): für jeden Bahn-Abschnitt den Zug im Fahrplan des
     Abfahrtsbahnhofs suchen (Zugnummer, sonst Linie + Uhrzeit), daraus die
     Fahrt-Kennung und den Zuglauf ablesen und die Zwischenhalte mit Zeiten
     ergänzen. Tram, U-Bahn und Bus kennt die API nicht; sie bleiben bei
     Ein- und Ausstieg aus dem Text.

Das Ergebnis hat das Format, das die Karte erwartet (siehe frontend
view-model.js): legs[].stopovers[] mit station {id, name, lat, lon} und
geplanten Zeiten als ISO-Text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .share_text import SharedJourney, SharedLeg
from .timetables import Station, Stop, TimetablesClient, TimetablesError, line_name

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2
MAX_TIME_DIFF_MIN = 3          # Abweichung Text ↔ Fahrplan ohne Zugnummer
MAX_TIME_DIFF_NUMBER_MIN = 60  # mit passender Zugnummer (Text zeigt evtl. Prognose)
MAX_LOOKUPS_PER_LEG = 30       # Obergrenze für Abfragen zu Zwischenhalten je Abschnitt


class StationDirectory(Protocol):
    """Liefert Koordinaten zu Bahnhöfen (EVA-Nummer, sonst Name)."""

    def coords(self, eva: str | None, name: str) -> tuple[float, float] | None: ...


class NoCoordinates:
    def coords(self, eva, name):
        return None


# --- Namen vergleichen -----------------------------------------------------------

_SUFFIX_RE = re.compile(r"\s*\((?:s|u|s\+u|u\+s|bus|tram|str)\)\s*", re.I)


def norm_name(name: str | None) -> str:
    """Für Vergleiche: Kleinschreibung, ß→ss, Zusätze wie "(S)" weg, Satzzeichen weg."""
    s = (name or "").lower().replace("ß", "ss")
    s = _SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


def norm_line(line: str | None) -> str:
    return re.sub(r"\s+", "", (line or "").upper())


def name_variants(name: str) -> list[str]:
    """Suchbegriffe für die Bahnhofssuche, vom genauesten zum gröbsten."""
    out = [name.strip()]
    plain = _SUFFIX_RE.sub(" ", name).strip()
    plain = re.sub(r"\s+,", ",", " ".join(plain.split()))
    if plain not in out:
        out.append(plain)
    # "Heidelberger Platz, Berlin" → "Berlin Heidelberger Platz"
    m = re.match(r"^(.+?),\s*([^,(]+?)(?:\s*\([^)]*\))?$", plain)
    if m:
        alt = f"{m.group(2).strip()} {m.group(1).strip()}"
        if alt not in out:
            out.append(alt)
    return out


# --- Hilfen ------------------------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _station(eva: str | None, name: str, directory: StationDirectory) -> dict:
    c = directory.coords(eva, name)
    return {"id": eva, "name": name, "lat": c[0] if c else None, "lon": c[1] if c else None}


def _platform(stop_platform: str | None, text_platform: str | None) -> str | None:
    return stop_platform or text_platform


def _line_info(leg: SharedLeg, stop: Stop | None = None) -> dict:
    return {
        "name": leg.line,
        "trainNumber": leg.number or (stop.number if stop else None),
        "productName": leg.category,
        "product": leg.mode,
        "operator": stop.operator if stop else None,
    }


def _text_leg(index: int, leg: SharedLeg, directory: StationDirectory) -> dict:
    """Abschnitt nur aus dem Text: Ein- und Ausstieg."""
    origin = _station(None, leg.dep.name, directory)
    destination = _station(None, leg.arr.name, directory)
    return {
        "kind": "ride",
        "index": index,
        "tripId": None,
        "line": _line_info(leg),
        "replacementService": leg.category in ("SEV", "EV"),
        "direction": leg.direction,
        "origin": origin,
        "destination": destination,
        "plannedDeparture": _iso(leg.dep.when),
        "plannedArrival": _iso(leg.arr.when),
        "plannedDeparturePlatform": leg.dep.platform,
        "plannedArrivalPlatform": leg.arr.platform,
        "stopovers": [
            {"station": origin, "plannedArrival": None, "plannedDeparture": _iso(leg.dep.when), "plannedPlatform": leg.dep.platform},
            {"station": destination, "plannedArrival": _iso(leg.arr.when), "plannedDeparture": None, "plannedPlatform": leg.arr.platform},
        ],
        "polyline": None,
        "remarks": [{"code": None, "type": "status", "text": t} for t in leg.extra],
        "matched": False,
        "source": "text",
        "iris": None,
    }


def _envelope(journey: SharedJourney, legs: list[dict], now: datetime) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": now.isoformat(),
        "source": "share-text",
        "vbid": journey.vbid,
        "origin": legs[0]["origin"] | {"name": journey.origin},
        "destination": legs[-1]["destination"] | {"name": journey.destination},
        "plannedDeparture": legs[0]["plannedDeparture"],
        "plannedArrival": legs[-1]["plannedArrival"],
        "legs": legs,
    }


def draft_snapshot(journey: SharedJourney, now: datetime, directory: StationDirectory | None = None) -> dict:
    directory = directory or NoCoordinates()
    return _envelope(journey, [_text_leg(i, l, directory) for i, l in enumerate(journey.legs)], now)


# --- Zug im Fahrplan finden ----------------------------------------------------------

@dataclass
class Match:
    eva: str
    stop: Stop
    score: float


def _score(stop: Stop, leg: SharedLeg, event_attr: str, when: datetime) -> float | None:
    ev = getattr(stop, event_attr)
    if ev is None or ev.planned is None:
        return None
    number_ok = bool(leg.number and stop.number == leg.number)
    line_ok = norm_line(line_name(stop, ev)) == norm_line(leg.line)
    if not (number_ok or line_ok):
        return None
    diff = abs((ev.planned - when).total_seconds()) / 60
    if diff > (MAX_TIME_DIFF_NUMBER_MIN if number_ok else MAX_TIME_DIFF_MIN):
        return None
    score = (100 if number_ok else 0) + (50 if line_ok else 0) - diff
    if stop.category and stop.category.upper() == leg.category:
        score += 10
    return score


async def resolve_station(client: TimetablesClient, name: str) -> Station | None:
    target = norm_name(name)
    fallback = None
    for q in name_variants(name):
        try:
            found = await client.stations(q)
        except TimetablesError as err:
            if err.code in ("BAD_CREDENTIALS", "RATE_LIMITED"):
                raise
            continue
        for s in found:
            if norm_name(s.name) in (target, norm_name(q)):
                return s
        if fallback is None and len(found) == 1:
            fallback = found[0]
    return fallback


async def find_stop(client: TimetablesClient, station: Station, leg: SharedLeg, *, arrival: bool) -> Match | None:
    """Den Zug des Abschnitts im Fahrplan eines Bahnhofs suchen (auch unter dessen
    Neben-Nummern, z. B. S-Bahn-Bereich), zuerst in der Stunde laut Text."""
    when = leg.arr.when if arrival else leg.dep.when
    attr = "arrival" if arrival else "departure"
    best: Match | None = None
    for eva in [station.eva, *station.meta]:
        for offset in (0, -1, 1):
            stops = await client.plan(eva, when + timedelta(hours=offset))
            for s in stops:
                sc = _score(s, leg, attr, when)
                if sc is not None and (best is None or sc > best.score):
                    best = Match(eva, s, sc)
            if best:
                return best
    return None


async def _find_by_trip(client: TimetablesClient, eva: str, trip_key: str, around: datetime) -> Stop | None:
    for offset in (0, -1, 1):
        for s in await client.plan(eva, around + timedelta(hours=offset)):
            if s.trip_key == trip_key:
                return s
    return None


def _cut_path(path: list[str], arrival_name: str) -> list[str] | None:
    """Zwischenhalte bis vor den Ausstieg; None, wenn der Ausstieg nicht im Zuglauf liegt."""
    target = norm_name(arrival_name)
    for i, name in enumerate(path):
        if norm_name(name) == target:
            return path[:i]
    return None


async def build_leg(client: TimetablesClient, index: int, leg: SharedLeg, directory: StationDirectory) -> dict:
    """Bahn-Abschnitt über die Timetables-API anreichern. Gelingt es nicht, bleibt
    der Abschnitt so, wie er im Text steht (mit Hinweis im Protokoll)."""
    base = _text_leg(index, leg, directory)
    if not leg.rail:
        return base

    dep_station = await resolve_station(client, leg.dep.name)
    if dep_station is None:
        _LOGGER.info("Abschnitt %s (%s): Bahnhof %s nicht gefunden", index, leg.line, leg.dep.name)
        return base
    dep = await find_stop(client, dep_station, leg, arrival=False)
    if dep is None:
        _LOGGER.info("Abschnitt %s (%s): Zug ab %s %s nicht im Fahrplan", index, leg.line, leg.dep.name, leg.dep.when)
        return base
    trip_key = dep.stop.trip_key
    dep_ev = dep.stop.departure

    arr_station = await resolve_station(client, leg.arr.name)
    arr_stop = None
    arr_eva = None
    if arr_station is not None:
        for eva in [arr_station.eva, *arr_station.meta]:
            arr_stop = await _find_by_trip(client, eva, trip_key, leg.arr.when)
            if arr_stop:
                arr_eva = eva
                break
    arr_ev = arr_stop.arrival if arr_stop else None

    stopovers = [{
        "station": _station(dep.eva, leg.dep.name, directory),
        "plannedArrival": None,
        "plannedDeparture": _iso(dep_ev.planned),
        "plannedPlatform": _platform(dep_ev.planned_platform, leg.dep.platform),
    }]

    # Zwischenhalte aus dem Zuglauf (geplanter Laufweg nach dem Einstieg)
    middle = _cut_path(dep_ev.planned_path, leg.arr.name)
    if middle is None and arr_ev is not None:
        # Zweite Quelle: Laufweg vor der Ankunft, ab dem Einstieg
        before = arr_ev.planned_path
        start = next((i for i, n in enumerate(before) if norm_name(n) == norm_name(leg.dep.name)), None)
        if start is not None:
            middle = before[start + 1:]
    if middle is None:
        _LOGGER.warning("Abschnitt %s (%s): Ausstieg %s nicht im Laufweg; ab: %s; an: %s",
                        index, leg.line, leg.arr.name, "|".join(dep_ev.planned_path),
                        "|".join(arr_ev.planned_path) if arr_ev else "-")
        middle = []
    t0, t1 = dep_ev.planned, (arr_ev.planned if arr_ev and arr_ev.planned else leg.arr.when)
    lookups = 0
    for k, name in enumerate(middle, start=1):
        # Schätzung der Durchfahrtszeit für die Wahl der Fahrplan-Stunde
        estimate = t0 + (t1 - t0) * (k / (len(middle) + 1))
        entry = {"station": _station(None, name, directory), "plannedArrival": _iso(estimate),
                 "plannedDeparture": _iso(estimate), "plannedPlatform": None, "estimated": True}
        if lookups < MAX_LOOKUPS_PER_LEG:
            st = await resolve_station(client, name)
            lookups += 1
            if st is not None:
                found = await _find_by_trip(client, st.eva, trip_key, estimate)
                lookups += 1
                entry["station"] = _station(st.eva, name, directory)
                if found:
                    ar, dp = found.arrival, found.departure
                    entry.update({
                        "plannedArrival": _iso(ar.planned if ar else None) or _iso(dp.planned if dp else None),
                        "plannedDeparture": _iso(dp.planned if dp else None) or _iso(ar.planned if ar else None),
                        "plannedPlatform": (dp or ar).planned_platform if (dp or ar) else None,
                        "estimated": False,
                    })
        stopovers.append(entry)

    stopovers.append({
        "station": _station(arr_eva, leg.arr.name, directory),
        "plannedArrival": _iso(arr_ev.planned if arr_ev and arr_ev.planned else leg.arr.when),
        "plannedDeparture": None,
        "plannedPlatform": _platform(arr_ev.planned_platform if arr_ev else None, leg.arr.platform),
    })

    base.update({
        "tripId": trip_key,
        "line": _line_info(leg, dep.stop),
        "origin": stopovers[0]["station"],
        "destination": stopovers[-1]["station"],
        "plannedDeparture": stopovers[0]["plannedDeparture"],
        "plannedArrival": stopovers[-1]["plannedArrival"],
        "plannedDeparturePlatform": stopovers[0]["plannedPlatform"],
        "plannedArrivalPlatform": stopovers[-1]["plannedPlatform"],
        "stopovers": stopovers,
        "matched": True,
        "source": "timetables",
        "iris": {"tripKey": trip_key, "depEva": dep.eva, "arrEva": arr_eva, "number": dep.stop.number,
                 "pathAfter": dep_ev.planned_path, "pathBefore": arr_ev.planned_path if arr_ev else None,
                 "category": dep.stop.category},
    })
    return base


async def build_snapshot(journey: SharedJourney, client: TimetablesClient, now: datetime,
                         directory: StationDirectory | None = None, horizon: timedelta | None = None) -> dict:
    """Vollständige Reise. Fehler bei einzelnen Abschnitten machen nur diesen
    Abschnitt ungenauer; Zugangs- oder Kontingentfehler werden weitergereicht."""
    directory = directory or NoCoordinates()
    legs = []
    for i, leg in enumerate(journey.legs):
        if horizon is not None and leg.dep.when - now > horizon:
            # Fahrplan der API reicht noch nicht so weit: später erneut (trips.due_for_enrichment)
            legs.append(_text_leg(i, leg, directory))
            continue
        try:
            legs.append(await build_leg(client, i, leg, directory))
        except TimetablesError as err:
            if err.code in ("BAD_CREDENTIALS", "RATE_LIMITED"):
                raise
            _LOGGER.warning("Abschnitt %s (%s): %s", i, leg.line, err)
            legs.append(_text_leg(i, leg, directory))
    return _envelope(journey, legs, now)
