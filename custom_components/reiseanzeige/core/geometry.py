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
import math
import re
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


def apply_diversion(leg: dict, div: dict | None, net: RailNet | None, places: Places | None,
                    fallback: Fallback | None = None, version: str = "") -> bool:
    """Geänderten Laufweg eines Abschnitts (realtime.leg_diversion) übernehmen:
    leg.diversion = {path, stopovers, polyline, geometry}; der Verlauf führt über
    die angefahrenen Halte. Kein Laufweg mehr → leg.diversion entfällt.
    True, wenn sich etwas geändert hat."""
    stopovers = _diversion_stopovers(leg, div) if div else []
    if not stopovers:
        return leg.pop("diversion", None) is not None
    mode = (leg.get("line") or {}).get("product") or ""
    fill_coordinates({"legs": [{"line": leg.get("line"), "stopovers": stopovers}]}, places, fallback)
    pts = [p for p in (_known(so["station"]) for so in stopovers if not so.get("cancelled")) if p]
    key = _key(version, mode, leg.get("line") or {}, pts)
    old = leg.get("diversion") or {}
    if old.get("path") == div.get("path") and (old.get("geometry") or {}).get("key") == key:
        return False
    polyline, geo = None, {"key": key, "source": None, "routed": 0, "segments": max(len(pts) - 1, 0)}
    if net is not None and len(pts) >= 2 and km(pts[0], pts[-1]) > 0:
        res = leg_polyline(net, leg, mode, pts)
        if res:
            line, source, found = res
            polyline = _round(line)
            geo.update(source=source, routed=found)
    leg["diversion"] = {"path": list(div.get("path") or []), "stops": copy.deepcopy(div.get("stops") or []),
                        "stopovers": stopovers, "polyline": polyline, "geometry": geo}
    return True


def apply_geometry(snapshot: dict, net: RailNet | None, places: Places | None,
                   fallback: Fallback | None = None, version: str = "", timetable: Timetable | None = None,
                   diversions: dict[int, dict | None] | None = None) -> bool:
    """Snapshot an Ort und Stelle ergänzen: Lage der Halte, Zwischenhalte aus dem
    Fahrplan (nur für Abschnitte aus dem Text), Streckenverlauf und – für die
    Abschnitte in diversions (Index → realtime.leg_diversion) – Umleitungen.
    True, wenn sich etwas geändert hat."""
    changed = fill_coordinates(snapshot, places, fallback)
    changed |= fill_from_timetable(snapshot, timetable)
    for leg in snapshot.get("legs", []):
        if diversions and leg.get("index") in diversions:
            changed |= apply_diversion(leg, diversions[leg["index"]], net, places, fallback, version)
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
