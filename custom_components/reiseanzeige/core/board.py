"""Abfahrtstafel eines Bahnhofs (Ruheansicht der Karte) aus der Timetables-API,
ergänzt um Ersatzbusse laut Fahrplan (GTFS, DELFI e.V./gtfs.de).

Ersatzverkehr eines anderen Betreibers kennt die Timetables-API oft nicht. Die
Ersatzbusse kommen deshalb aus dem Fahrplan – als Planung, ohne Echtzeit:
  * Fährt laut Bahn der Zug derselben Linie zur gleichen Zeit (±5 min) in dieselbe
    Richtung, entfällt der Ersatzbus (der Plan ist überholt). Dieselbe Richtung:
    das Ziel des Busses liegt auf dem weiteren Weg des Zuges – nach dem Namen
    oder, wo die Namen abweichen, nach der Lage (höchstens NEAR_DEST_KM). Ist das
    nicht zu entscheiden, bleibt der Bus stehen; er ist als Plan gekennzeichnet.
    (Nürnberg Hbf: S2-Ersatzbus nach Roth, S-Bahn S2 nach Altdorf fährt.)
  * Kennt die Bahn den Bus selbst (Bus/SEV ±3 min), gilt ihre Fassung (mit Echtzeit).

Ergebnis im Format der Karte (frontend render-board.js):
    {station: {eva, name}, fetchedAt, departures: [{id, line, destination, via,
     planned, prognosed, delayMin, platform, plannedPlatform, platformChanged,
     cancelled, added, messages, replacement?, stop?}]}
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from .journey import resolve_station
from .places import km
from .timetables import PASSENGER_MESSAGES, Station, TimetablesClient, TimetablesError, line_name, merge_changes

_LOGGER = logging.getLogger(__name__)

# Ersatzbusse laut Fahrplan: (Bahnhof, jetzt, Minuten) → [{line, planned (datetime), destination, via, stop, to_at}]
Replacements = Callable[[Station, datetime, int], list[dict]]
# Lage von Halten eines Zuges (Namen der Bahn, in Fahrtrichtung) → [(lat, lon) oder None]
Locate = Callable[[list[str], Station], list]
TRAIN_RUNS = timedelta(minutes=5)
SAME_BUS = timedelta(minutes=3)
NEAR_DEST_KM = 2.0
# Kein Ort: Zusätze in Bahnhofsnamen
_GENERIC = {"hbf", "bf", "pbf", "bahnhof", "hauptbahnhof", "b", "a", "i", "am", "an", "im", "bei", "der", "d"}


def _key(line: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (line or "").upper())


def _bare(line: str | None) -> str:
    """„Bus RE80“, „SEV RE80“ → „RE80“."""
    return re.sub(r"^(BUS|SEV|EV)(?=[A-Z])", "", _key(line))


def _words(name: str | None) -> set[str]:
    return set(re.findall(r"[^\W\d_]+", (name or "").casefold()))


def _place_words(name: str) -> set[str]:
    """„Altdorf(b Nürnberg)“ → {altdorf}, „Fürth(Bay)Hbf“ → {fürth}, „Nürnberg-Eibach“ → {nürnberg, eibach}."""
    return _words(re.sub(r"\([^)]*\)", " ", name)) - _GENERIC


def same_way(path: list[str], bus: dict, coords: list | None = None) -> bool:
    """Liegt das Ziel des Ersatzbusses auf dem weiteren Weg des Zuges? path: Halte
    des Zuges nach dem Bahnhof (Namen der Bahn); coords: ihre Lage, soweit bekannt.
    Bushaltestellen heißen „Bahnhof, Roth (Mittelfranken)“, Bahnhöfe „Roth“."""
    dest = _words(bus.get("destination"))
    if any((w := _place_words(n)) and w <= dest for n in path):
        return True
    at = bus.get("to_at")
    return bool(at) and any(c and km(tuple(at), tuple(c)) <= NEAR_DEST_KM for c in coords or [])


def replacement_rows(rows: list[dict], candidates: list[dict], now: datetime, minutes: int,
                     paths: dict | None = None, locate: Callable[[list[str]], list] | None = None) -> list[dict]:
    """Ersatzbusse laut Fahrplan als Zeilen der Tafel – ohne die, die ein Zug laut
    Bahn überflüssig macht, und ohne solche, die die Bahn selbst führt.
    paths: Zeilen-id → alle weiteren Halte des Zuges; locate: deren Lage."""
    paths = paths or {}
    located: dict = {}

    def when(r: dict) -> datetime:
        return datetime.fromisoformat(r["prognosed"] or r["planned"])

    def covers(r: dict, bus: dict) -> bool:
        path = paths.get(r.get("id")) or ([*r.get("via", []), r["destination"]] if r.get("destination") else [])
        if not path:
            return False
        rid = r.get("id")
        if locate is not None and rid not in located:
            try:
                located[rid] = locate(path)
            except Exception:  # noqa: BLE001 – dann nur nach dem Namen
                _LOGGER.debug("Lage der Halte von %s nicht ermittelt", r.get("line"), exc_info=True)
                located[rid] = []
        return same_way(path, bus, located.get(rid))

    out: list[dict] = []
    seen: set[tuple] = set()
    for c in candidates:
        t, line = c["planned"], c["line"]
        if t < now - timedelta(minutes=1) or t > now + timedelta(minutes=minutes):
            continue
        key = (_bare(line), t, c.get("destination"))
        if key in seen:
            continue
        seen.add(key)
        if any(_bare(r["line"]) == _bare(line) and not r["cancelled"] and not _key(r["line"]).startswith(("BUS", "SEV"))
               and abs(when(r) - t) <= TRAIN_RUNS and covers(r, c) for r in rows):
            continue   # der Zug fährt, in dieselbe Richtung
        if any(_key(r["line"]).startswith(("BUS", "SEV")) and abs(datetime.fromisoformat(r["planned"]) - t) <= SAME_BUS
               for r in rows):
            continue   # die Bahn kennt den Bus selbst
        out.append({
            "id": f"gtfs-{_bare(line)}-{t.isoformat()}-{c.get('destination')}",
            "line": line if _key(line).startswith(("BUS", "SEV")) else f"Bus {line}",
            "destination": c.get("destination"),
            "via": list(c.get("via") or [])[:3],
            "planned": t.isoformat(),
            "prognosed": None,
            "delayMin": None,
            "platform": None,
            "plannedPlatform": None,
            "platformChanged": False,
            "cancelled": False,
            "added": False,
            "messages": [],
            "replacement": True,
            "stop": c.get("stop"),
        })
    return out


async def departures(client: TimetablesClient, query: str, now: datetime, *, minutes: int = 90, limit: int = 20,
                     replacements: Replacements | None = None, locate: Locate | None = None) -> dict:
    station = await resolve_station(client, query)
    if station is None:
        raise TimetablesError("NOT_FOUND", f"Bahnhof „{query}“ nicht gefunden")
    slices = [now, now + timedelta(hours=1)] + ([now + timedelta(hours=2)] if minutes > 60 else [])
    plan = []
    for t in slices:
        plan += await client.plan(station.eva, t)
    fetched, changes = await client.changes(station.eva)
    merged = merge_changes(plan, changes)

    out = []
    paths: dict[str, list[str]] = {}
    for s in merged.values():
        dp = s.departure
        if dp is None or dp.hidden:
            continue  # Endhalt
        planned = dp.planned or dp.changed
        if planned is None:
            continue
        eff = dp.changed or planned
        if eff < now - timedelta(minutes=1) or planned > now + timedelta(minutes=minutes):
            continue
        path = dp.path
        paths[s.id] = list(path)
        texts = []
        for m in s.messages + dp.messages:
            if m.get("text") and m.get("type") in PASSENGER_MESSAGES and m["text"] not in texts:
                texts.append(m["text"])
        out.append({
            "id": s.id,
            "line": line_name(s, dp),
            "destination": path[-1] if path else None,
            "via": path[:-1][:3],
            "planned": planned.isoformat(),
            "prognosed": dp.changed.isoformat() if dp.changed else None,
            "delayMin": round((dp.changed - planned).total_seconds() / 60) if dp.changed else None,
            "platform": dp.changed_platform or dp.planned_platform,
            "plannedPlatform": dp.planned_platform,
            "platformChanged": bool(dp.changed_platform and dp.planned_platform and dp.changed_platform != dp.planned_platform),
            "cancelled": dp.cancelled,
            "added": dp.changed_status == "a",
            "messages": texts,
        })
    if replacements is not None:
        try:
            out += replacement_rows(out, replacements(station, now, minutes), now, minutes, paths,
                                    (lambda names: locate(names, station)) if locate else None)
        except Exception:  # noqa: BLE001 – ohne Ersatzbusse geht es auch
            _LOGGER.exception("Ersatzbusse für %s nicht ermittelt", station.name)
    out.sort(key=lambda d: datetime.fromisoformat(d["prognosed"] or d["planned"]))
    return {
        "station": {"eva": station.eva, "name": station.name},
        "fetchedAt": datetime.fromtimestamp(fetched, tz=timezone.utc).isoformat(),
        "departures": out[:limit],
    }
