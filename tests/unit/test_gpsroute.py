"""Umleitung ohne Halt per GPS: ICE 1004 fährt ab Erfurt über Leipzig statt Halle,
die Bahn meldet nichts – die Standorte des Handys legen die Linie neu."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.geometry import apply_geometry
from core.gpsroute import Fix, Watch, dist_to_line_m, read_fix, step
from core.journey import build_snapshot
from core.places import Places, km
from core.railnet.router import RailNet
from core.realtime import compare_paths
from core.share_text import parse_share_text
from core.timetables import RateLimiter, TimetablesClient
from fake_iris import november_world
from railnet_fixture import P, world
from testdata import fixture_text

TZ = ZoneInfo("Europe/Berlin")


@pytest.fixture(scope="module")
def net():
    return RailNet(world())


@pytest.fixture(scope="module")
def places(net):
    return Places(net.stations, net.routes)


async def trip(net, places):
    client = TimetablesClient(november_world(), "cid", "key", limiter=RateLimiter(limit=1000))
    snap = await build_snapshot(parse_share_text(fixture_text("share", "moritzplatz-berlin-2026-11-01.txt")), client,
                                datetime(2026, 11, 1, 14, 17, tzinfo=TZ))
    apply_geometry(snap, net, places, version="v")
    return snap


def at(h, m):
    return datetime(2026, 11, 1, h, m, tzinfo=TZ)


def between(a, b, f):
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


def near(line, p, tol_km):
    return any(km(tuple(q), p) <= tol_km for q in line)


async def test_umleitung_ohne_meldung_der_bahn(net, places):
    snap = await trip(net, places)
    ice = snap["legs"][2]
    planned = ice["polyline"]
    erf, lei, sk = P["Erfurt Hbf"], P["Leipzig Hbf"], P["Berlin Südkreuz"]
    w = Watch()
    go = lambda p, t: step(snap, None, w, Fix(p[0], p[1], 20.0, t), net, places, version="v")  # noqa: E731

    assert not go(between(erf, lei, 0.25), at(18, 0)), "ein Standort daneben genügt nicht"
    assert "diversion" not in ice
    assert go(between(erf, lei, 0.5), at(18, 10)), "zweiter Standort in Folge: neu zeichnen"
    d = ice["diversion"]
    assert d["gps"]["after"]["name"] == "Erfurt Hbf" and d["path"] is None and d["geometry"]["gps"]
    assert near(d["polyline"], P["Erfurt Hbf"], 0.1) and ice["polyline"] == planned, "geplanter Verlauf bleibt daneben"

    go(between(erf, lei, 0.8), at(18, 20))
    go(between(lei, sk, 0.3), at(18, 50))
    go(between(lei, sk, 0.7), at(19, 15))
    line = ice["diversion"]["polyline"]
    assert near(line, lei, 0.1) and near(line, sk, 0.1)
    assert not near(line, P["Halle (Saale) Hbf"], 10), "Halle ohne Standort in der Nähe: nicht angefahren"
    assert line[:len(ice["diversion"]["gps"]["prefix"])] == ice["diversion"]["gps"]["prefix"]
    assert all(dist_to_line_m(between(lei, sk, f), line) < 50 for f in (0.1, 0.5, 0.9))

    # Gemeldete Echtzeit ohne Umleitung ändert daran nichts (GPS bleibt)
    assert not apply_geometry(snap, net, places, version="v", diversions={2: None})
    assert ice["diversion"]["gps"]


async def test_auf_der_geplanten_strecke_und_ohne_gleis_passiert_nichts(net, places):
    snap = await trip(net, places)
    erf, hal, lei = P["Erfurt Hbf"], P["Halle (Saale) Hbf"], P["Leipzig Hbf"]
    w = Watch()
    for k, f in enumerate((0.2, 0.4, 0.6, 0.8)):
        p = between(erf, hal, f)
        assert not step(snap, None, w, Fix(p[0] + 0.0015, p[1], 80.0, at(17, 46 + 3 * k)), net, places)  # ~170 m daneben
    # Weit neben jedem Gleis (Straße): zählt nicht, auch mehrmals
    for k in range(3):
        p = between(erf, lei, 0.3 + 0.1 * k)
        assert not step(snap, None, w, Fix(p[0] + 0.05, p[1], 10.0, at(18, 15 + k)), net, places)
    # Ungenau oder alt: zählt nicht
    p = between(erf, lei, 0.5)
    assert not step(snap, None, w, Fix(p[0], p[1], 400.0, at(18, 30)), net, places)
    assert not step(snap, None, w, Fix(p[0], p[1], 10.0, at(18, 30)), net, places), "gleiche Zeit: schon gesehen"
    assert "diversion" not in snap["legs"][2]


async def test_gemeldete_umleitung_und_gps_zusammen(net, places):
    """Bahn meldet Halle entfällt, Leipzig neu; GPS füllt den Weg – die Haltliste bleibt die der Bahn."""
    snap = await trip(net, places)
    ice = snap["legs"][2]
    div = compare_paths([so["station"]["name"] for so in ice["stopovers"]],
                        ["Nürnberg Hbf", "Erfurt Hbf", "Leipzig Hbf", "Berlin Südkreuz"])
    apply_geometry(snap, net, places, version="v", diversions={2: div})
    erf, lei = P["Erfurt Hbf"], P["Leipzig Hbf"]
    w = Watch()
    # Die gemeldete Umleitung führt schon über Leipzig: Standorte darauf lösen nichts aus
    for k, f in enumerate((0.3, 0.6)):
        p = between(erf, lei, f)
        assert not step(snap, None, w, Fix(p[0], p[1], 20.0, at(18, 5 * k + 5)), net, places, version="v")
    assert not ice["diversion"].get("gps")
    names = [so["station"]["name"] for so in ice["diversion"]["stopovers"]]
    assert names == ["Nürnberg Hbf", "Erfurt Hbf", "Halle (Saale) Hbf", "Leipzig Hbf", "Berlin Südkreuz"]


def test_standort_aus_dem_zustand():
    class S:
        def __init__(self, state, attrs, when):
            self.state, self.attributes, self.last_updated = state, attrs, when
    now = at(18, 0)
    f = read_fix(S("not_home", {"latitude": 51.0, "longitude": 11.5, "gps_accuracy": 12}, now - timedelta(minutes=1)), now)
    assert f.point == (51.0, 11.5) and f.accuracy == 12
    assert read_fix(S("not_home", {"latitude": 51.0, "longitude": 11.5}, now), now).accuracy == float("inf")
    assert read_fix(S("not_home", {"latitude": 51.0, "longitude": 11.5}, now - timedelta(minutes=11)), now) is None, "zu alt"
    assert read_fix(S("unavailable", {}, now), now) is None
    assert read_fix(None, now) is None


async def test_keine_umleitung_wenn_der_zug_nur_verspaetet_ist(net, places):
    """Verspätung erst später bekannt: Die Linie wurde ab einem Halt neu gezeichnet, an
    dem der Zug laut neuer Echtzeit noch gar nicht war – die Umleitung entfällt."""
    snap = await trip(net, places)
    ice = snap["legs"][2]
    erf, lei = P["Erfurt Hbf"], P["Leipzig Hbf"]
    w = Watch()
    go = lambda p, t, rt=None: step(snap, rt, w, Fix(p[0], p[1], 20.0, t), net, places, version="v")  # noqa: E731
    go(between(erf, lei, 0.25), at(18, 0))
    assert go(between(erf, lei, 0.5), at(18, 10)) and ice["diversion"]["gps"]["after"]["name"] == "Erfurt Hbf"
    def part(t=None):
        return {"planned": None, "prognosed": t, "delay": None}
    stops = [{"stationId": so["station"]["id"], "arrival": part(), "departure": part()} for so in ice["stopovers"]]
    stops[1]["departure"] = part("2026-11-01T18:30:00+01:00")     # Erfurt ab 18:30 statt 17:44
    rt = {"legs": {"2": {"stopovers": stops}}}
    assert go(between(erf, lei, 0.6), at(18, 20), rt), "Zug noch vor Erfurt: Umleitung entfällt"
    assert "diversion" not in ice
