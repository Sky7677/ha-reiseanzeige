"""Auslesen des geteilten Textes aus dem DB Navigator – mit echten Texten."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.share_text import (
    ShareTextError,
    looks_like_share,
    parse_line,
    parse_share_text,
    split_platform,
)
from testdata import fixture_text

TZ = ZoneInfo("Europe/Berlin")


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def test_augsburg_berlin_zwei_zuege():
    j = parse_share_text(fixture_text("share", "augsburg-berlin-2026-10-08.txt"))
    assert (j.origin, j.destination) == ("Augsburg Hbf", "Berlin Südkreuz")
    assert j.day.isoformat() == "2026-10-08"
    assert j.vbid == "00000000-0000-4000-8000-000000000001"
    assert len(j.legs) == 2
    re16, ice = j.legs
    assert (re16.line, re16.number, re16.category, re16.mode) == ("RE16", "59116", "RE", "regional")
    assert re16.direction == "Nürnberg Hbf"
    assert (re16.dep.name, re16.dep.when, re16.dep.platform) == ("Augsburg Hbf", at(2026, 10, 8, 5, 57), "5")
    assert (re16.arr.name, re16.arr.when, re16.arr.platform) == ("Nürnberg Hbf", at(2026, 10, 8, 7, 30), "9")
    assert (ice.line, ice.number, ice.mode) == ("ICE 1604", "1604", "longdistance")
    assert ice.arr.when == at(2026, 10, 8, 10, 17)
    assert j.departure == at(2026, 10, 8, 5, 57) and j.arrival == at(2026, 10, 8, 10, 17)


def test_tram_re_ice_sbahn_ubahn():
    j = parse_share_text(fixture_text("share", "moritzplatz-berlin-2026-11-01.txt"))
    assert j.origin == "Moritzplatz, Augsburg (Bayern)"
    assert j.destination == "Breitenbachplatz (U), Berlin"
    assert [(l.line, l.number, l.mode) for l in j.legs] == [
        ("STR 1", None, "tram"),
        ("RE1", "4024", "regional"),
        ("ICE 1004", "1004", "longdistance"),
        ("S41", None, "suburban"),
        ("U3", None, "subway"),
    ]
    tram, re1, ice, s41, u3 = j.legs
    assert tram.dep.name == "Moritzplatz, Augsburg (Bayern)" and tram.dep.platform is None
    assert tram.arr.name == "Königsplatz, Augsburg (Bayern)"
    assert tram.direction == "Lechhausen N. Ostfr., Augsburg (Bayern)"
    assert (re1.arr.name, re1.arr.platform, re1.arr.platform_note) == ("Nürnberg Hbf", "5", "C-F")
    assert (s41.dep.name, s41.dep.platform) == ("Berlin Südkreuz (S)", "11")
    assert s41.direction == "Ring S41"
    assert (u3.dep.name, u3.dep.platform, u3.dep.platform_note) == ("Heidelberger Platz (S+U), Berlin", "2", "(U3)")
    assert u3.arr.platform is None
    assert [l.rail for l in j.legs] == [False, True, True, True, False]
    assert j.arrival == at(2026, 11, 1, 20, 10)


def test_mitternacht_wird_erkannt():
    text = """A → B
Fr. 30.10.2026

ICE 999
Nach B
Ab 23:40 A, Gleis 1
An 00:35 C, Gleis 2

