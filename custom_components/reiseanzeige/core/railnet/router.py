"""Routing auf dem Gleisnetz. Reines Python ohne Abhängigkeiten.

Diese Datei (mit fileformat.py und geo.py) wird unverändert in die Integration
übernommen; Änderungen hier, dann dort kopieren.

Suche: A* über Zustände (Kante, Richtung). Von einem Knoten geht es nur auf
eine andere Kante weiter, und an Weichen nur, wenn die Richtung sich dabei
höchstens um MAX_TURN_SWITCH (Knoten mit 3 Kanten) bzw. MAX_TURN_CROSSING
(4 und mehr, Kreuzungen) ändert. So kehrt ein Zug nie an einer Weiche um.
Am Start ist die Richtung frei, an Halten darf er also wenden (Kopfbahnhof).

Kosten: Länge mal Faktor der Streckenart, je nach Verkehrsmittel; None heißt
gesperrt. Dazu SNAP_WEIGHT mal Abstand der Halte zum Gleis.
"""

from __future__ import annotations

import bisect
import heapq
import math
from dataclasses import dataclass

from . import fileformat as ff
from .geo import R, turn

SNAP_RADIUS_M = 300.0
SNAP_WEIGHT = 2.0
SNAP_LIMIT = 16
MAX_TURN_SWITCH = 90.0
MAX_TURN_CROSSING = 60.0
MAX_EXPANSIONS = 1_500_000

_RAIL = {ff.MAIN: 1.0, ff.BRANCH: 1.05, ff.RAIL_OTHER: 1.1, ff.FREIGHT: 1.6, ff.CROSSOVER: 1.0,
         ff.LIGHT_RAIL: 3.0, ff.NARROW: None, ff.SUBWAY: None, ff.TRAM: None}
COSTS: dict[str, dict[int, float | None]] = {
    "longdistance": _RAIL,
    "regional": {**_RAIL, ff.NARROW: 1.2},
    "suburban": {**_RAIL, ff.LIGHT_RAIL: 1.0},
    "subway": {ff.SUBWAY: 1.0, ff.LIGHT_RAIL: 1.2, ff.TRAM: 2.0},
    "tram": {ff.TRAM: 1.0, ff.LIGHT_RAIL: 1.2, ff.SUBWAY: 1.5},
}


@dataclass(frozen=True)
class Snap:
    edge: int
    offset: float          # Meter ab edge_from (in edge_len gemessen)
    dist: float            # Abstand des Punkts zum Gleis in Metern
    point: tuple[float, float]


