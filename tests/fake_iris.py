"""Nachgebaute Timetables-API für Tests: Bahnhöfe und Fahrten → XML wie die echte API.

Die Fahrten sind an echte Verbindungen angelehnt; Zeiten der Zwischenhalte
sind für Testzwecke ausgedacht.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote
from xml.sax.saxutils import quoteattr
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")


@dataclass
class FakeStation:
    eva: str
    name: str
    meta: list[str] = field(default_factory=list)


@dataclass
class FakeTrip:
    key: str
    category: str
    number: str
    line: str | None
    stops: list[tuple]  # (eva, name, ankunft "HH:MM"|None, abfahrt "HH:MM"|None, gleis)
    day: str = "2026-11-01"


def _dt(day: str, hhmm: str | None) -> datetime | None:
    if not hhmm:
        return None
    y, m, d = map(int, day.split("-"))
    hh, mm = map(int, hhmm.split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def _iris(dt: datetime) -> str:
    return dt.strftime("%y%m%d%H%M")


class FakeIris:
    def __init__(self, stations: list[FakeStation], trips: list[FakeTrip]) -> None:
        self.stations = stations
        self.trips = trips
        self.paths: list[str] = []
        self.changes: dict[str, str] = {}  # eva → XML der Änderungen

    def _stop_xml(self, trip: FakeTrip, i: int) -> str:
        eva, name, arr, dep, platform = trip.stops[i]
        before = "|".join(s[1] for s in trip.stops[:i])
        after = "|".join(s[1] for s in trip.stops[i + 1:])
        first_dep = _dt(trip.day, trip.stops[0][3])
        sid = f"{trip.key}-{_iris(first_dep)}-{i + 1}"
        line = f' l="{trip.line}"' if trip.line else ""
        parts = [f'<s id="{sid}"><tl c="{trip.category}" n="{trip.number}" o="80" f="D"/>']
        if arr:
            parts.append(f'<ar pt="{_iris(_dt(trip.day, arr))}" pp="{platform}"{line} ppth={quoteattr(before)}/>')
        if dep:
            parts.append(f'<dp pt="{_iris(_dt(trip.day, dep))}" pp="{platform}"{line} ppth={quoteattr(after)}/>')
        parts.append("</s>")
        return "".join(parts)

    def plan_xml(self, eva: str, day: str, hour: str) -> str:
        body = []
        for trip in self.trips:
            for i, (s_eva, _, arr, dep, _) in enumerate(trip.stops):
                t = _dt(trip.day, dep) or _dt(trip.day, arr)
                if s_eva == eva and t.strftime("%y%m%d") == day and t.strftime("%H") == hour:
                    body.append(self._stop_xml(trip, i))
        name = next((s.name for s in self.stations if s.eva == eva), "?")
        return f'<?xml version="1.0"?><timetable station={quoteattr(name)}>{"".join(body)}</timetable>'

    def station_xml(self, query: str) -> str:
        q = query.lower()
        hits = [s for s in self.stations if q in s.name.lower()]
        items = "".join(f'<station name={quoteattr(s.name)} eva="{s.eva}" meta="{"|".join(s.meta)}"/>' for s in hits)
        return f"<stations>{items}</stations>"

    async def __call__(self, url: str, headers: dict) -> tuple[int, str]:
        path = url.split("/timetables/v1", 1)[1]
        self.paths.append(path)
        parts = path.strip("/").split("/")
        if parts[0] == "station":
            return 200, self.station_xml(unquote(parts[1]))
        if parts[0] == "plan":
            return 200, self.plan_xml(parts[1], parts[2], parts[3])
        if parts[0] == "fchg":
            return 200, self.changes.get(parts[1], '<timetable station="x"/>')
        return 404, ""


def november_world() -> FakeIris:
    """Verbindung vom 01.11.2026: RE1 4024, ICE 1004, S41 – plus Störzüge."""
    st = [
        FakeStation("8000013", "Augsburg Hbf"),
        FakeStation("8000080", "Donauwörth"),
        FakeStation("8000122", "Treuchtlingen"),
        FakeStation("8000284", "Nürnberg Hbf"),
        FakeStation("8010101", "Erfurt Hbf"),
        FakeStation("8010159", "Halle (Saale) Hbf"),
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
    return FakeIris(st, trips)
