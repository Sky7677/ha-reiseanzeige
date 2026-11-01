"""Reise aus geteiltem Text + Fahrplan zusammensetzen."""

from datetime import datetime
from zoneinfo import ZoneInfo

from core.journey import build_snapshot, draft_snapshot, name_variants, norm_name
from core.share_text import parse_share_text
from core.timetables import RateLimiter, TimetablesClient
from testdata import fixture_text
from fake_iris import november_world

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 1, 14, 17, tzinfo=TZ)


class Coords:
    """Koordinaten für einige Bahnhöfe (sonst unbekannt)."""
    known = {"8000013": (48.3654, 10.8856), "8000284": (49.4457, 11.0827)}

    def coords(self, eva, name):
        return self.known.get(eva)


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
    assert snap["vbid"] == "00000000-0000-4000-8000-000000000002"


async def test_bahnabschnitte_werden_im_fahrplan_gefunden():
    iris = november_world()
    client = TimetablesClient(iris, "cid", "key", limiter=RateLimiter(limit=1000))
    snap = await build_snapshot(journey(), client, NOW, Coords())
    tram, re1, ice, s41, u3 = snap["legs"]

    # Tram und U-Bahn: nur aus dem Text
    assert not tram["matched"] and not u3["matched"]
    assert [s["station"]["name"] for s in tram["stopovers"]] == ["Moritzplatz, Augsburg (Bayern)", "Königsplatz, Augsburg (Bayern)"]

    # RE1 über die Zugnummer, mit Zwischenhalten und Koordinaten wo bekannt
    assert re1["matched"] and re1["tripId"] == "-111-2611011437"
    assert [s["station"]["name"] for s in re1["stopovers"]] == ["Augsburg Hbf", "Donauwörth", "Treuchtlingen", "Nürnberg Hbf"]
    assert [s["station"]["id"] for s in re1["stopovers"]] == ["8000013", "8000080", "8000122", "8000284"]
    assert re1["stopovers"][1]["plannedArrival"] == "2026-11-01T14:58:00+01:00"
    assert re1["stopovers"][1]["plannedDeparture"] == "2026-11-01T15:00:00+01:00"
    assert not re1["stopovers"][1]["estimated"]
    assert re1["stopovers"][0]["station"]["lat"] == 48.3654
    assert re1["stopovers"][-1]["plannedPlatform"] == "5"
    assert re1["iris"]["arrEva"] == "8000284"

    # ICE: Ausstieg Südkreuz, der Zug fährt weiter bis Berlin Hbf
    assert [s["station"]["name"] for s in ice["stopovers"]] == ["Nürnberg Hbf", "Erfurt Hbf", "Halle (Saale) Hbf", "Berlin Südkreuz"]
    assert ice["stopovers"][-1]["plannedArrival"] == "2026-11-01T19:40:00+01:00"

    # S41 ohne Zugnummer: über Linie + Minute; Halt liegt unter der Neben-Nummer des Südkreuzes
    assert s41["matched"] and s41["tripId"].startswith("-333-")
    assert s41["iris"]["depEva"] == "8089110"
    assert [s["station"]["name"] for s in s41["stopovers"]] == [
        "Berlin Südkreuz (S)", "Berlin-Schöneberg", "Berlin Innsbrucker Platz", "Berlin Heidelberger Platz"]

    # Sparsam: jede Fahrplan-Stunde eines Bahnhofs höchstens einmal
    plans = [p for p in iris.paths if p.startswith("/plan")]
    assert len(plans) == len(set(plans))
    assert len(iris.paths) < 50, iris.paths


async def test_unbekannter_zug_bleibt_textabschnitt():
    iris = november_world()
    iris.trips = [t for t in iris.trips if t.number != "1004"]
    client = TimetablesClient(iris, "cid", "key", limiter=RateLimiter(limit=1000))
    snap = await build_snapshot(journey(), client, NOW)
    ice = snap["legs"][2]
    assert not ice["matched"] and len(ice["stopovers"]) == 2
    assert snap["legs"][1]["matched"], "andere Abschnitte trotzdem angereichert"


def test_namen():
    assert norm_name("Berlin Südkreuz (S)") == norm_name("Berlin Südkreuz")
    assert norm_name("Breitenbachplatz (U), Berlin") == "breitenbachplatz berlin"
    assert name_variants("Heidelberger Platz (S+U), Berlin") == [
        "Heidelberger Platz (S+U), Berlin", "Heidelberger Platz, Berlin", "Berlin Heidelberger Platz"]


async def test_zwischenhalte_aus_dem_laufweg_vor_der_ankunft():
    """Fehlt der Ausstieg im Laufweg ab Einstieg, helfen die Halte vor der Ankunft."""
    iris = november_world()
    original = iris._stop_xml

    def truncated(trip, i):
        xml = original(trip, i)
        if trip.number == "4024" and i == 0:
            xml = xml.replace('ppth="Donauwörth|Treuchtlingen|Nürnberg Hbf"', 'ppth="Donauwörth"')
        return xml
    iris._stop_xml = truncated
    client = TimetablesClient(iris, "cid", "key", limiter=RateLimiter(limit=1000))
    snap = await build_snapshot(journey(), client, NOW, Coords())
    re1 = snap["legs"][1]
    assert [s["station"]["name"] for s in re1["stopovers"]] == ["Augsburg Hbf", "Donauwörth", "Treuchtlingen", "Nürnberg Hbf"]
    assert re1["iris"]["pathAfter"] == ["Donauwörth"]
    assert re1["iris"]["pathBefore"] == ["Augsburg Hbf", "Donauwörth", "Treuchtlingen"]


async def test_horizont_keine_abfragen_fuer_ferne_abschnitte():
    from datetime import timedelta
    iris = november_world()
    client = TimetablesClient(iris, "cid", "key", limiter=RateLimiter(limit=1000))
    early = datetime(2026, 10, 29, 12, 0, tzinfo=TZ)
    snap = await build_snapshot(journey(), client, early, horizon=timedelta(hours=24))
    assert iris.paths == [] and not any(l["matched"] for l in snap["legs"])
