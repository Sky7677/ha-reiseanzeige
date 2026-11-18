"""Zuordnung, Echtzeit, Umleitungen und Tafel über OpenBoards – dieselben Fälle wie
OpenBoardsTest.kt, BoardDiversionTest.kt und DepartureBoardTest der App, gegen dieselben
echten Tafeln (Augsburg Hbf, 12.11.2026 8–9 Uhr, abgefragt am 08.11.2026; 08.11.2026
15–17 Uhr; 08.12.2026) und dieselbe Bahnhofsliste."""

from datetime import datetime, timedelta, timezone

import pytest

from core import boardmatch as bm
from core import openboards as ob
from core import realtime as brt
from core.board import Station as BoardStation, departures
from core.geometry import fill_coordinates
from core.journey import draft_snapshot
from core.openboards import OpenBoardsError
from core.places import Places
from core.realtime import RouteStop, compare_paths
from core.share_text import parse_share_text
from core.stationlist import Station, StationList
from core.trips import effective_departure
from fake_openboards import Boards, recorded, station_list

CET = timezone(timedelta(hours=1))
BOARD = recorded("augsburg-ab-2026-11-12.json")
NOW_DEP = recorded("augsburg-ab-2026-11-08-jetzt.json")
NOW_ARR = recorded("augsburg-an-2026-11-08-jetzt.json")

PLACES = Places([
    ("Augsburg Hbf", "8000013", "MA", 48.3655, 10.8856, "rail"),
    ("Ulm Hbf", "8000170", "TU", 48.3994, 9.9829, "rail"),
    ("Stuttgart Hbf", "8000096", "TS", 48.7840, 9.1817, "rail"),
    ("Mannheim Hbf", "8000244", "RM", 49.4794, 8.4694, "rail"),
    ("Kissing", "8003299", "MKS", 48.2990, 10.9705, "rail"),
    ("Mering-St Afra", "8004008", "MMEA", 48.2766, 10.9897, "rail"),
    ("Mering", "8003982", "MME", 48.2656, 10.9846, "rail"),
    ("Bobingen", "8001033", "MBO", 48.2706, 10.8340, "rail"),
    ("München Hbf", "8000261", "MH", 48.1402, 11.5600, "rail"),
])


def text(line, dep, frm, to, arr, dep_platform=None, arr_platform=None, day="Do. 12.11.2026"):
    a = f", Gleis {dep_platform}" if dep_platform else ""
    b = f", Gleis {arr_platform}" if arr_platform else ""
    return f"{frm} → {to}\n{day}\n\n{line}\nAb {dep} {frm}{a}\nAn {arr} {to}{b}\n"


def share(t, places=PLACES, now="2026-11-08T15:00:00+01:00"):
    snap = draft_snapshot(parse_share_text(t), datetime.fromisoformat(now))
    fill_coordinates(snap, places)
    return snap


def ice612():
    return share(text("ICE 612", "08:01", "Augsburg Hbf", "Mannheim Hbf", "10:24", "3", "5"))


def ice_entry() -> dict:
    return next(e for e in BOARD.departures if (e.get("transport") or {}).get("journeyNumber") == 612)


def entry(**kw) -> dict:
    return {**ice_entry(), **kw}


def at(h, m, day=12) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=CET)


def names(leg):
    return [so["station"]["name"] for so in leg["stopovers"]]


def ids(leg):
    return [so["station"]["id"] for so in leg["stopovers"]]


def test_anfrage_wie_in_der_spezifikation():
    q = ob.query(False, ["8000013"], datetime.fromisoformat("2026-11-12T08:00:00+01:00"),
                 datetime.fromisoformat("2026-11-12T20:00:00+01:00"), ["HIGH_SPEED_TRAIN"])
    o = ob.body(q)
    assert o["stopPlaceOrStation"]["evaNumbers"] == ["8000013"]
    assert o["timeWindow"] == {"timeStart": "2026-11-12T08:00:00+01:00", "timeEnd": "2026-11-12T16:00:00+01:00"}, "höchstens 8 Stunden"
    assert o["transportTypes"] == ["HIGH_SPEED_TRAIN"]
    assert o["maxViaStops"] == 50 and o["languages"] == ["deu"]


def test_tafel_lesen():
    assert len(BOARD.departures) == 24
    ice = ice_entry()
    assert ice["journeyId"] == "20261112-d136f45d-9f67-3032-af2b-6d3cf146aa89"
    assert ice["transport"]["journeyDescription"] == "ICE 612"
    assert ob.platform(ice, "platformSchedule") == "3"
    assert ob.prognosed(ice) is None, "nur Plan"
    assert len(ob.via(ice)) == 14
    assert BOARD.name("8000170") == "Ulm Hbf"
    assert BOARD.name(ob.destination(ice)["evaNumber"]) == "Hamburg-Altona"


