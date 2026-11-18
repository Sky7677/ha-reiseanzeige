"""Echtzeit aus OpenBoards an der nachgebauten Verbindung vom 01.11.2026: welche Halte,
Verspätung, Gleis, Ausfall, Umleitungen. Die echten Tafeln prüft test_openboards.py."""

from datetime import datetime
from zoneinfo import ZoneInfo

from core.boardmatch import match_snapshot
from core.journey import draft_snapshot
from core.realtime import leg_realtime, stops_to_poll
from core.share_text import parse_share_text
from fake_openboards import leipzig_instead_of_halle, november_world
from testdata import fixture_text

TZ = ZoneInfo("Europe/Berlin")


def at(h, m):
    return datetime(2026, 11, 1, h, m, tzinfo=TZ)


async def world():
    w = november_world()
    snap = draft_snapshot(parse_share_text(fixture_text("share", "moritzplatz-berlin-2026-11-01.txt")), at(14, 17))
    snap = await match_snapshot(snap, w, None, w.directory())
    w.queries.clear()
    return w, snap


async def test_abgefragte_halte_je_lage():
    _, snap = await world()
    # vor der Reise: nur der Einstieg des ersten Bahn-Abschnitts (Tram kennt OpenBoards nicht)
    assert stops_to_poll(snap, None, at(13, 0)) == {1: [0]}
    assert stops_to_poll(snap, None, at(10, 0)) == {}, "mehr als 3 Stunden vorher: nichts"
    # im ICE zwischen Nürnberg und Erfurt: nächste zwei Halte + Ausstieg, dazu Einstieg der S41
    assert stops_to_poll(snap, None, at(17, 30)) == {2: [1, 2, 3], 3: [0]}
    # alles vorbei
    assert stops_to_poll(snap, None, at(21, 0)) == {}


async def test_verspaetung_erst_spaeter_bekannt():
    """Verspätung nur am Ausstieg bekannt (+45 min): Die Halte davor sind nach Plan
    vorbei, der Zug war aber noch nicht dort – sie werden weiter abgefragt."""
    _, snap = await world()
    ice = snap["legs"][2]

    def part(t=None):
        return {"planned": None, "prognosed": t, "delay": None}
    stops = [{"stationId": so["station"]["id"], "arrival": part(), "departure": part()} for so in ice["stopovers"]]
    stops[-1]["arrival"] = part("2026-11-01T20:25:00+01:00")      # Südkreuz geplant 19:40
    rt = {"legs": {"2": {"stopovers": stops}}}
    assert stops_to_poll(snap, None, at(18, 40))[2] == [3], "nach Plan: Halle (18:22) vorbei"
    assert stops_to_poll(snap, rt, at(18, 40))[2] == [2, 3], "Halle erwartet 19:07: noch abfragen"
    assert stops_to_poll(snap, rt, at(19, 20))[2] == [3]


async def test_zuordnung_mit_bahnhofsnummern_und_laufweg():
    _, snap = await world()
    tram, re1, ice, s41, u3 = snap["legs"]
    assert not tram["matched"] and not u3["matched"], "Tram und U-Bahn: nur aus dem Text"
    assert re1["tripId"] == "20261101-111" and re1["source"] == "openboards"
    assert [s["station"]["id"] for s in re1["stopovers"]] == ["8000013", "8000080", "8000122", "8000284"]
    assert re1["stopovers"][-1]["plannedPlatform"] == "5"
    assert [s["station"]["name"] for s in ice["stopovers"]] == ["Nürnberg Hbf", "Erfurt Hbf", "Halle (Saale) Hbf", "Berlin Südkreuz"]
    assert ice["stopovers"][-1]["plannedArrival"] == "2026-11-01T19:40:00+01:00"
    # S41 ohne Zugnummer über Linie und Minute; Halt liegt unter der Nebennummer des Südkreuzes
    assert s41["tripId"] == "20261101-333" and s41["stopovers"][0]["station"]["id"] == "8089110"


