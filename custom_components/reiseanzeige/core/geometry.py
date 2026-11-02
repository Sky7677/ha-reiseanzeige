"""Lage der Halte und Streckenverlauf einer Reise aus dem Gleisnetz.

apply_geometry() ergänzt einen Snapshot (Format von journey.py):
  * fehlende Koordinaten der Halte – über die Bahnhofsnummer, sonst über den
    Namen mit dem nächsten bekannten Halt als Anker (core/places.py);
    StaDa (falls abonniert) dient als weitere Quelle;
  * je Abschnitt legs[].polyline als [[lat, lon], …]:
      Bahn (Fern-, Regional-, S-Bahn): auf den Gleisen von Halt zu Halt,
      Tram/U-Bahn: entlang der OSM-Linie mit passender Nummer, die beide Halte
      berührt; sonst auf den Gleisen dieses Verkehrsmittels.
Reine Funktionen ohne Home Assistant; laufen im Executor (rechenintensiv).
Schon berechnete Verläufe werden nur neu berechnet, wenn sich Halte, Verkehrsmittel
oder die Version des Gleisnetzes geändert haben (legs[].geometry.key).
Umleitungen (geänderter Laufweg laut Echtzeit) landen in legs[].diversion,
neben dem geplanten Verlauf; die Karte zeigt dann diese Fassung.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Callable

from .places import Places, km
from .railnet.plan import Timetable
from .railnet.router import RailNet
from .schedule import fill_from_timetable
from .share_text import RAIL_MODES

LINE_MAX_DIST_M = 200.0     # Halt so nah an der OSM-Linie, sonst passt sie nicht
LINE_ROUTE_TYPES = {"tram": ("tram", "light_rail"), "subway": ("subway", "light_rail")}
ROUND = 5                   # Nachkommastellen (≈ 1 m)

Fallback = Callable[[str | None, str], "tuple[float, float] | None"]


# --- Koordinaten ------------------------------------------------------------------

def _set(st: dict, c: tuple[float, float] | None) -> bool:
    if c and st.get("lat") is None:
        st["lat"], st["lon"] = round(c[0], 6), round(c[1], 6)
        return True
    return False


def _known(st: dict) -> tuple[float, float] | None:
    return (st["lat"], st["lon"]) if st.get("lat") is not None and st.get("lon") is not None else None


def fill_coordinates(snapshot: dict, places: Places | None, fallback: Fallback | None = None) -> bool:
    """Fehlende lat/lon der Halte ergänzen. True, wenn sich etwas geändert hat."""
    seq: list[tuple[str, dict]] = []
    for leg in snapshot.get("legs", []):
        mode = (leg.get("line") or {}).get("product")
        seq += [(mode, so["station"]) for so in leg.get("stopovers", []) if so.get("station")]
    changed = False
    # 1. über die Bahnhofsnummer
    for _mode, st in seq:
        if st.get("lat") is None and st.get("id"):
            c = places.by_eva(st["id"]) if places else None
            changed |= _set(st, c or (fallback(st["id"], st.get("name") or "") if fallback else None))
    # 2. über den Namen, vorwärts mit dem letzten bekannten Halt als Anker, dann rückwärts
    for order in (seq, seq[::-1]):
        anchor = None
        for mode, st in order:
            if st.get("lat") is None:
                c = places.find(st.get("name") or "", near=anchor, mode=mode) if places else None
                if c is None and fallback and anchor is None:
                    c = fallback(None, st.get("name") or "")
                changed |= _set(st, c)
            anchor = _known(st) or anchor
    # Abschnitts- und Reiseanfang/-ende spiegeln die Halte
    for leg in snapshot.get("legs", []):
        sos = leg.get("stopovers") or []
        if sos:
            for key, so in (("origin", sos[0]), ("destination", sos[-1])):
                if leg.get(key) is not None:
                    changed |= _set(leg[key], _known(so["station"]))
    legs = snapshot.get("legs") or []
    if legs:
        for key, leg in (("origin", legs[0]), ("destination", legs[-1])):
            if snapshot.get(key) is not None and leg.get(key):
                changed |= _set(snapshot[key], _known(leg[key]))
    return changed


# --- Linien von Tram und U-Bahn ------------------------------------------------------

def _refs(line: dict) -> set[str]:
    """„STR 1“ → {"STR1", "1"}, „U3“ → {"U3", "3"}, „Tram M10“ → {"TRAMM10", "M10"}."""
    name = (line.get("name") or "").upper()
    out = {re.sub(r"\s+", "", name)}
    parts = name.split()
    if len(parts) >= 2:
        out.add("".join(parts[1:]))
    m = re.match(r"^(?:U|STR|TRAM|STB)\s*(\w+)$", name)
    if m:
        out.add(m.group(1))
    return {r for r in out if r}


def _project(shape: list, p: tuple[float, float], start: int = 0) -> tuple[float, int, float, tuple[float, float]] | None:
    """Nächster Punkt auf shape ab Segment start: (Abstand m, Segment, Anteil, Punkt)."""
    lat0 = math.radians(p[0])
    kx, ky = 6371008.8 * math.cos(lat0) * math.pi / 180, 6371008.8 * math.pi / 180
    best = None
    for i in range(start, len(shape) - 1):
        (a_lat, a_lon), (b_lat, b_lon) = shape[i], shape[i + 1]
        x1, y1 = (a_lon - p[1]) * kx, (a_lat - p[0]) * ky
        x2, y2 = (b_lon - p[1]) * kx, (b_lat - p[0]) * ky
        dx, dy = x2 - x1, y2 - y1
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / seg))
        d = math.hypot(x1 + t * dx, y1 + t * dy)
        if best is None or d < best[0]:
            best = (d, i, t, (a_lat + (b_lat - a_lat) * t, a_lon + (b_lon - a_lon) * t))
    return best


def cut_line(shape: list, a: tuple[float, float], b: tuple[float, float], max_dist: float = LINE_MAX_DIST_M) -> tuple[list, float] | None:
    """Stück des Linienverlaufs von a bis b (in dieser Reihenfolge) und die Summe
    der Abstände der Halte zur Linie – oder None, wenn die Linie nicht passt."""
    if len(shape) < 2:
        return None
    pa = _project(shape, a)
    if pa is None or pa[0] > max_dist:
        return None
    pb = _project(shape, b, pa[1])
    if pb is None or pb[0] > max_dist or (pb[1] == pa[1] and pb[2] < pa[2]):
        return None
    pts = [pa[3]] + [tuple(p) for p in shape[pa[1] + 1:pb[1] + 1]] + [pb[3]]
    return pts, pa[0] + pb[0]


def line_polyline(routes: list, line: dict, mode: str, a: tuple[float, float], b: tuple[float, float]) -> list | None:
    types = LINE_ROUTE_TYPES.get(mode)
    if not types:
        return None
    refs = _refs(line)
    best = None
    for r in routes:
        if r.get("route") not in types or re.sub(r"\s+", "", str(r.get("ref") or "").upper()) not in refs:
            continue
        shape = r.get("shape") or []
        for penalty, s in ((0.0, shape), (50.0, shape[::-1])):  # Gegenrichtung, falls nur eine Richtung erfasst ist
            cut = cut_line(s, a, b)
            if cut and (best is None or cut[1] + penalty < best[1]):
                best = (cut[0], cut[1] + penalty)
    return best[0] if best else None


# --- Verlauf je Abschnitt --------------------------------------------------------------

def _key(version: str, mode: str, line: dict, pts: list) -> str:
    raw = f"{version}|{mode}|{line.get('name')}|" + ";".join(f"{p[0]:.5f},{p[1]:.5f}" for p in pts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _round(line: list) -> list[list[float]]:
    out: list[list[float]] = []
    for lat, lon in line:
        p = [round(lat, ROUND), round(lon, ROUND)]
        if not out or out[-1] != p:
            out.append(p)
    return out


def leg_polyline(net: RailNet, leg: dict, mode: str, pts: list) -> tuple[list, str, int] | None:
    """(Verlauf, Quelle, auf Gleisen gefundene Teilstücke) oder None."""
    if mode in LINE_ROUTE_TYPES:
        line = line_polyline(net.routes, leg.get("line") or {}, mode, pts[0], pts[-1])
        if line:
            return line, "line", len(pts) - 1
    if mode in RAIL_MODES or mode in LINE_ROUTE_TYPES:
        line, found = net.route_via(pts, mode)
        if found:
            return line, "tracks", found
    return None


# --- Umleitungen ------------------------------------------------------------------------

def _diversion_stopovers(leg: dict, div: dict) -> list[dict]:
    """Haltliste laut geändertem Laufweg: geplante Halte (entfallene markiert) und
    neue Halte ohne Zeiten. Leer, wenn die Angaben nicht zum Abschnitt passen."""
    sos = leg.get("stopovers") or []
    known = {so["station"].get("name"): _known(so["station"])
             for so in (leg.get("diversion") or {}).get("stopovers", []) if so.get("added")}
    out: list[dict] = []
    for st in div.get("stops") or []:
        if st.get("kind") == "added":
            name = st.get("name") or ""
            c = known.get(name)
            out.append({"station": {"id": None, "name": name, "lat": c[0] if c else None, "lon": c[1] if c else None},
                        "plannedArrival": None, "plannedDeparture": None, "plannedPlatform": None, "added": True})
            continue
        i = st.get("index")
        if not isinstance(i, int) or not 0 <= i < len(sos):
            return []
        so = copy.deepcopy(sos[i])
        if st.get("kind") == "cancelled":
            so["cancelled"] = True
        out.append(so)
    return out


def leg_stopovers(leg: dict) -> list[dict]:
    """Haltliste des Abschnitts – bei einer Umleitung deren Fassung (wie legs.js)."""
    d = leg.get("diversion") or {}
    return d["stopovers"] if len(d.get("stopovers") or []) >= 2 else leg.get("stopovers") or []


def _dt(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def stop_states(stopovers: list[dict], rt_leg: dict | None) -> list[tuple[datetime | None, bool]]:
    """Je Halt (Zeit, entfällt): Zeit = Prognose, sonst Plan (Abfahrt, am Ende
    Ankunft); neue Halte einer Umleitung haben keine. Die Echtzeit gehört in
    Reihenfolge zu den geplanten Halten (realtime.leg_realtime)."""
    rt_stops = (rt_leg or {}).get("stopovers") or []
    out: list[tuple[datetime | None, bool]] = []
    k = 0
    for so in stopovers:
        if so.get("added"):
            out.append((None, bool(so.get("cancelled"))))
            continue
        r = rt_stops[k] if k < len(rt_stops) else {}
        k += 1
        t = (_dt((r.get("departure") or {}).get("prognosed")) or _dt(so.get("plannedDeparture"))
             or _dt((r.get("arrival") or {}).get("prognosed")) or _dt(so.get("plannedArrival")))
        out.append((t, bool(so.get("cancelled") or r.get("cancelled"))))
    return out


# Umleitung per GPS (core/gpsroute.py) in leg.diversion.gps:
#   after   {id, name} des Halts, ab dem neu gezeichnet wird; anchor [lat, lon] dessen Lage
#   prefix  Verlauf bis dorthin (fest); points [[lat, lon, Zeit], …] Standorte seit dort,
#           aufs Gleis gesetzt (keine rohen GPS-Punkte)
CONFIRM_KM = 3.0   # Halt mit vergangener Zeit zählt nur, wenn ein Standort so nah war
REJOIN_M = 200.0   # jüngster Standort so nah an der geplanten Strecke: ab dort wieder wie geplant


def _index_of(stopovers: list[dict], ref: dict) -> int:
    for i, so in enumerate(stopovers):
        if ref.get("id") and so["station"].get("id") == ref["id"]:
            return i
    for i, so in enumerate(stopovers):
        if ref.get("name") and so["station"].get("name") == ref["name"]:
            return i
    return -1


def _insert_cheapest(seq: list, p: tuple[float, float]) -> None:
    """p dort einfügen, wo es den kleinsten Umweg macht (nie vor den Anfang)."""
    best_i, best_c = len(seq), math.inf
    for i in range(len(seq)):
        a, b = seq[i], seq[i + 1] if i + 1 < len(seq) else None
        c = km(a, p) + (km(p, b) - km(a, b) if b else 0.0)
        if c < best_c:
            best_i, best_c = i + 1, c
    seq.insert(best_i, p)


def gps_route_points(stopovers: list[dict], gps: dict, states: list) -> tuple[list, list]:
    """Wegpunkte ab dem Anker: Standorte in ihrer Reihenfolge; Halte, deren Zeit vorbei
    ist, nur mit Standort in der Nähe (sonst fuhr der Zug anders) und neue Halte ohne
    Zeit jeweils dort, wo sie den kleinsten Umweg machen (head); kommende Halte als
    Ziel (tail)."""
    after = _index_of(stopovers, gps.get("after") or {})
    points = [tuple(p[:2]) for p in gps.get("points") or []]
    seq = [tuple(gps["anchor"])] + points
    last_t = _dt(gps["points"][-1][2]) if gps.get("points") else _dt(gps.get("anchorAt"))
    tail: list[tuple[float, float]] = []
    for i, so in enumerate(stopovers):
        c = _known(so["station"])
        if i <= after or c is None or so.get("cancelled") or states[i][1]:
            continue
        t = states[i][0]
        # Kommende Halte sind Ziel – der Ausstieg immer, auch wenn der Umweg die Ankunft verspätet
        if tail or i == len(stopovers) - 1 or (t is not None and (last_t is None or t > last_t)):
            tail.append(c)
        elif t is None or any(km(c, q) <= CONFIRM_KM for q in points):
            _insert_cheapest(seq, c)
    return seq, tail


def _rejoin(planned: list | None, anchor: tuple[float, float], last: tuple[float, float]) -> list | None:
    """Liegt der jüngste Standort wieder auf der geplanten Strecke (hinter dem Anker),
    führt die Linie ab dort wie geplant weiter – der geplante Verlauf zwischen bekannten
    Halten ist verlässlicher als ein Weg vom Standort aus."""
    if not planned or len(planned) < 2:
        return None
    start = _project(planned, anchor)
    hit = _project(planned, last, start[1] if start else 0)
    if hit is None or hit[0] > REJOIN_M:
        return None
    return [hit[3]] + [tuple(q) for q in planned[hit[1] + 1:]]


def _route_via(net: RailNet, pts: list, mode: str, cache: dict | None) -> tuple[list, int]:
    """Wie RailNet.route_via, Teilstücke auf Wunsch zwischengespeichert (GPS: jeder
    neue Standort ändert nur das Ende)."""
    out: list = []
    found = 0
    for a, b in zip(pts, pts[1:]):
        hit = cache.get((a, b, mode)) if cache is not None else None
        if hit is None:
            part = net.route(a, b, mode) if a != b else None
            hit = (part, True) if part else ([a, b], False)
            if cache is not None:
                cache[(a, b, mode)] = hit
        part, ok = hit
        found += ok
        out.extend(part if not out else part[1:] if tuple(out[-1]) == tuple(part[0]) else part)
    return out, found


def _store_diversion(leg: dict, stopovers: list[dict], div: dict | None, gps: dict | None, net: RailNet | None,
                     places: Places | None, fallback: Fallback | None, version: str, states: list,
                     cache: dict | None = None) -> bool:
    mode = (leg.get("line") or {}).get("product") or ""
    fill_coordinates({"legs": [{"line": leg.get("line"), "stopovers": stopovers}]}, places, fallback)
    pts = [p for p in (_known(so["station"]) for so in stopovers if not so.get("cancelled")) if p]
    key = _key(version, mode, leg.get("line") or {}, pts)
    if gps:
        key = hashlib.sha1((key + json.dumps(gps, sort_keys=True)).encode()).hexdigest()[:16]
    old = leg.get("diversion") or {}
    path = list(div.get("path") or []) if div else None
    if old.get("path") == path and (old.get("geometry") or {}).get("key") == key:
        return False
    rejoin = None
    if gps:
        head, tail = gps_route_points(stopovers, gps, states)
        if not div and gps.get("points"):   # nur ohne gemeldete Umleitung gilt der geplante Verlauf
            rejoin = _rejoin(leg.get("polyline"), tuple(gps["anchor"]), tuple(gps["points"][-1][:2]))
        seq = head + ([] if rejoin else tail)
    else:
        seq = pts
    seq = [p for i, p in enumerate(seq) if i == 0 or p != seq[i - 1]]
    prefix = [tuple(p) for p in (gps or {}).get("prefix") or []]
    polyline, geo = None, {"key": key, "source": None, "routed": 0, "segments": max(len(seq) - 1, 0)}
    if gps:
        geo["gps"] = True
    if net is not None and len(seq) >= 2 and (gps or km(seq[0], seq[-1]) > 0):
        if gps:
            line, found = _route_via(net, seq, mode, cache)
            res = (line, "tracks", found) if found else None
        else:
            res = leg_polyline(net, leg, mode, seq)
        if res:
            line, source, found = res
            polyline = _round(prefix + list(line[1:] if prefix else line) + list((rejoin or [])[1:]))
            geo.update(source=source, routed=found)
    if polyline is None and gps:
        polyline = _round((prefix + seq[1:] if prefix else seq) + list((rejoin or [])[1:]))   # ohne Gleisnetz: gerade Linien
    if rejoin:
        geo["rejoined"] = True
    leg["diversion"] = {"path": path, "stops": copy.deepcopy(div.get("stops") or []) if div else None,
                        "stopovers": stopovers, "gps": gps, "polyline": polyline, "geometry": geo}
    return True


def apply_diversion(leg: dict, div: dict | None, net: RailNet | None, places: Places | None,
                    fallback: Fallback | None = None, version: str = "", rt_leg: dict | None = None) -> bool:
    """Geänderten Laufweg eines Abschnitts (realtime.leg_diversion) übernehmen:
    leg.diversion = {path, stops, stopovers, gps, polyline, geometry}; der Verlauf
    führt über die angefahrenen Halte (und GPS-Standorte, falls vorhanden). Kein
    Laufweg und kein GPS mehr → leg.diversion entfällt. True bei Änderung."""
    gps = (leg.get("diversion") or {}).get("gps")
    stopovers = _diversion_stopovers(leg, div) if div else []
    if not stopovers:
        div = None
        if not gps:
            return leg.pop("diversion", None) is not None
        stopovers = [copy.deepcopy(so) for so in leg.get("stopovers") or []]   # nur GPS: geplante Halte
    return _store_diversion(leg, stopovers, div, gps, net, places, fallback, version, stop_states(stopovers, rt_leg))


def set_gps(leg: dict, gps: dict, net: RailNet | None, places: Places | None, fallback: Fallback | None = None,
            version: str = "", rt_leg: dict | None = None, cache: dict | None = None) -> bool:
    """Umleitung laut GPS (core/gpsroute.py) übernehmen; eine gemeldete Umleitung
    bleibt Grundlage der Haltliste."""
    old = leg.get("diversion") or {}
    if old.get("path"):
        div, stopovers = {"path": old["path"], "stops": old.get("stops")}, old["stopovers"]
    else:
        div, stopovers = None, [copy.deepcopy(so) for so in leg.get("stopovers") or []]
    return _store_diversion(leg, stopovers, div, gps, net, places, fallback, version, stop_states(stopovers, rt_leg), cache)


def apply_geometry(snapshot: dict, net: RailNet | None, places: Places | None,
                   fallback: Fallback | None = None, version: str = "", timetable: Timetable | None = None,
                   diversions: dict[int, dict | None] | None = None, realtime: dict | None = None) -> bool:
    """Snapshot an Ort und Stelle ergänzen: Lage der Halte, Zwischenhalte aus dem
    Fahrplan (nur für Abschnitte aus dem Text), Streckenverlauf und – für die
    Abschnitte in diversions (Index → realtime.leg_diversion) – Umleitungen.
    realtime (trip.realtime) liefert die Zeiten für Umleitungen per GPS.
    True, wenn sich etwas geändert hat."""
    changed = fill_coordinates(snapshot, places, fallback)
    changed |= fill_from_timetable(snapshot, timetable)
    rt_legs = (realtime or {}).get("legs") or {}
    for leg in snapshot.get("legs", []):
        if diversions and leg.get("index") in diversions:
            changed |= apply_diversion(leg, diversions[leg["index"]], net, places, fallback, version,
                                       rt_legs.get(str(leg.get("index"))))
    if net is None:
        return changed
    for leg in snapshot.get("legs", []):
        mode = (leg.get("line") or {}).get("product") or ""
        pts = [_known(so["station"]) for so in leg.get("stopovers", []) if so.get("station")]
        pts = [p for p in pts if p]
        if len(pts) < 2 or km(pts[0], pts[-1]) == 0:
            continue
        key = _key(version, mode, leg.get("line") or {}, pts)
        if (leg.get("geometry") or {}).get("key") == key:
            continue
        res = leg_polyline(net, leg, mode, pts)
        if res:
            line, source, found = res
            leg["polyline"] = _round(line)
            leg["geometry"] = {"key": key, "source": source, "routed": found, "segments": len(pts) - 1}
        else:
            leg["polyline"] = None
            leg["geometry"] = {"key": key, "source": None, "routed": 0, "segments": len(pts) - 1}
        changed = True
    return changed