class RailNet:
    def __init__(self, data: bytes) -> None:
        self.header, b = ff.read(data)
        self.node_lat, self.node_lon = b["node_lat"], b["node_lon"]
        self.e_from, self.e_to, self.e_len, self.e_cls = b["edge_from"], b["edge_to"], b["edge_len"], b["edge_class"]
        self.b_from, self.b_to = b["edge_bear_from"], b["edge_bear_to"]
        self.shape_start, self.s_lat, self.s_lon = b["edge_shape_start"], b["shape_lat"], b["shape_lon"]
        self.adj_start, self.adj_items = b["adj_start"], b["adj_items"]
        self.grid_keys, self.grid_start, self.grid_items = b["grid_keys"], b["grid_start"], b["grid_items"]
        self.stations: list = b["stations"]
        self.routes: list = b["routes"]
        self.cell_deg = float(self.header.get("cell_deg", ff.CELL_DEG))
        self.grid_sample_m = float(self.header.get("grid_sample_m", 400.0))

    @classmethod
    def load(cls, path: str) -> "RailNet":
        with open(path, "rb") as f:
            return cls(f.read())

    # --- Geometrie einzelner Kanten -------------------------------------------------

    def shape(self, e: int) -> list[tuple[float, float]]:
        a, b = self.shape_start[e], self.shape_start[e + 1]
        return [(self.s_lat[i] / 1e6, self.s_lon[i] / 1e6) for i in range(a, b)]

    def _slice(self, e: int, lo: float, hi: float) -> list[tuple[float, float]]:
        """Stück der Kante zwischen lo und hi (Meter ab edge_from, lo <= hi)."""
        pts = self.shape(e)
        cum = [0.0]
        for i in range(1, len(pts)):
            cum.append(cum[-1] + _dist(pts[i - 1], pts[i]))
        total = cum[-1]
        scale = total / (self.e_len[e] or 1.0)
        a = max(0.0, min(total, lo * scale))
        b = max(a, min(total, hi * scale))

        def at(x: float) -> tuple[float, float]:
            i = max(1, bisect.bisect_left(cum, x))
            i = min(i, len(pts) - 1)
            span = cum[i] - cum[i - 1]
            t = 0.0 if span == 0 else (x - cum[i - 1]) / span
            p, q = pts[i - 1], pts[i]
            return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)

        return _dedupe([at(a)] + [pts[i] for i in range(len(pts)) if a < cum[i] < b] + [at(b)])

    def _cut(self, e: int, offset: float) -> tuple[list, list]:
        """Verlauf an offset teilen: (davor, danach), beide mit dem Schnittpunkt."""
        return self._slice(e, 0.0, offset), self._slice(e, offset, float(self.e_len[e]))

    # --- Andocken ----------------------------------------------------------------------

    def edges_near(self, lat: float, lon: float, radius_m: float) -> set[int]:
        margin = radius_m + self.grid_sample_m / 2 + 50
        dlat = margin / 111_195
        dlon = margin / (111_195 * max(0.1, math.cos(math.radians(lat))))
        cd = self.cell_deg
        found: set[int] = set()
        for ilat in range(int((lat - dlat + 90) // cd), int((lat + dlat + 90) // cd) + 1):
            for ilon in range(int((lon - dlon + 180) // cd), int((lon + dlon + 180) // cd) + 1):
                key = ilat * ff.GRID_STRIDE + ilon
                i = bisect.bisect_left(self.grid_keys, key)
                if i < len(self.grid_keys) and self.grid_keys[i] == key:
                    found.update(self.grid_items[self.grid_start[i]:self.grid_start[i + 1]])
        return found

    def snap(self, lat: float, lon: float, radius_m: float = SNAP_RADIUS_M,
             costs: dict | None = None, limit: int = SNAP_LIMIT) -> list[Snap]:
        """Gleise im Umkreis, nächstes zuerst; je Kante der nächste Punkt."""
        kx = R * math.cos(math.radians(lat)) * math.pi / 180
        ky = R * math.pi / 180
        out: list[Snap] = []
        for e in self.edges_near(lat, lon, radius_m):
            if costs is not None and costs.get(self.e_cls[e]) is None:
                continue
            pts = self.shape(e)
            xy = [((p[1] - lon) * kx, (p[0] - lat) * ky) for p in pts]
            best = (math.inf, 0.0, pts[0])
            acc = 0.0
            for i in range(1, len(xy)):
                (x1, y1), (x2, y2) = xy[i - 1], xy[i]
                dx, dy = x2 - x1, y2 - y1
                seg2 = dx * dx + dy * dy
                t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / seg2))
                d = math.hypot(x1 + t * dx, y1 + t * dy)
                sl = math.sqrt(seg2)
                if d < best[0]:
                    a, b = pts[i - 1], pts[i]
                    best = (d, acc + t * sl, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
                acc += sl
            if best[0] <= radius_m:
                offset = best[1] / (acc or 1.0) * self.e_len[e]
                out.append(Snap(e, offset, best[0], best[2]))
        out.sort(key=lambda s: (s.dist, s.edge))
        return out[:limit]

    # --- Suche ---------------------------------------------------------------------------

    def _end_node(self, s: int) -> int:
        e = s >> 1
        return self.e_from[e] if s & 1 else self.e_to[e]

    def _arrive_bearing(self, s: int) -> float:
        e = s >> 1
        return (self.b_from[e] + 180) % 360 if s & 1 else self.b_to[e]

    def _depart_bearing(self, s: int) -> float:
        e = s >> 1
        return (self.b_to[e] + 180) % 360 if s & 1 else self.b_from[e]

    def route(self, a: tuple[float, float], b: tuple[float, float], mode: str,
              radius_m: float = SNAP_RADIUS_M) -> list[tuple[float, float]] | None:
        """Weg auf den Gleisen von a nach b, oder None."""
        costs = COSTS.get(mode)
        if not costs:
            return None
        src = self.snap(a[0], a[1], radius_m, costs)
        dst = self.snap(b[0], b[1], radius_m, costs)
        if not src or not dst:
            return None
        targets = {s.edge: s for s in dst}
        min_factor = min(v for v in costs.values() if v is not None)
        lat_b, lon_b = math.radians(b[0]), math.radians(b[1])
        cos_b = math.cos(lat_b)

        def h(node: int) -> float:
            lat = math.radians(self.node_lat[node] / 1e6)
            lon = math.radians(self.node_lon[node] / 1e6)
            d = math.hypot((lon - lon_b) * cos_b, lat - lat_b) * R
            return max(0.0, d - radius_m) * min_factor

        g: dict[int, float] = {}
        parent: dict[int, int] = {}
        heap: list[tuple[float, float, int, object]] = []
        tie = 0

        def push_final(cost: float, how: tuple) -> None:
            nonlocal tie
            tie += 1
            heapq.heappush(heap, (cost, tie, -1, how))

        for s in src:
            f = costs[self.e_cls[s.edge]]
            for d in (0, 1):
                state = s.edge * 2 + d
                rest = (self.e_len[s.edge] - s.offset) if d == 0 else s.offset
                cost = rest * f + SNAP_WEIGHT * s.dist
                if cost < g.get(state, math.inf):
                    g[state] = cost
                    parent[state] = -1
                    tie += 1
                    heapq.heappush(heap, (cost + h(self._end_node(state)), tie, state, cost))
            t = targets.get(s.edge)
            if t is not None:  # Start und Ziel auf derselben Kante
                cost = abs(t.offset - s.offset) * f + SNAP_WEIGHT * (s.dist + t.dist)
                push_final(cost, ("direct", s, t))
        src_by_edge = {s.edge: s for s in src}

        done: set[int] = set()
        expansions = 0
        while heap:
            _prio, _tie, state, payload = heapq.heappop(heap)
            if state == -1:
                return self._polyline(payload, parent, src_by_edge)
            if state in done:
                continue
            done.add(state)
            expansions += 1
            if expansions > MAX_EXPANSIONS:
                return None
            cost = payload
            node = self._end_node(state)
            e_in = state >> 1
            lo, hi = self.adj_start[node], self.adj_start[node + 1]
            degree = hi - lo
            limit = MAX_TURN_SWITCH if degree == 3 else MAX_TURN_CROSSING if degree > 3 else 180.0
            arrive = self._arrive_bearing(state)
            for k in range(lo, hi):
                item = self.adj_items[k]
                e2 = item >> 1
                if e2 == e_in:
                    continue
                f = costs.get(self.e_cls[e2])
                if f is None:
                    continue
                nxt = item  # Kante*2 + r, r=1: rückwärts befahren – entspricht genau dem Zustand
                if limit < 180.0 and turn(arrive, self._depart_bearing(nxt)) > limit:
                    continue
                t = targets.get(e2)
                if t is not None:
                    part = t.offset if not nxt & 1 else self.e_len[e2] - t.offset
                    push_final(cost + part * f + SNAP_WEIGHT * t.dist, ("via", state, nxt, t))
                c2 = cost + self.e_len[e2] * f
                if c2 < g.get(nxt, math.inf):
                    g[nxt] = c2
                    parent[nxt] = state
                    tie += 1
                    heapq.heappush(heap, (c2 + h(self._end_node(nxt)), tie, nxt, c2))
        return None

    def _polyline(self, how: tuple, parent: dict[int, int], src_by_edge: dict[int, Snap]) -> list[tuple[float, float]]:
        if how[0] == "direct":
            _, s, t = how
            pts = self._slice(s.edge, min(s.offset, t.offset), max(s.offset, t.offset))
            return pts if s.offset <= t.offset else pts[::-1]
        _, last, entry, t = how
        chain = []
        s = last
        while s != -1:
            chain.append(s)
            s = parent[s]
        chain.reverse()
        out: list[tuple[float, float]] = []
        first = chain[0]
        e0 = first >> 1
        before, after = self._cut(e0, src_by_edge[e0].offset)
        out.extend(after if not first & 1 else before[::-1])
        for s in chain[1:]:
            pts = self.shape(s >> 1)
            out.extend(pts[::-1] if s & 1 else pts)
        before, after = self._cut(t.edge, t.offset)
        out.extend(before if not entry & 1 else after[::-1])
        return _dedupe(out)

    def route_via(self, points: list[tuple[float, float]], mode: str) -> tuple[list[tuple[float, float]], int]:
        """Weg durch alle Punkte; nicht routbare Abschnitte als gerade Linie.
        Rückgabe: (Verlauf, Zahl der auf Gleisen gefundenen Abschnitte)."""
        out: list[tuple[float, float]] = []
        found = 0
        for a, b in zip(points, points[1:]):
            part = self.route(a, b, mode) if a != b else None
            if part:
                found += 1
            else:
                part = [a, b]
            out.extend(part if not out else part[1:] if out[-1] == part[0] else part)
        return _dedupe(out), found


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(x, math.radians(b[0] - a[0])) * R


def _dedupe(pts: list) -> list:
    out = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    return out
