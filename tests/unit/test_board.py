"""Abfahrtstafel: Plan + Änderungen, Zeitfenster, Sortierung."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.board import departures
from core.timetables import RateLimiter, TimetablesClient, TimetablesError
from fake_iris import november_world

TZ = ZoneInfo("Europe/Berlin")


async def test_abfahrten_mit_aenderungen():
    iris = november_world()
    iris.changes["8000013"] = """<timetable station="Augsburg Hbf">
      <s id="-111-2611011437-1"><dp ct="2611011441" cp="4"><m id="1" t="f" ext="Reparatur am Zug"/></dp></s>
      <s id="-112-2611011537-1"><dp cs="c"/></s></timetable>"""
    client = TimetablesClient(iris, "cid", "key", limiter=RateLimiter(limit=1000))
    board = await departures(client, "Augsburg Hbf", datetime(2026, 11, 1, 14, 30, tzinfo=TZ))
    assert board["station"] == {"eva": "8000013", "name": "Augsburg Hbf"}
    re1, re1b = board["departures"]
    assert (re1["line"], re1["destination"], re1["via"]) == ("RE1", "Nürnberg Hbf", ["Donauwörth", "Treuchtlingen"])
    assert (re1["delayMin"], re1["platform"], re1["platformChanged"]) == (4, "4", True)
    assert re1["messages"] == ["Reparatur am Zug"]
    assert re1b["cancelled"]


async def test_unbekannter_bahnhof():
    client = TimetablesClient(november_world(), "cid", "key", limiter=RateLimiter(limit=1000))
    with pytest.raises(TimetablesError) as e:
        await departures(client, "Atlantis", datetime(2026, 11, 1, 14, 30, tzinfo=TZ))
    assert e.value.code == "NOT_FOUND"
