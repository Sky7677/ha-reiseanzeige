"""Abfahrtstafel eines Bahnhofs (Ruheansicht der Karte) aus der Timetables-API,
ergänzt um Ersatzbusse laut Fahrplan (GTFS, DELFI e.V./gtfs.de).

Ersatzverkehr eines anderen Betreibers kennt die Timetables-API oft nicht. Die
Ersatzbusse kommen deshalb aus dem Fahrplan – als Planung, ohne Echtzeit:
  * Fährt laut Bahn der Zug derselben Linie zur gleichen Zeit (±5 min), entfällt
    der Ersatzbus (der Plan ist überholt).
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
from .timetables import PASSENGER_MESSAGES, Station, TimetablesClient, TimetablesError, line_name, merge_changes

_LOGGER = logging.getLogger(__name__)

# Ersatzbusse laut Fahrplan: (Bahnhof, jetzt, Minuten) → [{line, planned (datetime), destination, via, stop}]
Replacements = Callable[[Station, datetime, int], list[dict]]
TRAIN_RUNS = timedelta(minutes=5)
SAME_BUS = timedelta(minutes=3)


def _key(line: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (line or "").upper())


def _bare(line: str | None) -> str:
    """„Bus RE80“, „SEV RE80“ → „RE80“."""
    return re.sub(r"^(BUS|SEV|EV)(?=[A-Z])", "", _key(line))


def replacement_rows(rows: list[dict], candidates: list[dict], now: datetime, minutes: int) -> list[dict]:
    """Ersatzbusse laut Fahrplan als Zeilen der Tafel – ohne die, die der Zug laut
    Bahn überflüssig macht, und ohne solche, die die Bahn selbst führt."""
    def when(r: dict) -> datetime:
        return datetime.fromisoformat(r["prognosed"] or r["planned"])

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
               and abs(when(r) - t) <= TRAIN_RUNS for r in rows):
            continue   # der Zug fährt
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
                     replacements: Replacements | None = None) -> dict:
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
            out += replacement_rows(out, replacements(station, now, minutes), now, minutes)
        except Exception:  # noqa: BLE001 – ohne Ersatzbusse geht es auch
            _LOGGER.exception("Ersatzbusse für %s nicht ermittelt", station.name)
    out.sort(key=lambda d: datetime.fromisoformat(d["prognosed"] or d["planned"]))
    return {
        "station": {"eva": station.eva, "name": station.name},
        "fetchedAt": datetime.fromtimestamp(fetched, tz=timezone.utc).isoformat(),
        "departures": out[:limit],
    }
