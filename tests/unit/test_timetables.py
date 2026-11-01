"""Timetables-API: Auswertung, Zwischenspeicher, Ratenbegrenzung, Fehler."""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from core.timetables import (
    RateLimiter,
    TimetablesClient,
    TimetablesError,
    berlin_slice,
    line_name,
    merge_changes,
    parse_iris_time,
    parse_stations,
    parse_timetable,
)

TZ = ZoneInfo("Europe/Berlin")

PLAN = """<?xml version="1.0" encoding="UTF-8"?>
<timetable station="Augsburg Hbf">
  <s id="-5296456185474546385-2610080740-12">
    <tl f="F" t="p" o="80" c="ICE" n="1291"/>
    <ar pt="2610080740" pp="4" ppth="Berlin Hbf|Nürnberg Hbf"/>
    <dp pt="2610080742" pp="4" ppth="München-Pasing|München Hbf"/>
  </s>
  <s id="re-9-2610080750-1">
    <tl f="D" t="p" o="800734" c="RE" n="57083"/>
    <dp pt="2610080750" pp="7" l="9" ppth="Dinkelscherben|Günzburg|Ulm Hbf"/>
  </s>
  <s id="s-end-2610080700-9">
    <tl c="S" n="123"/>
    <ar pt="2610080755" pp="1" l="4"/>
  </s>
  <s id="rb-x-2610080758-1">
    <tl c="RB" n="57400"/>
    <dp pt="2610080758" pp="2" l="RB87" ppth="Gessertshausen|Mindelheim"/>
  </s>
</timetable>"""

CHANGES = """<?xml version="1.0" encoding="UTF-8"?>
<timetable station="Augsburg Hbf" eva="8000013">
  <s id="-5296456185474546385-2610080740-12" eva="8000013">
    <dp ct="2610080748" cp="5">
      <m id="m1" t="d" c="43" ts="2610080700" ext="Verspätung aus vorheriger Fahrt"/>
    </dp>
  </s>
  <s id="rb-x-2610080758-1" eva="8000013"><dp cs="c" clt="2610080700"/></s>
  <s id="extra-2610080800-1" eva="8000013">
    <tl c="RE" n="99999"/>
    <dp ct="2610080800" cp="3" cs="a" l="80" cpth="Donauwörth"/>
  </s>
  <s id="ohne-plan-1" eva="8000013"><dp ct="2610080900"/></s>
</timetable>"""

STATIONS = """<?xml version="1.0" encoding="UTF-8"?>
<stations>
  <station p="1|2|3" meta="8070003|8089222" name="Augsburg Hbf" eva="8000013" ds100="MA" db="true" creationts="25-01-01"/>
  <station name="Augsburg-Oberhausen" eva="8000713" ds100="MAO"/>
</stations>"""


def test_iris_zeiten_sommer_winter_und_stunde():
    assert parse_iris_time("2610080736") == datetime(2026, 10, 8, 7, 36, tzinfo=TZ)
    assert parse_iris_time("2610080736").astimezone(timezone.utc).hour == 5  # Sommerzeit +2
    assert parse_iris_time("2611080736").astimezone(timezone.utc).hour == 6  # Winterzeit +1
    assert parse_iris_time("kaputt") is None
    assert parse_iris_time(None) is None
    assert berlin_slice(datetime(2026, 10, 8, 22, 30, tzinfo=timezone.utc)) == ("261009", "00")


def test_fahrplan_auswerten():
    station, stops = parse_timetable(PLAN)
    assert station == "Augsburg Hbf"
    ice, re9, s_end, rb = stops
    assert (ice.category, ice.number, ice.filter) == ("ICE", "1291", "F")
    assert ice.trip_key == "-5296456185474546385-2610080740" and ice.index == 12
    assert ice.departure.planned == datetime(2026, 10, 8, 7, 42, tzinfo=TZ)
    assert ice.departure.planned_path == ["München-Pasing", "München Hbf"]
    assert ice.arrival.planned_path == ["Berlin Hbf", "Nürnberg Hbf"]
    assert s_end.departure is None
    assert [line_name(s) for s in stops] == ["ICE 1291", "RE9", "S4", "RB87"]


