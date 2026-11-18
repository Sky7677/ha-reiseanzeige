"""Reise aus dem geteilten Text zusammensetzen.

draft_snapshot(): sofort aus dem Text – Ein- und Ausstieg je Abschnitt, ohne
Bahnhofsnummern. Damit kann die Reise gespeichert und bestätigt werden, bevor
irgendeine Anfrage läuft. Lage der Halte, Zwischenhalte aus dem Fahrplan (gtfs.de)
und Verlauf ergänzt geometry.py, die Züge der Bahn-Abschnitte sucht boardmatch.py
in OpenBoards.

Das Ergebnis hat das Format, das die Karte erwartet (siehe frontend
view-model.js): legs[].stopovers[] mit station {id, name, lat, lon} und
geplanten Zeiten als ISO-Text.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol

from .share_text import SharedJourney, SharedLeg

SCHEMA_VERSION = 2


class StationDirectory(Protocol):
    """Liefert Koordinaten zu Bahnhöfen (EVA-Nummer, sonst Name)."""

    def coords(self, eva: str | None, name: str) -> tuple[float, float] | None: ...


class NoCoordinates:
    def coords(self, eva, name):
        return None


# --- Namen vergleichen -----------------------------------------------------------

_SUFFIX_RE = re.compile(r"\s*\((?:s|u|s\+u|u\+s|bus|tram|str)\)\s*", re.I)


def norm_name(name: str | None) -> str:
    """Für Vergleiche: Kleinschreibung, ß→ss, Zusätze wie "(S)" weg, Satzzeichen weg."""
    s = (name or "").lower().replace("ß", "ss")
    s = _SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


def norm_line(line: str | None) -> str:
    return re.sub(r"\s+", "", (line or "").upper())


def name_variants(name: str) -> list[str]:
    """Suchbegriffe für die Bahnhofssuche, vom genauesten zum gröbsten."""
    out = [name.strip()]
    plain = _SUFFIX_RE.sub(" ", name).strip()
    plain = re.sub(r"\s+,", ",", " ".join(plain.split()))
    if plain not in out:
        out.append(plain)
    # "Heidelberger Platz, Berlin" → "Berlin Heidelberger Platz"
    m = re.match(r"^(.+?),\s*([^,(]+?)(?:\s*\([^)]*\))?$", plain)
    if m:
        alt = f"{m.group(2).strip()} {m.group(1).strip()}"
        if alt not in out:
            out.append(alt)
    return out


# --- Hilfen ------------------------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _station(eva: str | None, name: str, directory: StationDirectory) -> dict:
    c = directory.coords(eva, name)
    return {"id": eva, "name": name, "lat": c[0] if c else None, "lon": c[1] if c else None}


def _line_info(leg: SharedLeg) -> dict:
    return {
        "name": leg.line,
        "trainNumber": leg.number,
        "productName": leg.category,
        "product": leg.mode,
        "operator": None,
    }


def _text_leg(index: int, leg: SharedLeg, directory: StationDirectory) -> dict:
    """Abschnitt nur aus dem Text: Ein- und Ausstieg."""
    origin = _station(None, leg.dep.name, directory)
    destination = _station(None, leg.arr.name, directory)
    return {
        "kind": "ride",
        "index": index,
        "tripId": None,
        "line": _line_info(leg),
        "replacementService": leg.category in ("SEV", "EV"),
        "direction": leg.direction,
        "origin": origin,
        "destination": destination,
        "plannedDeparture": _iso(leg.dep.when),
        "plannedArrival": _iso(leg.arr.when),
        "plannedDeparturePlatform": leg.dep.platform,
        "plannedArrivalPlatform": leg.arr.platform,
        "stopovers": [
            {"station": origin, "plannedArrival": None, "plannedDeparture": _iso(leg.dep.when), "plannedPlatform": leg.dep.platform},
            {"station": destination, "plannedArrival": _iso(leg.arr.when), "plannedDeparture": None, "plannedPlatform": leg.arr.platform},
        ],
        "polyline": None,
        "remarks": [{"code": None, "type": "status", "text": t} for t in leg.extra],
        "matched": False,
        "source": "text",
    }


def _envelope(journey: SharedJourney, legs: list[dict], now: datetime) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": now.isoformat(),
        "source": "share-text",
        "vbid": journey.vbid,
        "origin": legs[0]["origin"] | {"name": journey.origin},
        "destination": legs[-1]["destination"] | {"name": journey.destination},
        "plannedDeparture": legs[0]["plannedDeparture"],
        "plannedArrival": legs[-1]["plannedArrival"],
        "legs": legs,
    }


def draft_snapshot(journey: SharedJourney, now: datetime, directory: StationDirectory | None = None) -> dict:
    directory = directory or NoCoordinates()
    return _envelope(journey, [_text_leg(i, l, directory) for i, l in enumerate(journey.legs)], now)
