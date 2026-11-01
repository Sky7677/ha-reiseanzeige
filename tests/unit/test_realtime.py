"""Echtzeit aus den Änderungsmeldungen: welche Bahnhöfe, Verspätung, Gleis, Ausfall."""

from datetime import datetime
from zoneinfo import ZoneInfo

from core.journey import build_snapshot
from core.realtime import leg_realtime, stations_to_poll
from core.share_text import parse_share_text
from core.timetables import RateLimiter, TimetablesClient
from testdata import fixture_text
from fake_iris import november_world

TZ = ZoneInfo("Europe/Berlin")


def at(h, m):
    return datetime(2026, 11, 1, h, m, tzinfo=TZ)


async def world():
    iris = november_world()
    client = TimetablesClient(iris, "cid", "key", limiter=RateLimiter(limit=1000))
    snap = await build_snapshot(parse_share_text(fixture_text("share", "moritzplatz-berlin-2026-11-01.txt")), client, at(14, 17))
    return iris, client, snap


async def test_abgefragte_bahnhoefe_je_lage():
    _, _, snap = await world()
    # vor der Reise: nur der Einstieg des ersten Bahn-Abschnitts (Tram kennt die API nicht)
    assert stations_to_poll(snap, None, at(13, 0)) == {1: ["8000013"]}
    assert stations_to_poll(snap, None, at(10, 0)) == {}, "mehr als 3 Stunden vorher: nichts"
    # im ICE zwischen Nürnberg und Erfurt: nächste zwei Halte + Ausstieg, dazu Einstieg der S41
    assert stations_to_poll(snap, None, at(17, 30)) == {2: ["8010101", "8010159", "8011113"], 3: ["8089110"]}
    # alles vorbei
    assert stations_to_poll(snap, None, at(21, 0)) == {}


async def test_verspaetung_gleiswechsel_und_fortschreibung():
    iris, client, snap = await world()
    iris.changes["8010101"] = """<timetable station="Erfurt Hbf">
      <s id="-222-2611011636-2"><ar ct="2611011747"/><dp ct="2611011749" cp="2">
        <m id="1" t="h" c="0" ext="Bauarbeiten zwischen Halle und Berlin"/></dp></s>
      <s id="-999-2611011600-3"><dp ct="2611011800"/></s></timetable>"""
    iris.changes["8011113"] = """<timetable station="Berlin Südkreuz">
      <s id="-222-2611011636-4"><ar ct="2611011947"/></s></timetable>"""
    ice = snap["legs"][2]
    rt = await leg_realtime(client, ice, ["8010101", "8010159", "8011113"], at(17, 30))
    nbg, erf, hal, sk = rt["stopovers"]
    assert nbg["arrival"]["prognosed"] is None and nbg["source"] is None, "vor dem ersten bekannten Halt nichts erfinden"
    assert (erf["arrival"]["delay"], erf["departure"]["delay"]) == (300, 300)
    assert erf["departure"]["prognosed"] == "2026-11-01T17:49:00+01:00"
    assert (erf["platform"], erf["plannedPlatform"], erf["platformChanged"]) == ("2", "1", True)
    assert hal["source"] == "propagated" and hal["arrival"]["prognosed"] == "2026-11-01T18:25:00+01:00"
    assert sk["arrival"]["delay"] == 420 and sk["source"] == "iris"
    assert rt["hasRealtime"] and not rt["cancelled"]
    assert rt["remarks"] == [{"code": "0", "type": "warning", "text": "Bauarbeiten zwischen Halle und Berlin"}]
    assert [p for p in iris.paths if p.startswith("/fchg")] == ["/fchg/8010101", "/fchg/8010159", "/fchg/8011113"]


async def test_ausfall_des_abschnitts():
    iris, client, snap = await world()
    s41 = snap["legs"][3]
    iris.changes["8089110"] = '<timetable station="x"><s id="-333-2611011947-1"><dp cs="c"/></s></timetable>'
    iris.changes["8089015"] = '<timetable station="x"><s id="-333-2611011947-4"><ar cs="c"/></s></timetable>'
    rt = await leg_realtime(client, s41, ["8089110", "8089015"], at(19, 30))
    assert rt["cancelled"] and rt["hasRealtime"]
    assert rt["stopovers"][0]["cancelled"] and not rt["stopovers"][1]["cancelled"]
