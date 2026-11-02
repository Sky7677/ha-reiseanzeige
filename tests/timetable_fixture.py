"""Kleiner Fahrplan für die Tests (Aufbau wie railnet/timetable.py im Repo reiseanzeige-gleisnetz).

Passend zu den Testreisen: RE16 und ICE 1604 am 08.10.2026 (augsburg-berlin),
Tram 1 am 01.11.2026 (moritzplatz-berlin), dazu Ersatzbusse ab der Bushaltestelle
am Augsburger Hauptbahnhof. Namen wie in den echten Feeds, Berlin also im VBB-Stil –
gefunden wird über die Lage.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
from array import array

from core.railnet import fileformat as ff
from railnet_fixture import P

WINDOW_START = "2026-10-01"

PLACES = [
    ["Augsburg Hbf", *P["Augsburg Hbf"]],                                   # 0
    ["Donauwörth", *P["Donauwörth"]],                                       # 1
    ["Treuchtlingen", *P["Treuchtlingen"]],                                 # 2
    ["Nürnberg Hbf", P["Nürnberg Hbf"][0] + 0.0003, P["Nürnberg Hbf"][1]],  # 3 (30 m daneben)
    ["Erfurt Hbf", *P["Erfurt Hbf"]],                                       # 4
    ["S Südkreuz Bhf (Berlin)", 52.4760, 13.3652],                          # 5
    ["Augsburg, Moritzplatz", *P["Moritzplatz"]],                           # 6
    ["Augsburg, Theodor-Heuss-Platz", 48.3670, 10.8960],                    # 7
    ["Augsburg, Königsplatz", *P["Königsplatz"]],                           # 8
    ["Hauptbahnhof, Augsburg", 48.3651, 10.8862],                          # 9  Bushaltestelle am Bahnhof
    ["Ersatzhalt Mitte", 48.54, 10.83],                                     # 10
    ["Bahnhof, Donauwörth", 48.7140, 10.7722],                              # 11
]
ROUTES = [["regional", "RE16"], ["longdistance", "ICE"], ["tram", "1"], ["replacement", "RE1"], ["replacement", "RE80"]]
# (Linie, [(Station, Ankunft, Abfahrt) in Minuten ab Abfahrt], [(Startminute, Tage ab WINDOW_START)])
PATTERNS = [
    (0, [(0, 0, 0), (1, 33, 34), (2, 60, 62), (3, 93, 93)], [(5 * 60 + 57, range(0, 32))]),
    (1, [(3, 0, 0), (4, 64, 66), (5, 161, 161)], [(7 * 60 + 36, [7, 14])]),      # nur 08.10. und 15.10.
    (2, [(6, 0, 0), (7, 4, 4), (8, 9, 9)], [(14 * 60 + 18, range(0, 32))]),
    # Ersatzbus RE1 am 01.11. – der Zug RE1 fährt laut Bahn um 14:37: Plan überholt
    (3, [(9, 0, 0), (11, 49, 49)], [(14 * 60 + 41, [31])]),
    # Ersatzbus RE80, auch über Mitternacht
    (4, [(9, 0, 0), (10, 25, 26), (11, 55, 55)], [(14 * 60 + 50, [31]), (23 * 60 + 41, range(0, 32))]),
]


def build() -> bytes:
    pat_route, pat_start, pat_place, pat_arr, pat_dep = array("I"), array("I", [0]), array("I"), array("h"), array("h")
    pat_trip_start, trip_start, trip_days = array("I", [0]), array("H"), array("I")
    items: dict[int, list] = {}
    for p, (route, stops, trips) in enumerate(PATTERNS):
        pat_route.append(route)
        for pos, (pl, a, d) in enumerate(stops):
            pat_place.append(pl)
            pat_arr.append(a)
            pat_dep.append(d)
            items.setdefault(pl, []).append((p, pos))
        pat_start.append(len(pat_place))
        for start, days in trips:
            trip_start.append(start)
            trip_days.append(sum(1 << d for d in days))
        pat_trip_start.append(len(trip_start))
    place_start, place_pat, place_pos = array("I", [0]), array("I"), array("H")
    for pl in range(len(PLACES)):
        for p, pos in items.get(pl, []):
            place_pat.append(p)
            place_pos.append(pos)
        place_start.append(len(place_pat))
    blocks = {
        "places": ("json", PLACES), "routes": ("json", ROUTES),
        "pat_route": ("I", pat_route), "pat_start": ("I", pat_start), "pat_place": ("I", pat_place),
        "pat_arr": ("h", pat_arr), "pat_dep": ("h", pat_dep), "pat_trip_start": ("I", pat_trip_start),
        "trip_start": ("H", trip_start), "trip_days": ("I", trip_days),
        "place_start": ("I", place_start), "place_pat": ("I", place_pat), "place_pos": ("H", place_pos),
    }
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        ff.write(path, {"kind": "timetable", "source": "Test", "license": "CC BY 4.0", "window_start": WINDOW_START,
                        "window_days": 32}, blocks)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


def release(raw: bytes, version: str = "2026-10-01") -> tuple[bytes, dict]:
    gz = gzip.compress(raw, mtime=0)
    return gz, {"format": 1, "kind": "timetable", "version": version, "file": "fahrplan-de.bin.gz", "size": len(gz),
                "sha256": hashlib.sha256(gz).hexdigest(), "window_start": WINDOW_START, "source": "Test"}
