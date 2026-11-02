"""Zwischenhalte aus dem Fahrplan (GTFS von gtfs.de, Daten DELFI e.V., CC BY 4.0).

Für Abschnitte, die nur aus dem geteilten Text bestehen (source "text"):
  * Züge, solange die Timetables-API ihren Fahrplan noch nicht liefert
    (mehr als etwa einen Tag vor Abfahrt) – bis 30 Tage im Voraus,
  * Straßenbahn und U-Bahn, die die Timetables-API gar nicht kennt,
  * Ersatzbusse („Bus RE80“); andere Busse stehen nicht im Fahrplan.
Gesucht wird die Fahrt über Einstiegs-Station, Abfahrtsminute, Linie und
Ausstieg (die freien Feeds haben keine Zugnummern). Treffer → alle Halte mit
Planzeiten und Lage; der Abschnitt bekommt source "gtfs". Findet die
Integration den Zug später in der Timetables-API, ersetzt deren Fassung (mit
Echtzeit) diese.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .places import name_keys
from .railnet.plan import Timetable, line_score

TZ = ZoneInfo("Europe/Berlin")
NEAR_KM = {"tram": 0.3, "subway": 0.3, "bus": 0.4}   # so weit darf die Station im Fahrplan von der bekannten Lage abweichen
NEAR_KM_RAIL = 0.6                        # Bahnhöfe sind groß, ihr Punkt liegt mal am Gebäude, mal am Gleis
MODES = {"longdistance", "regional", "suburban", "tram", "subway", "bus"}


def _dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone(TZ)
    except ValueError:
        return None


def _candidates(tt: Timetable, station: dict, mode: str) -> list[int]:
    out: list[int] = []
    if station.get("lat") is not None and station.get("lon") is not None:
        out += tt.near(station["lat"], station["lon"], NEAR_KM.get(mode, NEAR_KM_RAIL))
    out += tt.named(name_keys(station.get("name") or ""))
    return list(dict.fromkeys(out))


def _iso(day, minutes: int | None) -> str | None:
    if minutes is None:
        return None
    return (datetime.combine(day, time(0), TZ) + timedelta(minutes=minutes)).isoformat()


def fill_leg(tt: Timetable, leg: dict) -> bool:
    line = leg.get("line") or {}
    mode = line.get("product")
    sos = leg.get("stopovers") or []
    if leg.get("source") != "text" or mode not in MODES or len(sos) < 2:
        return False
    first, last = sos[0], sos[-1]
    dep, arr = _dt(first.get("plannedDeparture")), _dt(last.get("plannedArrival"))
    if dep is None:
        return False
    if not (tt.covers(dep.date()) or tt.covers(dep.date() - timedelta(days=1))):
        return False
    dep_c, arr_c = _candidates(tt, first["station"], mode), _candidates(tt, last["station"], mode)
    if not dep_c or (not arr_c and arr is None):
        return False   # ohne bekannten Ausstieg entscheidet sonst die Ankunftsminute (plan.find)
    dep_min = dep.hour * 60 + dep.minute
    arr_min = None
    if arr is not None:
        arr_min = (arr.date() - dep.date()).days * 1440 + arr.hour * 60 + arr.minute
    trip = tt.find(dep_c, arr_c, dep.date(), dep_min, line_score(mode, line.get("name") or "", line.get("productName")),
                   arr_minute=arr_min)
    if trip is None:
        return False

    def station(s) -> dict:
        return {"id": None, "name": s.name, "lat": s.lat, "lon": s.lon}

    middle = [{"station": station(s), "plannedArrival": _iso(trip.day, s.arrival),
               "plannedDeparture": _iso(trip.day, s.departure), "plannedPlatform": None}
              for s in trip.stops[1:-1]]
    for so, s in ((first, trip.stops[0]), (last, trip.stops[-1])):
        if so["station"].get("lat") is None:
            so["station"]["lat"], so["station"]["lon"] = s.lat, s.lon
    leg["stopovers"] = [first, *middle, last]
    leg["source"] = "gtfs"
    leg["plan"] = {"line": trip.line, "mode": trip.mode, "day": trip.day.isoformat()}
    return True


def fill_from_timetable(snapshot: dict, tt: Timetable | None) -> bool:
    """Alle passenden Abschnitte ergänzen. True, wenn sich etwas geändert hat."""
    if tt is None:
        return False
    changed = False
    for leg in snapshot.get("legs", []):
        changed |= fill_leg(tt, leg)
    return changed