async def test_ice_ueber_die_nummer_mit_laufweg_und_geschaetzten_zeiten():
    source = Boards([BOARD])
    leg = await bm.match_leg(ice612()["legs"][0], source, PLACES)
    assert leg["tripId"] == "20261112-d136f45d-9f67-3032-af2b-6d3cf146aa89"
    assert leg["source"] == "openboards" and leg["matched"]
    assert names(leg) == ["Augsburg Hbf", "Ulm Hbf", "Stuttgart Hbf", "Mannheim Hbf"]
    assert ids(leg) == ["8000013", "8000170", "8000096", "8000244"]
    assert leg["stopovers"][0]["plannedDeparture"] == "2026-11-12T08:01:00+01:00"
    assert leg["plannedDeparturePlatform"] == "3"
    # Ohne Ankunftstafel: Ankunft und Gleis aus dem Text, Zwischenhalte geschätzt
    assert leg["plannedArrival"] == "2026-11-12T10:24:00+01:00"
    assert leg["plannedArrivalPlatform"] == "5"
    assert [so.get("estimated") for so in leg["stopovers"]] == [None, True, True, None]
    assert leg["stopovers"][1]["plannedDeparture"] == "2026-11-12T08:48:40+01:00"
    # Abfragen: Einstieg (±10 min), Ankunft am Ausstieg, je Zwischenhalt eine
    assert [q.arrivals for q in source.queries] == [False, True, False, False]
    assert source.queries[0].evas == ("8000013",)
    assert source.queries[1].evas == ("8000244",)


async def test_zwischenhalte_aus_der_tafel_des_halts():
    # Ulm kennt den Zug: Planzeit und Gleis von dort, Stuttgart bleibt geschätzt
    ulm = entry(evaNumber="8000170", timeSchedule="2026-11-12T08:47:00+01:00", time="2026-11-12T08:47:00+01:00",
                platformSchedule={"name": "2"})
    leg = await bm.match_leg(ice612()["legs"][0], Boards([BOARD, ob.Board({"departures": [ulm]})]), PLACES)
    assert leg["stopovers"][1]["plannedArrival"] == "2026-11-12T08:47:00+01:00"
    assert leg["stopovers"][1]["plannedPlatform"] == "2"
    assert [so.get("estimated") for so in leg["stopovers"]] == [None, False, True, None]


async def test_regionalzug_ueber_die_nummer_bis_mering():
    # Zwei Züge um 08:08 auf Gleis 8 mit gleichem Laufweg (RE89 94031, RE9 57131): die Nummer entscheidet
    s = share(text("RE9 (57131)", "08:08", "Augsburg Hbf", "Mering", "08:21", "8"))
    leg = await bm.match_leg(s["legs"][0], Boards([BOARD]), PLACES)
    assert leg["tripId"].startswith("20261112-6debdfda")
    assert names(leg) == ["Augsburg Hbf", "Augsburg Haunstetterstraße", "Augsburg-Hochzoll", "Kissing", "Mering-St Afra", "Mering"]
    assert ids(leg)[-1] == "8003982"


async def test_ohne_nummer_ueber_linie_und_minute():
    ok = share(text("RB69", "08:13", "Augsburg Hbf", "Bobingen", "08:25"))
    leg = await bm.match_leg(ok["legs"][0], Boards([BOARD]), PLACES)
    assert names(leg) == ["Augsburg Hbf", "Augsburg Morellstr.", "Augsburg Messe", "Inningen", "Bobingen"]
    # Ohne Nummer zählt die Minute: 7 Minuten daneben ist ein anderer Zug
    off = share(text("RB69", "08:20", "Augsburg Hbf", "Bobingen", "08:32"))
    source = Boards([BOARD])
    assert await bm.match_leg(off["legs"][0], source, PLACES) is None
    assert len(source.queries) == 2, "ohne Nummer: eng und spät"


async def test_mit_nummer_auch_mit_prognose_im_text():
    # Text zeigt 08:20 (Prognose), geplant ist 08:01: über die Nummer im weiten Fenster
    s = share(text("ICE 612", "08:20", "Augsburg Hbf", "Mannheim Hbf", "10:40"))
    leg = await bm.match_leg(s["legs"][0], Boards([BOARD]), PLACES)
    assert leg["plannedDeparture"] == "2026-11-12T08:01:00+01:00"


async def test_nichts_zu_tun():
    tram = share(text("STR 1", "08:01", "Augsburg Hbf", "Mannheim Hbf", "10:24"))
    source = Boards([BOARD])
    assert await bm.match_leg(tram["legs"][0], source, PLACES) is None, "keine Bahn"
    matched = await bm.match_snapshot(ice612(), source, PLACES)
    assert not bm.pending(matched)
    assert await bm.match_snapshot(matched, source, PLACES) is None, "schon zugeordnet"
    # Zugangsfehler gehen weiter, andere lassen den Abschnitt
    failing = Boards([BOARD])
    failing.fail = OpenBoardsError("BAD_CREDENTIALS", "401")
    with pytest.raises(OpenBoardsError):
        await bm.match_snapshot(ice612(), failing, PLACES)
    down = Boards([BOARD])
    down.fail = OpenBoardsError("UNAVAILABLE", "503")
    assert await bm.match_snapshot(ice612(), down, PLACES) is None


