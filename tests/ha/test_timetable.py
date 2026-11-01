"""Fahrplan in Home Assistant: Reise Tage im Voraus bekommt ihre Zwischenhalte."""

from homeassistant.core import Context
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reiseanzeige.const import DOMAIN
from testdata import fixture_text


async def test_reise_im_voraus_mit_zwischenhalten_aus_dem_fahrplan(hass, iris, timetable_release, hass_admin_user, freezer):
    freezer.move_to("2026-10-05T12:00:00+02:00")
    entry = MockConfigEntry(domain=DOMAIN, data={"client_id": "cid", "api_key": "key"}, options={"station": "Augsburg Hbf"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    rt = entry.runtime_data
    assert rt.timetable.loaded and rt.timetable.status()["windowStart"] == "2026-10-01"

    hass.bus.async_fire("mobile_app.share", {"text": fixture_text("share", "augsburg-berlin-2026-10-08.txt")},
                        context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done(wait_background_tasks=True)
    trip = rt.trips.list()[0]
    re16, ice = trip["snapshot"]["legs"]
    assert not [p for p in iris.paths if p.startswith("/plan")], "drei Tage vorher keine Fahrplan-Abfragen bei der Bahn"
    assert re16["source"] == "gtfs" and [s["station"]["name"] for s in re16["stopovers"]] == [
        "Augsburg Hbf", "Donauwörth", "Treuchtlingen", "Nürnberg Hbf"]
    assert ice["source"] == "gtfs" and len(ice["stopovers"]) == 3
    assert all(s["station"]["lat"] is not None for leg in (re16, ice) for s in leg["stopovers"]), "Lage aus dem Fahrplan"
