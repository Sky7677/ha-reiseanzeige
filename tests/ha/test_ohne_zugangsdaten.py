"""Reiseanzeige ohne Zugangsdaten der Bahn: Einrichtung, Betrieb nach Fahrplan,
Zugangsdaten später eintragen und wieder entfernen."""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import Context
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reiseanzeige.const import DOMAIN
from testdata import fixture_text


async def test_einrichtung_ohne_zugangsdaten(hass, iris):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert "guide" in result["description_placeholders"]

    half = await hass.config_entries.flow.async_configure(result["flow_id"], {"client_id": "cid", "api_key": " "})
    assert half["errors"] == {"base": "incomplete_credentials"}

    ok = await hass.config_entries.flow.async_configure(result["flow_id"], {"station": "Augsburg Hbf"})
    assert ok["type"] is FlowResultType.CREATE_ENTRY and ok["data"] == {}
    assert iris.paths == [], "keine Anfrage bei der Bahn"
    await hass.async_block_till_done()


async def test_zugangsdaten_spaeter_eintragen_und_entfernen(hass, iris):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"station": "Augsburg Hbf"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.client is None

    def reconfigure():
        return hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id})

    flow = await reconfigure()
    with patch("custom_components.reiseanzeige.config_flow.validate", return_value="invalid_auth"):
        bad = await hass.config_entries.flow.async_configure(flow["flow_id"], {"client_id": "x", "api_key": "y"})
    assert bad["errors"] == {"base": "invalid_auth"}
    done = await hass.config_entries.flow.async_configure(flow["flow_id"], {"client_id": "cid", "api_key": "key"})
    assert done["reason"] == "reconfigure_successful" and entry.data == {"client_id": "cid", "api_key": "key"}
    await hass.async_block_till_done()
    assert entry.runtime_data.client is not None, "nach dem Neuladen mit Echtzeit"

    flow = await reconfigure()
    done = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert done["reason"] == "reconfigure_successful" and entry.data == {}
    await hass.async_block_till_done()
    assert entry.runtime_data.client is None


async def test_betrieb_nach_fahrplan(hass, iris, timetable_release, hass_admin_user, hass_ws_client, freezer):
    ws = await hass_ws_client(hass)
    freezer.move_to("2026-10-05T12:00:00+02:00")
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"station": "Augsburg Hbf"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    rt = entry.runtime_data

    hass.bus.async_fire("mobile_app.share", {"text": fixture_text("share", "augsburg-berlin-2026-10-08.txt")},
                        context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done(wait_background_tasks=True)
    trip = rt.trips.list()[0]
    assert [leg["source"] for leg in trip["snapshot"]["legs"]] == ["gtfs", "gtfs"], "Zwischenhalte aus dem Fahrplan"

    # Am Reisetag: kein Abgleich, keine Echtzeit – keine einzige Anfrage bei der Bahn
    freezer.move_to("2026-10-08T06:10:00+02:00")
    await rt.tick()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert iris.paths == []
    assert rt.trips.get(trip["id"])["realtime"] is None

    await ws.send_json({"id": 1, "type": f"{DOMAIN}/me"})
    assert (await ws.receive_json())["result"]["realtime"] is False
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/board"})
    assert (await ws.receive_json())["result"] == {"status": "no-credentials"}

    from custom_components.reiseanzeige.diagnostics import async_get_config_entry_diagnostics
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["credentials"] is False and diag["timetables_calls_since_start"] == 0
