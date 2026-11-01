"""Geteilten Text aus dem DB Navigator auslesen.

Teilt man im DB Navigator eine Verbindung an Home Assistant, kommt ein Text
wie dieser an (Stand November 2026, Android, Sprache Deutsch)::

    Augsburg Hbf → Berlin Südkreuz
    Do. 08.10.2026

    RE16 (59116)
    Nach Nürnberg Hbf
    Ab 05:57 Augsburg Hbf, Gleis 5
    An 07:30 Nürnberg Hbf, Gleis 9

    ICE 1604
    ...

    Verbindung ansehen: https://www.bahn.de/buchung/start?vbid=…

Er enthält alles, was für die Reise nötig ist: Datum, je Abschnitt Linie,
Zugnummer (falls vorhanden), Richtung, Ab/An mit Uhrzeit, Halt und Gleis.
Der Link wird nicht aufgelöst (die Schnittstelle dahinter ist für
automatisierte Zugriffe gesperrt); er dient nur zum Öffnen in der App.

Beobachtete Besonderheiten:
  * Haltestellen außerhalb der Bahn tragen die Stadt im Namen
    ("Königsplatz, Augsburg (Bayern)"), daher das Gleis über das letzte
    ", Gleis " abtrennen.
  * Gleise mit Zusatz: "5 C-F" (Abschnitte), "2 (U3)".
  * Tram/U-Bahn haben oft kein Gleis.
  * Fußwege stehen nicht im Text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")

VBID_RE = re.compile(r"vbid=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
LINK_RE = re.compile(r"https?://\S+")
DATE_RE = re.compile(r"^(?:[A-Za-zÄÖÜäöü]{1,4}\.?,?\s+)?(\d{1,2})\.(\d{1,2})\.(\d{4})$")
STOP_RE = re.compile(r"^(Ab|An)\s+(\d{1,2}):(\d{2})\s+(.+)$")
NUMBER_RE = re.compile(r"^(.*?)\s*\((\d+)\)$")
HEADER_SEP = "→"

# Gattungen → Art des Verkehrsmittels
LONG_DISTANCE = {"ICE", "IC", "EC", "ECE", "EN", "NJ", "RJ", "RJX", "TGV", "FLX", "D", "EST", "WB", "ES", "IR"}
REGIONAL = {"RE", "RB", "IRE", "MEX", "ALX", "ERB", "ERX", "AG", "BRB", "HLB", "NWB", "VIA", "FEX", "RS", "SWE", "TLX", "ME", "WFB", "OPB", "VBG", "EB", "TER", "R", "REX", "CJX", "RX"}
SUBURBAN = {"S"}
SUBWAY = {"U"}
TRAM = {"STR", "TRAM", "STB"}
BUS = {"BUS", "SEV", "EV", "RUF", "AST", "X", "N", "NE", "SB", "ALT"}
SHIP = {"SCH", "SCHIFF", "FÄHRE", "F"}

# Arten, für die die Timetables-API Daten hat (Halte an Bahnhöfen der DB)
RAIL_MODES = {"longdistance", "regional", "suburban"}


class ShareTextError(ValueError):
    """Text lässt sich nicht als Verbindung lesen. code: NO_CONNECTION | NO_DATE | NO_LEGS | BAD_LEG"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class SharedStop:
    name: str
    when: datetime                 # Ortszeit Berlin, mit Zeitzone
    platform: str | None = None    # "5"
    platform_note: str | None = None  # "C-F", "(U3)"


@dataclass
class SharedLeg:
    line: str                      # "RE1", "ICE 1004", "S41", "STR 1"
    number: str | None             # Zugnummer: "4024", "1004"; None ohne Nummer
    category: str                  # "RE", "ICE", "S", "STR", …
    mode: str                      # longdistance | regional | suburban | subway | tram | bus | ship | other
    direction: str | None
    dep: SharedStop
    arr: SharedStop
    extra: list[str] = field(default_factory=list)  # unbekannte Zeilen (Hinweise), unverändert

    @property
    def rail(self) -> bool:
        """Liegt der Abschnitt im Bereich der Timetables-API?"""
        return self.mode in RAIL_MODES


@dataclass
class SharedJourney:
    origin: str
    destination: str
    day: date
    legs: list[SharedLeg]
    vbid: str | None = None
    link: str | None = None

    @property
    def departure(self) -> datetime:
        return self.legs[0].dep.when

    @property
    def arrival(self) -> datetime:
        return self.legs[-1].arr.when


def looks_like_share(text: str | None) -> bool:
    """Schnelle Vorprüfung: Teilen-Text aus dem DB Navigator (oder ein bahn.de-Link)?"""
    if not text:
        return False
    return "bahn.de" in text or (HEADER_SEP in text and bool(re.search(r"^\s*Ab\s+\d{1,2}:\d{2}\s", text, re.M)))


def classify(line: str) -> tuple[str, str]:
    """Gattung und Art aus dem Liniennamen: "RE1" → ("RE", "regional")."""
    m = re.match(r"^([A-Za-zÄÖÜäöü]+)", line.strip())
    cat = m.group(1).upper() if m else ""
    if cat in LONG_DISTANCE:
        mode = "longdistance"
    elif cat in SUBURBAN:
        mode = "suburban"
    elif cat in SUBWAY:
        mode = "subway"
    elif cat in TRAM:
        mode = "tram"
    elif cat in BUS:
        mode = "bus"
    elif cat in SHIP:
        mode = "ship"
    elif cat in REGIONAL:
        mode = "regional"
    else:
        mode = "other"
    return cat, mode


