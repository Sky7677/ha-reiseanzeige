"""Fahrplan lesen und eine Fahrt finden. Reines Python ohne Abhängigkeiten.

Wird wie router.py unverändert in die Integration übernommen.

Gesucht wird eine Fahrt, die an einer der Einstiegs-Stationen zur angegebenen
Minute abfährt und danach an einer der Ausstiegs-Stationen hält. Die freien
GTFS-Feeds enthalten keine Zugnummern; Linie bzw. Gattung, Station und Minute
bestimmen die Fahrt trotzdem eindeutig. Ergebnis: alle Halte dazwischen mit
Planzeiten.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from . import fileformat as ff

CELL = 0.01            # Rasterweite für die Umkreissuche (Grad)
RAIL = {"longdistance", "regional", "suburban"}


@dataclass
class PlanStop:
    name: str
    lat: float
    lon: float
    arrival: int | None     # Minuten ab Mitternacht des Betriebstags
    departure: int | None


@dataclass
class PlanDeparture:
    minute: int             # Minuten ab Mitternacht des abgefragten Tags (auch < 0 oder > 1440)
    mode: str
    line: str
    stop: str               # Haltestelle der Abfahrt
    to: str                 # letzter Halt der Fahrt
    via: list[str]          # nächste Halte (höchstens drei)


@dataclass
class PlanTrip:
    day: date               # Betriebstag
    mode: str
    line: str
    stops: list[PlanStop]
    score: float


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(x, math.radians(b[0] - a[0])) * 6371.0


class Timetable:
    def __init__(self, data: bytes, key: Callable[[str], str] | None = None) -> None:
        self.header, b = ff.read(data)
        if self.header.get("kind") != "timetable":
            raise ValueError("Keine Fahrplan-Datei")
        self.places: list = b["places"]
        self.routes: list = b["routes"]
        self.pat_route, self.pat_start, self.pat_place = b["pat_route"], b["pat_start"], b["pat_place"]
        self.pat_arr, self.pat_dep, self.pat_trip_start = b["pat_arr"], b["pat_dep"], b["pat_trip_start"]
        self.trip_start, self.trip_days = b["trip_start"], b["trip_days"]
        self.place_start, self.place_pat, self.place_pos = b["place_start"], b["place_pat"], b["place_pos"]
        self.window_start = date.fromisoformat(self.header["window_start"])
        self.window_days = int(self.header.get("window_days", 32))
        self.key = key or (lambda s: "".join(ch for ch in s.lower() if ch.isalnum()))
        self._grid: dict[tuple[int, int], list[int]] = {}
        self._names: dict[str, list[int]] = {}
        for i, (name, lat, lon) in enumerate(self.places):
            self._grid.setdefault((int(lat // CELL), int(lon // CELL)), []).append(i)
            self._names.setdefault(self.key(name), []).append(i)

    def covers(self, day: date) -> bool:
        return 0 <= (day - self.window_start).days < self.window_days

    # --- Stationen ---

    def near(self, lat: float, lon: float, radius_km: float = 0.4) -> list[int]:
        r = int(radius_km / 111 / CELL) + 1
        ci, cj = int(lat // CELL), int(lon // CELL)
        out = []
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for p in self._grid.get((i, j), ()):
                    if _km((lat, lon), (self.places[p][1], self.places[p][2])) <= radius_km:
                        out.append(p)
        return out

    def named(self, keys: list[str]) -> list[int]:
        out: list[int] = []
        for k in keys:
            out += self._names.get(k, [])
        return out

    # --- Fahrten ---

    def find(self, dep_places: list[int], arr_places: list[int], day: date, dep_minute: int,
             line_score: Callable[[str, str], float | None], arr_minute: int | None = None,
             tolerance: int = 1) -> PlanTrip | None:
        """Fahrt ab einer der dep_places zur Minute dep_minute am Tag day (auch nach
        Mitternacht: dann am Vortag mit +1440) bis zu einer der arr_places.
        Ist arr_places leer (Ausstieg weder über Lage noch Namen zu finden), entscheidet
        allein die Ankunftsminute – nur auf die Minute genau und mit Abschlag.
        line_score(mode, line) → Punkte oder None (passt nicht)."""
        arr_set = set(arr_places)
        best: PlanTrip | None = None
        for service_day, minute in ((day, dep_minute), (day - timedelta(days=1), dep_minute + 1440)):
            off = (service_day - self.window_start).days
            if not 0 <= off < self.window_days:
                continue
            bit = 1 << off
            for place in set(dep_places):
                for k in range(self.place_start[place], self.place_start[place + 1]):
                    pat, pos = self.place_pat[k], self.place_pos[k]
                    mode, line = self.routes[self.pat_route[pat]]
                    ls = line_score(mode, line)
                    if ls is None:
                        continue
                    a, b = self.pat_start[pat], self.pat_start[pat + 1]
                    # alle möglichen Ausstiege: im Umkreis liegen oft mehrere Halte derselben Linie
                    if arr_set:
                        ends = [i for i in range(a + pos + 1, b) if self.pat_place[i] in arr_set]
                    elif arr_minute is not None:
                        ends = list(range(a + pos + 1, b))
                    else:
                        ends = []
                    if not ends:
                        continue
                    dep_off = self.pat_dep[a + pos]
                    for t in range(self.pat_trip_start[pat], self.pat_trip_start[pat + 1]):
                        if not self.trip_days[t] & bit:
                            continue
                        dep = self.trip_start[t] + dep_off
                        if abs(dep - minute) > tolerance:
                            continue
                        score = ls - abs(dep - minute)
                        if arr_minute is None:
                            end = ends[0]
                        else:
                            want = arr_minute + (minute - dep_minute)
                            end = min(ends, key=lambda i: abs(self.trip_start[t] + self.pat_arr[i] - want))
                            diff = abs(self.trip_start[t] + self.pat_arr[end] - want)
                            if diff > (3 if arr_set else 1):
                                continue
                            score -= diff / 2 + (0 if arr_set else 20)
                        if best is None or score > best.score:
                            start = self.trip_start[t]
                            stops = []
                            for i in range(a + pos, end + 1):
                                name, lat, lon = self.places[self.pat_place[i]]
                                stops.append(PlanStop(name, lat, lon,
                                                      None if i == a + pos else start + self.pat_arr[i],
                                                      None if i == end else start + self.pat_dep[i]))
                            best = PlanTrip(service_day, mode, line, stops, score)
        return best


    def departures(self, places: list[int], day: date, start: int, minutes: int,
                   modes: set[str] | None = None, limit: int = 50) -> list[PlanDeparture]:
        """Abfahrten an places zwischen start und start + minutes (Minuten ab Mitternacht
        von day; Fahrten des Vor- und Folgetags, die in dieses Fenster fallen, zählen mit).
        Endhalte sind keine Abfahrten."""
        out: list[PlanDeparture] = []
        for service_day, shift in ((day - timedelta(days=1), -1440), (day, 0), (day + timedelta(days=1), 1440)):
            off = (service_day - self.window_start).days
            if not 0 <= off < self.window_days:
                continue
            bit = 1 << off
            for place in set(places):
                for k in range(self.place_start[place], self.place_start[place + 1]):
                    pat, pos = self.place_pat[k], self.place_pos[k]
                    a, b = self.pat_start[pat], self.pat_start[pat + 1]
                    if a + pos >= b - 1:
                        continue
                    mode, line = self.routes[self.pat_route[pat]]
                    if modes and mode not in modes:
                        continue
                    dep_off = self.pat_dep[a + pos]
                    for t in range(self.pat_trip_start[pat], self.pat_trip_start[pat + 1]):
                        if not self.trip_days[t] & bit:
                            continue
                        m = self.trip_start[t] + dep_off + shift
                        if start <= m <= start + minutes:
                            nxt = [self.places[self.pat_place[i]][0] for i in range(a + pos + 1, b)]
                            out.append(PlanDeparture(m, mode, line, self.places[place][0], nxt[-1], nxt[:-1][:3]))
        out.sort(key=lambda d: (d.minute, d.line))
        return out[:limit]


def line_score(leg_mode: str, leg_line: str, leg_category: str | None = None) -> Callable[[str, str], float | None]:
    """Bewertung für find(). Gleiche Linie oder Gattung zählt viel: „RE1“ = „RE 1“,
    „ICE 1004“ passt zu „ICE“, „STR 1“ zu „1“. Sonst passt jede Fahrt desselben
    Verkehrsmittels; Station und Minute entscheiden. Tram und U-Bahn werden je nach
    Quelle verschieden eingeordnet (Stadtbahn), daher gegenseitig mit Abschlag. Ein Bus
    passt zum Ersatzverkehr nur über die Linie („Bus RE80“ zu „RE80“) oder wenn er als
    SEV gekennzeichnet ist – sonst nie ein Stadtbus, der zufällig zur selben Minute fährt."""
    import re

    norm = lambda s: "".join((s or "").upper().split())
    words = (leg_line or "").split()
    refs = {norm(leg_line), norm(leg_category)}
    if len(words) > 1:
        refs.add(norm(" ".join(words[1:])))
    m = re.match(r"^(?:U|STR|TRAM|STB)\s*(\w+)$", (leg_line or "").strip(), re.I)
    if m:
        refs.add(m.group(1).upper())
    refs.discard("")

    def score(mode: str, line: str) -> float | None:
        if leg_mode in RAIL:
            if mode not in RAIL:
                return None
        elif {mode, leg_mode} == {"bus", "replacement"}:
            if norm(line) in refs:
                return 95.0
            return 10.0 if refs & {"SEV", "EV", "BUSSEV", "BUSEV"} else None
        elif mode != leg_mode and {mode, leg_mode} != {"tram", "subway"}:
            return None
        base = 100.0 if norm(line) in refs else 10.0
        return base if mode == leg_mode or leg_mode in RAIL else base - 5
    return score
