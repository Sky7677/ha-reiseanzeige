"""Die Integration in einem echten (Test-)Home-Assistant: Einrichtung, Teilen,
WebSocket-Befehle der Karte, Sensor, Kartenproxy, Entladen."""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.components import persistent_notification
from homeassistant.core import Context
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reiseanzeige.const import DOMAIN
from testdata import fixture_text

SHARE = fixture_text("share", "moritzplatz-berlin-2026-11-01.txt")


async def setup(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, data={"client_id": "cid", "api_key": "key"}, options=options or {"station": "Augsburg Hbf"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_einrichtung_mit_pruefung_der_zugangsdaten(hass, iris):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    with patch("custom_components.reiseanzeige.config_flow.validate", return_value="invalid_auth"):
        bad = await hass.config_entries.flow.async_configure(result["flow_id"], {"client_id": "x", "api_key": "y"})
    assert bad["errors"] == {"base": "invalid_auth"}

    ok = await hass.config_entries.flow.async_configure(result["flow_id"], {"client_id": " cid ", "api_key": "key", "station": "Augsburg Hbf"})
    assert ok["type"] is FlowResultType.CREATE_ENTRY
    assert ok["data"] == {"client_id": "cid", "api_key": "key"}
    assert ok["options"] == {"station": "Augsburg Hbf"}
    assert any(p.startswith("/station/") for p in iris.paths), "eine Testanfrage"
    await hass.async_block_till_done()

    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert again["type"] is FlowResultType.ABORT


async def test_teilen_speichert_reise_und_reichert_an(hass, iris, hass_admin_user, hass_ws_client, freezer):
    ws = await hass_ws_client(hass)  # vor dem Zeitsprung anmelden
    freezer.move_to("2026-11-01T12:00:00+01:00")
    entry = await setup(hass)
    hass.bus.async_fire("mobile_app.share", {"text": "https://youtube.com/watch?v=x"}, context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()
    assert entry.runtime_data.trips.trips == [], "fremdes Teilen wird ignoriert"

    hass.bus.async_fire("mobile_app.share", {"caller": "android-app://de.hafas.android.db", "text": SHARE},
                        context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()

    trips = entry.runtime_data.trips.list()
    assert len(trips) == 1
    trip = trips[0]
    assert trip["ownerId"] == hass_admin_user.id and trip["status"] == "upcoming"
    assert trip["enriched"], "im Hintergrund über die Timetables-API angereichert"
    assert [l["matched"] for l in trip["snapshot"]["legs"]] == [False, True, True, True, False]

    # Ohne bekanntes Handy: Bestätigung als Hinweis in Home Assistant
    notes = list(persistent_notification._async_get_or_create_notifications(hass).values())
    assert [n["message"] for n in notes] == [
        "Mock User: Reise Moritzplatz, Augsburg (Bayern) → Breitenbachplatz (U), Berlin am 01.11. gespeichert."]

    # Sensor der Person
    state = hass.states.get("sensor.reiseanzeige_mr_admin") or next(
        s for s in hass.states.async_all("sensor") if s.entity_id.startswith("sensor.reiseanzeige_"))
    assert state.state == "upcoming"
    assert state.attributes["destination"] == "Breitenbachplatz (U), Berlin"

    # Karte: Befehle über die angemeldete Verbindung
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/me"})
    me = (await ws.receive_json())["result"]
    assert me["id"] == hass_admin_user.id and me["station"] == "Augsburg Hbf" and me["canAdd"] is False

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/trips"})
    listed = (await ws.receive_json())["result"]["trips"]
    assert [t["id"] for t in listed] == [trip["id"]]
    assert listed[0]["ownerName"] == hass_admin_user.name and listed[0]["ownerCanLocate"] is False

    await ws.send_json({"id": 3, "type": f"{DOMAIN}/seat", "trip_id": trip["id"], "leg": 2, "coach": "9", "seat": "64"})
    assert (await ws.receive_json())["result"]["seats"] == {"2": {"coach": "9", "seat": "64"}}

    await ws.send_json({"id": 4, "type": f"{DOMAIN}/locate", "trip_id": trip["id"]})
    err = await ws.receive_json()
    assert not err["success"] and err["error"]["code"] == "not_available"

    await ws.send_json({"id": 5, "type": f"{DOMAIN}/add", "text": "nur Unsinn"})
    err = await ws.receive_json()
    assert not err["success"] and "keine Verbindung" in err["error"]["message"]

    await ws.send_json({"id": 6, "type": f"{DOMAIN}/trip", "trip_id": "gibt-es-nicht"})
    err = await ws.receive_json()
    assert err["error"]["code"] == "not_found"

    await ws.send_json({"id": 7, "type": f"{DOMAIN}/delete", "trip_id": trip["id"]})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert hass.states.get(state.entity_id).state == "none"


async def test_abfahrtstafel_und_kartenproxy(hass, iris, hass_ws_client, hass_client_no_auth, freezer):
    ws = await hass_ws_client(hass)  # vor dem Zeitsprung anmelden
    freezer.move_to("2026-11-01T14:30:00+01:00")
    entry = await setup(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/board"})
    board = (await ws.receive_json())["result"]
    assert board["status"] == "ok" and board["station"]["name"] == "Augsburg Hbf"
    assert [d["line"] for d in board["departures"]] == ["RE1", "RE1"]

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/map_base"})
    base = (await ws.receive_json())["result"]["base"]
    assert base == f"/api/{DOMAIN}/{entry.runtime_data.map_token}/"

    client = await hass_client_no_auth()
    assert (await client.get(f"{base}map/style/muted-light.json")).status == 200
    assert (await client.get(f"/api/{DOMAIN}/falsch/map/style/muted-light.json")).status == 404
    assert (await client.get(f"{base}map/../../etc/passwd")).status == 404


async def test_entladen(hass, iris):
    entry = await setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.NOT_LOADED


async def test_handy_wird_erkannt_push_standort_und_echtzeit(hass, iris, hass_admin_user, hass_ws_client, freezer):
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from pytest_homeassistant_custom_component.common import async_mock_service

    ws = await hass_ws_client(hass)
    freezer.move_to("2026-11-01T12:00:00+01:00")
    # Handy wie von der Companion-App angelegt: Gerät, Benachrichtigungsdienst, device_tracker
    app_entry = MockConfigEntry(domain="mobile_app", data={"device_name": "Testhandy", "device_id": "abc123"})
    app_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=app_entry.entry_id, identifiers={("mobile_app", "abc123")}, name="Testhandy")
    er.async_get(hass).async_get_or_create("device_tracker", "mobile_app", "abc123", config_entry=app_entry, device_id=device.id, suggested_object_id="testhandy")
    pushes = async_mock_service(hass, "notify", "mobile_app_testhandy")

    entry = await setup(hass)
    hass.bus.async_fire("mobile_app.share", {"caller": "android-app://de.hafas.android.db", "text": SHARE, "device_id": "abc123"},
                        context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()
    assert [c.data["message"] for c in pushes][0].startswith("Reise Moritzplatz"), "Bestätigung aufs eigene Handy"
    assert entry.runtime_data.owners.get(hass_admin_user.id)["tracker"] == "device_tracker.testhandy"

    await ws.send_json({"id": 1, "type": f"{DOMAIN}/me"})
    assert (await ws.receive_json())["result"]["canAdd"] is True
    trip_id = entry.runtime_data.trips.list()[0]["id"]

    # Während der ICE-Fahrt: Echtzeit aus Erfurt, Standort anfordern
    iris.changes["8010101"] = '<timetable station="Erfurt Hbf"><s id="-222-2611011636-2"><ar ct="2611011747"/><dp ct="2611011749" cp="2"/></s></timetable>'
    freezer.move_to("2026-11-01T17:30:00+01:00")
    await entry.runtime_data.tick()
    trip = entry.runtime_data.trips.get(trip_id)
    erfurt = trip["realtime"]["legs"]["2"]["stopovers"][1]
    assert erfurt["departure"]["delay"] == 300 and erfurt["platform"] == "2"
    assert "/fchg/8010101" in iris.paths

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/trips"})
    listed = (await ws.receive_json())["result"]["trips"][0]
    assert listed["status"] == "running" and listed["ownerCanLocate"] is True
    assert listed["ownerLocation"] == "device_tracker.testhandy"

    await ws.send_json({"id": 3, "type": f"{DOMAIN}/locate", "trip_id": trip_id})
    assert (await ws.receive_json())["result"] == {"status": "sent"}
    await hass.async_block_till_done()
    assert pushes[-1].data == {"message": "request_location_update"}
    await ws.send_json({"id": 4, "type": f"{DOMAIN}/locate", "trip_id": trip_id})
    assert (await ws.receive_json())["result"]["status"] == "cooldown"


async def test_diagnose_ohne_zugangsdaten(hass, iris):
    from custom_components.reiseanzeige.diagnostics import async_get_config_entry_diagnostics
    entry = await setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["data"] == {"client_id": "**REDACTED**", "api_key": "**REDACTED**"}
    assert diag["trips"] == [] and diag["version"] == "0.7.1"
    assert diag["railnet"]["enabled"] is True and diag["railnet"]["loaded"] is False
    assert diag["timetable"]["enabled"] is True and diag["timetable"]["loaded"] is False


async def test_reise_im_voraus_wird_am_reisetag_abgeglichen(hass, iris, hass_admin_user, hass_ws_client, freezer):
    await hass_ws_client(hass)
    freezer.move_to("2026-10-29T12:00:00+01:00")
    entry = await setup(hass)
    hass.bus.async_fire("mobile_app.share", {"text": SHARE}, context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()
    trip = entry.runtime_data.trips.list()[0]
    assert not any(l["matched"] for l in trip["snapshot"]["legs"])
    assert not [p for p in iris.paths if p.startswith("/plan")], "drei Tage vorher keine Fahrplan-Abfragen"

    freezer.move_to("2026-11-01T08:00:00+01:00")
    await entry.runtime_data.tick()
    await hass.async_block_till_done()
    trip = entry.runtime_data.trips.get(trip["id"])
    assert [l["matched"] for l in trip["snapshot"]["legs"]] == [False, True, True, True, False]