# --- Echtzeit ---

async def matched():
    return await bm.match_snapshot(ice612(), Boards([BOARD]), PLACES)


async def test_welche_halte_abgefragt_werden():
    s = await matched()
    assert brt.stops_to_poll(s, None, at(4, 30)) == {}, "mehr als 3 Stunden vorher"
    assert brt.stops_to_poll(s, None, at(7, 30)) == {0: [0]}, "vor der Abfahrt: Einstieg"
    assert brt.stops_to_poll(s, None, at(8, 5)) == {0: [0, 1, 2, 3]}, "gerade abgefahren: Einstieg noch dabei"
    assert brt.stops_to_poll(s, None, at(8, 30)) == {0: [1, 2, 3]}, "unterwegs: 2 nächste und Ausstieg"
    # Verspätet und noch nicht abgefahren: Einstieg bleibt dabei
    late = {"legs": {"0": {"stopovers": [{"departure": {"prognosed": "2026-11-12T08:40:00+01:00"}}]}}}
    assert brt.stops_to_poll(s, late, at(8, 30))[0] == [0, 1, 2, 3]
    assert brt.stops_to_poll(s, None, at(9, 0)) == {0: [2, 3]}
    assert brt.stops_to_poll(s, None, at(10, 30)) == {}, "angekommen"


async def test_verspaetung_erst_spaeter_bekannt():
    """Verspätung nur am Ausstieg bekannt (+45 min): Stuttgart (geschätzt 09:36) ist nach Plan
    vorbei, der Zug war aber noch nicht dort – es wird weiter abgefragt (wie in der App)."""
    s = await matched()
    assert brt.stops_to_poll(s, None, at(9, 45)) == {0: [3]}, "nach Plan"
    known = {"legs": {"0": {"stopovers": [
        {"arrival": {"prognosed": "2026-11-12T11:09:00+01:00" if k == 3 else None}} for k in range(4)]}}}
    assert brt.stops_to_poll(s, known, at(9, 45)) == {0: [2, 3]}, "Stuttgart erwartet 10:21"
    assert brt.stops_to_poll(s, known, at(10, 30)) == {0: [3]}


async def test_verspaetung_und_gleiswechsel_vor_der_abfahrt():
    s = await matched()
    late = entry(time="2026-11-12T08:06:00+01:00", timeType="PREVIEW", platform={"name": "4"})
    rt = await brt.update(s, None, Boards([ob.Board({"departures": [late]})]), at(7, 50))
    leg = rt["legs"]["0"]
    assert leg["hasRealtime"] and leg["tripId"] == s["legs"][0]["tripId"]
    first = leg["stopovers"][0]
    assert first["departure"]["delay"] == 300
    assert first["departure"]["prognosed"] == "2026-11-12T08:06:00+01:00"
    assert first["platform"] == "4" and first["platformChanged"]
    # fortgeschrieben bis zum Ausstieg, damit die Zugposition stimmt
    assert [st["source"] for st in leg["stopovers"]] == ["openboards", "propagated", "propagated", "propagated"]
    assert leg["stopovers"][3]["arrival"]["prognosed"] == "2026-11-12T10:29:00+01:00"
    assert effective_departure({"snapshot": s, "realtime": rt}) == at(8, 6)


async def test_unterwegs_mit_tatsaechlichen_zeiten_und_ausfall():
    s = await matched()
    before = await brt.update(s, None, Boards([ob.Board({"departures": [entry(time="2026-11-12T08:06:00+01:00", timeType="REAL")]})]), at(7, 58))
    ulm = entry(evaNumber="8000170", timeSchedule="2026-11-12T08:49:00+01:00", time="2026-11-12T08:56:00+01:00", timeType="PREVIEW")
    # Ankunftstafel: Via-Halte sind die Halte davor, der nächste zuerst
    prev = [{"evaNumber": e} for e in ("8000096", "8000170", "8000013")]
    mannheim = entry(evaNumber="8000244", timeSchedule="2026-11-12T10:24:00+01:00", time="2026-11-12T10:24:00+01:00",
                     cancelled=True, transport={**ice_entry()["transport"], "via": prev})
    source = Boards([ob.Board({"departures": [ulm]})], [ob.Board({"arrivals": [mannheim]})])
    rt = await brt.update(s, before, source, at(8, 30))
    st = rt["legs"]["0"]["stopovers"]
    assert st[0]["source"] == "kept", "Einstieg schon passiert: Stand von vorher"
    assert st[0]["departure"]["delay"] == 300
    assert st[1]["departure"]["delay"] == 420 and st[1]["source"] == "openboards"
    assert st[2]["source"] == "propagated" and st[2]["arrival"]["delay"] == 420
    assert st[3]["cancelled"], "Halt fällt aus"
    assert not rt["legs"]["0"]["cancelled"], "nur der Ausstieg, nicht der ganze Abschnitt"
    # Der Zug fährt nicht bis zum Ausstieg: als Umleitung
    assert [x["kind"] for x in rt["legs"]["0"]["diversion"]["stops"]] == ["served", "served", "served", "cancelled"]
    # Abfragen: Ulm um die erwartete Zeit, Stuttgart erst eng, dann weit, Mannheim als Ankunft
    assert [q.evas[0] for q in source.queries] == ["8000170", "8000096", "8000096", "8000244"]
    assert source.queries[-1].arrivals


