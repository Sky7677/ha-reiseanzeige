"""Echtzeit einer Reise aus OpenBoards – wie BoardRealtime und BoardDiversion der App.

Auch OpenBoards liefert je Bahnhof, nicht je Zug; der Zug wird in der Tafel über seine
Fahrt-Kennung (journeyId) gefunden. Abgefragt werden nur wenige Halte (stops_to_poll):
vor der Abfahrt der Einstieg, unterwegs die nächsten 2 Halte und der Ausstieg. Für
Halte, die nicht abgefragt wurden, wird die zuletzt bekannte Verspätung
fortgeschrieben („propagated“), damit die Zugposition stimmt; schon passierte Halte
behalten, was eine frühere Abfrage ergab („kept“), auch die tatsächlichen Zeiten.

Ergebnis je Abschnitt im Format der Karte (frontend view-model.js):
    {legIndex, tripId, origin, fetchedAt, cancelled, hasRealtime,
     stopovers: [{stationId, cancelled, arrival: {planned, prognosed, delay},
                  departure: {...}, plannedPlatform, platform, platformChanged,
                  source}], remarks,
     diversion: None | {path: [Namen der angefahrenen Halte],
                        stops: [{kind: served|cancelled|added, index?, name?, eva?}]}}

Umleitungen laut Bahn: Die Via-Halte einer Abfahrt nennen den restlichen Laufweg, die
einer Ankunft den bisherigen (der nächste zuerst); entfallene und zusätzliche Halte
sind darin markiert. Daraus ergibt sich der Laufweg des Zuges und im Vergleich mit den
geplanten Halten des Abschnitts, welche entfallen und welche dazukommen. Verglichen
wird über die Bahnhofsnummer und – weil Ein- und Ausstieg aus dem geteilten Text
stammen – über die Schreibweisen des Namens. Den Weg dazwischen legt das Gleisnetz
(geometry.py).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import openboards as ob
from .boardmatch import SOURCE as LEG_SOURCE
from .journey import name_variants, norm_name
from .openboards import Board, BoardSource, iso, parse_time

ORIGIN = "openboards"
SOURCE_BOARD = "openboards"
SOURCE_KEPT = "kept"
SOURCE_PROPAGATED = "propagated"
CAUSE = "cause"            # Verspätungsgrund: wird über die Fahrt gesammelt

RUNNING_AHEAD = 2          # so viele kommende Halte des laufenden Abschnitts
NEXT_LEG_WINDOW = timedelta(hours=3)
PASSED = timedelta(minutes=5)
DONE = timedelta(minutes=2)
BEFORE = timedelta(minutes=10)      # erstes Zeitfenster um die erwartete Zeit …
AFTER = timedelta(minutes=30)
WIDE_BEFORE = timedelta(minutes=15)  # … danach ein weites um den Plan
WIDE_AFTER = timedelta(hours=3)


def _rides(snapshot: dict) -> list[dict]:
    return [l for l in snapshot.get("legs") or [] if l.get("kind") == "ride" and l.get("matched")
            and l.get("tripId") and l.get("source") == LEG_SOURCE]


def trackable(snapshot: dict) -> bool:
    """Hat die Reise Abschnitte mit Fahrt-Kennung aus OpenBoards (sonst gibt es nichts abzufragen)?"""
    return bool(_rides(snapshot))


def _rt_stop(rt: dict | None, k: int) -> dict:
    sos = (rt or {}).get("stopovers") or []
    return sos[k] if 0 <= k < len(sos) and isinstance(sos[k], dict) else {}


def _prog(part: dict | None) -> datetime | None:
    return parse_time((part or {}).get("prognosed"))


def expected_arrivals(sos: list[dict], rt: dict | None, fallback: datetime | None) -> list[datetime | None]:
    """Erwartete Ankunft je Halt: Prognose, sonst Plan. Ist eine Verspätung erst an einem
    späteren Halt bekannt, gilt sie auch für die Halte davor ohne Angabe – sonst gälten
    Halte nach Plan als vorbei, an denen der Zug noch gar nicht war (Integration 0.7.2,
    12.11.2026: RE16 nach Treuchtlingen)."""
    times = []
    for k, so in enumerate(sos):
        planned = parse_time(so.get("plannedArrival")) or (fallback if k else None)
        times.append([planned, _prog(_rt_stop(rt, k).get("arrival"))])
    later = None
    for t in reversed(times):
        if t[0] is not None and t[1] is not None:
            later = max(t[1] - t[0], timedelta(0))
        elif t[0] is not None and later:
            t[1] = t[0] + later
    return [q or p for p, q in times]


def stops_to_poll(snapshot: dict, realtime: dict | None, now: datetime) -> dict[int, list[int]]:
    """Welche Halte (Stelle in der Haltliste) je Abschnitt jetzt abgefragt werden; dazu der
    Einstieg, solange ein verspäteter Zug dort noch nicht abgefahren ist."""
    out: dict[int, list[int]] = {}
    next_leg_done = False
    for leg in _rides(snapshot):
        dep, arr = parse_time(leg.get("plannedDeparture")), parse_time(leg.get("plannedArrival"))
        rt = ((realtime or {}).get("legs") or {}).get(str(leg["index"]))
        sos = leg.get("stopovers") or []
        rt_list = (rt or {}).get("stopovers") or []
        # Verspätung berücksichtigen: Abschnitt läuft bis zur prognostizierten Ankunft
        arr_eff = (_prog((rt_list[-1] if rt_list else {}).get("arrival")) if rt_list else None) or arr
        if arr_eff is not None and now > arr_eff + DONE:
            continue
        idx: list[int] = []
        if dep is not None and now < dep:
            if next_leg_done or dep - now > NEXT_LEG_WINDOW:
                continue
            next_leg_done = True
            idx.append(0)
        else:
            # Verspäteter Zug noch nicht abgefahren: Einstieg weiter abfragen (Gleis, Abfahrt)
            dep_eff = _prog(_rt_stop(rt, 0).get("departure")) or dep
            if dep_eff is not None and dep_eff >= now - PASSED:
                idx.append(0)
            # Prognose der früheren Abfrage zählt: ein verspäteter Zug ist noch vor dem Halt
            expected = expected_arrivals(sos, rt, arr)
            upcoming = [k for k in range(1, len(sos)) if expected[k] is not None and expected[k] >= now - PASSED]
            idx += upcoming[:RUNNING_AHEAD]
            idx.append(len(sos) - 1)
        stops = []
        for k in idx:
            if k not in stops and 0 <= k < len(sos) and sos[k]["station"].get("id"):
                stops.append(k)
        if stops:
            out[leg["index"]] = stops
    return out


def _part(planned: str | None, entry: dict | None) -> dict:
    prognosed = ob.prognosed(entry) if entry else None
    base = parse_time((entry or {}).get("timeSchedule")) or parse_time(planned)
    p = parse_time(prognosed)
    delay = (p - base) // timedelta(seconds=1) if p is not None and base is not None else None
    return {"planned": iso(planned), "prognosed": iso(prognosed), "delay": delay}


def _propagated(planned: str | None, carried: int | None) -> dict:
    t = parse_time(planned)
    if t is None or carried is None:
        return {"planned": iso(planned), "prognosed": None, "delay": None}
    return {"planned": iso(planned), "prognosed": (t + timedelta(seconds=carried)).astimezone(ob.TZ).isoformat(),
            "delay": carried}


async def _lookup(source: BoardSource, leg: dict, k: int, shift: int) -> tuple[dict, Board] | None:
    """Eintrag der Fahrt an einem Halt suchen: erst um die erwartete Zeit, dann weit."""
    sos = leg["stopovers"]
    so = sos[k]
    arrival = k == len(sos) - 1
    planned = parse_time(so.get("plannedArrival") if arrival else (so.get("plannedDeparture") or so.get("plannedArrival")))
    eva = so["station"].get("id")
    journey_id = leg.get("tripId")
    if planned is None or not eva or not journey_id:
        return None
    expected = planned + timedelta(seconds=shift)
    types = ob.transport_types((leg.get("line") or {}).get("product"))
    for start, end in ((expected - BEFORE, expected + AFTER), (planned - WIDE_BEFORE, planned + WIDE_AFTER)):
        board = await source.board(ob.query(arrival, [eva], start, end, types, max_via=50))
        e = board.same_journey(journey_id)
        if e is not None:
            return e, board
    return None


def _remarks(found) -> list[dict]:
    seen, out = set(), []
    for e, board in found:
        for kind, code, text in board.texts(e.get("messages")):
            if text in seen:
                continue
            seen.add(text)
            typ = "warning" if kind == ob.DISRUPTION else CAUSE if kind == ob.CAUSE else "status"
            out.append({"code": code, "type": typ, "text": text})
    return out


def merge_remarks(previous: list[dict] | None, found) -> list[dict]:
    """Hinweise dieser Abfrage; Verspätungsgründe werden gesammelt: An den gerade
    abgefragten Halten nennt die Bahn oft nur den jüngsten Grund, der DB Navigator zeigt
    alle der Fahrt. Ältere Gründe bleiben deshalb vorn stehen. Ohne Treffer gilt der
    letzte Stand weiter."""
    found = list(found)
    if not found:
        return list(previous or [])
    fresh = _remarks(found)
    texts = {r["text"] for r in fresh}
    return [r for r in previous or [] if r.get("type") == CAUSE and r.get("text") not in texts] + fresh


# --- Umleitungen laut Bahn ---------------------------------------------------------

@dataclass(frozen=True)
class RouteStop:
    """Halt im Laufweg: Nummer (falls bekannt) und Name."""
    eva: str | None
    name: str


PLANNED = {"path": [], "stops": []}   # leg_diversion: Laufweg ausdrücklich wie geplant


def _keys(s: RouteStop) -> set[str]:
    out = {f"#{s.eva}"} if s.eva else set()
    if s.name.strip():
        out |= {k for k in (norm_name(v) for v in name_variants(s.name)) if k}
    return out


def _route_keys(s: RouteStop) -> set[str]:
    out = {f"#{s.eva}"} if s.eva else set()
    k = norm_name(s.name)
    if k:
        out.add(k)
    return out


def compare_paths(planned: list[RouteStop], route: list[RouteStop]) -> dict | None:
    """Geplante Halte eines Abschnitts (Einstieg … Ausstieg) mit dem Laufweg des Zuges
    vergleichen; None, wenn für diesen Abschnitt alles bleibt, wie es war. Entfallene
    Halte stehen vor den neuen, die an ihre Stelle treten."""
    keys = [_keys(p) for p in planned]
    rkeys = [_route_keys(r) for r in route]

    def hit(j: int, i: int) -> bool:
        return bool(rkeys[j] & keys[i])
    hits = [j for j in range(len(route)) if any(hit(j, i) for i in range(len(keys)))]
    if not keys or not hits:
        return None
    # Ausschnitt: vom Einstieg (oder dem ersten noch angefahrenen geplanten Halt) bis zum Ausstieg
    start = next((j for j in range(len(route)) if hit(j, 0)), hits[0])
    end = next((j for j in range(start, len(route)) if hit(j, len(keys) - 1)), None)
    if end is None:
        end = [h for h in hits if h >= start][-1]
    served = list(range(start, end + 1))

    stops: list[dict] = []
    added: list[RouteStop] = []
    j = 0
    for r in served:
        pos = next((p for p in range(j, len(keys)) if hit(r, p)), None)
        if pos is None:
            added.append(route[r])
            continue
        stops += [{"kind": "cancelled", "index": c} for c in range(j, pos)]
        stops += [{"kind": "added", "name": a.name, "eva": a.eva} for a in added]
        stops.append({"kind": "served", "index": pos})
        added, j = [], pos + 1
    stops += [{"kind": "cancelled", "index": c} for c in range(j, len(keys))]
    stops += [{"kind": "added", "name": a.name, "eva": a.eva} for a in added]
    if all(s["kind"] == "served" for s in stops):
        return None
    return {"path": [route[i].name for i in served], "stops": stops}


def leg_diversion(leg: dict, found: dict[int, tuple[dict, Board]]) -> dict | None:
    """Geänderter Laufweg aus den Einträgen der abgefragten Halte (Stelle in der Haltliste
    → Eintrag und Tafel): die Umleitung, PLANNED (Laufweg wie geplant) oder None (keine Aussage)."""
    sos = leg.get("stopovers") or []
    planned = [RouteStop(so["station"].get("id"), so["station"].get("name") or "") for so in sos]
    confirmed = False
    for k in sorted(found):
        e, board = found[k]
        if not 0 <= k < len(planned):
            continue
        via = ob.via(e)
        if not any(v.get("cancelled") or v.get("additional") for v in via) and not e.get("cancelled"):
            confirmed = True
            continue

        def stop(eva: str) -> RouteStop:
            return RouteStop(eva, board.name(eva) or "")
        served = [stop(v["evaNumber"]) for v in via if not v.get("cancelled") and v.get("evaNumber")]
        here = [] if e.get("cancelled") else [planned[k]]
        if k == len(planned) - 1:
            # Ankunft: Via-Halte sind die Halte davor, der nächste zuerst
            route = served[::-1] + here + planned[k + 1:]
        else:
            d = ob.destination(e)
            dest = [stop(d["evaNumber"])] if d and d.get("evaNumber") and not d.get("cancelled") else []
            route = planned[:k] + here + served + dest
        result = compare_paths(planned, route)
        if result is not None:
            return result
        confirmed = True
    return PLANNED if confirmed else None


def known_diversion(previous: dict | None, found: dict[int, tuple[dict, Board]]) -> dict | None:
    """Ohne neue Aussage gilt die bekannte Umleitung weiter – sie endet, sobald ein
    entfallener Halt doch wieder angefahren wird."""
    if not previous or not previous.get("stops"):
        return None
    dropped = {s.get("index") for s in previous["stops"] if s.get("kind") == "cancelled" and isinstance(s.get("index"), int)}
    if any(k in dropped and not e.get("cancelled") and ob.prognosed(e) is not None for k, (e, _b) in found.items()):
        return None
    return copy.deepcopy(previous)


# --- Abschnitt abfragen ------------------------------------------------------------

async def leg_realtime(leg: dict, stops: list[int], previous: dict | None, source: BoardSource, now: datetime) -> dict:
    """Echtzeit eines Abschnitts aus den Tafeln der Halte stops (Stelle in der Haltliste);
    previous ist der letzte Stand dieses Abschnitts."""
    sos = leg.get("stopovers") or []
    # früherer Stand nur, wenn er zur selben Fahrt und Haltliste gehört
    prev = previous if previous and previous.get("tripId") == leg.get("tripId") \
        and len(previous.get("stopovers") or []) == len(sos) else None
    prev_stops = (prev or {}).get("stopovers") or []

    def delay_of(s: dict | None) -> int | None:
        s = s or {}
        d = (s.get("departure") or {}).get("delay")
        return d if d is not None else (s.get("arrival") or {}).get("delay")
    last_known = next((delay_of(s) for s in reversed(prev_stops) if s.get("source") in (SOURCE_BOARD, SOURCE_KEPT)), None)

    found: dict[int, tuple[dict, Board]] = {}
    for k in sorted(stops):
        if not 0 <= k < len(sos):
            continue
        shift = None
        if found:
            shift = _part(None, list(found.values())[-1][0])["delay"]
        if shift is None:
            shift = delay_of(prev_stops[k]) if k < len(prev_stops) else None
        if shift is None:
            shift = last_known
        hit = await _lookup(source, leg, k, shift or 0)
        if hit is not None:
            found[k] = hit
    first_polled = min(stops) if stops else len(sos)

    out: list[dict] = []
    carried: int | None = None
    for k, so in enumerate(sos):
        planned_platform = so.get("plannedPlatform")
        old = prev_stops[k] if k < len(prev_stops) else None
        if k in found:
            e = found[k][0]
            if k == len(sos) - 1:
                arr = _part(so.get("plannedArrival"), e)
                dep = {"planned": iso(so.get("plannedDeparture")), "prognosed": None, "delay": None}
            else:
                dep = _part(so.get("plannedDeparture"), e)
                arr = _propagated(so.get("plannedArrival"), dep["delay"])   # Ankunft am selben Halt: wie die Abfahrt
            for p in (arr, dep):
                if p["delay"] is not None:
                    carried = p["delay"]
            now_platform = ob.platform(e) or planned_platform
            pp = planned_platform or ob.platform(e, "platformSchedule")
            out.append({
                "stationId": so["station"].get("id"),
                "cancelled": bool(e.get("cancelled") or ob.journey_cancelled(e)),
                "arrival": arr,
                "departure": dep,
                "plannedPlatform": pp,
                "platform": now_platform,
                "platformChanged": bool(now_platform and pp and now_platform != pp),
                "source": SOURCE_BOARD,
            })
        elif old is not None and k < first_polled and old.get("source") in (SOURCE_BOARD, SOURCE_KEPT):
            # schon passiert: Stand der früheren Abfrage behalten
            for p in (old.get("arrival"), old.get("departure")):
                if p and p.get("delay") is not None:
                    carried = p["delay"]
            out.append({**copy.deepcopy(old), "source": SOURCE_KEPT})
        else:
            out.append({
                "stationId": so["station"].get("id"),
                "cancelled": False,
                "arrival": _propagated(so.get("plannedArrival"), carried),
                "departure": _propagated(so.get("plannedDeparture"), carried),
                "plannedPlatform": planned_platform,
                "platform": planned_platform,
                "platformChanged": False,
                "source": SOURCE_PROPAGATED if carried is not None else None,
            })

    # Umleitung laut Bahn: neue Aussage, sonst gilt die bekannte weiter
    diversion = leg_diversion(leg, found)
    if diversion is None:
        diversion = known_diversion((prev or {}).get("diversion"), found)
    elif diversion is PLANNED:
        diversion = None
    for st in (diversion or {}).get("stops") or []:
        i = st.get("index")
        if st.get("kind") == "cancelled" and isinstance(i, int) and 0 <= i < len(out):
            out[i]["cancelled"] = True   # auch Halte, die gerade nicht abgefragt wurden

    leg_cancelled = any(ob.journey_cancelled(e) for e, _b in found.values()) or \
        bool(out and out[0]["cancelled"] and out[-1]["cancelled"])
    has_realtime = leg_cancelled or diversion is not None or any(
        s.get("source") in (SOURCE_BOARD, SOURCE_KEPT)
        and (s.get("cancelled") or (s.get("arrival") or {}).get("delay") is not None
             or (s.get("departure") or {}).get("delay") is not None)
        for s in out)
    return {
        "legIndex": leg.get("index"),
        "tripId": leg.get("tripId"),
        "origin": ORIGIN,
        "fetchedAt": now.astimezone(ob.TZ).replace(microsecond=0).isoformat(),
        "cancelled": leg_cancelled,
        "hasRealtime": has_realtime,
        "stopovers": out,
        "remarks": merge_remarks((prev or {}).get("remarks"), found.values()),
        "diversion": diversion,
    }


async def update(snapshot: dict, realtime: dict | None, source: BoardSource, now: datetime) -> dict | None:
    """Ganze Reise einmal abfragen (für Tests und einfache Aufrufer). Abschnitte, die gerade
    nicht dran sind, behalten ihren letzten Stand. Fehler gehen weiter."""
    poll = stops_to_poll(snapshot, realtime, now)
    if not poll:
        return None
    legs = dict(((realtime or {}).get("legs") or {}))
    for index, stops in poll.items():
        leg = next((l for l in snapshot.get("legs") or [] if l.get("index") == index and l.get("kind") == "ride"), None)
        if leg is None:
            continue
        legs[str(index)] = await leg_realtime(leg, stops, legs.get(str(index)), source, now)
    return {"legs": legs}
