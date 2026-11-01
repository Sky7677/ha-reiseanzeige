"""Gleisnetz in Home Assistant: Download, Prüfsumme, Verlauf der geteilten Reise."""

from datetime import timedelta

from homeassistant.core import Context
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reiseanzeige.const import DOMAIN
from testdata import fixture_text

SHARE = fixture_text("share", "moritzplatz-berlin-2026-11-01.txt")


async def setup(hass, **options):
    entry = MockConfigEntry(domain=DOMAIN, data={"client_id": "cid", "api_key": "key"},
                            options={"station": "Augsburg Hbf", **options})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def share(hass, user):
    hass.bus.async_fire("mobile_app.share", {"text": SHARE}, context=Context(user_id=user.id))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_gleisnetz_wird_geladen_und_zeichnet_die_strecke(hass, iris, railnet_release, hass_admin_user, freezer):
    freezer.move_to("2026-11-01T12:00:00+01:00")
    entry = await setup(hass)
    rt = entry.runtime_data
    assert rt.railnet.net is not None and rt.railnet.version == "2026-11-02"
    assert railnet_release.path.exists()
    assert railnet_release.mock.call_count == 2, "Manifest und Datei"

    await share(hass, hass_admin_user)
    trip = rt.trips.list()[0]
    assert trip["enriched"]
    legs = trip["snapshot"]["legs"]
    assert [l["geometry"]["source"] for l in legs] == ["line", "tracks", "tracks", "tracks", "line"]
    assert all(s["station"]["lat"] is not None for l in legs for s in l["stopovers"]), "alle Halte ohne StaDa verortet"
    assert trip["snapshot"]["origin"]["lat"] is not None

    # Die Karte bekommt den Verlauf; in der kurzen Liste nur den Hinweis darauf
    full = rt.trip_view(rt.trips.get(trip["id"]))
    assert len(full["snapshot"]["legs"][1]["polyline"]) >= 4
    light = rt.trip_view(rt.trips.get(trip["id"]), light=True)
    assert light["snapshot"]["legs"][1]["polyline"] is None and light["snapshot"]["legs"][1]["hasPolyline"]

    # Wöchentliche Prüfung: gleiche Version → nur das Manifest
    railnet_release.mock.clear_requests()
    railnet_release.publish("2026-11-02")
    rt.railnet.checked_at -= timedelta(days=8)
    await rt.tick()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert railnet_release.mock.call_count == 1

    # Neue Fassung → neu laden, Verläufe neu berechnen
    old_key = legs[1]["geometry"]["key"]
    railnet_release.publish("2026-12-02")
    rt.railnet.checked_at -= timedelta(days=8)
    await rt.tick()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert rt.railnet.version == "2026-12-02" and railnet_release.mock.call_count == 2
    assert rt.trips.get(trip["id"])["snapshot"]["legs"][1]["geometry"]["key"] != old_key

    # Nach dem Neustart aus der gespeicherten Datei, ohne Download
    railnet_release.mock.clear_requests()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.railnet.version == "2026-12-02" and entry.runtime_data.railnet.net is not None
    assert railnet_release.mock.call_count == 0


async def test_falsche_pruefsumme_wird_verworfen(hass, iris, railnet_release, freezer):
    freezer.move_to("2026-11-01T12:00:00+01:00")
    railnet_release.publish(corrupt=True)
    entry = await setup(hass)
    rt = entry.runtime_data
    assert rt.railnet.net is None and "Prüfsumme" in rt.railnet.error
    assert not railnet_release.path.exists() and not (railnet_release.path.parent / (railnet_release.path.name + ".part")).exists()

    # Nicht sofort erneut, aber nach 6 Stunden
    calls = railnet_release.mock.call_count
    await rt.tick()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert railnet_release.mock.call_count == calls
    railnet_release.publish()
    freezer.tick(timedelta(hours=7))
    await rt.tick()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert rt.railnet.net is not None and rt.railnet.error is None


async def test_gleisnetz_abgeschaltet(hass, iris, railnet_release, hass_admin_user, freezer):
    freezer.move_to("2026-11-01T12:00:00+01:00")
    entry = await setup(hass, railnet=False)
    assert railnet_release.mock.call_count == 0
    await share(hass, hass_admin_user)
    trip = entry.runtime_data.trips.list()[0]
    assert all(l["polyline"] is None for l in trip["snapshot"]["legs"])
