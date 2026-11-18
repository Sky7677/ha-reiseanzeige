"""Reise aus geteiltem Text: Entwurf, Namen; Zuordnung in der nachgebauten Bahn."""

from datetime import datetime
from zoneinfo import ZoneInfo

from core.boardmatch import match_snapshot
from core.journey import draft_snapshot, name_variants, norm_name
from core.share_text import parse_share_text
from fake_openboards import november_world
from testdata import fixture_text

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 1, 14, 17, tzinfo=TZ)


def journey():
    return parse_share_text(fixture_text("share", "moritzplatz-berlin-2026-11-01.txt"))


def test_entwurf_sofort_ohne_abfragen():
    snap = draft_snapshot(journey(), NOW)
    assert snap["schemaVersion"] == 2 and snap["source"] == "share-text"
    assert snap["origin"]["name"] == "Moritzplatz, Augsburg (Bayern)"
    assert snap["plannedDeparture"] == "2026-11-01T14:18:00+01:00"
    assert snap["plannedArrival"] == "2026-11-01T20:10:00+01:00"
    assert [len(l["stopovers"]) for l in snap["legs"]] == [2, 2, 2, 2, 2]
    assert all(not l["matched"] for l in snap["legs"])
    assert all(l["source"] == "text" and "iris" not in l for l in snap["legs"])
    assert snap["vbid"] == "00000000-0000-4000-8000-000000000002"


async def test_zuordnung_mit_zwischenhalten_und_wenigen_abfragen():
    w = november_world()
    snap = await match_snapshot(draft_snapshot(journey(), NOW), w, None, w.directory())
    tram, re1, ice, s41, u3 = snap["legs"]
    assert [s["station"]["name"] for s in tram["stopovers"]] == ["Moritzplatz, Augsburg (Bayern)", "Königsplatz, Augsburg (Bayern)"]
    # Zeit aus der Abfahrtstafel des Halts: dessen Abfahrt (die Ankunft nennt sie nicht)
    assert re1["stopovers"][1]["plannedArrival"] == re1["stopovers"][1]["plannedDeparture"] == "2026-11-01T15:00:00+01:00"
    assert not re1["stopovers"][1]["estimated"]
    assert re1["line"]["trainNumber"] == "4024" and ice["line"]["trainNumber"] == "1004"
    assert [s["station"]["name"] for s in s41["stopovers"]] == [
        "Berlin Südkreuz (S)", "Berlin-Schöneberg", "Berlin Innsbrucker Platz", "Berlin Heidelberger Platz"]
    # Sparsam: Einstieg, Ausstieg und je Zwischenhalt höchstens eine Abfrage
    assert len(w.queries) <= 3 * 2 + 2 + 2 + 3, [(q.evas, q.arrivals) for q in w.queries]


async def test_unbekannter_zug_bleibt_textabschnitt():
    w = november_world()
    w.trips = [t for t in w.trips if t.number != "1004"]
    snap = await match_snapshot(draft_snapshot(journey(), NOW), w, None, w.directory())
    ice = snap["legs"][2]
    assert not ice["matched"] and ice["source"] == "text" and len(ice["stopovers"]) == 2
    assert snap["legs"][1]["matched"], "andere Abschnitte trotzdem zugeordnet"


def test_namen():
    assert norm_name("Berlin Südkreuz (S)") == norm_name("Berlin Südkreuz")
    assert norm_name("Breitenbachplatz (U), Berlin") == "breitenbachplatz berlin"
    assert name_variants("Heidelberger Platz (S+U), Berlin") == [
        "Heidelberger Platz (S+U), Berlin", "Heidelberger Platz, Berlin", "Berlin Heidelberger Platz"]