async def test_meldungen_und_fehler():
    s = await matched()
    messages = {
        "disruptions": [{"messageID": 1, "descriptions": [{"language": "deu", "text": "Bauarbeiten zwischen Ulm und Stuttgart"}]}],
        "risCauseCodes": [{"messageID": 2, "code": "38", "descriptions": [{"language": "deu", "text": "Reparatur am Zug"}]}],
    }
    e = entry(time="2026-11-12T08:11:00+01:00", timeType="PREVIEW", messages=[1, 2])
    rt = await brt.update(s, None, Boards([ob.Board({"departures": [e], "messages": messages})]), at(7, 50))
    remarks = rt["legs"]["0"]["remarks"]
    assert [(r["type"], r["text"]) for r in remarks] == [("warning", "Bauarbeiten zwischen Ulm und Stuttgart"), ("cause", "Reparatur am Zug")]
    # Später nennt die Bahn nur noch den jüngsten Grund: der frühere bleibt (wie im DB Navigator),
    # die Störung von vorhin nicht
    later = {"risCauseCodes": [{"messageID": 3, "code": "51", "descriptions": [{"language": "deu", "text": "Technischer Defekt an einem anderen Zug"}]}]}
    e2 = entry(time="2026-11-12T08:20:00+01:00", timeType="PREVIEW", messages=[3])
    rt2 = await brt.update(s, rt, Boards([ob.Board({"departures": [e2], "messages": later})]), at(7, 55))
    assert [r["text"] for r in rt2["legs"]["0"]["remarks"]] == ["Reparatur am Zug", "Technischer Defekt an einem anderen Zug"]
    # Ein Fehler geht an den Aufrufer (der zählt ihn für „Echtzeitdaten nicht verfügbar“)
    down = Boards([])
    down.fail = OpenBoardsError("UNAVAILABLE", "503")
    with pytest.raises(OpenBoardsError):
        await brt.update(s, None, down, at(7, 50))
    assert await brt.update(s, None, down, at(4, 0)) is None, "nichts dran: keine Abfrage"


# --- Echte Tafeln vom 08.11.2026 (15–17 Uhr, Augsburg Hbf) und vom 08.12.2026 ---

def so(eva, name, arr, dep, day="2026-11-08"):
    return {"station": {"id": eva, "name": name},
            "plannedArrival": f"{day}T{arr}:00+01:00" if arr else None,
            "plannedDeparture": f"{day}T{dep}:00+01:00" if dep else None, "plannedPlatform": None}


def ride(journey_id, product, *stops, name="x", **line):
    return {"kind": "ride", "index": 0, "tripId": journey_id, "line": {"name": name, "product": product, **line},
            "plannedDeparture": stops[0]["plannedDeparture"], "plannedArrival": stops[-1]["plannedArrival"],
            "stopovers": list(stops), "matched": True, "source": "openboards"}


def snap(leg):
    return {"createdAt": "2026-11-08T12:00:00+01:00", "origin": leg["stopovers"][0]["station"],
            "destination": leg["stopovers"][-1]["station"], "plannedDeparture": leg["plannedDeparture"],
            "plannedArrival": leg["plannedArrival"], "legs": [leg]}


async def test_echte_tafeln_zeitfenster_und_kennung():
    # Das Fenster gilt für die beste bekannte Zeit: ICE 515 (Plan 13:58) steht in der Tafel ab 15:00
    ice515 = next(e for e in NOW_DEP.departures if (e.get("transport") or {}).get("journeyNumber") == 515)
    assert ice515["timeSchedule"] == "2026-11-08T13:58:00+01:00" and ice515["timeType"] == "REAL"
    # Dieselbe Fahrt hat in Ab- und Ankunftstafel dieselbe Kennung
    common = {e["journeyId"] for e in NOW_DEP.departures if e.get("journeyId")} & {e["journeyId"] for e in NOW_ARR.arrivals if e.get("journeyId")}
    assert len(common) == 26
    # Der Plan steht auch einen Monat vorher bereit
    dec = recorded("augsburg-ab-2026-12-08.json")
    assert len(dec.departures) == 24 and all(e["timeType"] == "SCHEDULE" for e in dec.departures)
    s = share(text("ICE 612", "08:01", "Augsburg Hbf", "Mannheim Hbf", "10:24", day="Di. 08.12.2026"))
    leg = await bm.match_leg(s["legs"][0], Boards([dec]), PLACES)
    assert leg["tripId"].startswith("20261208-")