async def test_verspaetung_gleiswechsel_und_fortschreibung():
    w, snap = await world()
    w.messages = {"disruptions": [{"messageID": 1, "descriptions": [{"language": "deu", "text": "Bauarbeiten zwischen Halle und Berlin"}]}]}
    w.change("-222", 1, delay=5, platform="2", messages=[1])
    w.change("-222", 3, delay=7)
    ice = snap["legs"][2]
    rt = await leg_realtime(ice, [1, 3], None, w, at(17, 30))
    nbg, erf, hal, sk = rt["stopovers"]
    assert nbg["departure"]["prognosed"] is None and nbg["source"] is None, "vor dem ersten bekannten Halt nichts erfinden"
    assert (erf["arrival"]["delay"], erf["departure"]["delay"]) == (300, 300)
    assert erf["departure"]["prognosed"] == "2026-11-01T17:49:00+01:00"
    assert (erf["platform"], erf["plannedPlatform"], erf["platformChanged"]) == ("2", "1", True)
    assert hal["source"] == "propagated" and hal["arrival"]["prognosed"] == "2026-11-01T18:27:00+01:00"
    assert sk["arrival"]["delay"] == 420 and sk["source"] == "openboards"
    assert rt["hasRealtime"] and not rt["cancelled"] and rt["origin"] == "openboards"
    assert rt["remarks"] == [{"code": None, "type": "warning", "text": "Bauarbeiten zwischen Halle und Berlin"}]
    assert [(q.evas, q.arrivals) for q in w.queries] == [(("8010101",), False), (("8011113",), True)]


async def test_ausfall_des_abschnitts():
    w, snap = await world()
    w.change("-333", 0, cancelled=True)
    w.change("-333", 3, cancelled=True)
    rt = await leg_realtime(snap["legs"][3], [0, 3], None, w, at(19, 30))
    assert rt["cancelled"] and rt["hasRealtime"]
    assert rt["stopovers"][0]["cancelled"] and not rt["stopovers"][1]["cancelled"]


async def test_umleitung_aus_dem_laufweg():
    w, snap = await world()
    ice = snap["legs"][2]
    leipzig_instead_of_halle(w)
    rt = await leg_realtime(ice, [1, 2, 3], None, w, at(17, 30))
    d = rt["diversion"]
    assert d["path"] == ["Nürnberg Hbf", "Erfurt Hbf", "Leipzig Hbf", "Berlin Südkreuz"]
    assert [s["kind"] for s in d["stops"]] == ["served", "served", "cancelled", "added", "served"]
    assert d["stops"][3] == {"kind": "added", "name": "Leipzig Hbf", "eva": "8010205"}
    assert [s["cancelled"] for s in rt["stopovers"]] == [False, False, True, False], "Halle entfällt"
    assert rt["hasRealtime"] and not rt["cancelled"]

    # Laufweg zurück wie geplant → keine Umleitung mehr, auch nicht die bekannte
    del w.route["-222"]
    rt = await leg_realtime(ice, [1, 2, 3], rt, w, at(17, 34))
    assert rt["diversion"] is None and not any(s["cancelled"] for s in rt["stopovers"])


async def test_umleitung_auch_aus_der_ankunft_am_ziel():
    """Nur der Ausstieg wurde abgefragt: dessen Via-Halte nennen den Laufweg davor."""
    w, snap = await world()
    leipzig_instead_of_halle(w)
    rt = await leg_realtime(snap["legs"][2], [3], None, w, at(17, 0))
    assert [s.get("name") for s in rt["diversion"]["stops"] if s["kind"] == "added"] == ["Leipzig Hbf"]


async def test_bekannte_umleitung_gilt_ohne_neue_aussage():
    w, snap = await world()
    ice = snap["legs"][2]
    leipzig_instead_of_halle(w)
    first = await leg_realtime(ice, [1], None, w, at(17, 30))
    div = first["diversion"]
    # Nichts gefunden (Zug in keiner Tafel): die bekannte Umleitung bleibt
    trips, w.trips = w.trips, [t for t in w.trips if t.key != "-222"]
    rt = await leg_realtime(ice, [2, 3], first, w, at(18, 0))
    assert rt["diversion"] == div and rt["stopovers"][2]["cancelled"], "keine neue Aussage: bleibt"
    w.trips = trips
    # Halle entfällt weiterhin: neue Aussage mit demselben Laufweg
    w.change("-222", 2, cancelled=True)
    rt = await leg_realtime(ice, [2, 3], rt, w, at(18, 2))
    assert rt["diversion"]["path"] == div["path"]
    # Halle wird doch angefahren, Laufweg wieder wie geplant
    del w.route["-222"]
    w.changes.clear()
    w.change("-222", 2, delay=3)
    rt = await leg_realtime(ice, [2, 3], rt, w, at(18, 4))
    assert rt["diversion"] is None and not rt["stopovers"][2]["cancelled"]
