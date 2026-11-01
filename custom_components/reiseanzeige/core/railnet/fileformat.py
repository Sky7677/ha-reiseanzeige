"""Dateiformat des Gleisnetzes (Version 1) – Schreiben und Lesen.

Aufbau (alle Zahlen little-endian):

    8 Byte  Kennung b"RLNET\\x00\\x01\\x00"
    4 Byte  Länge L des Kopfes (uint32)
    L Byte  Kopf, JSON (UTF-8):
              {"format": 1, "created": …, "source": …, "license": …,
               "cell_deg": 0.02,
               "blocks": {name: [typ, anzahl, offset], …}}
    danach  Blöcke, jeweils auf 8 Byte ausgerichtet. typ ist ein Kürzel des
            array-Moduls ("i" int32, "I" uint32, "h" int16, "B" uint8,
            "f" float32) oder "json" (UTF-8-Text, anzahl = Bytes).

Blöcke:
    node_lat, node_lon      i   Knoten des Graphen (Weichen, Enden) in 1e-6 Grad
    edge_from, edge_to      I   Kante = Gleisstück zwischen zwei Knoten
    edge_len                f   Länge in Metern
    edge_class              B   Streckenart (CLASSES)
    edge_bear_from          h   Richtung beim Verlassen von edge_from (Grad, 0 = Nord)
    edge_bear_to            h   Richtung beim Ankommen an edge_to (in Fahrtrichtung from→to)
    edge_shape_start        I   Anfang des Verlaufs je Kante in shape_*, Länge E+1
    shape_lat, shape_lon    i   Verlauf aller Kanten hintereinander, von from nach to
    adj_start               I   je Knoten Anfang in adj_items, Länge N+1
    adj_items               I   Kante*2 + r: r=0 Kante beginnt hier (vorwärts befahren),
                                r=1 Kante endet hier (rückwärts befahren)
    grid_keys               I   Rasterzellen (sortiert), Schlüssel = ilat*20000 + ilon
                                mit ilat = floor((lat+90)/cell_deg), ilon = floor((lon+180)/cell_deg)
    grid_start              I   Anfang je Zelle in grid_items, Länge C+1
    grid_items              I   Kanten, die die Zelle berühren
    stations                json [[name, eva|null, ds100|null, lat, lon, art], …]
    routes                  json [{"route", "ref", "name", "network", "from", "to",
                                   "shape": [[lat, lon], …], "stops": [[name, lat, lon], …]}, …]

Lizenz der Daten: ODbL 1.0, © OpenStreetMap-Mitwirkende.
"""

from __future__ import annotations

import json
import struct
import sys
from array import array

MAGIC = b"RLNET\x00\x01\x00"
FORMAT_VERSION = 1
CELL_DEG = 0.02
GRID_STRIDE = 20000

# Streckenarten
MAIN, BRANCH, RAIL_OTHER, FREIGHT, CROSSOVER, LIGHT_RAIL, SUBWAY, TRAM, NARROW = range(9)
CLASSES = {
    MAIN: "main", BRANCH: "branch", RAIL_OTHER: "rail", FREIGHT: "freight", CROSSOVER: "crossover",
    LIGHT_RAIL: "light_rail", SUBWAY: "subway", TRAM: "tram", NARROW: "narrow_gauge",
}


def cell_of(lat: float, lon: float, cell_deg: float = CELL_DEG) -> int:
    ilat = int((lat + 90) // cell_deg)
    ilon = int((lon + 180) // cell_deg)
    return ilat * GRID_STRIDE + ilon


def write(path: str, header: dict, blocks: dict[str, tuple[str, object]]) -> None:
    """blocks: name → (typ, array oder JSON-fähiges Objekt)."""
    payloads: list[tuple[str, str, int, bytes]] = []
    for name, (typ, data) in blocks.items():
        if typ == "json":
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            payloads.append((name, typ, len(raw), raw))
        else:
            arr = data if isinstance(data, array) else array(typ, data)
            if arr.typecode != typ:
                arr = array(typ, arr)
            payloads.append((name, typ, len(arr), arr.tobytes()))

    def layout(header_len: int) -> tuple[dict, int]:
        pos = 8 + 4 + header_len
        pos += (-pos) % 8
        table = {}
        for name, typ, count, raw in payloads:
            table[name] = [typ, count, pos]
            pos += len(raw)
            pos += (-pos) % 8
        return table, pos

    head = dict(header, format=FORMAT_VERSION, cell_deg=CELL_DEG)
    # Kopflänge hängt von den Offsets ab und umgekehrt: bis zur Stabilität wiederholen
    length = 0
    for _ in range(5):
        head["blocks"], _end = layout(length)
        raw_head = json.dumps(head, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw_head) == length:
            break
        length = len(raw_head)
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<I", len(raw_head)))
        f.write(raw_head)
        for name, typ, count, raw in payloads:
            offset = head["blocks"][name][2]
            f.write(b"\x00" * (offset - f.tell()))
            f.write(raw)


def read(data: bytes) -> tuple[dict, dict[str, object]]:
    """Datei-Inhalt → (Kopf, Blöcke). JSON-Blöcke als Objekt, Zahlenblöcke als
    Sequenz mit Index-Zugriff: auf Little-Endian-Rechnern (praktisch alle) als
    memoryview direkt auf data – ohne Kopie, das spart in Home Assistant die
    Hälfte des Speichers –, sonst als umgedrehtes array."""
    if data[:8] != MAGIC:
        raise ValueError("Keine Gleisnetz-Datei (Kennung fehlt) oder unbekannte Version")
    (length,) = struct.unpack_from("<I", data, 8)
    head = json.loads(bytes(data[12:12 + length]).decode("utf-8"))
    view = memoryview(data)
    blocks: dict[str, object] = {}
    for name, (typ, count, offset) in head["blocks"].items():
        if typ == "json":
            blocks[name] = json.loads(bytes(view[offset:offset + count]).decode("utf-8"))
            continue
        size = array(typ).itemsize
        raw = view[offset:offset + count * size]
        if len(raw) != count * size:
            raise ValueError(f"Gleisnetz-Datei unvollständig (Block {name})")
        if sys.byteorder == "little":
            blocks[name] = raw.cast(typ)
        else:
            arr = array(typ)
            arr.frombytes(raw)
            arr.byteswap()
            blocks[name] = arr
    return head, blocks