async def test_zug_mit_zwei_nummern():
    """Der DB Navigator schreibt Züge mit zwei Nummern als „ICE 700 / ICE 29000“ (08.11.2026, Augsburg 19:44)."""
    s = share(text("ICE 707 / ICE 29000", "16:10", "Augsburg Hbf", "München Hbf", "16:46", "4", day="So. 08.11.2026"))
    assert bm.train_numbers(s["legs"][0]) == {707, 29000}
    assert bm.line_parts(s["legs"][0]["line"]["name"]) == ["ICE 707", "ICE 29000"]
    leg = await bm.match_leg(s["legs"][0], Boards([NOW_DEP]), PLACES)
    assert leg["tripId"] == "20261108-8aec0614-db46-315f-9c23-b88a58859c00"
    # Regionalzüge: die Zahl im Namen ist die Linie, keine Zugnummer
    assert bm.train_numbers({"line": {"name": "RE 9 / RE 80", "product": "regional"}}) == set()


async def test_nur_gueltige_bahnhofsnummern_und_zweiter_versuch_nach_400():
    assert ob.is_eva("8000013")
    assert not ob.is_eva("955964") and not ob.is_eva("80923444") and not ob.is_eva(None)
    odd = {"id": "955964", "name": "Augsburg Hbf", "lat": 48.3655, "lon": 10.8856}
    assert bm.evas_of(odd, PLACES) == ["8000013"], "6-stellige Nummer aus dem Gleisnetz fällt weg"

    # OpenBoards weist die Abfrage mit mehreren Bahnhöfen ab: noch einmal nur mit dem ersten
    inner = Boards([NOW_DEP])

    class Picky:
        async def board(self, q):
            if len(q.evas) > 1:
                raise OpenBoardsError("BAD_REQUEST", "HTTP 400", 400)
            return await inner.board(q)
    s = share(text("ICE 707", "16:10", "Augsburg Hbf", "München Hbf", "16:46", "4", day="So. 08.11.2026"))
    two = s["legs"][0]
    two["stopovers"][0]["station"]["id"] = "8000013"
    evas = bm.evas_of(two["stopovers"][0]["station"], PLACES)
    found = await bm.find_departure(Picky(), [*evas, "8002140"], two, datetime.fromisoformat("2026-11-08T16:10:00+01:00"))
    assert found.entry["transport"]["journeyNumber"] == 707


async def test_verspaeteter_zug_wird_auch_spaeter_gefunden():
    s = share(text("ICE 515", "13:58", "Augsburg Hbf", "München Hbf", "14:40", "4", day="So. 08.11.2026"),
              now="2026-11-08T14:30:00+01:00")
    source = Boards([NOW_DEP])
    leg = await bm.match_leg(s["legs"][0], source, PLACES)
    assert leg["tripId"] == "20261108-d22ee9a8-a1a3-3d00-b04b-d6fba482e17d"
    assert leg["plannedDeparture"] == "2026-11-08T13:58:00+01:00", "Planzeit, nicht die tatsächliche"
    assert ids(leg) == ["8000013", "8000261"]
    # ±10, −60, +60 leer; gefunden im späten Fenster
    assert sum(1 for q in source.queries if not q.arrivals) == 4


async def test_echtzeit_aus_echten_tafeln():
    nov8 = lambda h, m: at(h, m, day=8)  # noqa: E731
    # RE9 57065 Ulm → München: Augsburg ist Zwischenhalt (an 16:33, ab 16:38, Prognose 16:39)
    re9 = ride("20261108-8e28ccb8-fbbf-3f8b-b18c-4c1303864731", "regional",
               so("8000170", "Ulm Hbf", None, "15:39"), so("8000013", "Augsburg Hbf", "16:33", "16:38"),
               so("8003982", "Mering", "16:50", None))
    source = Boards([NOW_DEP], [NOW_ARR])
    rt = await brt.update(snap(re9), None, source, nov8(16, 0))
    st = rt["legs"]["0"]["stopovers"]
    assert st[1]["departure"]["prognosed"] == "2026-11-08T16:39:00+01:00" and st[1]["departure"]["delay"] == 60
    assert st[1]["arrival"]["prognosed"] == "2026-11-08T16:34:00+01:00", "Ankunft wie die Abfahrt verspätet"
    assert st[1]["platform"] == "9"
    assert st[2]["source"] == "propagated"
    assert [r["text"] for r in rt["legs"]["0"]["remarks"]] == ["Verfügbarkeit der Gleise derzeit eingeschränkt"]

    # ICE 514 vor der Abfahrt: 18 Minuten später
    ice514 = ride("20261108-cdad4419-425e-35e6-8247-58b337ab163a", "longdistance",
                  so("8000013", "Augsburg Hbf", None, "16:01"), so("8000170", "Ulm Hbf", "16:45", None))
    rt514 = await brt.update(snap(ice514), None, source, nov8(15, 30))
    assert rt514["legs"]["0"]["stopovers"][0]["departure"]["delay"] == 1080
    assert effective_departure({"snapshot": snap(ice514), "realtime": rt514}) == nov8(16, 19)

    # RB87 57148 München → Augsburg: Ankunft aus der Ankunftstafel, pünktlich
    rb87 = ride("20261108-3a57922b-4368-3065-ad3c-7bf320e401c9", "regional",
                so("8000261", "München Hbf", None, "15:10"), so("8000013", "Augsburg Hbf", "15:46", None))
    rt87 = await brt.update(snap(rb87), None, source, nov8(15, 30))
    assert rt87["legs"]["0"]["stopovers"][1]["arrival"]["delay"] == 0
    assert rt87["legs"]["0"]["hasRealtime"]

    # Tatsächliche Zeit mit Sekunden: RB87 57059 ab 15:08:38
    rb87b = ride("20261108-5432aea3-b93e-3cce-b47c-903843c8591f", "regional",
                 so("8000013", "Augsburg Hbf", None, "15:08"), so("8003982", "Mering", "15:20", None))
    rtb = await brt.update(snap(rb87b), None, source, nov8(15, 5))
    assert rtb["legs"]["0"]["stopovers"][0]["departure"]["prognosed"] == "2026-11-08T15:08:38+01:00"
    assert rtb["legs"]["0"]["stopovers"][0]["departure"]["delay"] == 38


