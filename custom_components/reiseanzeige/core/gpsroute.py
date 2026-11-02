"""Umleitungen ohne Halt per GPS: Fährt der Zug eine andere Strecke, ohne dass die
Bahn neue Halte meldet, legen die Standorte des Handys der reisenden Person die
Linie neu.

Je Standort (step):
  1. Nur hinreichend genaue Standorte (höchstens MAX_ACCURACY_M) während eines
     laufenden Bahn-Abschnitts.
  2. Aufs Gleis setzen: unter den nächsten passenden Gleisen (höchstens TRACK_M
     entfernt) das erste, das vom vorigen Punkt auf den Gleisen ohne großen Umweg
     erreichbar ist. Ohne solches Gleis zählt der Standort nicht (Straße, Fußweg,
     Lücke im Gleisnetz).
  3. Abstand zum Teil der Linie, der noch vor dem Zug liegt. Zwei Standorte in
     Folge mehr als OFF_ROUTE_M daneben → neu zeichnen: bis zum zuletzt
     angefahrenen Halt wie bisher, ab dort über die Standorte zu den kommenden
     Halten (geometry.gps_route_points).
  4. Danach legt jeder neue Standort sie erneut (dicht beieinander liegende auf
     der Linie ausgenommen); die Teilstücke dazwischen bleiben zwischengespeichert.

Geprüft am 02.11.2026 mit dem echten Gleisnetz an fünf typischen Umleitungen
(Erkennung nach 3–18 km, Linie am Ende höchstens rund 80 m neben der echten
Strecke, keine Auslösung durch verrauschtes GPS auf der geplanten Strecke).
Gespeichert werden nur aufs Gleis gesetzte Punkte, nie rohe Standorte.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .geometry import Fallback, _known, _project, leg_stopovers, set_gps, stop_states
from .places import Places, km
from .railnet.router import COSTS, RailNet
from .share_text import RAIL_MODES

MAX_ACCURACY_M = 250.0     # wie die GPS-Korrektur der Karte; im Zug oft WLAN/Mobilfunk
MAX_AGE = timedelta(minutes=10)
OFF_ROUTE_M = 500.0
TRACK_M = 150.0            # Gleis so nah am Standort – mindestens; ungenauere Standorte suchen weiter
TRACK_MAX_M = 300.0
LATE = timedelta(minutes=30)   # Umweg ohne neue Prognose: Abschnitt läuft noch so lange nach der Ankunft
HINTS = 2
DETOUR = 1.5          # Weg auf den Gleisen höchstens so viel länger als die Luftlinie …
DETOUR_KM = 2.0       # … plus so viel
REFINE_M = 100.0
MIN_SPACING_KM = 1.0  # dichter liegende Standorte auf der Linie bringen nichts
MAX_POINTS = 150


@dataclass
class Fix:
    lat: float
    lon: float
    accuracy: float
    at: datetime

    @property
    def point(self) -> tuple[float, float]:
        return (self.lat, self.lon)


def read_fix(state, now: datetime) -> Fix | None:
    """Zustand eines device_tracker (state, attributes, last_updated) → Fix oder None."""
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    a = state.attributes or {}
    try:
        lat, lon = float(a["latitude"]), float(a["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        accuracy = float(a.get("gps_accuracy"))
    except (TypeError, ValueError):
        accuracy = math.inf
    at = state.last_updated
    if at is None or now - at > MAX_AGE:
        return None
    return Fix(lat, lon, accuracy, at)


@dataclass
class Watch:
    """Was zwischen zwei Standorten einer Reise gemerkt wird (nur im Speicher)."""
    leg: int | None = None
    last_at: datetime | None = None
    since: int | None = None          # Halt, seit dessen Abfahrt Kandidaten gesammelt werden
    hints: int = 0
    candidates: list = field(default_factory=list)
    ahead: int = 0                    # Linienpunkt am Zug (nur vorwärts)
    line_key: str | None = None
    cache: dict = field(default_factory=dict)

    def reset(self, leg: int) -> None:
        self.leg, self.since, self.hints, self.candidates, self.ahead, self.line_key = leg, None, 0, [], 0, None
        self.cache.clear()


# --- Geometrie ------------------------------------------------------------------------

def _seg_dist_m(p: tuple[float, float], a, b) -> float:
    k = math.cos(math.radians(p[0]))
    dx, dy = (b[1] - a[1]) * k, b[0] - a[0]
    px, py = (p[1] - a[1]) * k, p[0] - a[0]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, (px * dx + py * dy) / l2))
    return km(p, (a[0] + dy * t, a[1] + (b[1] - a[1]) * t)) * 1000


def dist_to_line_m(p: tuple[float, float], line: list) -> float:
    if len(line) == 1:
        return km(p, tuple(line[0])) * 1000
    return min((_seg_dist_m(p, line[i], line[i + 1]) for i in range(len(line) - 1)), default=math.inf)


def _nearest_vertex(line: list, p: tuple[float, float], start: int = 0) -> int:
    best, best_d = start, math.inf
    for i in range(min(start, len(line) - 1), len(line)):
        d = km(p, tuple(line[i]))
        if d < best_d:
            best, best_d = i, d
    return best


def _length_km(line: list) -> float:
    return sum(km(tuple(a), tuple(b)) for a, b in zip(line, line[1:]))


def on_track(net: RailNet, p: tuple[float, float], prev: tuple[float, float], mode: str,
             radius_m: float = TRACK_M) -> tuple[float, float] | None:
    """Standort aufs Gleis gesetzt – nur auf eines, das vom vorigen Punkt ohne großen
    Umweg erreichbar ist (sonst springt die Linie zwischen Strecken, die nicht
    zusammenhängen, etwa bei einer Fahrt auf der Straße)."""
    costs = COSTS.get(mode)
    if not costs:
        return None
    for s in net.snap(p[0], p[1], radius_m, costs)[:4]:
        route = net.route(prev, s.point, mode)
        if route and _length_km(route) <= DETOUR * km(prev, s.point) + DETOUR_KM:
            return s.point
    return None


# --- Reise -----------------------------------------------------------------------------

def running_leg(snapshot: dict, realtime: dict | None, t: datetime) -> dict | None:
    """Bahn-Abschnitt, der zur Zeit t läuft: der zuletzt begonnene Abschnitt (Abfahrt
    mit Prognose), bis LATE nach seiner Ankunft – ein Umweg ohne neue Prognose kostet
    Zeit. Hat danach schon Tram, Bus o. Ä. begonnen: keiner."""
    rt_legs = (realtime or {}).get("legs") or {}
    current = None
    for leg in snapshot.get("legs", []):
        if leg.get("kind") != "ride":
            continue
        states = stop_states(leg_stopovers(leg), rt_legs.get(str(leg.get("index"))))
        if len(states) < 2 or states[0][0] is None or states[-1][0] is None:
            continue
        if states[0][0] - timedelta(minutes=2) <= t:
            current = (leg, states[-1][0])
    if current is None or (current[0].get("line") or {}).get("product") not in RAIL_MODES or t > current[1] + LATE:
        return None
    return current[0]


def _departed(stopovers: list[dict], states: list, t: datetime) -> int | None:
    """Zuletzt verlassener Halt (Zeit vorbei, findet statt, nicht der Ausstieg)."""
    found = None
    for i, so in enumerate(stopovers[:-1]):
        when, cancelled = states[i]
        if when is not None and when <= t and not cancelled and not so.get("cancelled") and _known(so["station"]):
            found = i
    return found


def _upto(line: list, p: tuple[float, float]) -> list[list[float]]:
    i = _nearest_vertex(line, p)
    return [list(q) for q in line[:i + 1]]


def step(snapshot: dict, realtime: dict | None, watch: Watch, fix: Fix, net: RailNet | None,
         places: Places | None = None, fallback: Fallback | None = None, version: str = "") -> bool:
    """Einen Standort verarbeiten; ändert snapshot an Ort und Stelle. True, wenn sich
    etwas geändert hat (neue Linie oder neue Punkte zum Speichern)."""
    if net is None or fix.accuracy > MAX_ACCURACY_M or (watch.last_at and fix.at <= watch.last_at):
        return False
    watch.last_at = fix.at
    leg = running_leg(snapshot, realtime, fix.at)
    if leg is None:
        return False
    if watch.leg != leg["index"]:
        watch.reset(leg["index"])
    mode = leg["line"]["product"]
    div = leg.get("diversion") or {}
    line = div.get("polyline") or leg.get("polyline")
    if not line or len(line) < 2:
        return False                      # ohne Verlauf aus dem Gleisnetz nichts zu vergleichen
    key = (div.get("geometry") or {}).get("key") or (leg.get("geometry") or {}).get("key")
    if key != watch.line_key:
        watch.line_key, watch.ahead = key, 0
    stopovers = leg_stopovers(leg)
    rt_leg = ((realtime or {}).get("legs") or {}).get(str(leg["index"]))
    states = stop_states(stopovers, rt_leg)
    gps = copy.deepcopy(div["gps"]) if div.get("gps") else None

    if gps:
        chain, start = gps["points"], tuple(gps["anchor"])
    else:
        departed = _departed(stopovers, states, fix.at)
        if watch.since != departed:
            watch.since, watch.candidates, watch.hints = departed, [], 0
        chain, start = watch.candidates, _known(stopovers[departed or 0]["station"])
        if start is None:
            return False
    # Voriger Punkt; der davor als Rückgriff, falls der vorige auf einem Nebengleis gelandet ist
    prevs = [tuple(q[:2]) for q in chain[-2:]][::-1] + [start]
    # Teilstück der Linie, auf dem der vorige Punkt liegt (nur vorwärts) – ab dort liegt alles vor dem Zug
    hit = _project(line, prevs[0], watch.ahead)
    watch.ahead = hit[1] if hit else watch.ahead
    d = dist_to_line_m(fix.point, line[watch.ahead:])
    radius = min(max(TRACK_M, fix.accuracy), TRACK_MAX_M)
    point = None
    for back, prev in enumerate(prevs[:2]):
        point = on_track(net, fix.point, prev, mode, radius)
        if point:
            if back and chain:
                chain.pop()                   # voriger Punkt war eine Sackgasse
            break
    entry = [round(point[0], 6), round(point[1], 6), fix.at.isoformat()] if point else None

    if gps is None:
        if entry is None:
            if d <= OFF_ROUTE_M:
                watch.hints = 0
            return False
        watch.candidates.append(entry)
        watch.hints = 0 if d <= OFF_ROUTE_M else watch.hints + 1
        if watch.hints < HINTS:
            return False
        i = watch.since or 0
        anchor = _known(stopovers[i]["station"])
        st = stopovers[i]["station"]
        gps = {"after": {"id": st.get("id"), "name": st.get("name")}, "anchor": [anchor[0], anchor[1]],
               "anchorAt": (states[i][0] or fix.at).isoformat(), "prefix": _upto(line, anchor),
               "points": list(watch.candidates)}
    else:
        if entry is None:
            return False
        last = gps["points"][-1] if gps["points"] else gps["anchor"]
        if d <= REFINE_M and km(point, tuple(last[:2])) < MIN_SPACING_KM:
            return False
        gps["points"].append(entry)
        if len(gps["points"]) > MAX_POINTS:   # ausdünnen, der jüngste bleibt
            gps["points"] = gps["points"][:-1][::2] + gps["points"][-1:]
    # Teilstücke sind zwischengespeichert: neu zu suchen sind nur die zum neuen Punkt
    return set_gps(leg, gps, net, places, fallback, version, rt_leg, watch.cache)
