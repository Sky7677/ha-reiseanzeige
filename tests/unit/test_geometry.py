"""Lage der Halte und Streckenverlauf aus dem Gleisnetz."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.geometry import apply_geometry, cut_line, fill_coordinates
from core.journey import build_snapshot, draft_snapshot
from core.places import Places, km, name_keys, norm_key
from core.railnet.router import RailNet
from core.share_text import parse_share_text
from core.timetables import RateLimiter, TimetablesClient
from fake_iris import november_world
from railnet_fixture import P, world
from testdata import fixture_text

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 1, 14, 17, tzinfo=TZ)


@pytest.fixture(scope="module")
def net():
    return RailNet(world())


@pytest.fixture(scope="module")
def places(net):
    return Places(net.stations, net.routes)


def journey():
    return parse_share_text(fixture_text("share", "moritzplatz-berlin-2026-11-01.txt"))


def near(line, p, tol_km=0.3):
    return any(km(tuple(q), p) <= tol_km for q in line)


# --- Namen ----------------------------------------------------------------------------

@pytest.mark.parametrize("hafas, osm", [
    ("Augsburger Str.(U), Berlin", "Augsburger Straße"),
    ("Heidelberger Platz (S+U), Berlin", "Heidelberger Platz"),
    ("Hauptstr., Musterstadt", "Hauptstraße"),
    ("Moritzplatz, Augsburg (Bayern)", "Moritzplatz"),
    ("Berlin Heidelberger Platz", "Heidelberger Platz"),
    ("München Hbf", "München Hauptbahnhof"),
])
def test_namen_aus_dem_navigator_passen_zu_osm(hafas, osm):
    assert norm_key(osm) in name_keys(hafas)


def test_gleiche_namen_in_verschiedenen_staedten(places):
    assert places.find("Moritzplatz, Augsburg (Bayern)") is None, "ohne Anker mehrdeutig"
    assert places.find("Moritzplatz, Augsburg (Bayern)", near=P["Augsburg Hbf"], mode="tram") == P["Moritzplatz"]
    assert places.find("Königsplatz, Augsburg (Bayern)", near=P["Augsburg Hbf"], mode="tram") == P["Königsplatz"]
    assert places.find("Breitenbachplatz (U), Berlin") == P["Breitenbachplatz"], "eindeutig"
    assert places.find("Augsburg Hbf") == P["Augsburg Hbf"]
    assert places.by_eva("8089015") == P["Berlin Heidelberger Platz"]
    # U-Bahn-Halt statt S-Bahnhof, wenn die U-Bahn gemeint ist
    assert places.find("Heidelberger Platz (S+U), Berlin", near=P["Berlin Südkreuz"], mode="subway") == P["Heidelberger Platz (U)"]


def test_koordinaten_nur_aus_dem_text(places):
    snap = draft_snapshot(journey(), NOW)
    assert fill_coordinates(snap, places)
    stops = [(so["station"]["name"], (so["station"]["lat"], so["station"]["lon"])) for l in snap["legs"] for so in l["stopovers"]]
    assert stops == [
        ("Moritzplatz, Augsburg (Bayern)", P["Moritzplatz"]),
        ("Königsplatz, Augsburg (Bayern)", P["Königsplatz"]),
        ("Augsburg Hbf", P["Augsburg Hbf"]),
        ("Nürnberg Hbf", P["Nürnberg Hbf"]),
        ("Nürnberg Hbf", P["Nürnberg Hbf"]),
        ("Berlin Südkreuz", P["Berlin Südkreuz"]),
        ("Berlin Südkreuz (S)", P["Berlin Südkreuz"]),  # ohne Nummer: derselbe Bahnhof, nächster Eintrag
        ("Berlin Heidelberger Platz", P["Berlin Heidelberger Platz"]),
        ("Heidelberger Platz (S+U), Berlin", P["Heidelberger Platz (U)"]),
        ("Breitenbachplatz (U), Berlin", P["Breitenbachplatz"]),
    ]
    assert (snap["origin"]["lat"], snap["origin"]["lon"]) == P["Moritzplatz"]
    assert (snap["legs"][1]["destination"]["lat"], snap["legs"][1]["destination"]["lon"]) == P["Nürnberg Hbf"]
    assert not fill_coordinates(snap, places), "nichts mehr zu tun"


def test_staDa_als_weitere_quelle():
    snap = draft_snapshot(journey(), NOW)
    stada = {"Augsburg Hbf": (48.0, 10.0)}
    assert fill_coordinates(snap, None, lambda eva, name: stada.get(name))
    assert (snap["legs"][1]["stopovers"][0]["station"]["lat"]) == 48.0
    assert snap["legs"][0]["stopovers"][0]["station"]["lat"] is None


# --- Verlauf -----------------------------------------------------------------------------

async def test_verlauf_je_verkehrsmittel(net, places):
    client = TimetablesClient(november_world(), "cid", "key", limiter=RateLimiter(limit=1000))
    snap = await build_snapshot(journey(), client, NOW)
    assert apply_geometry(snap, net, places, version="2026-11-02")
    tram, re1, ice, s41, u3 = snap["legs"]

    assert tram["geometry"]["source"] == "line", "Tram 1 entlang der OSM-Linie (dort in Gegenrichtung erfasst)"
    assert tram["polyline"][0] == list(P["Moritzplatz"]) and tram["polyline"][-1] == list(P["Königsplatz"])

    assert re1["geometry"] == {"key": re1["geometry"]["key"], "source": "tracks", "routed": 3, "segments": 3}
    assert near(re1["polyline"], P["Kurve"], 0.05), "auf den Gleisen, nicht gerade"
    assert near(re1["polyline"], P["Donauwörth"], 0.05) and near(re1["polyline"], P["Treuchtlingen"], 0.05)

    assert ice["geometry"]["source"] == "tracks" and near(ice["polyline"], P["Erfurt Hbf"], 0.05)
    assert s41["geometry"]["source"] == "tracks" and near(s41["polyline"], P["Berlin Innsbrucker Platz"], 0.05)
    assert u3["geometry"]["source"] == "line" and u3["polyline"][-1] == list(P["Breitenbachplatz"])
    assert not near(u3["polyline"], P["Fehrbelliner Platz (U)"]), "nur das gefahrene Stück"

    # Zweiter Durchlauf: nichts neu zu berechnen; neue Gleisnetz-Version: neu
    assert not apply_geometry(snap, net, places, version="2026-11-02")
    assert apply_geometry(snap, net, places, version="2026-12-02")


def test_ohne_gleise_bleibt_es_bei_geraden_linien(net, places):
    snap = draft_snapshot(parse_share_text(fixture_text("share", "augsburg-berlin-2026-10-08.txt")), NOW)
    apply_geometry(snap, net, places, version="v")
    re16, ice = snap["legs"]
    assert re16["geometry"]["source"] == "tracks"
    assert ice["geometry"]["source"] == "tracks"
    # Ohne Gleisnetz: nur Koordinaten, kein Verlauf
    snap2 = draft_snapshot(parse_share_text(fixture_text("share", "augsburg-berlin-2026-10-08.txt")), NOW)
    apply_geometry(snap2, None, places)
    assert all(l["polyline"] is None and "geometry" not in l for l in snap2["legs"])


def test_bus_ohne_verlauf(net, places):
    snap = draft_snapshot(parse_share_text(fixture_text("share", "berlin-moritzplatz-bus-2026-11-01.txt")), NOW)
    apply_geometry(snap, net, places, version="v")
    for leg in snap["legs"]:
        if leg["line"]["product"] == "bus":
            assert leg["polyline"] is None


def test_linie_schneiden():
    shape = [[0.0, 0.0], [0.0, 0.01], [0.0, 0.02], [0.0, 0.03]]
    pts, d = cut_line(shape, (0.0001, 0.005), (0.0, 0.025))
    assert pts[0] == (0.0, 0.005) and pts[-1] == (0.0, 0.025) and pts[1:-1] == [(0.0, 0.01), (0.0, 0.02)]
    assert d == pytest.approx(11.1, abs=0.5)
    assert cut_line(shape, (0.0, 0.025), (0.0, 0.005)) is None, "falsche Richtung"
    assert cut_line(shape, (0.01, 0.005), (0.0, 0.025)) is None, "Halt zu weit weg"
