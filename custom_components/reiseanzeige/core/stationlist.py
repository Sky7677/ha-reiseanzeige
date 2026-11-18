"""Bahnhofsnummern (EVA), wie OpenBoards sie erwartet – wie EvaDirectory der App.

Das Gleisnetz kennt nur uic_ref aus OpenStreetMap; bei vielen großen Bahnhöfen ist das
der UIC-Code (München Hbf 8020347), nicht die EVA (8000261). Die Liste kommt aus dem
Release „bahnhofsnummern“ von reiseanzeige-gleisnetz (wöchentlich): Nummern, amtlicher
Name, RIL100 und DHID aus OpenStation (DB InfraGO, CC0), Lage und weitere Schreibweisen
aus trainline-eu/stations und OpenStreetMap (ODbL). Ein Bahnhof kann mehrere Nummern
haben (München Hbf: 8000261, 8098261 …; Berlin Südkreuz: Fern- und S-Bahn); abgefragt
werden dann alle.

Gesucht wird über den Namen (amtlich und weitere Schreibweisen, Varianten wie
name_variants) nahe der bekannten Lage, dazu über die Lage allein.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from .journey import name_variants, norm_name
from .openboards import is_eva
from .places import km

NEAR_M = 400.0
NAME_NEAR_KM = 3.0
FORMAT = 1
_CELL = 0.02

Point = tuple[float, float]


@dataclass
class Station:
    """Ein Bahnhof; evas sind seine Nummern, die erste ist die Hauptnummer."""
    evas: list[str]
    name: str
    aliases: list[str] = field(default_factory=list)
    ril: str | None = None
    dhid: str | None = None
    point: Point | None = None


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / _CELL), math.floor(lon / _CELL)


def station_point(station: dict | None) -> Point | None:
    """Lage eines Halts im Snapshot ({id, name, lat, lon})."""
    if not station or station.get("lat") is None or station.get("lon") is None:
        return None
    return float(station["lat"]), float(station["lon"])


class StationList:
    def __init__(self, stations: list[Station]) -> None:
        self.stations = [s for s in stations if s.evas]
        self._by_eva: dict[str, Station] = {}
        self._by_name: dict[str, list[Station]] = {}
        self._grid: dict[tuple[int, int], list[Station]] = {}
        for s in self.stations:
            for e in s.evas:
                self._by_eva.setdefault(e, s)
            seen = []
            for n in [s.name, *s.aliases]:
                k = norm_name(n)
                if k and k not in seen:
                    seen.append(k)
                    self._by_name.setdefault(k, []).append(s)
            if s.point is not None:
                self._grid.setdefault(_cell(*s.point), []).append(s)

    def __len__(self) -> int:
        return len(self.stations)

    def group(self, eva: str | None) -> list[str]:
        """Alle Nummern des Bahnhofs mit dieser Nummer (Hauptnummer zuerst); unbekannt: nur sie selbst."""
        if not eva:
            return []
        s = self._by_eva.get(eva)
        return list(s.evas) if s else [eva]

    def near(self, p: Point, max_m: float = NEAR_M, limit: int = 8) -> list[str]:
        """Nummern der Bahnhöfe im Umkreis, nächster zuerst, je Bahnhof alle Nummern."""
        ci, cj = _cell(*p)
        found: list[tuple[float, str, int, Station]] = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for s in self._grid.get((ci + di, cj + dj), []):
                    d = km(p, s.point) * 1000
                    if d <= max_m:
                        found.append((d, s.evas[0], id(s), s))
        found.sort(key=lambda f: (f[0], f[1]))
        stations: list[Station] = []
        for *_x, s in found:
            if s not in stations:
                stations.append(s)
        out: list[str] = []
        for s in stations[:limit]:
            for e in s.evas:
                if e not in out:
                    out.append(e)
        return out

    def by_name(self, name: str | None, near: Point | None) -> list[str]:
        """Nummern der Bahnhöfe mit diesem Namen (alle je Bahnhof); mit Lage nur die in
        ihrer Nähe, ohne nur ein eindeutiger Bahnhof."""
        if not name or not name.strip():
            return []
        for variant in name_variants(name):
            hits: list[Station] = []
            for s in self._by_name.get(norm_name(variant), []):
                if s not in hits:
                    hits.append(s)
            if not hits:
                continue
            if near is not None:
                close = sorted(((s, km(near, s.point)) for s in hits if s.point is not None), key=lambda x: x[1])
                close = [c for c in close if c[1] <= NAME_NEAR_KM]
                if close:
                    out: list[str] = []
                    for s, _d in close:
                        out += [e for e in s.evas if e not in out]
                    return out
            elif len(hits) == 1:
                return list(hits[0].evas)
        return []

    def candidates(self, station: dict) -> list[str]:
        """Nummern für eine Abfrage zu diesem Halt: Name zuerst, dann die Umgebung, dann seine eigene Gruppe."""
        out: list[str] = []
        p = station_point(station)

        def add(items):
            for e in items:
                if e not in out:
                    out.append(e)
        add(self.by_name(station.get("name"), p))
        if p is not None:
            add(self.near(p))
        sid = station.get("id")
        if sid and sid in self._by_eva:
            add(self._by_eva[sid].evas)
        return out

    def first(self) -> str | None:
        return self.stations[0].evas[0] if self.stations else None

    def point(self, eva: str | None) -> Point | None:
        s = self._by_eva.get(eva) if eva else None
        return s.point if s else None

    def name(self, eva: str | None) -> str | None:
        s = self._by_eva.get(eva) if eva else None
        return s.name if s else None

    def coords(self, eva: str | None, name: str | None) -> Point | None:
        """Lage über die Nummer, sonst über einen eindeutigen Namen."""
        p = self.point(eva)
        if p is None and name:
            evas = self.by_name(name, None)
            p = self.point(evas[0]) if evas else None
        return p

    def search(self, query: str, limit: int = 8) -> list[tuple[str, Point | None, str]]:
        """Bahnhöfe, deren Name die Eingabe enthält: (Hauptnummer, Lage, amtlicher Name);
        Anfang des Namens bzw. eines Worts zuerst."""
        q = norm_name(query)
        if len(q) < 2:
            return []
        ranked = []
        for s in self.stations:
            rank = 3
            for n in (norm_name(x) for x in [s.name, *s.aliases]):
                if n.startswith(q):
                    r = 0
                elif f" {q}" in f" {n}":
                    r = 1
                elif q in n:
                    r = 2
                else:
                    r = 3
                rank = min(rank, r)
            if rank <= 2:
                ranked.append((rank, len(norm_name(s.name)), s.name, s))
        ranked.sort(key=lambda x: (x[0], x[1], x[2]))
        return [(s.evas[0], s.point, s.name) for *_x, s in ranked[:limit]]

    def resolve(self, query: str) -> tuple[str, str] | None:
        """Bahnhof für die Abfahrtstafel: Nummer oder Name → (Hauptnummer, amtlicher Name)."""
        q = (query or "").strip()
        if is_eva(q):
            return q, self.name(q) or q
        evas = self.by_name(q, None)
        if evas:
            return evas[0], self.name(evas[0]) or q
        hits = self.search(q, limit=1)
        return (hits[0][0], hits[0][2]) if hits else None


def parse(text: str) -> StationList:
    """Liste aus dem Release „bahnhofsnummern“ (JSON, Format 1); wirft bei unbrauchbarer Datei."""
    doc = json.loads(text)
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError(f"Format {doc.get('format') if isinstance(doc, dict) else '?'} der Bahnhofsliste wird nicht unterstützt")
    if doc.get("kind") != "stations":
        raise ValueError("Keine Bahnhofsliste")
    fields = list(doc.get("fields") or [])

    def idx(name: str, required: bool = True) -> int:
        if name in fields:
            return fields.index(name)
        if required:
            raise ValueError(f"Feld {name} fehlt")
        return -1
    i_eva, i_name, i_lat, i_lon = idx("eva"), idx("name"), idx("lat"), idx("lon")
    i_aliases, i_ril, i_dhid = idx("aliases", False), idx("ril", False), idx("dhid", False)

    def get(row, i):
        return row[i] if 0 <= i < len(row) else None

    stations = []
    for row in doc.get("stations") or []:
        if not isinstance(row, list):
            continue
        evas = [e for e in (get(row, i_eva) or []) if is_eva(e)]
        name = get(row, i_name)
        if not evas or not isinstance(name, str):
            continue
        lat, lon = get(row, i_lat), get(row, i_lon)
        aliases = get(row, i_aliases) if i_aliases >= 0 else None
        stations.append(Station(
            evas=evas, name=name,
            aliases=[a for a in (aliases or []) if isinstance(a, str)],
            ril=get(row, i_ril) if i_ril >= 0 else None,
            dhid=get(row, i_dhid) if i_dhid >= 0 else None,
            point=(float(lat), float(lon)) if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) else None,
        ))
    if not stations:
        raise ValueError("Bahnhofsliste ist leer")
    return StationList(stations)
