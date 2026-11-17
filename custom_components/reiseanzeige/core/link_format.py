"""Nachrichtenformat v1 zwischen der Android-Reiseanzeige und Zielen wie dieser
Integration (Beschreibung: docs/format-v1.md, gleichlautend im Repo
android-reiseanzeige). Reine Logik ohne Home Assistant: Anfragen prüfen,
Verbindungen umwandeln, Kopplungstexte und Links.

Geprüft wird streng, aber vorwärtsverträglich: unbekannte Felder und unbekannte
Nachrichtenarten derselben Hauptversion werden übergangen, eine neuere
Hauptversion abgelehnt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlencode

from .share_text import SharedJourney, SharedLeg, SharedStop, classify

FORMAT_VERSION = 1
MAX_BODY_BYTES = 256 * 1024
MAX_MESSAGES = 50
MAX_LEGS = 30
MAX_FIXES = 30
MAX_GPS_POINTS = 400
MAX_PREFIX_POINTS = 5000
MAX_SEAT_LENGTH = 12

MODES = {"longdistance", "regional", "suburban", "subway", "tram", "bus", "ship", "other"}
TYPES = {"link.hello", "link.bye", "trip.upsert", "trip.delete", "location", "route.gps", "sharing.stop"}

APP_PACKAGE = "de.sky7677.reiseanzeige"
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
PAIR_ID_RE = re.compile(r"^[a-z0-9]{16}$")
TRIP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PAIRING_RE = re.compile(r"reiseanzeige-kopplung v1 ([a-z0-9]{16}) ([0-9a-f]{64})(?![0-9a-f])")

# Zustände je Reise in der Antwort
OK, DELETED, UNKNOWN, IGNORED = "ok", "deleted", "unknown", "ignored"


class FormatError(ValueError):
    """code: format_unsupported | bad_request | too_large"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _bad(message: str) -> FormatError:
    return FormatError("bad_request", message)


# --- Grundtypen ----------------------------------------------------------------------

