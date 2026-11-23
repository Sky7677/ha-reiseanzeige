"""Kopplung mit der Android-Reiseanzeige (links.py, Format v1 in docs/format-v1.md):
Verbinden per Link/QR-Code und per Teilen an die Companion-App, Reisen, Standort und
Umleitung aus der App, Löschen und Aufheben auf beiden Seiten."""

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from homeassistant.core import Context
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.reiseanzeige.const import DOMAIN
from custom_components.reiseanzeige.core.link_format import journey_to_dict
from custom_components.reiseanzeige.core.share_text import parse_share_text
from custom_components.reiseanzeige.runtime import LINKED_HINT
from railnet_fixture import P
from testdata import fixture_text

SAMPLES = Path(__file__).parents[2] / "docs" / "format-v1"
TRIP = "5f0c2a9e1b7d4c3a"
TRACKER = "device_tracker.reiseanzeige_mock_user_nokia_8_3_5g"


def sample(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


async def setup(hass, user, **options):
    """Integration mit bekanntem Handy der Person (Companion-App)."""
    app_entry = MockConfigEntry(domain="mobile_app", data={"device_name": "Testhandy", "device_id": "abc123"})
    app_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=app_entry.entry_id,
                                                     identifiers={("mobile_app", "abc123")}, name="Testhandy")
    er.async_get(hass).async_get_or_create("device_tracker", "mobile_app", "abc123", config_entry=app_entry,
                                           device_id=device.id, suggested_object_id="testhandy")
    calls = async_mock_service(hass, "notify", "mobile_app_testhandy")
    hass.config.internal_url = "http://192.168.178.10:8123"
    hass.config.location_name = "Zuhause"
    entry = MockConfigEntry(domain=DOMAIN, data={"client_id": "cid", "api_key": "key"},
                            options={"station": "Augsburg Hbf", **options})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data, calls


async def share(hass, user, text):
    hass.bus.async_fire("mobile_app.share", {"text": text, "device_id": "abc123"}, context=Context(user_id=user.id))
    await hass.async_block_till_done(wait_background_tasks=True)


async def post(client, webhook_id, body):
    res = await client.post(f"/api/webhook/{webhook_id}", data=json.dumps(body), headers={"Content-Type": "application/json"})
    text = await res.text()
    return res.status, (json.loads(text) if text else None)


async def connect(hass, ws, client):
    """Karte → „Android-App verbinden“ → App meldet sich am Einmal-Schlüssel."""
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/link_start"})
    started = (await ws.receive_json())["result"]
    q = parse_qs(urlsplit(started["link"]).query)
    assert q["url"] == ["http://192.168.178.10:8123"] and q["name"] == ["Zuhause"] and q["user"] == ["Mock User"]
    assert started["intent"].endswith("#Intent;scheme=reiseanzeige;package=de.sky7677.reiseanzeige;end")
    token = q["token"][0]
    status, body = await post(client, token, sample("hello.json"))
    assert status == 200 and body["ok"]
    return token, body["link"]


