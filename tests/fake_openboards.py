"""OpenBoards für Tests.

* Tafeln: liefert aus aufgezeichneten Antworten (tests/fixtures/openboards, dieselben
  wie in der Android-App) nur die Einträge, die die echte API liefern würde – gefragte
  Bahnhöfe und Zugarten, beste bekannte Zeit im Fenster. Wie Fake in OpenBoardsTest.kt.
* World: nachgebaute Fahrten (Verbindung vom 01.11.2026 wie bisher in fake_iris) als
  Tafeln, mit Verspätungen, Gleiswechseln, Ausfällen und geändertem Laufweg zum Setzen.
"""

from __future__ import annotations

import copy
import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core import openboards as ob
from core.openboards import Board, BoardQuery, OpenBoardsError
from core.stationlist import Station as ListStation, StationList, parse as parse_stations
from testdata import FIXTURES

TZ = ZoneInfo("Europe/Berlin")


def recorded(name: str) -> Board:
    return ob.parse((FIXTURES / "openboards" / name).read_text(encoding="utf-8"))


def station_list() -> StationList:
    """Echte Bahnhofsliste (Release „bahnhofsnummern“, gebaut am 09.11.2026)."""
    return parse_stations(gzip.decompress((FIXTURES / "bahnhofsnummern.json.gz").read_bytes()).decode("utf-8"))


def _keep(e: dict, q: BoardQuery) -> bool:
    t = datetime.fromisoformat(e.get("time") or e["timeSchedule"])
    return e.get("evaNumber") in q.evas and q.start <= t <= q.end and \
        (q.types is None or (e.get("transport") or {}).get("type") in q.types)


class Boards:
    """Tafeln wie die API aus festen Antworten."""

    def __init__(self, departures: list[Board], arrivals: list[Board] | None = None) -> None:
        self.departures, self.arrivals = departures, arrivals or []
        self.queries: list[BoardQuery] = []
        self.fail: OpenBoardsError | None = None

    async def board(self, q: BoardQuery) -> Board:
        self.queries.append(q)
        if self.fail is not None:
            raise self.fail
        source = self.arrivals if q.arrivals else self.departures
        entries = [e for b in source for e in (b.arrivals if q.arrivals else b.departures) if _keep(e, q)]
        names, seen = [], set()
        for b in source:
            for sp in b.data.get("stopPlaces") or []:
                if sp.get("evaNumber") not in seen:
                    seen.add(sp.get("evaNumber"))
                    names.append(sp)
        msgs = next((b.data.get("messages") for b in source if b.data.get("messages")), None)
        data = {"arrivals" if q.arrivals else "departures": entries, "stopPlaces": names}
        if msgs:
            data["messages"] = msgs
        return Board(data)


def board_of(departures=(), arrivals=(), names: dict | None = None, messages: dict | None = None) -> Board:
    data = {"departures": list(departures), "arrivals": list(arrivals),
            "stopPlaces": [{"evaNumber": k, "localizedNames": [{"language": "deu", "name": v}]} for k, v in (names or {}).items()]}
    if messages:
        data["messages"] = messages
    return Board(data)


# --- Nachgebaute Fahrten -------------------------------------------------------------

_TYPES = {"ICE": "HIGH_SPEED_TRAIN", "IC": "INTERCITY_TRAIN", "EC": "INTERCITY_TRAIN", "RE": "REGIONAL_TRAIN",
          "RB": "REGIONAL_TRAIN", "S": "CITY_TRAIN"}


@dataclass
class FakeStation:
    eva: str
    name: str
    meta: list[str] = field(default_factory=list)   # weitere Nummern desselben Bahnhofs
    point: tuple[float, float] | None = None


@dataclass
class FakeTrip:
    key: str
    category: str
    number: str
    line: str | None
    stops: list[tuple]  # (eva, name, ankunft "HH:MM"|None, abfahrt "HH:MM"|None, gleis)
    day: str = "2026-11-01"

    @property
    def journey_id(self) -> str:
        return f"{self.day.replace('-', '')}-{self.key.strip('-')}"