def test_aenderungen_uebernehmen_ohne_plan_zu_veraendern():
    _, plan = parse_timetable(PLAN)
    _, changes = parse_timetable(CHANGES)
    merged = merge_changes(plan, changes)
    ice = merged["-5296456185474546385-2610080740-12"]
    assert ice.departure.changed == datetime(2026, 10, 8, 7, 48, tzinfo=TZ)
    assert ice.departure.changed_platform == "5"
    assert ice.departure.messages[0]["text"] == "Verspätung aus vorheriger Fahrt"
    assert merged["rb-x-2610080758-1"].departure.cancelled
    extra = merged["extra-2610080800-1"]
    assert extra.departure.changed_status == "a" and extra.departure.path == ["Donauwörth"]
    assert "ohne-plan-1" not in merged
    assert plan[0].departure.changed is None, "Planhalte im Zwischenspeicher bleiben unverändert"


def test_bahnhofssuche():
    found = parse_stations(STATIONS)
    assert [(s.eva, s.name, s.ds100) for s in found] == [("8000013", "Augsburg Hbf", "MA"), ("8000713", "Augsburg-Oberhausen", "MAO")]
    assert found[0].meta == ["8070003", "8089222"]


class FakeApi:
    def __init__(self, routes):
        self.routes, self.paths = routes, []

    async def __call__(self, url, headers):
        assert headers["DB-Client-Id"] == "cid" and headers["DB-Api-Key"] == "key"
        path = url.split("/timetables/v1", 1)[1]
        self.paths.append(path)
        await asyncio.sleep(0)
        r = self.routes(path)
        return (r, "") if isinstance(r, int) else (200, r)


def client(api, clock=lambda: 1000.0, limiter=None):
    return TimetablesClient(api, "cid", "key", clock=clock, limiter=limiter or RateLimiter(limit=1000))


async def test_fahrplan_einmal_je_stunde_aenderungen_mit_ablauf():
    now = [1000.0]
    api = FakeApi(lambda p: PLAN if p.startswith("/plan") else CHANGES)
    c = client(api, clock=lambda: now[0])
    when = datetime(2026, 10, 8, 7, 45, tzinfo=TZ)
    a, b = await asyncio.gather(c.plan("8000013", when), c.plan("8000013", when))
    assert a is b and api.paths == ["/plan/8000013/261008/07"], "gleichzeitige Anfragen teilen sich einen Abruf"
    await c.plan("8000013", when)
    assert len(api.paths) == 1
    await c.changes("8000013")
    now[0] += 60
    await c.changes("8000013")
    assert api.paths.count("/fchg/8000013") == 1, "Änderungen höchstens alle 3 Minuten"
    now[0] += 200
    await c.changes("8000013")
    assert api.paths.count("/fchg/8000013") == 2


async def test_bahnhof_suchen_wird_kodiert_und_gemerkt():
    api = FakeApi(lambda p: STATIONS)
    c = client(api)
    found = await c.stations("Augsburg Hbf")
    assert found[0].eva == "8000013"
    await c.stations("augsburg hbf ")
    assert api.paths == ["/station/Augsburg%20Hbf"]


@pytest.mark.parametrize(("status", "code"), [(401, "BAD_CREDENTIALS"), (403, "BAD_CREDENTIALS"), (429, "RATE_LIMITED"), (500, "UNAVAILABLE")])
async def test_fehlercodes(status, code):
    c = client(FakeApi(lambda p: status))
    with pytest.raises(TimetablesError) as e:
        await c.changes("1")
    assert e.value.code == code


async def test_nicht_gefunden_ist_leer_und_netzfehler_unavailable():
    assert await client(FakeApi(lambda p: 404)).plan("1", datetime(2026, 1, 1, tzinfo=TZ)) == []

    async def broken(url, headers):
        raise OSError("keine Verbindung")
    with pytest.raises(TimetablesError) as e:
        await client(broken).changes("1")
    assert e.value.code == "UNAVAILABLE"


async def test_ratenbegrenzer_wartet_bei_vollem_fenster():
    t = [0.0]
    slept = []

    async def sleep(s):
        slept.append(s)
        t[0] += s
    lim = RateLimiter(limit=3, window=60, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        await lim.acquire()
    assert slept == []
    await lim.acquire()
    assert len(slept) == 1 and 59 < slept[0] <= 60.1