def _str(value: Any, field: str, *, max_len: int = 200, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() and not optional:
        raise _bad(f"{field}: Text erwartet")
    value = value.strip()
    if len(value) > max_len:
        raise _bad(f"{field}: höchstens {max_len} Zeichen")
    return value or None


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise _bad(f"{field}: Zeit erwartet")
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        raise _bad(f"{field}: keine gültige Zeit") from None
    if t.tzinfo is None:
        raise _bad(f"{field}: Zeit ohne Zeitzone")
    return t


def _num(value: Any, field: str, lo: float | None = None, hi: float | None = None, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"{field}: Zahl erwartet")
    v = float(value)
    if v != v or (lo is not None and v < lo) or (hi is not None and v > hi):
        raise _bad(f"{field}: außerhalb des erlaubten Bereichs")
    return v


def _int(value: Any, field: str, lo: int = 0, hi: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lo or (hi is not None and value > hi):
        raise _bad(f"{field}: ganze Zahl erwartet")
    return value


def _trip_id(value: Any) -> str:
    if not isinstance(value, str) or not TRIP_ID_RE.match(value):
        raise _bad("tripId: 1 bis 64 Zeichen aus A–Z, a–z, 0–9, _ und -")
    return value


def _point(value: Any, field: str, *, with_time: bool = False) -> list:
    n = 3 if with_time else 2
    if not isinstance(value, list) or len(value) < 2 or len(value) > n:
        raise _bad(f"{field}: Punkt erwartet")
    lat = _num(value[0], field, -90, 90)
    lon = _num(value[1], field, -180, 180)
    if with_time:
        if len(value) != 3:
            raise _bad(f"{field}: Punkt mit Zeit erwartet")
        _time(value[2], field)
        return [lat, lon, value[2]]
    return [lat, lon]


# --- Verbindung ----------------------------------------------------------------------

def _iso(t: datetime) -> str:
    return t.isoformat()


def journey_to_dict(journey: SharedJourney) -> dict:
    """SharedJourney → Verbindung im Format v1 (für Tests und Beispieldaten)."""
    def stop(s: SharedStop) -> dict:
        return {"name": s.name, "time": _iso(s.when), "platform": s.platform, "platformNote": s.platform_note}
    return {
        "origin": journey.origin,
        "destination": journey.destination,
        "day": journey.day.isoformat(),
        "vbid": journey.vbid,
        "link": journey.link,
        "legs": [{"line": l.line, "number": l.number, "category": l.category, "mode": l.mode,
                  "direction": l.direction, "dep": stop(l.dep), "arr": stop(l.arr), "extra": list(l.extra)}
                 for l in journey.legs],
    }


def _stop(value: Any, field: str) -> SharedStop:
    if not isinstance(value, dict):
        raise _bad(f"{field}: Halt erwartet")
    return SharedStop(
        name=_str(value.get("name"), f"{field}.name"),
        when=_time(value.get("time"), f"{field}.time"),
        platform=_str(value.get("platform"), f"{field}.platform", max_len=20, optional=True),
        platform_note=_str(value.get("platformNote"), f"{field}.platformNote", max_len=40, optional=True),
    )


def journey_from_dict(value: Any) -> SharedJourney:
    """Verbindung im Format v1 → SharedJourney, wie aus dem geteilten Text."""
    if not isinstance(value, dict):
        raise _bad("connection: Objekt erwartet")
    legs_in = value.get("legs")
    if not isinstance(legs_in, list) or not legs_in or len(legs_in) > MAX_LEGS:
        raise _bad(f"connection.legs: 1 bis {MAX_LEGS} Abschnitte erwartet")
    legs = []
    for i, raw in enumerate(legs_in):
        f = f"connection.legs[{i}]"
        if not isinstance(raw, dict):
            raise _bad(f"{f}: Objekt erwartet")
        line = _str(raw.get("line"), f"{f}.line", max_len=40)
        cat, mode = classify(line)
        category = _str(raw.get("category"), f"{f}.category", max_len=20, optional=True) or cat
        mode = raw.get("mode") or mode
        if mode not in MODES:
            raise _bad(f"{f}.mode: unbekannt")
        extra = raw.get("extra") or []
        if not isinstance(extra, list) or len(extra) > 20 or not all(isinstance(e, str) and len(e) <= 300 for e in extra):
            raise _bad(f"{f}.extra: Liste von Texten erwartet")
        dep, arr = _stop(raw.get("dep"), f"{f}.dep"), _stop(raw.get("arr"), f"{f}.arr")
        if arr.when < dep.when:
            raise _bad(f"{f}: Ankunft vor Abfahrt")
        legs.append(SharedLeg(line=line, number=_str(raw.get("number"), f"{f}.number", max_len=20, optional=True),
                              category=category, mode=mode,
                              direction=_str(raw.get("direction"), f"{f}.direction", optional=True),
                              dep=dep, arr=arr, extra=list(extra)))
    try:
        day = date.fromisoformat(value.get("day") or "")
    except (TypeError, ValueError):
        day = legs[0].dep.when.date()
    return SharedJourney(
        origin=_str(value.get("origin"), "connection.origin", optional=True) or legs[0].dep.name,
        destination=_str(value.get("destination"), "connection.destination", optional=True) or legs[-1].arr.name,
        day=day,
        legs=legs,
        vbid=_str(value.get("vbid"), "connection.vbid", max_len=100, optional=True),
        link=_str(value.get("link"), "connection.link", max_len=500, optional=True),
    )


def _seats(value: Any, legs: int) -> dict[str, dict]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _bad("seats: Objekt erwartet")
    out = {}
    for k, v in value.items():
        if not (isinstance(k, str) and k.isdigit() and int(k) < legs) or not isinstance(v, dict):
            raise _bad("seats: je Abschnitt (Index als Text) Wagen und Sitzplatz")
        coach = _str(v.get("coach"), "seats.coach", max_len=MAX_SEAT_LENGTH, optional=True)
        seat = _str(v.get("seat"), "seats.seat", max_len=MAX_SEAT_LENGTH, optional=True)
        if coach or seat:
            out[str(int(k))] = {"coach": coach, "seat": seat}
    return out


# --- Nachrichten ---------------------------------------------------------------------

def _gps(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _bad("gps: Objekt erwartet")
    after = value.get("after")
    if not isinstance(after, dict):
        raise _bad("gps.after: Halt erwartet")
    prefix, points = value.get("prefix") or [], value.get("points") or []
    if not isinstance(prefix, list) or len(prefix) > MAX_PREFIX_POINTS:
        raise _bad("gps.prefix: Liste von Punkten erwartet")
    if not isinstance(points, list) or len(points) > MAX_GPS_POINTS:
        raise _bad(f"gps.points: höchstens {MAX_GPS_POINTS} Punkte")
    out = {
        "after": {"id": _str(after.get("id"), "gps.after.id", max_len=20, optional=True),
                  "name": _str(after.get("name"), "gps.after.name", optional=True)},
        "anchor": _point(value.get("anchor"), "gps.anchor"),
        "prefix": [_point(p, "gps.prefix") for p in prefix],
        "points": [_point(p, "gps.points", with_time=True) for p in points],
    }
    if value.get("anchorAt") is not None:
        _time(value["anchorAt"], "gps.anchorAt")
        out["anchorAt"] = value["anchorAt"]
    if not out["after"]["id"] and not out["after"]["name"]:
        raise _bad("gps.after: Nummer oder Name erwartet")
    return out


def _fix(value: Any, i: int) -> dict:
    f = f"fixes[{i}]"
    if not isinstance(value, dict):
        raise _bad(f"{f}: Objekt erwartet")
    return {
        "lat": _num(value.get("lat"), f"{f}.lat", -90, 90),
        "lon": _num(value.get("lon"), f"{f}.lon", -180, 180),
        "accuracy": _num(value.get("accuracy"), f"{f}.accuracy", 0, 100000),
        "time": _time(value.get("time"), f"{f}.time"),
        "speed": _num(value.get("speed"), f"{f}.speed", 0, 200, optional=True),
        "bearing": _num(value.get("bearing"), f"{f}.bearing", 0, 360, optional=True),
    }


def _message(raw: Any) -> dict | None:
    """Eine Nachricht prüfen. None = unbekannte Art (übergehen)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
        raise _bad("Nachricht ohne type")
    kind = raw["type"]
    if kind not in TYPES:
        return None
    msg: dict[str, Any] = {"type": kind}
    if kind == "link.hello":
        msg["device"] = _str(raw.get("device"), "device", max_len=80, optional=True)
        msg["appInstance"] = _str(raw.get("appInstance"), "appInstance", max_len=64, optional=True)
    elif kind == "link.bye":
        pass
    elif kind == "trip.upsert":
        msg["tripId"] = _trip_id(raw.get("tripId"))
        msg["revision"] = _int(raw.get("revision"), "revision")
        msg["journey"] = journey_from_dict(raw.get("connection"))
        msg["connection"] = journey_to_dict(msg["journey"])
        msg["seats"] = _seats(raw.get("seats"), len(msg["journey"].legs))
    elif kind in ("trip.delete", "sharing.stop"):
        msg["tripId"] = _trip_id(raw.get("tripId"))
    elif kind == "location":
        msg["tripId"] = _trip_id(raw.get("tripId"))
        fixes = raw.get("fixes") or []
        if not isinstance(fixes, list) or len(fixes) > MAX_FIXES:
            raise _bad(f"fixes: höchstens {MAX_FIXES} Messungen")
        msg["fixes"] = sorted((_fix(f, i) for i, f in enumerate(fixes)), key=lambda f: f["time"])
        train = raw.get("train")
        if train is not None:
            if not isinstance(train, dict):
                raise _bad("train: Objekt erwartet")
            msg["train"] = {"leg": _int(train.get("leg"), "train.leg", 0, MAX_LEGS),
                            "lat": _num(train.get("lat"), "train.lat", -90, 90),
                            "lon": _num(train.get("lon"), "train.lon", -180, 180),
                            "time": _time(train.get("time"), "train.time"),
                            "gps": bool(train.get("gps"))}
    elif kind == "route.gps":
        msg["tripId"] = _trip_id(raw.get("tripId"))
        msg["leg"] = _int(raw.get("leg"), "leg", 0, MAX_LEGS)
        key = raw.get("legKey") or {}
        if not isinstance(key, dict):
            raise _bad("legKey: Objekt erwartet")
        msg["legKey"] = {"line": _str(key.get("line"), "legKey.line", max_len=40, optional=True),
                         "dep": _time(key["dep"], "legKey.dep") if key.get("dep") else None}
        msg["gps"] = _gps(raw.get("gps"))
    return msg


@dataclass
class Request:
    sent_at: datetime | None
    app_version: str | None
    messages: list[dict]
    ignored: int = 0


def parse_request(body: Any) -> Request:
    """Anfrage prüfen. FormatError bei ungültigem Inhalt oder zu neuer Hauptversion."""
    if not isinstance(body, dict):
        raise _bad("JSON-Objekt erwartet")
    version = body.get("format")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _bad("format: Versionsnummer erwartet")
    if version > FORMAT_VERSION:
        raise FormatError("format_unsupported", f"Format {version} ist neuer als {FORMAT_VERSION} – bitte die Integration aktualisieren")
    raw = body.get("messages")
    if not isinstance(raw, list) or len(raw) > MAX_MESSAGES:
        raise _bad(f"messages: Liste mit höchstens {MAX_MESSAGES} Nachrichten erwartet")
    messages, ignored = [], 0
    for m in raw:
        parsed = _message(m)
        if parsed is None:
            ignored += 1
        else:
            messages.append(parsed)
    app = body.get("app") if isinstance(body.get("app"), dict) else {}
    sent = None
    if body.get("sentAt") is not None:
        sent = _time(body["sentAt"], "sentAt")
    return Request(sent, _str(app.get("version"), "app.version", max_len=40, optional=True), messages, ignored)


def response(trips: dict[str, str] | None = None, **extra) -> dict:
    return {"format": FORMAT_VERSION, "ok": True, "trips": trips or {}, **extra}


def error_response(code: str, message: str) -> dict:
    return {"format": FORMAT_VERSION, "ok": False, "error": {"code": code, "message": message}}


# --- Verbinden -----------------------------------------------------------------------

def parse_pairing(text: str | None) -> tuple[str, str] | None:
    """Geteilter Kopplungstext der App → (Kopplungskennung, Einmal-Schlüssel) oder None."""
    m = PAIRING_RE.search(text or "")
    return (m.group(1), m.group(2)) if m else None


def deep_link(base: str, *, name: str, user: str, token: str | None = None, pair: str | None = None) -> str:
    """reiseanzeige://ziel/verbinden?… – mit Einmal-Schlüssel (QR-Code, Knopf) oder
    Kopplungskennung (nach dem Teilen an die Companion-App)."""
    params = {"v": "1", "url": base.rstrip("/")}
    if token:
        params["token"] = token
    if pair:
        params["pair"] = pair
    params.update(name=name, user=user)
    return "reiseanzeige://ziel/verbinden?" + urlencode(params, quote_via=quote)


def intent_link(link: str) -> str:
    """Derselbe Link als Android-Intent (öffnet die App auch aus einer WebView heraus)."""
    rest = link.split("://", 1)[1]
    return f"intent://{rest}#Intent;scheme=reiseanzeige;package={APP_PACKAGE};end"
