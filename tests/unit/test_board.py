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


# --- Ersatzbusse laut Fahrplan ---------------------------------------------------------

def _replacements(tt, near):
    """Wie Reiseanzeige.board_replacements, mit fester Lage des Bahnhofs."""
    from datetime import time, timedelta

    def fn(station, now, minutes):
        local = now.astimezone(TZ)
        deps = tt.departures(tt.near(*near, 0.5), local.date(), local.hour * 60 + local.minute - 1, minutes + 1, modes={"replacement"})
        midnight = datetime.combine(local.date(), time(0), TZ)
        return [{"line": d.line, "planned": midnight + timedelta(minutes=d.minute), "destination": d.to, "via": d.via, "stop": d.stop,
                 "to_at": d.to_at} for d in deps]
    return fn


async def test_ersatzbusse_laut_fahrplan():
    from core.railnet.plan import Timetable
    from timetable_fixture import build
    tt = Timetable(build())
    client = TimetablesClient(november_world(), "cid", "key", limiter=RateLimiter(limit=1000))
    board = await departures(client, "Augsburg Hbf", datetime(2026, 11, 1, 14, 30, tzinfo=TZ),
                             replacements=_replacements(tt, (48.3655, 10.8856)))
    rows = [(d["line"], d["planned"][11:16], d.get("replacement", False)) for d in board["departures"]]
    assert rows == [("RE1", "14:37", False), ("Bus RE80", "14:50", True), ("RE1", "15:37", False)], \
        "Ersatzbus RE1 um 14:41 entfällt: der Zug RE1 fährt laut Bahn um 14:37"
    bus = board["departures"][1]
    assert (bus["destination"], bus["via"], bus["stop"], bus["prognosed"]) == ("Bahnhof, Donauwörth", ["Ersatzhalt Mitte"], "Hauptbahnhof, Augsburg", None)


def test_ersatzbus_den_die_bahn_selbst_kennt():
    from core.board import replacement_rows
    now = datetime(2026, 11, 1, 23, 30, tzinfo=TZ)
    bus = {"line": "RE80", "planned": datetime(2026, 11, 1, 23, 41, tzinfo=TZ), "destination": "X", "via": [], "stop": "S"}
    iris_bus = {"line": "Bus SEV", "planned": "2026-11-01T23:42:00+01:00", "prognosed": None, "cancelled": False}
    assert replacement_rows([iris_bus], [bus], now, 90) == [], "Bahn kennt den Bus: ihre Fassung gilt"
    cancelled_train = {"line": "RE80", "planned": "2026-11-01T23:44:00+01:00", "prognosed": None, "cancelled": True}
    assert len(replacement_rows([cancelled_train], [bus], now, 90)) == 1, "Zug fällt aus: Ersatzbus bleibt"
    assert replacement_rows([], [bus, dict(bus)], now, 90)[0]["line"] == "Bus RE80" and len(replacement_rows([], [bus, dict(bus)], now, 90)) == 1
    assert replacement_rows([], [bus], now, 5) == [], "außerhalb des Zeitfensters"


def test_zug_der_gleichen_linie_in_die_andere_richtung():
    """Nürnberg Hbf, 07.11.2026: Ersatzbus S2 nach Roth um 23:35; die S-Bahn S2 um 23:34
    fährt nach Altdorf, also in die andere Richtung – der Bus bleibt."""
    from core.board import replacement_rows, same_way
    now = datetime(2026, 11, 7, 23, 5, tzinfo=TZ)
    bus = {"line": "S2", "planned": datetime(2026, 11, 7, 23, 35, tzinfo=TZ), "destination": "Bahnhof, Roth (Mittelfranken)",
           "via": ["Eibach Bahnhof Hafenstraße, Nürnberg", "Reichelsdorf Bahnhof, Nürnberg", "Limbach Bahnhof, Schwabach"],
           "stop": "Nürnberg Hbf (Nelson-Mandela-Platz)", "to_at": (49.2462, 11.0921)}
    altdorf = {"id": "a", "line": "S2", "planned": "2026-11-07T23:34:00+01:00", "prognosed": None, "cancelled": False,
               "destination": "Altdorf(b Nürnberg)", "via": ["Nürnberg-Dürrenhof", "Nürnberg-Gleißhammer", "Nürnberg-Dutzendteich"]}
    altdorf_path = ["Nürnberg-Dürrenhof", "Nürnberg-Gleißhammer", "Nürnberg-Dutzendteich", "Nürnberg-Fischbach",
                    "Feucht", "Winkelhaid", "Ludersheim", "Altdorf West", "Altdorf(b Nürnberg)"]
    rows = replacement_rows([altdorf], [bus], now, 90, {"a": altdorf_path})
    assert [(r["line"], r["destination"]) for r in rows] == [("Bus S2", "Bahnhof, Roth (Mittelfranken)")]
    assert len(replacement_rows([altdorf], [bus], now, 90)) == 1, "nur mit den drei Halten der Zeile: ebenso"

    roth = {**altdorf, "id": "r", "destination": "Roth", "via": ["Nürnberg-Steinbühl", "Nürnberg-Sandreuth", "Nürnberg-Eibach"]}
    roth_path = ["Nürnberg-Steinbühl", "Nürnberg-Sandreuth", "Nürnberg-Eibach", "Nürnberg-Reichelsdorf", "Schwabach-Limbach",
                 "Schwabach", "Rednitzhembach", "Büchenbach", "Roth"]
    assert replacement_rows([roth], [bus], now, 90, {"r": roth_path}) == [], "S2 nach Roth fährt: Plan überholt"
    short = {**roth, "id": "s", "destination": "Schwabach"}
    assert len(replacement_rows([short], [bus], now, 90, {"s": roth_path[:6]})) == 1, \
        "Zug endet vor dem Ziel des Busses: der Bus bleibt"

    # Namen, die sich nicht decken, entscheidet die Lage
    ost = {"destination": "Ostbahnhof, München", "to_at": (48.1273, 11.6047)}
    assert not same_way(["München Ost"], ost)
    assert same_way(["München Ost"], ost, [(48.1270, 11.6050)])
    assert not same_way(["München Ost"], ost, [None])
    assert same_way(["Fürth(Bay)Hbf"], {"destination": "Hauptbahnhof, Fürth"})
    assert same_way(["Hersbruck(r Pegnitz)"], {"destination": "Bahnhof (rechts Pegnitz), Hersbruck"})
    assert not same_way(["Nürnberg-Dürrenhof"], {"destination": "Eibach Bahnhof Hafenstraße, Nürnberg"})