# --- Bahnhofsnummern ---

async def test_bahnhofsnummern_statt_uic_code():
    # Im Gleisnetz steht bei vielen großen Bahnhöfen der UIC-Code statt der EVA
    uic = Places([("Augsburg Hbf", "8002140", "MA", 48.3655, 10.8856, "rail"), ("Mannheim Hbf", "8014008", "RM", 49.4794, 8.4694, "rail")])
    s = share(text("ICE 612", "08:01", "Augsburg Hbf", "Mannheim Hbf", "10:24"), places=uic)
    lines = []
    assert await bm.match_leg(s["legs"][0], Boards([BOARD]), uic, log=lines.append) is None
    assert len(lines) == 1 and lines[0].startswith("ICE 612: nicht in der Tafel von Augsburg Hbf"), lines
    directory = StationList([Station(["8000013"], "Augsburg Hbf", point=(48.36544, 10.88557)),
                             Station(["8000244"], "Mannheim Hbf", point=(49.47960, 8.46890))])
    leg = await bm.match_leg(s["legs"][0], Boards([BOARD]), uic, directory)
    assert ids(leg) == ["8000013", "8000170", "8000096", "8000244"]


def test_bahnhofsliste():
    d = station_list()
    assert len(d) > 7000

    def first(name, lat, lon):
        return d.candidates({"name": name, "lat": lat, "lon": lon})[0]
    # Lage wie im Gleisnetz, Namen wie im geteilten Text
    assert first("München Hbf", 48.140725, 11.556943) == "8000261"
    assert first("Ulm Hbf", 48.399416, 9.982602) == "8000170"
    assert first("Frankfurt(Main)Hbf", 50.106654, 8.662581) == "8000105"
    assert first("Berlin Hbf", 52.524945, 13.369661) == "8011160"
    assert first("Kissing", 48.300797, 10.959597) == "8003299"
    assert first("Neusäß", 48.394508, 10.83404) == "8004318"
    assert "8011113" in d.candidates({"name": "Berlin Südkreuz (S)", "lat": 52.4758, "lon": 13.3650})
    # Ohne Lage nur eindeutige Namen
    assert d.by_name("Hauptbahnhof", None) == []
    # Alle Nummern eines Bahnhofs (OpenStation)
    assert d.by_name("München Hbf", (48.1407, 11.5569)) == ["8000261", "8098261", "8098262", "8098263"]
    assert d.group("8089073") == ["8011113", "8089073"]
    assert d.group("9999999") == ["9999999"], "unbekannte Nummer bleibt"
    # Schreibweisen: DB Navigator, amtlich, OpenStreetMap
    assert d.by_name("Frankfurt(M) Flughafen Regionalbf", None)[0] == "8070004"
    assert d.by_name("Berlin Hauptbahnhof", None)[0] == "8011160"
    assert d.name("8089021") == "Berlin Hauptbahnhof"
    # Neuer Halt, den trainline noch nicht kennt
    assert d.by_name("Trier West", None) == ["8005906"]
    assert any(eva == "8011113" and name == "Berlin Südkreuz" for eva, _p, name in d.search("Südkr"))
    # Bahnhofssuche für die Tafel
    assert d.search("augsburg hbf")[0][2] == "Augsburg Hbf"
    assert d.search("München Hbf")[0][0] == "8000261"
    assert {"Mering", "Mering-St Afra"} <= {n for _e, _p, n in d.search("Mering")}
    assert d.search("a") == []
    assert d.resolve("Augsburg Hbf") == ("8000013", "Augsburg Hbf")
    assert d.resolve("8000261") == ("8000261", "München Hbf")
    assert d.coords(None, "Ulm Hbf") is not None


