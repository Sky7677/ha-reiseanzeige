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
                  source}], remarks,
     diversion: None | {path: [Namen der angefahrenen Halte],
                        stops: [{kind: served|cancelled|added, index?, name?}]}}

Umleitungen: Jede Ankunft nennt die Halte davor, jede Abfahrt die Halte danach,
geplant (ppth) und – wenn sich der Laufweg geändert hat – neu (cpth). Daraus
ergibt sich der Laufweg des Zuges und im Vergleich mit den geplanten Halten
des Abschnitts, welche entfallen und welche dazukommen (leg_diversion). Die API
nennt nur Bahnhöfe; den Weg dazwischen legt das Gleisnetz (geometry.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .journey import name_variants, norm_name
from .timetables import PASSENGER_MESSAGES, Event, Stop, TimetablesClient

RUNNING_AHEAD = 2                  # so viele kommende Halte des laufenden Abschnitts
NEXT_LEG_WINDOW = timedelta(hours=3)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _leg_times(leg: dict) -> tuple[datetime | None, datetime | None]:
    return _dt(leg.get("plannedDeparture")), _dt(leg.get("plannedArrival"))


def expected_arrivals(stops: list[dict], rt: dict | None, fallback: datetime | None) -> list[datetime | None]:
    """Erwartete Zeit je Halt: Prognose, sonst Plan. Ist eine Verspätung erst an
    einem späteren Halt bekannt, gilt sie auch für die Halte davor ohne Angabe –
    sonst gälten Halte nach Plan als vorbei, an denen der Zug noch gar nicht war
    (Verspätung entsteht unterwegs, die Halte dazwischen wurden nicht abgefragt)."""
    rt_stops = (rt or {}).get("stopovers") or []
    times = []
    for i, so in enumerate(stops):
        r = rt_stops[i] if i < len(rt_stops) and rt_stops[i].get("stationId") == so["station"].get("id") else {}
        planned = _dt(so.get("plannedArrival")) or _dt(so.get("plannedDeparture")) or (fallback if i else None)
        prog = _dt((r.get("arrival") or {}).get("prognosed")) or _dt((r.get("departure") or {}).get("prognosed"))
        times.append([planned, prog])
    later = None
    for t in reversed(times):
        if t[0] is not None and t[1] is not None:
            later = max(t[1] - t[0], timedelta(0))
        elif t[0] is not None and later:
            t[1] = t[0] + later
    return [q or p for p, q in times]


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
            # Entfallene Halte einer Umleitung werden weiter abgefragt: Dort zeigt sich, wenn sie zurückgenommen wird
            # nach erwarteter Zeit, nicht nach Plan: bei Verspätung sind die Halte noch nicht vorbei
            expected = expected_arrivals(stops, rt, arr)
            upcoming = [s for s, t in zip(stops[1:], expected[1:]) if t is None or t >= now - timedelta(minutes=5)]
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


def compare_paths(planned: list[str], route: list[str]) -> dict | None:
    """Geplante Halte eines Abschnitts mit dem Laufweg des Zuges vergleichen.

    planned: Namen der geplanten Halte (Einstieg … Ausstieg), route: alle Halte
    des Zuges in Fahrtrichtung. Ergebnis wie leg_realtime()["diversion"] oder
    None, wenn für diesen Abschnitt alles bleibt, wie es war. Entfallene Halte
    stehen vor den neuen, die an ihre Stelle treten."""
    # Ein- und Ausstieg stammen aus dem geteilten Text („Heidelberger Platz, Berlin“),
    # die übrigen Namen von der Bahn („Berlin Heidelberger Platz“): Schreibweisen zulassen
    keys = [{norm_name(v) for v in name_variants(n)} if n.strip() else {""} for n in planned]
    rkeys = [norm_name(n) for n in route]
    hits = [i for i, k in enumerate(rkeys) if any(k in ks for ks in keys)]
    if not keys or not hits:
        return None
    # Ausschnitt des Abschnitts: vom Einstieg (oder dem ersten noch angefahrenen
    # geplanten Halt) bis zum Ausstieg (oder dem letzten noch angefahrenen)
    start = next((i for i, k in enumerate(rkeys) if k in keys[0]), hits[0])
    ends = [i for i in range(start, len(rkeys)) if rkeys[i] in keys[-1]]
    later = [i for i in hits if i >= start]
    end = ends[0] if ends else later[-1]
    served = list(zip(route[start:end + 1], rkeys[start:end + 1]))

    stops: list[dict] = []
    added: list[str] = []
    j = 0
    for name, k in served:
        pos = next((p for p in range(j, len(keys)) if k in keys[p]), None)
        if pos is None:
            added.append(name)
            continue
        stops += [{"kind": "cancelled", "index": c} for c in range(j, pos)]
        stops += [{"kind": "added", "name": n} for n in added]
        stops.append({"kind": "served", "index": pos})
        added, j = [], pos + 1
    stops += [{"kind": "cancelled", "index": c} for c in range(j, len(keys))]
    stops += [{"kind": "added", "name": n} for n in added]
    if all(s["kind"] == "served" for s in stops):
        return None
    return {"path": [name for name, _k in served], "stops": stops}


PLANNED = "planned"   # leg_diversion: Laufweg ausdrücklich wie geplant


def leg_diversion(leg: dict, found: dict[str, Stop]) -> dict | str | None:
    """Geänderter Laufweg des Abschnitts aus den Änderungen der abgefragten
    Bahnhöfe (found: EVA → Halt des Zuges): die Umleitung, PLANNED (ein neuer
    Laufweg ist gemeldet, gleicht aber dem Plan) oder None (keine Aussage).

    Die Änderungsmeldungen enthalten nur, was sich geändert hat: einen neuen
    Laufweg (cpth) davor bzw. danach. Wo keiner steht, gelten die geplanten
    Halte des Abschnitts."""
    sos = leg.get("stopovers") or []
    planned = [so["station"].get("name") or "" for so in sos]
    position = {so["station"].get("id"): i for i, so in enumerate(sos) if so["station"].get("id")}
    confirmed = False
    for eva, s in found.items():   # in Fahrtrichtung: der nächste Halt weiß am meisten über den Rest
        before = s.arrival.changed_path if s.arrival else None
        after = s.departure.changed_path if s.departure else None
        i = position.get(eva)
        if (before is None and after is None) or i is None:
            continue
        events = [e for e in (s.arrival, s.departure) if e is not None]
        here = [] if all(e.cancelled for e in events) else [planned[i]]
        route = (planned[:i] if before is None else before) + here + (planned[i + 1:] if after is None else after)
        result = compare_paths(planned, route)
        if result is not None:
            return result
        confirmed = True
    return PLANNED if confirmed else None


def _served(ev: Event | None) -> bool:
    """Halt findet ausdrücklich statt: Status „geplant“ (auch: Ausfall zurückgenommen) oder neue Zeit."""
    return ev is not None and not ev.cancelled and (ev.changed_status == "p" or ev.changed is not None)


def _known_diversion(leg: dict, found: dict[str, Stop]) -> dict | None:
    """Ohne neue Aussage gilt die bekannte Umleitung weiter – die Meldung steht oft
    nur an Bahnhöfen, die der Zug schon hinter sich hat. Sie endet, sobald ein
    entfallener Halt doch wieder angefahren wird."""
    prev = leg.get("diversion") or {}
    if not prev.get("stops"):
        return None
    sos = leg.get("stopovers") or []
    dropped = {sos[st["index"]]["station"].get("id") for st in prev["stops"]
               if st.get("kind") == "cancelled" and isinstance(st.get("index"), int) and 0 <= st["index"] < len(sos)}
    if any(eva in dropped and (_served(s.arrival) or _served(s.departure)) for eva, s in found.items()):
        return None
    return {"path": list(prev.get("path") or []), "stops": list(prev["stops"])}


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

    diversion = leg_diversion(leg, found)
    if diversion is None:
        diversion = _known_diversion(leg, found)
    elif diversion == PLANNED:
        diversion = None
    for st in (diversion or {}).get("stops", []):
        i = st.get("index")
        if st.get("kind") == "cancelled" and isinstance(i, int) and 0 <= i < len(rt_stops):
            rt_stops[i]["cancelled"] = True   # auch Halte, die gerade nicht abgefragt wurden

    leg_cancelled = bool(rt_stops and rt_stops[0]["cancelled"] and rt_stops[-1]["cancelled"])
    return {
        "legIndex": leg["index"],
        "tripId": key,
        "origin": "timetables",
        "fetchedAt": datetime.fromtimestamp(fetched_at, tz=now.tzinfo).isoformat() if fetched_at else now.isoformat(),
        "cancelled": leg_cancelled,
        "hasRealtime": leg_cancelled or diversion is not None
        or any(s["source"] == "iris" and (s["arrival"]["delay"] is not None or s["departure"]["delay"] is not None) for s in rt_stops),
        "stopovers": rt_stops,
        "remarks": _remarks(list(found.values())),
        "diversion": diversion,
    }
