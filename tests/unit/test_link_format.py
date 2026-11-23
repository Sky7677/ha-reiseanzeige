"""Nachrichtenformat v1 (docs/format-v1.md) gegen die Beispieldaten, die gleichlautend
im Repo android-reiseanzeige liegen."""

import json
from pathlib import Path

import pytest

from custom_components.reiseanzeige.core.link_format import (
    FormatError, deep_link, intent_link, journey_from_dict, journey_to_dict, parse_pairing, parse_request, response,
)
from custom_components.reiseanzeige.core.share_text import parse_share_text
from testdata import fixture_text

SAMPLES = Path(__file__).parents[2] / "docs" / "format-v1"
REQUESTS = ["hello.json", "upsert-augsburg-berlin.json", "upsert-zubringer.json", "location.json",
            "route-gps.json", "route-gps-ende.json", "delete-stop-bye.json"]


def sample(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", REQUESTS)
def test_beispieldaten_sind_gueltig(name):
    req = parse_request(sample(name))
    assert req.messages and req.ignored == 0 and req.app_version == "0.8.0"


def test_verbindung_wie_aus_dem_geteilten_text():
    """Die App schickt die Verbindung im Format des ausgelesenen Textes – beide
    Fassungen lesen den Testtext gleich (die App prüft dieselbe Datei)."""
    msg = sample("upsert-augsburg-berlin.json")["messages"][0]
    parsed = parse_share_text(fixture_text("share", "augsburg-berlin-2026-10-08.txt"))
    assert msg["connection"] == journey_to_dict(parsed)
    assert journey_from_dict(msg["connection"]) == parsed


def test_zubringer_und_sitzplatz():
    msg = parse_request(sample("upsert-zubringer.json")).messages[0]
    assert [l.line for l in msg["journey"].legs] == ["RE16", "ICE 1604", "ICE 1006"]
    assert msg["journey"].legs[1].arr.name == "Erfurt Hbf"
    assert msg["seats"] == {"1": {"coach": "7", "seat": "45"}}
    assert msg["revision"] == 4


def test_standort_und_umleitung():
    loc = parse_request(sample("location.json")).messages[0]
    assert [f["accuracy"] for f in loc["fixes"]] == [14.0, 11.0] and loc["train"]["leg"] == 1
    assert loc["train"]["speed"] == 24.9 and loc["train"]["speedApprox"] is False
    assert loc["train"]["speedTime"].isoformat() == "2026-10-08T07:40:31+02:00"
    # Ältere App ohne Geschwindigkeit im Zugpunkt, oder ohne bekannte Geschwindigkeit
    raw = sample("location.json")
    for k in ("speed", "speedTime", "speedApprox"):
        raw["messages"][0]["train"][k] = None
    old = parse_request(raw).messages[0]["train"]
    assert old["speed"] is None and old["speedTime"] is None and old["speedApprox"] is False
    route = parse_request(sample("route-gps.json")).messages[0]
    assert route["gps"]["after"]["name"] == "Erfurt Hbf" and len(route["gps"]["points"]) == 2
    assert parse_request(sample("route-gps-ende.json")).messages[0]["gps"] is None


def test_neuere_hauptversion_wird_abgelehnt():
    with pytest.raises(FormatError) as err:
        parse_request({"format": 2, "messages": []})
    assert err.value.code == "format_unsupported"


def test_unbekannte_nachricht_wird_uebergangen():
    req = parse_request({"format": 1, "messages": [{"type": "trip.zukunft", "x": 1}, {"type": "link.bye"}]})
    assert req.ignored == 1 and [m["type"] for m in req.messages] == ["link.bye"]


@pytest.mark.parametrize("message", [
    {"type": "trip.delete", "tripId": "a b"},
    {"type": "trip.delete", "tripId": "x" * 65},
    {"type": "trip.upsert", "tripId": "t", "revision": -1, "connection": {}},
    {"type": "trip.upsert", "tripId": "t", "revision": 1, "connection": {"legs": []}},
    {"type": "location", "tripId": "t", "fixes": [{"lat": 91, "lon": 0, "accuracy": 5, "time": "2026-10-08T07:40:01+02:00"}]},
    {"type": "location", "tripId": "t", "fixes": [{"lat": 49, "lon": 11, "accuracy": 5, "time": "2026-10-08T07:40:01"}]},
])
def test_ungueltiges_wird_abgelehnt(message):
    with pytest.raises(FormatError) as err:
        parse_request({"format": 1, "messages": [message]})
    assert err.value.code == "bad_request"


def test_sitzplatz_nur_fuer_vorhandene_abschnitte():
    msg = sample("upsert-augsburg-berlin.json")["messages"][0] | {"seats": {"5": {"coach": "1"}}}
    with pytest.raises(FormatError):
        parse_request({"format": 1, "messages": [msg]})


def test_kopplung_und_links():
    pair = parse_pairing((SAMPLES / "kopplung.txt").read_text(encoding="utf-8"))
    assert pair == ("k4p7x2m9q1w8z3t6", "3c8f1a9e5b2d7f4a6c0e8b3d1f9a7c5e2b4d6f8a0c3e5b7d9f1a2c4e6b8d0f3a")
    assert parse_pairing("Augsburg Hbf → Berlin") is None
    link = deep_link("http://192.168.178.10:8123/", name="Zuhause", user="Marlon Müller", pair=pair[0])
    assert link == ("reiseanzeige://ziel/verbinden?v=1&url=http%3A%2F%2F192.168.178.10%3A8123&pair=k4p7x2m9q1w8z3t6"
                    "&name=Zuhause&user=Marlon%20M%C3%BCller")
    assert intent_link(link).startswith("intent://ziel/verbinden?v=1&")
    assert intent_link(link).endswith("#Intent;scheme=reiseanzeige;package=de.sky7677.reiseanzeige;end")
    assert response({"x": "ok"}) == sample("response-ok.json") | {"trips": {"x": "ok"}}