# --- Umleitungen laut Bahn ---

def stops(*n):
    return [RouteStop(None, x) for x in n]


def kinds(d):
    return [s["kind"] for s in d["stops"]]


def test_laufweg_vergleichen():
    plan = stops("Nürnberg Hbf", "Erfurt Hbf", "Halle (Saale) Hbf", "Berlin Südkreuz")
    assert compare_paths(plan, stops("München Hbf") + plan + stops("Berlin Hbf")) is None, "alles wie geplant"
    d = compare_paths(plan, stops("Nürnberg Hbf", "Erfurt Hbf", "Leipzig Hbf", "Berlin Südkreuz", "Berlin Hbf"))
    assert d["path"] == ["Nürnberg Hbf", "Erfurt Hbf", "Leipzig Hbf", "Berlin Südkreuz"]
    assert d["stops"] == [{"kind": "served", "index": 0}, {"kind": "served", "index": 1}, {"kind": "cancelled", "index": 2},
                          {"kind": "added", "name": "Leipzig Hbf", "eva": None}, {"kind": "served", "index": 3}], "entfallener Halt vor dem neuen"
    # Zug endet vorzeitig: der Ausstieg entfällt
    assert kinds(compare_paths(plan, stops("Nürnberg Hbf", "Erfurt Hbf"))) == ["served", "served", "cancelled", "cancelled"]
    assert compare_paths(stops("Berlin Südkreuz (S)", "Berlin-Schöneberg"), stops("Berlin Südkreuz", "Berlin-Schöneberg")) is None
    assert compare_paths(plan, stops("Hamburg Hbf", "Bremen Hbf")) is None, "nichts gemeinsam: keine Aussage"
    s41 = stops("Berlin Südkreuz", "Berlin-Schöneberg", "Heidelberger Platz (S+U), Berlin")
    assert compare_paths(s41, stops("Berlin Südkreuz", "Berlin-Schöneberg", "Berlin Heidelberger Platz")) is None
    assert kinds(compare_paths(s41, stops("Berlin Südkreuz", "Berlin Heidelberger Platz"))) == ["served", "cancelled", "served"]
    # Über die Bahnhofsnummer auch bei ganz anderer Schreibweise
    assert compare_paths([RouteStop("8000105", "Frankfurt(Main)Hbf")], [RouteStop("8000105", "Frankfurt Hauptbahnhof")]) is None


def ice515():
    """ICE 515 Wuppertal → Augsburg, geplant über Köln Messe/Deutz; gefahren über Köln Hbf (echte Ankunftstafel)."""
    st = [so("8000266", "Wuppertal Hbf", None, "09:54"), so("8000087", "Solingen Hbf", "10:07", "10:08"),
          so("8003368", "Köln Messe/Deutz Gl.11-12", "10:23", "10:26"), so("8070003", "Frankfurt(M) Flughafen Fernbf", "11:22", "11:25"),
          so("8000244", "Mannheim Hbf", "11:57", "12:00"), so("8000096", "Stuttgart Hbf", "12:38", "12:44"),
          so("8000170", "Ulm Hbf", "13:40", "13:42"), so("8000013", "Augsburg Hbf", "13:56", None)]
    jid = next(e for e in NOW_ARR.arrivals if (e.get("transport") or {}).get("journeyNumber") == 515)["journeyId"]
    return snap(ride(jid, "longdistance", *st, name="ICE 515", trainNumber="515", productName="ICE"))


async def test_echte_umleitung_ueber_koeln_hbf():
    s = ice515()

    class Source:
        async def board(self, q):
            return NOW_ARR if q.arrivals and "8000013" in q.evas else ob.Board()
    # Ausstieg wird abgefragt (Zug laut letzter Abfrage über eine Stunde zu spät, noch unterwegs);
    # auch Ulm: mit derselben Verspätung erst um 14:53 erwartet (wie in der App)
    known = {"legs": {"0": {"tripId": s["legs"][0]["tripId"], "stopovers": [
        {"stationId": x["station"]["id"], "arrival": {"prognosed": "2026-11-08T15:09:00+01:00" if k == 7 else None}}
        for k, x in enumerate(s["legs"][0]["stopovers"])]}}}
    assert brt.stops_to_poll(s, known, at(14, 30, day=8)) == {0: [6, 7]}
    rt = await brt.update(s, known, Source(), at(14, 30, day=8))
    leg = rt["legs"]["0"]
    d = leg["diversion"]
    assert kinds(d) == ["served", "served", "cancelled", "added", "served", "served", "served", "served", "served"]
    assert d["stops"][3]["name"] == "Köln Hbf" and d["stops"][3]["eva"] == "8000207"
    assert leg["stopovers"][2]["cancelled"], "Köln Messe/Deutz entfällt, obwohl nicht abgefragt"
    assert leg["hasRealtime"]
    assert leg["stopovers"][-1]["arrival"]["delay"] == 4432, "15:09:52 statt 13:56"
    # Ohne neue Aussage gilt die bekannte Umleitung weiter
    again = await brt.update(s, rt, type("Q", (), {"board": staticmethod(lambda q: _empty())})(), at(14, 32, day=8))
    assert again["legs"]["0"]["diversion"] == d


