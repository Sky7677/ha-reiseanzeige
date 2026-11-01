"""Reiseverwaltung: Anlegen aus Text, Ersetzen, Zustände, Abfrageplan, Echtzeit-Gesundheit."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.journey import draft_snapshot
from core.share_text import parse_share_text
from core.trips import (
    COMPLETED, RUNNING, UPCOMING,
    MemoryStore, TripError, TripManager,
    next_refresh_at, realtime_unavailable, share_confirmation, share_error, trip_status,
)
from testdata import fixture_text

TZ = ZoneInfo("Europe/Berlin")


def at(d, h, m=0):
    return datetime(2026, 11, d, h, m, tzinfo=TZ)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def shared(name="moritzplatz-berlin-2026-11-01.txt"):
    return parse_share_text(fixture_text("share", name))


def other_journey(day=1, hour=16):
    return parse_share_text(f"""Augsburg Hbf → München Hbf
So. {day:02d}.11.2026

RE 9 (57000)
Nach München Hbf
Ab {hour}:05 Augsburg Hbf, Gleis 1
An {hour}:45 München Hbf, Gleis 20
""")


async def manager(clock):
    m = TripManager(MemoryStore(), clock=clock)
    await m.init()
    return m


def matched(snapshot):
    for leg in snapshot["legs"]:
        if leg["line"]["product"] in ("regional", "longdistance", "suburban"):
            leg["matched"], leg["tripId"] = True, f"trip-{leg['index']}"
    return snapshot


async def test_anlegen_doppelt_ersetzen_vorbei():
    clock = Clock(at(1, 12))
    m = await manager(clock)
    j = shared()
    r = await m.add_from_share("max", j, draft_snapshot(j, clock()))
    assert r["status"] == "saved" and r["replaced"] == []
    assert r["trip"]["status"] == UPCOMING
    assert share_confirmation(r) == "Reise Moritzplatz, Augsburg (Bayern) → Breitenbachplatz (U), Berlin am 01.11. gespeichert."

    again = await m.add_from_share("max", j, draft_snapshot(j, clock()))
    assert again["status"] == "duplicate" and len(m.trips) == 1

    # Spontan andere Verbindung im selben Zeitraum → ersetzt die alte
    o = other_journey(hour=16)
    rep = await m.add_from_share("max", o, draft_snapshot(o, clock()))
    assert [x["destinationName"] for x in rep["replaced"]] == ["Breitenbachplatz (U), Berlin"]
    assert "Sie ersetzt die Reise" in share_confirmation(rep)
    assert len(m.trips) == 1

    # Andere Person: ersetzt nichts
    await m.add_from_share("anna", j, draft_snapshot(j, clock()))
    assert len(m.trips) == 2

    clock.t = at(2, 12)
    with pytest.raises(TripError) as e:
        await m.add_from_share("max", j, draft_snapshot(j, clock()))
    assert e.value.code == "IN_PAST" and share_error("IN_PAST").startswith("Diese Reise ist bereits vorbei")


async def test_zustaende_und_aufraeumen():
    clock = Clock(at(1, 12))
    m = await manager(clock)
    j = shared()
    tid = (await m.add_from_share("max", j, draft_snapshot(j, clock())))["trip"]["id"]
    t = m.trips[0]
    grace = timedelta(hours=1)
    assert trip_status(t, at(1, 14, 17), grace) == UPCOMING
    assert trip_status(t, at(1, 17), grace) == RUNNING
    assert trip_status(t, at(1, 20, 30), grace) == COMPLETED
    clock.t = at(1, 21, 30)
    assert m.list() == []
    assert [s["id"] for s in await m.cleanup()] == [tid]
    assert m.trips == []


async def test_anreichern_behaelt_sitzplaetze_und_startet_abfragen():
    clock = Clock(at(1, 10))
    m = await manager(clock)
    j = shared()
    tid = (await m.add_from_share("max", j, draft_snapshot(j, clock())))["trip"]["id"]
    await m.set_seat(tid, 2, "9", "64")
    assert m.due_for_refresh() == [], "Entwurf wird nicht abgefragt"

    assert await m.set_snapshot(tid, matched(draft_snapshot(j, clock())))
    assert m.get(tid)["seats"] == {"2": {"coach": "9", "seat": "64"}}
    assert m.due_for_refresh() == [], "mehr als 3 h vor Abfahrt: noch nichts"
    clock.t = at(1, 11, 30)  # Tram 14:18: jetzt weniger als 3 h vorher
    assert [t["id"] for t in m.due_for_refresh()] == [tid]
    assert not await m.set_snapshot("weg", {})

    with pytest.raises(TripError):
        await m.set_seat(tid, 9, "1", "1")
    with pytest.raises(TripError):
        await m.set_seat(tid, 2, "x" * 13, "")
    await m.set_seat(tid, 2, "", "")
    assert m.get(tid)["seats"] == {}


async def test_echtzeit_und_einmalige_meldung_bei_ausfall():
    clock = Clock(at(1, 16))
    m = await manager(clock)
    j = shared()
    tid = (await m.add_from_share("max", j, draft_snapshot(j, clock())))["trip"]["id"]
    await m.set_snapshot(tid, matched(draft_snapshot(j, clock())))

    rt = {"legIndex": 2, "stopovers": [
        {"departure": {"prognosed": "2026-11-01T16:40:00+01:00"}, "arrival": {"prognosed": None}},
        {"departure": {"prognosed": None}, "arrival": {"prognosed": "2026-11-01T19:47:00+01:00"}}]}
    assert await m.apply_realtime(tid, [{"legIndex": 2, "status": "ok", "realtime": rt}]) == []
    assert m.get(tid)["realtimeHealth"]["lastSuccessAt"] == at(1, 16).isoformat()

    err = [{"legIndex": 2, "status": "error", "error": "UNAVAILABLE"}]
    assert await m.apply_realtime(tid, err) == []
    events = await m.apply_realtime(tid, err)
    assert [e["type"] for e in events] == ["realtime_unavailable"]
    assert realtime_unavailable(events[0]) == "Für die Reise nach Breitenbachplatz (U), Berlin sind in Home Assistant keine Echtzeitdaten verfügbar."
    assert "2" not in m.get(tid)["realtime"]["legs"], "veraltete Prognose entfernt"
    assert await m.apply_realtime(tid, err) == [], "nur einmal melden"


def test_abfrageplan_mit_rueckzug_bei_fehlern():
    j = shared()
    trip = {"snapshot": matched(draft_snapshot(j, at(1, 12))), "realtime": None, "lastRefreshAt": at(1, 16).isoformat(),
            "realtimeHealth": {"consecutiveFailures": 0}}
    grace, run = timedelta(hours=1), timedelta(seconds=90)
    # ICE-Fahrt, nächstes Ereignis (Ankunft 19:40) weit weg → 5 Minuten
    assert next_refresh_at(trip, at(1, 17), grace, run) == at(1, 17)
    trip["lastRefreshAt"] = at(1, 17).isoformat()
    assert next_refresh_at(trip, at(1, 17, 1), grace, run) == at(1, 17, 5)
    # kurz vor der Ankunft → 90 Sekunden
    trip["lastRefreshAt"] = at(1, 19, 33).isoformat()
    assert next_refresh_at(trip, at(1, 19, 34), grace, run) == at(1, 19, 33) + timedelta(seconds=90)
    # Fehlschläge verdoppeln den Abstand, höchstens 15 Minuten
    trip["realtimeHealth"]["consecutiveFailures"] = 5
    assert next_refresh_at(trip, at(1, 19, 34), grace, run) == at(1, 19, 33) + timedelta(minutes=15)
    # ohne erkannte Bahn-Abschnitte keine Abfragen
    trip["snapshot"] = draft_snapshot(j, at(1, 12))
    assert next_refresh_at(trip, at(1, 17), grace, run) is None


async def test_erneuter_abgleich_erst_im_zeitfenster_und_im_takt():
    from core.trips import ENRICH_EVERY
    clock = Clock(datetime(2026, 10, 29, 12, 0, tzinfo=TZ))
    m = await manager(clock)
    j = shared()
    text = fixture_text("share", "moritzplatz-berlin-2026-11-01.txt")
    tid = (await m.add_from_share("max", j, draft_snapshot(j, clock()), text))["trip"]["id"]
    assert m.due_for_enrichment() == [], "drei Tage vorher: nichts"
    clock.t = datetime(2026, 10, 31, 15, 0, tzinfo=TZ)  # RE1 01.11. 14:37 in < 24 h
    assert [t["id"] for t in m.due_for_enrichment()] == [tid]
    await m.mark_enrich_attempt(tid)
    assert m.due_for_enrichment() == []
    clock.t += ENRICH_EVERY
    assert len(m.due_for_enrichment()) == 1
    await m.mark_enrich_attempt(tid)
    clock.t = datetime(2026, 11, 1, 12, 0, tzinfo=TZ)  # < 3 h: alle 20 Minuten
    await m.mark_enrich_attempt(tid)
    clock.t = datetime(2026, 11, 1, 12, 21, tzinfo=TZ)
    assert len(m.due_for_enrichment()) == 1

    # Gefundene Abschnitte bleiben, auch wenn ein späterer Abgleich sie nicht findet
    found = matched(draft_snapshot(j, clock()))
    await m.set_snapshot(tid, found)
    assert m.due_for_enrichment() == [], "alle Bahn-Abschnitte gefunden"
    await m.set_snapshot(tid, draft_snapshot(j, clock()))
    assert [l["matched"] for l in m.get(tid)["snapshot"]["legs"]] == [False, True, True, True, False]