async def test_verbinden_reisen_standort_loeschen(hass, boards, hass_admin_user, hass_ws_client, hass_client_no_auth, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to("2026-10-07T21:00:00+02:00")
    rt, calls = await setup(hass, hass_admin_user)
    client = await hass_client_no_auth()

    token, link = await connect(hass, ws, client)
    assert link["instance"] == "Zuhause" and link["user"] == "Mock User" and link["localOnly"] is True
    webhook_id = link["webhookId"]
    assert len(webhook_id) == 64 and webhook_id != token
    # Antwort verloren: dieselbe Adresse noch einmal
    assert (await post(client, token, sample("hello.json")))[1]["link"]["webhookId"] == webhook_id
    await hass.async_block_till_done()
    assert hass.states.get(TRACKER) is not None, "Tracker der verbundenen App"

    # Reise aus der App, mit Sitzplatz
    status, body = await post(client, webhook_id, sample("upsert-augsburg-berlin.json"))
    assert status == 200 and body == {"format": 1, "ok": True, "trips": {TRIP: "ok"}}
    await hass.async_block_till_done(wait_background_tasks=True)
    [trip] = rt.trips.list()
    assert trip["ownerId"] == hass_admin_user.id and trip["app"]["tripId"] == TRIP
    assert trip["seats"] == {"1": {"coach": "7", "seat": "45"}}
    assert [l["line"]["name"] for l in trip["snapshot"]["legs"]] == ["RE16", "ICE 1604"]

    await ws.send_json({"id": 11, "type": f"{DOMAIN}/trips"})
    [listed] = (await ws.receive_json())["result"]["trips"]
    assert listed["fromApp"] is True and listed["ownerCanLocate"] is False and listed["ownerLocation"] == TRACKER
    assert "connection" not in listed

    await ws.send_json({"id": 12, "type": f"{DOMAIN}/seat", "trip_id": trip["id"], "leg": 0, "coach": "1"})
    err = await ws.receive_json()
    assert not err["success"] and err["error"]["code"] == "from_app"

    await ws.send_json({"id": 13, "type": f"{DOMAIN}/me"})
    me = (await ws.receive_json())["result"]
    assert me["linked"] is True and me["canAdd"] is False

    # Teilen über die Companion-App: keine zweite Reise, Hinweis auf die App
    await share(hass, hass_admin_user, fixture_text("share", "augsburg-berlin-2026-10-08.txt"))
    assert len(rt.trips.list()) == 1
    assert calls[-1].data["message"] == LINKED_HINT

    # Umplanen unterwegs (Zubringer), danach eine veraltete Revision
    assert (await post(client, webhook_id, sample("upsert-zubringer.json")))[1]["trips"] == {TRIP: "ok"}
    stale = copy.deepcopy(sample("upsert-augsburg-berlin.json"))
    assert (await post(client, webhook_id, stale))[1]["trips"] == {TRIP: "ok"}
    await hass.async_block_till_done(wait_background_tasks=True)
    [trip] = rt.trips.list()
    assert [l["line"]["name"] for l in trip["snapshot"]["legs"]] == ["RE16", "ICE 1604", "ICE 1006"]
    assert trip["app"]["revision"] == 4

    # Standort: Tracker mit Messzeit, ohne Benachrichtigungen an die verbundene Person
    n = len(calls)
    assert (await post(client, webhook_id, sample("location.json")))[1]["trips"] == {TRIP: "ok"}
    await hass.async_block_till_done()
    state = hass.states.get(TRACKER)
    assert state.attributes["latitude"] == 49.4512 and state.attributes["gps_accuracy"] == 11
    assert state.attributes["fix_time"] == "2026-10-08T07:40:31+02:00" and state.attributes["speed"] == 24.9
    # Ergebnis der App: Zugpunkt und Geschwindigkeit, wie die App sie anzeigt
    assert state.attributes["train_leg"] == 1 and state.attributes["train_latitude"] == 49.4510
    assert state.attributes["train_speed"] == 24.9 and state.attributes["train_speed_approx"] is False
    assert state.attributes["train_speed_time"] == "2026-10-08T07:40:31+02:00"
    # „Jetzt aktualisieren“: dieselbe Messung mit neuem Ergebnis noch einmal
    again = copy.deepcopy(sample("location.json"))
    again["messages"][0]["train"] |= {"speed": 30.0, "speedTime": "2026-10-08T07:40:40+02:00", "speedApprox": True}
    assert (await post(client, webhook_id, again))[1]["trips"] == {TRIP: "ok"}
    await hass.async_block_till_done()
    state = hass.states.get(TRACKER)
    assert state.attributes["train_speed"] == 30.0 and state.attributes["train_speed_approx"] is True
    await rt.notify(hass_admin_user.id, "Echtzeit fehlt")
    assert len(calls) == n

    # Reisen aus der App löscht man nur dort (Entscheidung 18.11.2026)
    await ws.send_json({"id": 14, "type": f"{DOMAIN}/delete", "trip_id": trip["id"]})
    res = await ws.receive_json()
    assert not res["success"] and res["error"]["code"] == "from_app"
    assert len(rt.trips.list()) == 1
    gone = copy.deepcopy(sample("delete-stop-bye.json"))
    gone["messages"] = [m for m in gone["messages"] if m["type"] == "trip.delete"]
    assert (await post(client, webhook_id, gone))[1]["trips"] == {TRIP: "ok"}
    assert (await post(client, webhook_id, sample("location.json")))[1]["trips"] == {TRIP: "unknown"}
    assert rt.trips.list() == []

    # Eine zweite Reise; dann Senden stoppen, löschen, aufheben in der App
    second = copy.deepcopy(sample("upsert-augsburg-berlin.json"))
    second["messages"][0]["tripId"] = "zweite-reise"
    assert (await post(client, webhook_id, second))[1]["trips"] == {"zweite-reise": "ok"}
    loc = copy.deepcopy(sample("location.json"))
    loc["messages"][0]["tripId"] = "zweite-reise"
    await post(client, webhook_id, loc)
    bye = copy.deepcopy(sample("delete-stop-bye.json"))
    for m in bye["messages"][:2]:
        m["tripId"] = "zweite-reise"
    status, body = await post(client, webhook_id, bye)
    assert status == 200 and body["trips"] == {"zweite-reise": "ok"}
    await hass.async_block_till_done()
    assert rt.trips.list() == [] and rt.links.links == {}
    assert hass.states.get(TRACKER) is None, "Tracker mit der Verbindung entfernt"
    assert await post(client, webhook_id, sample("location.json")) == (200, None), "leere Antwort = aufgehoben"


async def test_verbinden_per_teilen_an_die_companion_app(hass, boards, hass_admin_user, hass_client_no_auth, freezer):
    freezer.move_to("2026-10-07T21:00:00+02:00")
    rt, calls = await setup(hass, hass_admin_user)
    client = await hass_client_no_auth()
    text = (SAMPLES / "kopplung.txt").read_text(encoding="utf-8")
    pair_id, token = text.split()[-2:]

    await share(hass, hass_admin_user, text)
    assert rt.trips.list() == [], "keine Reise"
    data = calls[-1].data
    click = data["data"]["clickAction"]
    assert click.startswith("deep-link://reiseanzeige://ziel/verbinden?") and data["data"]["actions"][0]["uri"] == click
    q = parse_qs(urlsplit(click.removeprefix("deep-link://")).query)
    assert q == {"v": ["1"], "url": ["http://192.168.178.10:8123"], "pair": [pair_id], "name": ["Zuhause"], "user": ["Mock User"]}
    assert token not in click, "der Einmal-Schlüssel steht nicht in der Benachrichtigung"

    status, body = await post(client, token, sample("hello.json"))
    assert status == 200 and len(body["link"]["webhookId"]) == 64
    assert rt.links.linked(hass_admin_user.id)


async def test_reise_in_mehreren_teilen_angekommen(hass, boards, hass_admin_user, hass_ws_client, hass_client_no_auth, freezer):
    """Größere Nachrichten (trip.upsert) kommen über WLAN, Mobilfunk oder VPN oft in mehreren
    Paketen an. Bis 0.8.1 las die Integration nur, was gerade da war, und antwortete dann
    „JSON erwartet“ (manchmal, je nach Netz)."""
    import asyncio
    ws = await hass_ws_client(hass)
    freezer.move_to("2026-10-07T21:00:00+02:00")
    rt, _ = await setup(hass, hass_admin_user)
    client = await hass_client_no_auth()
    _, link = await connect(hass, ws, client)
    raw = json.dumps(sample("upsert-augsburg-berlin.json")).encode("utf-8")
    assert len(raw) > 1000

    async def pieces():
        for i in range(0, len(raw), 400):
            yield raw[i:i + 400]
            for _ in range(5):   # Zeit steht (freezer): nur dem Server den Vortritt lassen
                await asyncio.sleep(0)
    res = await client.post(f"/api/webhook/{link['webhookId']}", data=pieces(), headers={"Content-Type": "application/json"})
    assert res.status == 200, await res.text()
    assert (await res.json())["trips"] == {TRIP: "ok"}
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [t["app"]["tripId"] for t in rt.trips.list()] == [TRIP]


async def test_fehler_und_aufheben_in_home_assistant(hass, boards, hass_admin_user, hass_ws_client, hass_client_no_auth, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to("2026-10-07T21:00:00+02:00")
    rt, _ = await setup(hass, hass_admin_user)
    client = await hass_client_no_auth()

    await ws.send_json({"id": 1, "type": f"{DOMAIN}/link_start"})
    token = parse_qs(urlsplit((await ws.receive_json())["result"]["link"]).query)["token"][0]
    status, body = await post(client, token, sample("upsert-augsburg-berlin.json"))
    assert status == 400 and body["error"]["code"] == "bad_request", "zuerst link.hello"
    status, body = await post(client, token, {"format": 2, "messages": []})
    assert status == 400 and body["error"]["code"] == "format_unsupported"

    _, hello = await post(client, token, sample("hello.json"))
    webhook_id = hello["link"]["webhookId"]
    await post(client, webhook_id, sample("upsert-augsburg-berlin.json"))
    loc = copy.deepcopy(sample("location.json"))
    loc["messages"][0]["tripId"] = "unbekannt"
    assert (await post(client, webhook_id, loc))[1]["trips"] == {"unbekannt": "unknown"}

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/links"})
    [link] = (await ws.receive_json())["result"]["links"]
    assert link["device"] == "Nokia 8.3 5G" and link["tracker"] == TRACKER and "webhookId" not in link

    await ws.send_json({"id": 3, "type": f"{DOMAIN}/link_remove", "link_id": link["id"]})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert rt.trips.list() == [], "Reisen der App sofort entfernt"
    assert await post(client, webhook_id, sample("upsert-augsburg-berlin.json")) == (200, None)
    await ws.send_json({"id": 4, "type": f"{DOMAIN}/me"})
    assert (await ws.receive_json())["result"]["linked"] is False


async def test_administratoren_sehen_alle_verbindungen(hass, boards, hass_admin_user, hass_read_only_user,
                                                      hass_read_only_access_token, hass_ws_client,
                                                      hass_client_no_auth, freezer):
    """Etwa das kaputte Handy einer anderen Person: Admins sehen und heben jede Verbindung auf."""
    ws = await hass_ws_client(hass)
    ro = await hass_ws_client(hass, hass_read_only_access_token)   # vor dem Stellen der Uhr anmelden
    freezer.move_to("2026-10-07T21:00:00+02:00")
    rt, _ = await setup(hass, hass_admin_user)
    client = await hass_client_no_auth()
    _, own = await connect(hass, ws, client)
    # Verbindung einer anderen Person (Handy dieser Person, Reise aus ihrer App)
    other = rt.links.begin(hass_read_only_user.id)
    status, body = await post(client, other["token"], sample("hello.json") | {"messages": [
        {"type": "link.hello", "device": "Pixel 9", "appInstance": "0" * 32}]})
    assert status == 200
    up = copy.deepcopy(sample("upsert-augsburg-berlin.json"))
    up["messages"][0]["tripId"] = "anna"
    assert (await post(client, body["link"]["webhookId"], up))[1]["trips"] == {"anna": "ok"}

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/links"})
    links = (await ws.receive_json())["result"]["links"]
    assert [(l["device"], l["own"], l.get("userName")) for l in links] == [
        ("Nokia 8.3 5G", True, None), ("Pixel 9", False, hass_read_only_user.name)]

    # ohne Adminrechte nur die eigene
    await ro.send_json({"id": 1, "type": f"{DOMAIN}/links"})
    assert [l["device"] for l in (await ro.receive_json())["result"]["links"]] == ["Pixel 9"]
    await ro.send_json({"id": 2, "type": f"{DOMAIN}/link_remove", "link_id": rt.links.for_user(hass_admin_user.id)[0]["id"]})
    assert (await ro.receive_json())["error"]["code"] == "not_found"

    # Admin hebt die fremde auf: ihre Reisen sind sofort weg, die eigene bleibt
    await ws.send_json({"id": 3, "type": f"{DOMAIN}/link_remove", "link_id": links[1]["id"]})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert [l["device"] for l in rt.links.links.values()] == ["Nokia 8.3 5G"]
    assert rt.trips.list() == []
    assert own["webhookId"] in {l["webhookId"] for l in rt.links.links.values()}


async def test_umleitung_per_gps_kommt_aus_der_app(hass, boards, railnet_release, hass_admin_user, hass_ws_client,
                                                  hass_client_no_auth, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to("2026-11-01T12:00:00+01:00")
    rt, _ = await setup(hass, hass_admin_user)
    client = await hass_client_no_auth()
    _, link = await connect(hass, ws, client)
    webhook_id = link["webhookId"]

    connection = journey_to_dict(parse_share_text(fixture_text("share", "moritzplatz-berlin-2026-11-01.txt")))
    up = {"format": 1, "messages": [{"type": "trip.upsert", "tripId": "ice", "revision": 1, "connection": connection}]}
    assert (await post(client, webhook_id, up))[1]["trips"] == {"ice": "ok"}
    await hass.async_block_till_done(wait_background_tasks=True)
    [trip] = rt.trips.list()
    assert trip["snapshot"]["legs"][2]["matched"], "Home Assistant gleicht selbst mit dem Fahrplan ab"

    # Die eigene Auswertung läuft für verbundene Personen nicht (Companion-Tracker zählt nicht)
    erf, lei = P["Erfurt Hbf"], P["Leipzig Hbf"]
    for when, f in (("2026-11-01T17:55:00+01:00", 0.3), ("2026-11-01T18:05:00+01:00", 0.6)):
        freezer.move_to(when)
        p = (erf[0] + (lei[0] - erf[0]) * f, erf[1] + (lei[1] - erf[1]) * f)
        hass.states.async_set("device_tracker.testhandy", "not_home", {"latitude": p[0], "longitude": p[1], "gps_accuracy": 20})
        await rt.tick()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert "diversion" not in rt.trips.list()[0]["snapshot"]["legs"][2]

    between = lambda f: [round(erf[0] + (lei[0] - erf[0]) * f, 5), round(erf[1] + (lei[1] - erf[1]) * f, 5)]
    gps = {"after": {"id": "8010101", "name": "Erfurt Hbf"}, "anchor": list(erf), "anchorAt": "2026-11-01T17:45:00+01:00",
           "prefix": [list(P["Nürnberg Hbf"]), list(erf)],
           "points": [between(0.3) + ["2026-11-01T17:55:00+01:00"], between(0.6) + ["2026-11-01T18:05:00+01:00"]]}
    route = {"format": 1, "messages": [{"type": "route.gps", "tripId": "ice", "leg": 2,
                                        "legKey": {"line": "ICE 1004", "dep": "2026-11-01T16:36:00+01:00"}, "gps": gps}]}
    assert (await post(client, webhook_id, route))[1]["trips"] == {"ice": "ok"}
    ice = rt.trips.list()[0]["snapshot"]["legs"][2]
    assert ice["diversion"]["gps"]["after"]["name"] == "Erfurt Hbf"
    assert any(abs(p[0] - between(0.6)[0]) < 0.01 for p in ice["diversion"]["polyline"])

    wrong = copy.deepcopy(route)
    wrong["messages"][0]["legKey"]["line"] = "RE1"
    assert (await post(client, webhook_id, wrong))[1]["trips"] == {"ice": "ignored"}, "anderer Abschnitt"

    route["messages"][0]["gps"] = None
    assert (await post(client, webhook_id, route))[1]["trips"] == {"ice": "ok"}
    assert "diversion" not in rt.trips.list()[0]["snapshot"]["legs"][2]
