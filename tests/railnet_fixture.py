"""Kleines Gleisnetz für die Tests, ohne OSM-Werkzeuge gebaut.

Das echte Netz baut https://github.com/Sky7677/reiseanzeige-gleisnetz aus
OpenStreetMap. Hier genügt ein vereinfachter Bau: jeder Weg ist eine Kante,
Wege hängen an gleichen Endpunkten zusammen (Weichen also selbst teilen).

world() folgt der Testreise vom 01.11.2026 (Tram 1 in Augsburg, RE1,
ICE 1004, S41, U3 in Berlin) mit Lockvögeln gleichen Namens in anderen Städten.
"""

from __future__ import annotations

import gzip
import hashlib
from array import array

from core.railnet import fileformat as ff
from core.railnet.geo import bearing, dist_m

# Lage der Halte (gerundet, für Tests genau genug)
P = {
    "Augsburg Hbf": (48.3655, 10.8856),
    "Kurve": (48.55, 10.95),                     # Gleisbogen zwischen Augsburg und Donauwörth
    "Donauwörth": (48.7136, 10.7717),
    "Treuchtlingen": (48.9567, 10.9097),
    "Nürnberg Hbf": (49.4456, 11.0825),
    "Erfurt Hbf": (50.9725, 11.0380),
    "Halle (Saale) Hbf": (51.4776, 11.9870),
    "Berlin Südkreuz": (52.4753, 13.3655),
    "Berlin Hbf": (52.5251, 13.3694),
    "Berlin Südkreuz (S)": (52.4758, 13.3650),
    "Berlin-Schöneberg": (52.4793, 13.3530),
    "Berlin Innsbrucker Platz": (52.4783, 13.3427),
    "Berlin Heidelberger Platz": (52.4798, 13.3123),
    "Moritzplatz": (48.3683, 10.8986),
    "Königsplatz": (48.3656, 10.8939),
    "Hauptbahnhof (Tram)": (48.3660, 10.8870),
    "Heidelberger Platz (U)": (52.4797, 13.3127),
    "Fehrbelliner Platz (U)": (52.4903, 13.3141),
    "Breitenbachplatz": (52.4667, 13.3083),
}


