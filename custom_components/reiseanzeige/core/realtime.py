"""Echtzeit für eine Reise aus den Änderungsmeldungen der Timetables-API.

Die API liefert Änderungen je Bahnhof (/fchg), nicht je Zug. Abgefragt
werden deshalb nur wenige Bahnhöfe (stations_to_poll): die nächsten Halte
des laufenden Abschnitts, sein Ausstieg und der Einstieg des nächsten
Abschnitts. Für Halte dazwischen, die nicht abgefragt wurden, wird die
zuletzt bekannte Verspätung fortgeschrieben (gekennzeichnet als
"propagated"), damit die Zugposition auf der Karte stimmt.

Ergebnis je Abschnitt im Format der Karte (frontend view-model.js):
    {legIndex, tripId, origin, fetchedAt, cancelled, hasRealtime,
     stopovers: [{stationId, cancelled, arrival: {planned, prognosed, delay},
                  departure: {...}, plannedPlatform, platform, platformChanged,
                  source}], remarks}
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .timetables import PASSENGER_MESSAGES, Event, Stop, TimetablesClient

RUNNING_AHEAD = 2                  # so viele kommende Halte des laufenden Abschnitts
NEXT_LEG_WINDOW = timedelta(hours=3)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _leg_times(leg: dict) -> tuple[datetime | None, datetime | None]:
    return _dt(leg.get("plannedDeparture")), _dt(leg.get("plannedArrival"))


def stations_to_poll(snapshot: dict, realtime: dict | None, now: datetime) -> dict[int, list[str]]:
    """Welche Bahnhöfe (EVA) je Abschnitt jetzt abgefragt werden sollen."""
    out: dict[int, list[str]] = {}
    legs = [l for l in snapshot.get("legs", []) if l.get("kind") == "ride" and l.get("matched") and l.get("tripId")]
    next_leg_done = False
    for leg in legs:
        dep, arr = _leg_times(leg)
        rt = (realtime or {}).get("legs", {}).get(str(leg["index"])) or {}
        # Verspätung berücksichtigen: Abschnitt läuft bis zur prognostizierten Ankunft
        last_rt = (rt.get("stopovers") or [None])[-1] if rt else None
        arr_eff = _dt((last_rt or {}).get("arrival", {}).get("prognosed")) or arr
        if arr_eff and now > arr_eff + timedelta(minutes=2):
            continue  # vorbei
        stops = leg["stopovers"]
        evas: list[str] = []
        if dep and now < dep:
            if next_leg_done or dep - now > NEXT_LEG_WINDOW:
                continue
            next_leg_done = True
            evas.append(stops[0]["station"]["id"])
        else:
            upcoming = [s for s in stops[1:] if (_dt(s.get("plannedArrival")) or arr) >= now - timedelta(minutes=5)]
            evas += [s["station"]["id"] for s in upcoming[:RUNNING_AHEAD]]
            evas.append(stops[-1]["station"]["id"])
        evas = [e for i, e in enumerate(evas) if e and e not in evas[:i]]
        if evas:
            out[leg["index"]] = evas
    return out


def _time_part(planned: datetime | None, ev: Event | None) -> dict:
    prognosed = ev.changed if ev and ev.changed else None
    delay = int((prognosed - planned).total_seconds()) if prognosed and planned else None
    return {"planned": planned.isoformat() if planned else None,
            "prognosed": prognosed.isoformat() if prognosed else None,
            "delay": delay}


def _remarks(stops: list[Stop]) -> list[dict]:
    seen, out = set(), []
    for s in stops:
        msgs = list(s.messages)
        for ev in (s.arrival, s.departure):
            if ev:
                msgs += ev.messages
        for m in msgs:
            text = m.get("text")
            if text and m.get("type") in PASSENGER_MESSAGES and text not in seen:
                seen.add(text)
                out.append({"code": m.get("code"), "type": "warning" if m.get("type") == "h" else "status", "text": text})
    return out


async def leg_realtime(client: TimetablesClient, leg: dict, evas: list[str], now: datetime) -> dict:
    """Echtzeit eines Abschnitts aus den Änderungen der angegebenen Bahnhöfe."""
    key = leg["tripId"]
    found: dict[str, Stop] = {}
    fetched_at = None
    for eva in evas:
        at, stops = await client.changes(eva)
        fetched_at = max(fetched_at or at, at)
        for s in stops:
            if s.trip_key == key:
                found[eva] = s
                break

    rt_stops = []
    carried = None  # zuletzt bekannte Verspätung in Sekunden
    for so in leg["stopovers"]:
        eva = so["station"]["id"]
        p_arr, p_dep = _dt(so.get("plannedArrival")), _dt(so.get("plannedDeparture"))
        s = found.get(eva)
        planned_platform = so.get("plannedPlatform")
        if s is not None:
            arr, dep = _time_part(p_arr, s.arrival), _time_part(p_dep, s.departure)
            cancelled = bool((s.arrival and s.arrival.cancelled) or (s.departure and s.departure.cancelled))
            ev = s.departure or s.arrival
            platform = (ev.changed_platform if ev else None) or planned_platform
            for part in (arr, dep):
                if part["delay"] is not None:
                    carried = part["delay"]
            source = "iris"
        else:
            arr = _time_part(p_arr, None)
            dep = _time_part(p_dep, None)
            if carried is not None:
                for part, planned in ((arr, p_arr), (dep, p_dep)):
                    if planned:
                        part["prognosed"] = (planned + timedelta(seconds=carried)).isoformat()
                        part["delay"] = carried
            cancelled = False
            platform = planned_platform
            source = "propagated" if carried is not None else None
        rt_stops.append({
            "stationId": eva,
            "cancelled": cancelled,
            "arrival": arr,
            "departure": dep,
            "plannedPlatform": planned_platform,
            "platform": platform,
            "platformChanged": bool(platform and planned_platform and platform != planned_platform),
            "source": source,
        })

    leg_cancelled = bool(rt_stops and rt_stops[0]["cancelled"] and rt_stops[-1]["cancelled"])
    return {
        "legIndex": leg["index"],
        "tripId": key,
        "origin": "timetables",
        "fetchedAt": datetime.fromtimestamp(fetched_at, tz=now.tzinfo).isoformat() if fetched_at else now.isoformat(),
        "cancelled": leg_cancelled,
        "hasRealtime": leg_cancelled or any(s["source"] == "iris" and (s["arrival"]["delay"] is not None or s["departure"]["delay"] is not None) for s in rt_stops),
        "stopovers": rt_stops,
        "remarks": _remarks(list(found.values())),
    }
