"""Zwischenhalte aus dem Fahrplan (GTFS) für Abschnitte aus dem geteilten Text."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.geometry import apply_geometry, fill_coordinates
from core.journey import draft_snapshot
from core.places import Places, km, norm_key
from core.railnet.plan import Timetable
from core.railnet.router import RailNet
from core.schedule import fill_from_timetable
from core.share_text import parse_share_text
from railnet_fixture import P, world
from testdata import fixture_text
from timetable_fixture import build

TZ = ZoneInfo("Europe/Berlin")


@pytest.fixture(scope="module")
def net():
    return RailNet(world())


@pytest.fixture(scope="module")
def places(net):
    return Places(net.stations, net.routes)


@pytest.fixture(scope="module")
def tt():
    return Timetable(build(), key=norm_key)


def draft(name, now):
    return draft_snapshot(parse_share_text(fixture_text("share", name)), now)


def test_zug_tage_im_voraus_bekommt_zwischenhalte(places, tt):
    snap = draft("augsburg-berlin-2026-10-08.txt", datetime(2026, 10, 5, 12, tzinfo=TZ))
    fill_coordinates(snap, places)
    assert fill_from_timetable(snap, tt)
    re16, ice = snap["legs"]
    assert re16["source"] == "gtfs" and re16["matched"] is False and re16["plan"]["line"] == "RE16"
    assert [s["station"]["name"] for s in re16["stopovers"]] == ["Augsburg Hbf", "Donauwörth", "Treuchtlingen", "Nürnberg Hbf"]
    assert re16["stopovers"][1]["plannedArrival"] == "2026-10-08T06:30:00+02:00"
    assert re16["stopovers"][1]["plannedDeparture"] == "2026-10-08T06:31:00+02:00"
    assert re16["stopovers"][0]["plannedPlatform"] == "5", "Gleis aus dem Text bleibt"
    # ICE: Ausstieg heißt im Fahrplan „S Südkreuz Bhf (Berlin)“ – gefunden über die Lage
    assert ice["source"] == "gtfs" and [s["station"]["name"] for s in ice["stopovers"]] == ["Nürnberg Hbf", "Erfurt Hbf", "Berlin Südkreuz"]
    assert ice["stopovers"][1]["plannedDeparture"] == "2026-10-08T08:42:00+02:00"
    assert not fill_from_timetable(snap, tt), "schon ergänzt"


def test_ohne_passende_fahrt_bleibt_der_text(places, tt):
    snap = draft("augsburg-berlin-2026-10-08.txt", datetime(2026, 10, 5, 12, tzinfo=TZ))
    snap["legs"][1]["stopovers"][0]["plannedDeparture"] = "2026-10-08T07:40:00+02:00"   # andere Minute
    fill_coordinates(snap, places)
    fill_from_timetable(snap, tt)
    assert snap["legs"][1]["source"] == "text" and len(snap["legs"][1]["stopovers"]) == 2
    assert snap["legs"][0]["source"] == "gtfs"


def test_tram_bekommt_zwischenhalte_und_alles_wird_gezeichnet(net, places, tt):
    snap = draft("moritzplatz-berlin-2026-11-01.txt", datetime(2026, 10, 29, 12, tzinfo=TZ))
    assert apply_geometry(snap, net, places, version="v", timetable=tt)
    tram, re1, ice, s41, u3 = snap["legs"]
    assert tram["source"] == "gtfs"
    assert [s["station"]["name"] for s in tram["stopovers"]] == [
        "Moritzplatz, Augsburg (Bayern)", "Augsburg, Theodor-Heuss-Platz", "Königsplatz, Augsburg (Bayern)"]
    assert tram["polyline"] and tram["geometry"]["source"] == "line"
    assert ice["source"] == "text", "ICE 1004 am 01.11. steht nicht im Testfahrplan"
    assert u3["source"] == "text"


def test_ausserhalb_des_zeitraums(places, tt):
    snap = draft("augsburg-berlin-2026-10-08.txt", datetime(2026, 10, 5, 12, tzinfo=TZ))
    for leg in snap["legs"]:
        for so in leg["stopovers"]:
            for k in ("plannedArrival", "plannedDeparture"):
                if so[k]:
                    so[k] = so[k].replace("2026-10-08", "2026-12-08")
    fill_coordinates(snap, places)
    assert not fill_from_timetable(snap, tt)
    assert not fill_from_timetable(snap, None)


def test_lage_aus_dem_fahrplan_ohne_gleisnetz(tt):
    snap = draft("augsburg-berlin-2026-10-08.txt", datetime(2026, 10, 5, 12, tzinfo=TZ))
    assert fill_from_timetable(snap, tt), "Namen reichen für Augsburg, Nürnberg, Erfurt"
    re16 = snap["legs"][0]
    assert km((re16["stopovers"][0]["station"]["lat"], re16["stopovers"][0]["station"]["lon"]), P["Augsburg Hbf"]) < 0.01


def test_ersatzbus_bekommt_zwischenhalte(tt):
    """Abschnitt „Bus RE80“ aus dem geteilten Text: Halte laut Fahrplan, auch über Mitternacht."""
    leg = {"index": 0, "kind": "ride", "source": "text", "line": {"name": "Bus RE80", "productName": "Bus", "product": "bus"},
           "stopovers": [
               {"station": {"id": None, "name": "Hauptbahnhof, Augsburg", "lat": 48.3652, "lon": 10.8861},
                "plannedArrival": None, "plannedDeparture": "2026-10-10T23:41:00+02:00", "plannedPlatform": None},
               {"station": {"id": None, "name": "Bahnhof, Donauwörth", "lat": None, "lon": None},
                "plannedArrival": "2026-10-11T00:36:00+02:00", "plannedDeparture": None, "plannedPlatform": None}]}
    assert fill_from_timetable({"legs": [leg]}, tt)
    assert [s["station"]["name"] for s in leg["stopovers"]] == ["Hauptbahnhof, Augsburg", "Ersatzhalt Mitte", "Bahnhof, Donauwörth"]
    assert leg["source"] == "gtfs" and leg["plan"]["mode"] == "replacement"
    assert leg["stopovers"][1]["plannedDeparture"] == "2026-10-11T00:07:00+02:00"
    # Ein gewöhnlicher Stadtbus steht nicht im Fahrplan: bleibt, wie er ist
    city = {**leg, "source": "text", "line": {"name": "Bus 32", "productName": "Bus", "product": "bus"},
            "stopovers": [dict(s) for s in leg["stopovers"][::2]]}
    assert not fill_from_timetable({"legs": [city]}, tt)