def build(ways: list[tuple[int, list]], stations: list, routes: list) -> bytes:
    nodes: dict[tuple[float, float], int] = {}
    node_lat, node_lon = array("i"), array("i")

    def node(p) -> int:
        key = (round(p[0], 6), round(p[1], 6))
        if key not in nodes:
            nodes[key] = len(node_lat)
            node_lat.append(round(p[0] * 1e6))
            node_lon.append(round(p[1] * 1e6))
        return nodes[key]

    e_from, e_to, e_len, e_cls = array("I"), array("I"), array("f"), array("B")
    b_from, b_to = array("h"), array("h")
    shape_start, s_lat, s_lon = array("I", [0]), array("i"), array("i")
    for cls, pts in ways:
        e_from.append(node(pts[0]))
        e_to.append(node(pts[-1]))
        e_len.append(sum(dist_m(a, b) for a, b in zip(pts, pts[1:])))
        e_cls.append(cls)
        b_from.append(round(bearing(pts[0], pts[1])) % 360)
        b_to.append(round(bearing(pts[-2], pts[-1])) % 360)
        for p in pts:
            s_lat.append(round(p[0] * 1e6))
            s_lon.append(round(p[1] * 1e6))
        shape_start.append(len(s_lat))
    incident = [[] for _ in range(len(node_lat))]
    for e in range(len(e_from)):
        incident[e_from[e]].append(e * 2)
        incident[e_to[e]].append(e * 2 + 1)
    adj_start, adj_items = array("I", [0]), array("I")
    for items in incident:
        adj_items.extend(items)
        adj_start.append(len(adj_items))
    cells: dict[int, set[int]] = {}
    for e in range(len(e_from)):
        pts = [(s_lat[i] / 1e6, s_lon[i] / 1e6) for i in range(shape_start[e], shape_start[e + 1])]
        for a, b in zip(pts, pts[1:]):
            n = int(dist_m(a, b) // 400) + 1
            for k in range(n + 1):
                t = k / n
                cells.setdefault(ff.cell_of(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), set()).add(e)
    grid_keys, grid_start, grid_items = array("I"), array("I", [0]), array("I")
    for key in sorted(cells):
        grid_keys.append(key)
        grid_items.extend(sorted(cells[key]))
        grid_start.append(len(grid_items))
    blocks = {
        "node_lat": ("i", node_lat), "node_lon": ("i", node_lon),
        "edge_from": ("I", e_from), "edge_to": ("I", e_to), "edge_len": ("f", e_len), "edge_class": ("B", e_cls),
        "edge_bear_from": ("h", b_from), "edge_bear_to": ("h", b_to),
        "edge_shape_start": ("I", shape_start), "shape_lat": ("i", s_lat), "shape_lon": ("i", s_lon),
        "adj_start": ("I", adj_start), "adj_items": ("I", adj_items),
        "grid_keys": ("I", grid_keys), "grid_start": ("I", grid_start), "grid_items": ("I", grid_items),
        "stations": ("json", stations), "routes": ("json", routes),
    }
    import os, tempfile
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        ff.write(path, {"source": "Test", "license": "ODbL 1.0 – © OpenStreetMap-Mitwirkende", "grid_sample_m": 400.0}, blocks)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


def world() -> bytes:
    rail = [P[n] for n in ("Augsburg Hbf", "Kurve", "Donauwörth")]
    ways = [
        (ff.MAIN, rail),
        (ff.MAIN, [P["Donauwörth"], P["Treuchtlingen"]]),
        (ff.MAIN, [P["Treuchtlingen"], P["Nürnberg Hbf"]]),
        (ff.MAIN, [P["Nürnberg Hbf"], P["Erfurt Hbf"]]),
        (ff.MAIN, [P["Erfurt Hbf"], P["Halle (Saale) Hbf"]]),
        (ff.MAIN, [P["Halle (Saale) Hbf"], P["Berlin Südkreuz"]]),
        (ff.MAIN, [P["Berlin Südkreuz"], P["Berlin Hbf"]]),
        (ff.LIGHT_RAIL, [P[n] for n in ("Berlin Südkreuz (S)", "Berlin-Schöneberg", "Berlin Innsbrucker Platz", "Berlin Heidelberger Platz")]),
        (ff.TRAM, [P[n] for n in ("Moritzplatz", "Königsplatz", "Hauptbahnhof (Tram)")]),
        (ff.SUBWAY, [P[n] for n in ("Fehrbelliner Platz (U)", "Heidelberger Platz (U)", "Breitenbachplatz")]),
    ]
    stations = [
        ["Augsburg Hbf", "8000013", "MA", *P["Augsburg Hbf"], "rail"],
        ["Donauwörth", "8000080", "MDO", *P["Donauwörth"], "rail"],
        ["Treuchtlingen", "8000122", "NTL", *P["Treuchtlingen"], "rail"],
        ["Nürnberg Hbf", "8000284", "NN", *P["Nürnberg Hbf"], "rail"],
        ["Erfurt Hbf", "8010101", "UE", *P["Erfurt Hbf"], "rail"],
        ["Halle (Saale) Hbf", "8010159", "LH", *P["Halle (Saale) Hbf"], "rail"],
        ["Berlin Südkreuz", "8011113", "BPAF", *P["Berlin Südkreuz"], "rail"],
        ["Berlin Hbf", "8011160", "BLS", *P["Berlin Hbf"], "rail"],
        ["Berlin Südkreuz", "8089110", None, *P["Berlin Südkreuz (S)"], "light_rail"],
        ["Berlin-Schöneberg", "8089032", None, *P["Berlin-Schöneberg"], "light_rail"],
        ["Berlin Innsbrucker Platz", "8089017", None, *P["Berlin Innsbrucker Platz"], "light_rail"],
        ["Berlin Heidelberger Platz", "8089015", None, *P["Berlin Heidelberger Platz"], "light_rail"],
        ["Heidelberger Platz", None, None, *P["Heidelberger Platz (U)"], "subway"],
        ["Breitenbachplatz", None, None, *P["Breitenbachplatz"], "subway"],
        ["Moritzplatz", None, None, *P["Moritzplatz"], "tram"],
        ["Königsplatz", None, None, *P["Königsplatz"], "tram"],
        # Lockvögel: gleicher Name, andere Stadt
        ["Moritzplatz", None, None, 52.5037, 13.4108, "subway"],     # Berlin, U8
        ["Königsplatz", None, None, 48.1458, 11.5655, "tram"],        # München
    ]
    routes = [
        {"route": "tram", "ref": "1", "name": "Tram 1: Göggingen => Lechhausen", "network": "AVV",
         "shape": [list(P[n]) for n in ("Moritzplatz", "Königsplatz", "Hauptbahnhof (Tram)")][::-1],  # andere Richtung erfasst
         "stops": [["Hauptbahnhof", *P["Hauptbahnhof (Tram)"]], ["Königsplatz", *P["Königsplatz"]], ["Moritzplatz", *P["Moritzplatz"]]]},
        {"route": "tram", "ref": "2", "name": "Tram 2", "shape": [list(P["Moritzplatz"]), list(P["Königsplatz"])], "stops": []},
        {"route": "subway", "ref": "U3", "name": "U3: Warschauer Straße => Krumme Lanke",
         "shape": [list(P[n]) for n in ("Fehrbelliner Platz (U)", "Heidelberger Platz (U)", "Breitenbachplatz")],
         "stops": [["Fehrbelliner Platz", *P["Fehrbelliner Platz (U)"]], ["Heidelberger Platz", *P["Heidelberger Platz (U)"]], ["Breitenbachplatz", *P["Breitenbachplatz"]]]},
    ]
    return build(ways, stations, routes)


def release(raw: bytes, version: str = "2026-11-02") -> tuple[bytes, dict]:
    gz = gzip.compress(raw, mtime=0)
    return gz, {"format": 1, "version": version, "file": "gleisnetz-de.bin.gz", "size": len(gz),
                "sha256": hashlib.sha256(gz).hexdigest(), "source": "Test", "license": "ODbL 1.0",
                "counts": {"edges": 10}}
