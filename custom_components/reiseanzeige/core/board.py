"""Abfahrtstafel eines Bahnhofs (Ruheansicht der Karte) aus der Timetables-API.

Ergebnis im Format der Karte (frontend render-board.js):
    {station: {eva, name}, fetchedAt, departures: [{id, line, destination, via,
     planned, prognosed, delayMin, platform, plannedPlatform, platformChanged,
     cancelled, added, messages}]}
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .journey import resolve_station
from .timetables import PASSENGER_MESSAGES, TimetablesClient, TimetablesError, line_name, merge_changes


async def departures(client: TimetablesClient, query: str, now: datetime, *, minutes: int = 90, limit: int = 20) -> dict:
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
    out.sort(key=lambda d: d["prognosed"] or d["planned"])
    return {
        "station": {"eva": station.eva, "name": station.name},
        "fetchedAt": datetime.fromtimestamp(fetched, tz=timezone.utc).isoformat(),
        "departures": out[:limit],
    }