def parse_line(raw: str) -> tuple[str, str | None, str, str]:
    """Kopfzeile eines Abschnitts → (Linie, Zugnummer, Gattung, Art).

    "RE1 (4024)" → ("RE1", "4024", "RE", "regional")
    "ICE 1004"   → ("ICE 1004", "1004", "ICE", "longdistance")
    "S41"        → ("S41", None, "S", "suburban")
    """
    raw = " ".join(raw.split())
    number = None
    m = NUMBER_RE.match(raw)
    if m:
        line, number = m.group(1).strip(), m.group(2)
    else:
        line = raw
    cat, mode = classify(line)
    if number is None and mode == "longdistance":
        # Fernverkehr: Name ist Gattung + Zugnummer
        n = re.match(r"^[A-Za-z]+\s*(\d+)$", line)
        if n:
            number = n.group(1)
    return line, number, cat, mode


def split_platform(rest: str) -> tuple[str, str | None, str | None]:
    """"Nürnberg Hbf, Gleis 5 C-F" → ("Nürnberg Hbf", "5", "C-F")."""
    idx = rest.rfind(", Gleis ")
    if idx == -1:
        return rest.strip(), None, None
    name = rest[:idx].strip()
    plat = rest[idx + len(", Gleis "):].strip()
    m = re.match(r"^(\S+)(?:\s+(.+))?$", plat)
    if not m:
        return name, None, None
    return name, m.group(1), m.group(2)


def _blocks(lines: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    for line in lines:
        if line.strip():
            cur.append(line.strip())
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def parse_share_text(text: str, tz: ZoneInfo = TZ) -> SharedJourney:
    """Teilen-Text → SharedJourney. Wirft ShareTextError, wenn er keine Verbindung enthält."""
    if not text or not text.strip():
        raise ShareTextError("NO_CONNECTION", "Leerer Text")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    vbid_m = VBID_RE.search(text)
    link_m = LINK_RE.search(text)

    header = next((l.strip() for l in lines if HEADER_SEP in l), None)
    day = None
    for l in lines:
        m = DATE_RE.match(l.strip())
        if m:
            day = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            break

    legs_raw: list[dict] = []
    for block in _blocks(lines):
        stop_lines = [(i, STOP_RE.match(l)) for i, l in enumerate(block)]
        stop_lines = [(i, m) for i, m in stop_lines if m]
        ab = next(((i, m) for i, m in stop_lines if m.group(1) == "Ab"), None)
        an = next(((i, m) for i, m in stop_lines if m.group(1) == "An"), None)
        if not ab or not an:
            continue
        head = [l for i, l in enumerate(block) if i < ab[0]]
        line_raw = next((l for l in head if not l.startswith("Nach ")), None)
        direction = next((l[5:].strip() for l in head if l.startswith("Nach ")), None)
        if not line_raw:
            raise ShareTextError("BAD_LEG", f"Abschnitt ohne Linie: {block[0]}")
        extra = [l for i, l in enumerate(block) if i > an[0] or (i not in (ab[0], an[0]) and l not in head)]
        legs_raw.append({"line": line_raw, "direction": direction, "ab": ab[1], "an": an[1], "extra": extra})

    if not legs_raw:
        if vbid_m and not header:
            raise ShareTextError("NO_CONNECTION", "Nur ein Link ohne Verbindungsdaten")
        raise ShareTextError("NO_LEGS", "Keine Abschnitte mit Ab/An gefunden")
    if day is None:
        raise ShareTextError("NO_DATE", "Kein Reisedatum im Text")

    # Uhrzeiten in Reihenfolge; fällt eine Zeit hinter die vorige, ist Mitternacht vorbei
    current_day = day
    last_minutes = -1

    def stamp(m: re.Match) -> datetime:
        nonlocal current_day, last_minutes
        hh, mm = int(m.group(2)), int(m.group(3))
        minutes = hh * 60 + mm
        if last_minutes >= 0 and minutes < last_minutes:
            current_day = current_day + timedelta(days=1)
        last_minutes = minutes
        return datetime.combine(current_day, time(hh, mm), tzinfo=tz)

    legs: list[SharedLeg] = []
    for r in legs_raw:
        line, number, cat, mode = parse_line(r["line"])
        dep_name, dep_pl, dep_note = split_platform(r["ab"].group(4))
        dep_when = stamp(r["ab"])
        arr_name, arr_pl, arr_note = split_platform(r["an"].group(4))
        arr_when = stamp(r["an"])
        legs.append(SharedLeg(
            line=line, number=number, category=cat, mode=mode, direction=r["direction"],
            dep=SharedStop(dep_name, dep_when, dep_pl, dep_note),
            arr=SharedStop(arr_name, arr_when, arr_pl, arr_note),
            extra=r["extra"],
        ))

    if header:
        origin, _, destination = header.partition(HEADER_SEP)
        origin, destination = origin.strip(), destination.strip()
    else:
        origin, destination = legs[0].dep.name, legs[-1].arr.name

    return SharedJourney(
        origin=origin or legs[0].dep.name,
        destination=destination or legs[-1].arr.name,
        day=day,
        legs=legs,
        vbid=vbid_m.group(1).lower() if vbid_m else None,
        link=link_m.group(0) if link_m else None,
    )