async def _empty():
    return ob.Board()


# --- Abfahrtstafel ---

async def test_tafel_aus_echter_antwort():
    now = datetime.fromisoformat("2026-11-08T15:24:00+01:00")
    b = await departures(Boards([NOW_DEP]), BoardStation("8000013", "Augsburg Hbf"), now, minutes=90, limit=50)
    rows = b["departures"]
    assert rows and b["station"] == {"eva": "8000013", "name": "Augsburg Hbf"}
    # abgefahrene Züge fallen weg, sortiert nach bester bekannter Zeit
    times = [datetime.fromisoformat(r["prognosed"] or r["planned"]) for r in rows]
    assert all(t >= now - timedelta(minutes=1) for t in times) and times == sorted(times)
    re9 = next(r for r in rows if r["id"] == "20261108-8e28ccb8-fbbf-3f8b-b18c-4c1303864731")
    assert re9["line"] == "RE9" and re9["delayMin"] == 1 and re9["platform"] == "9"
    assert re9["messages"] == ["Verfügbarkeit der Gleise derzeit eingeschränkt"]
    ice = next(r for r in rows if r["line"] == "ICE 514")
    assert ice["delayMin"] == 18 and len(ice["via"]) <= 3


async def test_client_fehler_und_zwischenspeicher():
    calls = []

    async def post(url, headers, payload):
        calls.append((url, headers, payload))
        return responses.pop(0)
    responses = [(200, '{"departures": []}'), (401, ""), (429, ""), (400, "Unknown eva"), (404, ""), (503, "")]
    client = ob.OpenBoardsClient(post, "cid", "key", limiter=ob.RateLimiter(limit=1000, min_gap=0))
    q = ob.query(False, ["8000013"], at(8, 0), at(9, 0))
    assert (await client.board(q)).departures == []
    assert calls[0][0].endswith("/departures") and calls[0][1]["DB-Client-ID"] == "cid" and calls[0][1]["DB-Api-Key"] == "key"
    assert (await client.board(q)).departures == [] and len(calls) == 1, "gleiche Abfrage: aus dem Zwischenspeicher"
    for code in ("BAD_CREDENTIALS", "RATE_LIMITED", "BAD_REQUEST"):
        with pytest.raises(OpenBoardsError) as err:
            await client.board(ob.query(True, ["8000013"], at(8, len(calls)), at(9, 0)))
        assert err.value.code == code
        client._paused_until = None   # Pause nach 429 hier übergehen (eigener Test unten)
    assert "Unknown eva" in str(err.value)
    assert (await client.board(ob.query(True, ["8000013"], at(8, 30), at(9, 0)))).entries == [], "404: leere Tafel"
    with pytest.raises(OpenBoardsError) as err:
        await client.board(ob.query(True, ["8000013"], at(8, 31), at(9, 0)))
    assert err.value.code == "UNAVAILABLE"
    assert client.calls == 6


async def test_hoechstens_vier_abfragen_pro_sekunde_und_60_pro_minute():
    now = [0.0]
    waits = []

    async def sleep(s):
        waits.append(round(s, 3))
        now[0] += s
    lim = ob.RateLimiter(limit=3, clock=lambda: now[0], sleep=sleep)
    for _ in range(4):
        await lim.acquire()
    # 0.25 s Abstand; die vierte wartet aufs Ende der Minute
    assert waits == [0.25, 0.25, 59.51]


async def test_pause_nach_zu_vielen_anfragen():
    now = [0.0]
    calls = []

    async def post(url, headers, payload):
        calls.append(url)
        return responses.pop(0)
    responses = [(429, ""), (200, '{"departures": []}')]
    client = ob.OpenBoardsClient(post, "cid", "key", limiter=ob.RateLimiter(limit=1000, min_gap=0), clock=lambda: now[0])
    q = ob.query(False, ["8000013"], at(8, 0), at(9, 0))
    with pytest.raises(OpenBoardsError) as err:
        await client.board(q)
    assert err.value.code == "RATE_LIMITED" and client.paused() == 60
    now[0] = 30.0
    with pytest.raises(OpenBoardsError) as err:
        await client.board(q)
    assert err.value.code == "RATE_LIMITED" and "Pause" in str(err.value)
    assert len(calls) == 1, "in der Pause keine Anfrage"
    now[0] = 61.0
    assert (await client.board(q)).departures == [] and len(calls) == 2


async def test_voruebergehende_fehler_einzelner_abschnitte():
    errors = []
    down = Boards([BOARD])
    down.fail = OpenBoardsError("UNAVAILABLE", "503")
    assert await bm.match_snapshot(ice612(), down, PLACES, on_error=lambda leg, err: errors.append(err.code)) is None
    assert errors == ["UNAVAILABLE"]
