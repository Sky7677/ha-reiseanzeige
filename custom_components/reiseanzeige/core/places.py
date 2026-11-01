"""Bahnhöfe und Haltestellen im Gleisnetz finden.

Zuerst über die Bahnhofsnummer (EVA, in OpenStreetMap als uic_ref), sonst über
den Namen. Namen aus dem DB Navigator und aus OpenStreetMap unterscheiden sich
oft („Augsburger Str.(U), Berlin“ ↔ „Augsburger Straße“), und viele kommen in
mehreren Städten vor („Hauptbahnhof“, „Rathaus“). Deshalb: Namen vereinheitlichen,
mehrere Schreibweisen probieren und unter den Treffern den nehmen, der dem
nächsten schon bekannten Halt der Reise am nächsten liegt (Anker).
"""

from __future__ import annotations

import math
import re
import unicodedata

ANCHOR_MAX_KM = 60.0       # Treffer in dieser Entfernung vom Anker bevorzugen (nächster zuerst)
UNANCHORED_SPREAD_KM = 3.0 # ohne Anker nur eindeutige Treffer (alle dicht beieinander)

_MARKERS = re.compile(r"\((?:s|u|s\s*\+\s*u|u\s*\+\s*s|bus|tram|str|stb)\)", re.I)
_ABBREV = {"str": "strasse", "hbf": "hauptbahnhof", "bf": "bahnhof", "bhf": "bahnhof", "pl": "platz", "st": "sankt"}


def norm_key(name: str | None) -> str:
    """Vergleichsschlüssel: klein, ohne Akzente/Umlaute-Varianten, ohne „(S+U)“,
    Abkürzungen ausgeschrieben, ohne Leer- und Satzzeichen."""
    s = (name or "").lower().replace("ß", "ss")
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = _MARKERS.sub(" ", s)
    tokens = re.findall(r"[a-z0-9]+", s)
    out = []
    for t in tokens:
        if t in _ABBREV:
            t = _ABBREV[t]
        elif len(t) > 4 and t.endswith("str"):
            t = t[:-3] + "strasse"   # „Hauptstr.“
        out.append(t)
    return "".join(out)


def name_keys(name: str) -> list[str]:
    """Schlüssel eines Namens aus dem DB Navigator, vom genauesten zum gröbsten.

    „Moritzplatz, Augsburg (Bayern)“ → Halt „Moritzplatz“, auch „Augsburg Moritzplatz“;
    „Berlin Heidelberger Platz“ → auch ohne vorangestellten Ort."""
    keys: list[str] = []

    def add(k: str) -> None:
        if k and k not in keys:
            keys.append(k)

    plain = _MARKERS.sub(" ", name)
    add(norm_key(plain))
    if "," in plain:
        stop, city = plain.split(",", 1)
        city_plain = re.sub(r"\([^)]*\)", " ", city)
        add(norm_key(stop))
        add(norm_key(f"{city_plain} {stop}"))
        add(norm_key(f"{city} {stop}"))
    else:
        words = plain.split()
        if len(words) >= 3 or (len(words) == 2 and norm_key(words[1]) not in ("hauptbahnhof", "bahnhof")):
            add(norm_key(" ".join(words[1:])))
    return keys


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(x, math.radians(b[0] - a[0])) * 6371.0


# Welche Art von Halt zu welchem Verkehrsmittel passt (kleiner = besser)
_KIND_RANK = {
    "longdistance": {"rail": 0, "light_rail": 1},
    "regional": {"rail": 0, "light_rail": 1},
    "suburban": {"rail": 0, "light_rail": 0},
    "subway": {"subway": 0, "line:subway": 0, "light_rail": 1, "line:light_rail": 1},
    "tram": {"tram": 0, "line:tram": 0, "line:light_rail": 1, "light_rail": 1},
}


class Places:
    """Index über die Bahnhöfe und die Halte der Linienverläufe eines Gleisnetzes."""

    def __init__(self, stations: list, routes: list | None = None) -> None:
        self._eva: dict[str, tuple[float, float]] = {}
        self._names: dict[str, list[tuple[float, float, str]]] = {}
        for name, eva, _ds100, lat, lon, kind in stations:
            if eva and eva not in self._eva:
                self._eva[eva] = (lat, lon)
            self._add(name, lat, lon, kind)
        for r in routes or []:
            kind = f"line:{r.get('route')}"
            for name, lat, lon in r.get("stops", []):
                self._add(name, lat, lon, kind)

    def _add(self, name: str, lat: float, lon: float, kind: str) -> None:
        key = norm_key(name)
        if key:
            self._names.setdefault(key, []).append((lat, lon, kind))

    def __len__(self) -> int:
        return len(self._eva)

    def by_eva(self, eva: str | None) -> tuple[float, float] | None:
        return self._eva.get(str(eva)) if eva else None

    def find(self, name: str, *, near: tuple[float, float] | None = None, mode: str | None = None) -> tuple[float, float] | None:
        ranks = _KIND_RANK.get(mode or "", {})
        for priority, key in enumerate(name_keys(name)):
            found = self._names.get(key)
            if not found:
                continue
            if near is not None:
                cands = [(ranks.get(k, 2), km(near, (la, lo)), (la, lo)) for la, lo, k in found]
                cands = [c for c in cands if c[1] <= ANCHOR_MAX_KM]
                if cands:
                    return min(cands)[2]
            # Kein Treffer beim Anker (z. B. nächster ICE-Halt 200 km weiter):
            # nur, wenn alle Treffer am selben Ort liegen
            first = (found[0][0], found[0][1])
            if all(km(first, (la, lo)) <= UNANCHORED_SPREAD_KM for la, lo, _k in found):
                best = min(found, key=lambda f: ranks.get(f[2], 2))
                return (best[0], best[1])
        return None
