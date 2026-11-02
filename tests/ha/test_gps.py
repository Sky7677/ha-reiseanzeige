"""Umleitung per GPS in Home Assistant: Standort vom Handy der Reisenden, Linie neu."""

from homeassistant.core import Context
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.reiseanzeige.const import DOMAIN
from railnet_fixture import P
from testdata import fixture_text


def between(a, b, f):
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


async def setup_with_phone(hass, user, **options):
    app_entry = MockConfigEntry(domain="mobile_app", data={"device_name": "Testhandy", "device_id": "abc123"})
    app_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=app_entry.entry_id, identifiers={("mobile_app", "abc123")}, name="Testhandy")
    er.async_get(hass).async_get_or_create("device_tracker", "mobile_app", "abc123", config_entry=app_entry, device_id=device.id, suggested_object_id="testhandy")
    async_mock_service(hass, "notify", "mobile_app_testhandy")
    entry = MockConfigEntry(domain=DOMAIN, data={"client_id": "cid", "api_key": "key"}, options={"station": "Augsburg Hbf", **options})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.bus.async_fire("mobile_app.share", {"text": fixture_text("share", "moritzplatz-berlin-2026-11-01.txt"), "device_id": "abc123"},
                        context=Context(user_id=user.id))
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data


async def fix(hass, rt, freezer, when, point, accuracy=20):
    freezer.move_to(when)
    hass.states.async_set("device_tracker.testhandy", "not_home", {"latitude": point[0], "longitude": point[1], "gps_accuracy": accuracy})
    await rt.tick()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_zug_faehrt_ueber_leipzig_ohne_meldung(hass, iris, railnet_release, hass_admin_user, freezer):
    freezer.move_to("2026-11-01T12:00:00+01:00")
    rt = await setup_with_phone(hass, hass_admin_user)
    trip_id = rt.trips.list()[0]["id"]
    erf, lei = P["Erfurt Hbf"], P["Leipzig Hbf"]

    await fix(hass, rt, freezer, "2026-11-01T17:55:00+01:00", between(erf, lei, 0.3))
    assert "diversion" not in rt.trips.get(trip_id)["snapshot"]["legs"][2], "ein Standort genügt nicht"
    await fix(hass, rt, freezer, "2026-11-01T18:05:00+01:00", between(erf, lei, 0.6))
    ice = rt.trips.get(trip_id)["snapshot"]["legs"][2]
    assert ice["diversion"]["gps"]["after"]["name"] == "Erfurt Hbf"
    assert any(abs(p[0] - between(erf, lei, 0.6)[0]) < 0.01 for p in ice["diversion"]["polyline"])

    # Die Karte bekommt die Linie, aber keine Standortpunkte
    full = rt.trip_view(rt.trips.get(trip_id))["snapshot"]["legs"][2]["diversion"]
    assert full["gps"] == {"active": True} and len(full["polyline"]) >= 3
    light = rt.trip_view(rt.trips.get(trip_id), light=True)["snapshot"]["legs"][2]["diversion"]
    assert light["gps"] == {"active": True} and light["polyline"] is None


async def test_abgeschaltet(hass, iris, railnet_release, hass_admin_user, freezer):
    freezer.move_to("2026-11-01T12:00:00+01:00")
    rt = await setup_with_phone(hass, hass_admin_user, gps_route=False)
    erf, lei = P["Erfurt Hbf"], P["Leipzig Hbf"]
    for when, f in (("2026-11-01T17:55:00+01:00", 0.3), ("2026-11-01T18:05:00+01:00", 0.6)):
        await fix(hass, rt, freezer, when, between(erf, lei, f))
    assert "diversion" not in rt.trips.list()[0]["snapshot"]["legs"][2]