RE 5 (123)
Nach B
Ab 00:50 C, Gleis 3
An 01:20 B
"""
    j = parse_share_text(text)
    assert j.legs[0].dep.when == at(2026, 10, 30, 23, 40)
    assert j.legs[0].arr.when == at(2026, 10, 31, 0, 35)
    assert j.legs[1].arr.when == at(2026, 10, 31, 1, 20)


def test_windows_zeilenenden_und_unbekannte_zeilen():
    text = "A → B\r\nMo. 02.11.2026\r\n\r\nRE1 (4024)\r\nNach B\r\nAb 10:00 A, Gleis 1\r\nAn 11:00 B, Gleis 2\r\nHalt entfällt\r\n"
    j = parse_share_text(text)
    assert j.legs[0].arr.when == at(2026, 11, 2, 11, 0)
    assert j.legs[0].extra == ["Halt entfällt"]


@pytest.mark.parametrize(("raw", "expected"), [
    ("RE1 (4024)", ("RE1", "4024", "RE", "regional")),
    ("RE 16 (59116)", ("RE 16", "59116", "RE", "regional")),
    ("ICE 1004", ("ICE 1004", "1004", "ICE", "longdistance")),
    ("IC 2013", ("IC 2013", "2013", "IC", "longdistance")),
    ("S41", ("S41", None, "S", "suburban")),
    ("S 3 (6312)", ("S 3", "6312", "S", "suburban")),
    ("STR 1", ("STR 1", None, "STR", "tram")),
    ("U3", ("U3", None, "U", "subway")),
    ("Bus 52", ("Bus 52", None, "BUS", "bus")),
    ("FLX 1234", ("FLX 1234", "1234", "FLX", "longdistance")),
    ("ag 84205 (84205)", ("ag 84205", "84205", "AG", "regional")),
])
def test_linien(raw, expected):
    assert parse_line(raw) == expected


def test_gleise():
    assert split_platform("Nürnberg Hbf, Gleis 5 C-F") == ("Nürnberg Hbf", "5", "C-F")
    assert split_platform("Heidelberger Platz (S+U), Berlin, Gleis 2 (U3)") == ("Heidelberger Platz (S+U), Berlin", "2", "(U3)")
    assert split_platform("Königsplatz, Augsburg (Bayern)") == ("Königsplatz, Augsburg (Bayern)", None, None)
    assert split_platform("München Hbf, Gleis 27") == ("München Hbf", "27", None)


def test_fehlerfaelle():
    with pytest.raises(ShareTextError) as e:
        parse_share_text("https://www.bahn.de/buchung/start?vbid=00000000-0000-4000-8000-000000000001")
    assert e.value.code == "NO_CONNECTION"
    with pytest.raises(ShareTextError) as e:
        parse_share_text("Schau mal: https://youtube.com/watch?v=x")
    assert e.value.code == "NO_LEGS"
    with pytest.raises(ShareTextError) as e:
        parse_share_text("A → B\n\nRE1 (1)\nAb 10:00 A\nAn 11:00 B")
    assert e.value.code == "NO_DATE"
    with pytest.raises(ShareTextError):
        parse_share_text("")


def test_vorpruefung():
    assert looks_like_share(fixture_text("share", "augsburg-berlin-2026-10-08.txt"))
    assert looks_like_share("Nur Link: https://www.bahn.de/buchung/start?vbid=x")
    assert not looks_like_share("https://youtube.com/watch?v=x")
    assert not looks_like_share(None)


def test_rueckfahrt_mit_rb_und_re_nummern():
    j = parse_share_text(fixture_text("share", "berlin-moritzplatz-2026-11-04.txt"))
    assert j.day.isoformat() == "2026-11-04"
    assert [(l.line, l.number, l.mode) for l in j.legs] == [
        ("U3", None, "subway"), ("S42", None, "suburban"), ("ICE 1103", "1103", "longdistance"),
        ("RB16", "59107", "regional"), ("RE80", "57185", "regional"), ("STR 1", None, "tram")]
    assert (j.legs[3].dep.platform, j.legs[3].dep.platform_note) == ("5", "C-F")
    assert j.legs[1].dep.name == "Berlin Heidelberger Platz"
    assert j.arrival == at(2026, 11, 4, 21, 51)


def test_mit_bus():
    j = parse_share_text(fixture_text("share", "berlin-moritzplatz-bus-2026-11-01.txt"))
    bus = j.legs[4]
    assert (bus.line, bus.mode, bus.dep.name, bus.arr.name) == ("Bus 32", "bus", "Hauptbahnhof, Augsburg (Bayern)", "Königsplatz, Augsburg (Bayern)")
    assert [l.number for l in j.legs] == [None, None, "1103", "4039", None, None]