def _dt(day: str, hhmm: str | None) -> datetime | None:
    if not hhmm:
        return None
    y, m, d = map(int, day.split("-"))
    hh, mm = map(int, hhmm.split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def leipzig_instead_of_halle(world: "World") -> None:
    """ICE 1004 fährt ab Erfurt über Leipzig statt Halle (Laufweg mit Markierungen)."""
    world.route["-222"] = [("8000284", False, False), ("8010101", False, False), ("8010159", True, False),
                           ("8010205", False, True), ("8011113", False, False), ("8011160", False, False)]


class World:
    """OpenBoards über nachgebaute Fahrten. Änderungen je (Fahrt, Halt):
    delay (Minuten), platform, cancelled, real (tatsächlich statt Prognose), messages."""

    def __init__(self, stations: list[FakeStation], trips: list[FakeTrip]) -> None:
        self.stations = stations
        self.trips = trips
        self.queries: list[BoardQuery] = []
        self.changes: dict[tuple[str, int], dict] = {}
        self.route: dict[str, list[tuple[str, bool, bool]]] = {}   # Fahrt → Laufweg (eva, entfällt, zusätzlich)
        self.messages: dict = {}
        self.fail: OpenBoardsError | None = None

    def change(self, key: str, index: int, **kw) -> None:
        self.changes.setdefault((key, index), {}).update(kw)

    def directory(self) -> StationList:
        """Bahnhofsliste der nachgebauten Welt (Nebennummern beim Hauptbahnhof)."""
        out = []
        for s in self.stations:
            if any(s.eva in o.meta for o in self.stations):
                continue
            aliases = [o.name for o in self.stations if o.eva in s.meta]
            out.append(ListStation(evas=[s.eva, *s.meta], name=s.name, aliases=aliases, point=s.point))
        return StationList(out)

    def _name(self, eva: str) -> str:
        return next((s.name for s in self.stations if s.eva == eva), eva)

    def _entry(self, trip: FakeTrip, i: int, arrival: bool) -> dict | None:
        eva, _name, arr, dep, platform = trip.stops[i]
        planned = _dt(trip.day, arr if arrival else dep)
        if planned is None:
            return None
        ch = self.changes.get((trip.key, i), {})
        delay = ch.get("delay")
        best = planned + timedelta(minutes=delay) if delay is not None else planned
        stops = [s[0] for s in trip.stops]
        route = self.route.get(trip.key)
        if route is None:
            via = [{"evaNumber": e} for e in (stops[:i][::-1] if arrival else stops[i + 1:-1])]
        else:
            evas = [r[0] for r in route]
            here = evas.index(eva) if eva in evas else i
            part = route[:here][::-1] if arrival else route[here + 1:-1]
            via = [{"evaNumber": e, **({"cancelled": True} if c else {}), **({"additional": True} if a else {})} for e, c, a in part]
        transport = {"category": trip.category, "journeyNumber": int(trip.number), "type": _TYPES.get(trip.category),
                     "journeyDescription": f"{trip.category} {trip.number}", "via": via}
        if trip.line:
            transport["line"] = {"name": f"{trip.category}{trip.line}"}
        e = {
            "evaNumber": eva,
            "journeyId": trip.journey_id,
            "timeSchedule": planned.isoformat(),
            "time": best.isoformat(),
            "timeType": ("REAL" if ch.get("real") else "PREVIEW") if delay is not None else "SCHEDULE",
            "platformSchedule": {"name": platform},
            "platform": {"name": ch.get("platform", platform)},
            "transport": transport,
            "journeyInfo": {"origin": {"evaNumber": stops[0]}, "destination": {"evaNumber": stops[-1]},
                            "headerJourneyNumber": int(trip.number)},
            "cancelled": bool(ch.get("cancelled")),
            "messages": list(ch.get("messages") or []),
        }
        return e

    async def board(self, q: BoardQuery) -> Board:
        self.queries.append(q)
        if self.fail is not None:
            raise self.fail
        entries = []
        evas = set()
        for trip in self.trips:
            for i, stop in enumerate(trip.stops):
                if stop[0] not in q.evas:
                    continue
                e = self._entry(trip, i, q.arrivals)
                if e is None or not _keep(e, q):
                    continue
                if q.max_via == 0:
                    e["transport"]["via"] = []
                entries.append(e)
                evas |= {e["evaNumber"], e["journeyInfo"]["destination"]["evaNumber"]}
                evas |= {v["evaNumber"] for v in e["transport"]["via"]}
        entries.sort(key=lambda e: e["time"])
        data = {"arrivals" if q.arrivals else "departures": entries,
                "stopPlaces": [{"evaNumber": e, "localizedNames": [{"language": "deu", "name": self._name(e)}]} for e in sorted(evas)]}
        if self.messages:
            data["messages"] = copy.deepcopy(self.messages)
        return Board(data)

    async def __call__(self, url: str, headers: dict, payload: dict) -> tuple[int, str]:
        """Als Post für OpenBoardsClient (Integrationstests)."""
        arrivals = url.endswith("/arrivals")
        w = payload["timeWindow"]
        q = ob.query(arrivals, payload["stopPlaceOrStation"]["evaNumbers"], datetime.fromisoformat(w["timeStart"]),
                     datetime.fromisoformat(w["timeEnd"]), payload.get("transportTypes"), payload.get("maxViaStops", 50),
                     payload.get("maxEntriesPerTransportType", 50))
        try:
            b = await self.board(q)
        except Exception as err:  # noqa: BLE001 – in Home Assistant ist es die Klasse aus custom_components
            if not hasattr(err, "code"):
                raise
            return getattr(err, "status", None) or 503, ""
        return 200, json.dumps(b.data)


async def matched_trip(text: str, now: datetime, world: World | None = None) -> dict:
    """Geteilter Text → Entwurf → in der nachgebauten Bahn zugeordnet (wie in der Integration,
    nur ohne Gleisnetz: Bahnhofsnummern aus der Bahnhofsliste der Welt)."""
    from core.boardmatch import match_snapshot
    from core.journey import draft_snapshot
    from core.share_text import parse_share_text
    w = world or november_world()
    snap = draft_snapshot(parse_share_text(text), now)
    return await match_snapshot(snap, w, None, w.directory()) or snap


def november_world() -> World:
    """Verbindung vom 01.11.2026: RE1 4024, ICE 1004, S41 – plus Störzüge."""
    st = [
        FakeStation("8000013", "Augsburg Hbf", point=(48.3654, 10.8856)),
        FakeStation("8000080", "Donauwörth"),
        FakeStation("8000122", "Treuchtlingen"),
        FakeStation("8000284", "Nürnberg Hbf", point=(49.4457, 11.0827)),
        FakeStation("8010101", "Erfurt Hbf"),
        FakeStation("8010159", "Halle (Saale) Hbf"),
        FakeStation("8010205", "Leipzig Hbf"),
        FakeStation("8011113", "Berlin Südkreuz", meta=["8089110"]),
        FakeStation("8089110", "Berlin Südkreuz (S)"),
        FakeStation("8011160", "Berlin Hbf"),
        FakeStation("8089032", "Berlin-Schöneberg"),
        FakeStation("8089017", "Berlin Innsbrucker Platz"),
        FakeStation("8089015", "Berlin Heidelberger Platz"),
    ]
    trips = [
        FakeTrip("-111", "RE", "4024", "1", [
            ("8000013", "Augsburg Hbf", None, "14:37", "3"),
            ("8000080", "Donauwörth", "14:58", "15:00", "2"),
            ("8000122", "Treuchtlingen", "15:21", "15:23", "4"),
            ("8000284", "Nürnberg Hbf", "15:49", None, "5"),
        ]),
        FakeTrip("-112", "RE", "4026", "1", [  # gleicher Bahnhof, eine Stunde später
            ("8000013", "Augsburg Hbf", None, "15:37", "3"),
            ("8000284", "Nürnberg Hbf", "16:49", None, "5"),
        ]),
        FakeTrip("-222", "ICE", "1004", None, [
            ("8000284", "Nürnberg Hbf", None, "16:36", "4"),
            ("8010101", "Erfurt Hbf", "17:42", "17:44", "1"),
            ("8010159", "Halle (Saale) Hbf", "18:20", "18:22", "8"),
            ("8011113", "Berlin Südkreuz", "19:40", "19:42", "7"),
            ("8011160", "Berlin Hbf", "19:50", None, "1"),
        ]),
        FakeTrip("-333", "S", "41047", "41", [
            ("8089110", "Berlin Südkreuz (S)", None, "19:47", "11"),
            ("8089032", "Berlin-Schöneberg", "19:49", "19:49", "1"),
            ("8089017", "Berlin Innsbrucker Platz", "19:51", "19:51", "1"),
            ("8089015", "Berlin Heidelberger Platz", "19:54", "19:54", "1"),
        ]),
        FakeTrip("-334", "S", "42047", "42", [  # andere Linie, gleiche Minute
            ("8089110", "Berlin Südkreuz (S)", None, "19:47", "12"),
            ("8089015", "Berlin Heidelberger Platz", "20:01", None, "2"),
        ]),
        FakeTrip("-335", "S", "41049", "41", [  # nächste S41
            ("8089110", "Berlin Südkreuz (S)", None, "19:52", "11"),
            ("8089015", "Berlin Heidelberger Platz", "19:59", None, "1"),
        ]),
    ]
    return World(st, trips)
