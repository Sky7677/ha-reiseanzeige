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


async def test_verspaetung_erst_spaeter_bekannt():
    """Verspätung entsteht unterwegs und ist nur am Ausstieg bekannt (+45 min): Die
    Halte davor sind nach Plan vorbei, der Zug war aber noch nicht dort – sie werden
    weiter abgefragt (12.11.2026: RE16 nach Treuchtlingen)."""
    _, _, snap = await world()
    ice = snap["legs"][2]
    def part(t=None):
        return {"planned": None, "prognosed": t, "delay": None}
    stops = [{"stationId": so["station"]["id"], "arrival": part(), "departure": part()} for so in ice["stopovers"]]
    stops[-1]["arrival"] = part("2026-11-01T20:25:00+01:00")      # Südkreuz geplant 19:40
    rt = {"legs": {"2": {"stopovers": stops}}}
    assert stations_to_poll(snap, None, at(18, 40))[2] == ["8011113"], "nach Plan: Halle (18:20) vorbei"
    assert stations_to_poll(snap, rt, at(18, 40))[2] == ["8010159", "8011113"], "Halle erwartet 19:05: noch abfragen"
    assert stations_to_poll(snap, rt, at(19, 20))[2] == ["8011113"]


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


# --- Umleitungen: geänderter Laufweg (cpth) ------------------------------------------

def test_laufweg_vergleichen():
    from core.realtime import compare_paths
    plan = ["Nürnberg Hbf", "Erfurt Hbf", "Halle (Saale) Hbf", "Berlin Südkreuz"]
    assert compare_paths(plan, ["München Hbf", *plan, "Berlin Hbf"]) is None, "alles wie geplant"
    d = compare_paths(plan, ["Nürnberg Hbf", "Erfurt Hbf", "Leipzig Hbf", "Berlin Südkreuz", "Berlin Hbf"])
    assert d["path"] == ["Nürnberg Hbf", "Erfurt Hbf", "Leipzig Hbf", "Berlin Südkreuz"]
    assert d["stops"] == [{"kind": "served", "index": 0}, {"kind": "served", "index": 1},
                          {"kind": "cancelled", "index": 2}, {"kind": "added", "name": "Leipzig Hbf"},
                          {"kind": "served", "index": 3}], "entfallener Halt vor dem neuen"
    # Zug endet vorzeitig: der Ausstieg entfällt
    d = compare_paths(plan, ["Nürnberg Hbf", "Erfurt Hbf"])
    assert [s["kind"] for s in d["stops"]] == ["served", "served", "cancelled", "cancelled"]
    # Namen wie in der API („Halle(Saale)Hbf“) und mit Zusatz „(S)“
    assert compare_paths(["Berlin Südkreuz (S)", "Berlin-Schöneberg"], ["Berlin Südkreuz", "Berlin-Schöneberg"]) is None
    assert compare_paths(plan, ["Hamburg Hbf", "Bremen Hbf"]) is None, "nichts gemeinsam: keine Aussage"
    # Ausstieg aus dem Text in der Form „Halt, Stadt“, im Laufweg „Stadt Halt“
    s41 = ["Berlin Südkreuz", "Berlin-Schöneberg", "Heidelberger Platz (S+U), Berlin"]
    assert compare_paths(s41, ["Berlin Südkreuz", "Berlin-Schöneberg", "Berlin Heidelberger Platz"]) is None
    d = compare_paths(s41, ["Berlin Südkreuz", "Berlin Heidelberger Platz"])
    assert [s["kind"] for s in d["stops"]] == ["served", "cancelled", "served"]


