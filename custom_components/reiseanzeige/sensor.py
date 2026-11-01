"""Je Person ein Sensor mit dem Zustand der wichtigsten Reise.

Zustand: running | upcoming | completed | none. Attribute für Automationen
(z. B. Licht im Flur, wenn jemand unterwegs ist).
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_NEW_OWNER, SIGNAL_TRIPS_CHANGED

PRIORITY = {"running": 0, "upcoming": 1, "completed": 2}
STATES = ["none", "upcoming", "running", "completed"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    rt = entry.runtime_data

    @callback
    def add_new() -> None:
        owners = {t["ownerId"] for t in rt.trips.trips} | set(rt.owners.data)
        new = [o for o in owners if o not in rt.sensor_owners]
        if new:
            rt.sensor_owners.update(new)
            async_add_entities([TripSensor(rt, entry, o) for o in new])

    add_new()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_OWNER, add_new))


class TripSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:train"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES
    _attr_translation_key = "trip"

    def __init__(self, rt, entry: ConfigEntry, owner_id: str) -> None:
        self.rt = rt
        self.owner_id = owner_id
        name = rt.name_of(owner_id) or owner_id
        self._attr_unique_id = f"{entry.entry_id}_{owner_id}"
        self._attr_name = f"Reise {name}"
        self._attr_has_entity_name = False
        self.entity_id = f"sensor.{DOMAIN}_{_slug(name)}"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_TRIPS_CHANGED, self.async_write_ha_state))

    def _main(self) -> dict | None:
        own = [t for t in self.rt.trips.list() if t["ownerId"] == self.owner_id]
        own.sort(key=lambda t: (PRIORITY.get(t["status"], 9), t["departure"]))
        return own[0] if own else None

    @property
    def native_value(self) -> str:
        t = self._main()
        return t["status"] if t else "none"

    @property
    def extra_state_attributes(self) -> dict:
        t = self._main()
        own = [x for x in self.rt.trips.list() if x["ownerId"] == self.owner_id]
        snap = t["snapshot"] if t else {}
        return {
            "user_id": self.owner_id,
            "trip_count": len(own),
            "trip_id": t["id"] if t else None,
            "origin": snap.get("origin", {}).get("name") if t else None,
            "destination": snap.get("destination", {}).get("name") if t else None,
            "departure": t["departure"] if t else None,
            "arrival": t["arrival"] if t else None,
            "realtime_available": (not t["realtimeHealth"]["failing"]) if t else None,
        }


def _slug(name: str) -> str:
    from homeassistant.util import slugify
    return slugify(name) or "unbekannt"