async def test_umleitung_aus_den_aenderungen():
    """ICE 1004 fährt ab Erfurt über Leipzig statt Halle. Die Änderungsmeldung
    enthält nur den neuen Laufweg nach Erfurt (cpth), keinen geplanten."""
    iris, client, snap = await world()
    ice = snap["legs"][2]
    iris.changes["8010101"] = """<timetable station="Erfurt Hbf"><s id="-222-2611011636-2">
      <dp cpth="Leipzig Hbf|Berlin Südkreuz|Berlin Hbf"/></s></timetable>"""
    rt = await leg_realtime(client, ice, ["8010101", "8010159", "8011113"], at(17, 30))
    d = rt["diversion"]
    assert d["path"] == ["Nürnberg Hbf", "Erfurt Hbf", "Leipzig Hbf", "Berlin Südkreuz"]
    assert [s["kind"] for s in d["stops"]] == ["served", "served", "cancelled", "added", "served"]
    assert [s["cancelled"] for s in rt["stopovers"]] == [False, False, True, False], "Halle entfällt, auch ohne eigene Meldung"
    assert rt["hasRealtime"] and not rt["cancelled"]

    # Laufweg zurück wie geplant (cpth gleich dem Plan) → keine Umleitung mehr
    iris.changes["8010101"] = """<timetable station="Erfurt Hbf"><s id="-222-2611011636-2">
      <dp cpth="Halle (Saale) Hbf|Berlin Südkreuz|Berlin Hbf"/></s></timetable>"""
    client.clear_changes()
    rt = await leg_realtime(client, ice, ["8010101", "8010159", "8011113"], at(17, 34))
    assert rt["diversion"] is None and not any(s["cancelled"] for s in rt["stopovers"])


async def test_umleitung_auch_aus_der_ankunft_am_ziel():
    """Nur der Ausstieg wurde abgefragt: dessen Ankunft nennt den neuen Laufweg davor."""
    iris, client, snap = await world()
    ice = snap["legs"][2]
    iris.changes["8011113"] = """<timetable station="Berlin Südkreuz"><s id="-222-2611011636-4">
      <ar cpth="Nürnberg Hbf|Erfurt Hbf|Leipzig Hbf"/></s></timetable>"""
    rt = await leg_realtime(client, ice, ["8011113"], at(17, 0))
    assert [s.get("name") for s in rt["diversion"]["stops"] if s["kind"] == "added"] == ["Leipzig Hbf"]


async def test_bekannte_umleitung_gilt_bis_zur_gegenmeldung():
    """Die Meldung steht oft nur an Bahnhöfen, die der Zug hinter sich hat: Ohne neue
    Aussage bleibt die Umleitung; ein doch angefahrener entfallener Halt beendet sie."""
    from core.realtime import compare_paths
    iris, client, snap = await world()
    ice = snap["legs"][2]
    div = compare_paths([so["station"]["name"] for so in ice["stopovers"]],
                        ["Nürnberg Hbf", "Erfurt Hbf", "Leipzig Hbf", "Berlin Südkreuz"])
    ice["diversion"] = {**div, "stopovers": []}     # so, wie geometry.apply_diversion es ablegt
    rt = await leg_realtime(client, ice, ["8010159", "8011113"], at(18, 0))
    assert rt["diversion"]["path"] == div["path"] and rt["stopovers"][2]["cancelled"], "keine neue Aussage: bleibt"

    iris.changes["8010159"] = '<timetable station="x"><s id="-222-2611011636-3"><ar ct="2611011825"/><dp ct="2611011827"/></s></timetable>'
    client.clear_changes()
    rt = await leg_realtime(client, ice, ["8010159", "8011113"], at(18, 2))
    assert rt["diversion"] is None and not rt["stopovers"][2]["cancelled"], "Halle wird doch angefahren"

    iris.changes["8010159"] = '<timetable station="x"><s id="-222-2611011636-3"><ar cs="c"/><dp cs="c"/></s></timetable>'
    client.clear_changes()
    rt = await leg_realtime(client, ice, ["8010159", "8011113"], at(18, 4))
    assert rt["diversion"]["path"] == div["path"], "entfällt weiterhin: Umleitung bleibt"
