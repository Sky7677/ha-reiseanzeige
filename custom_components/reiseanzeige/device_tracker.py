"""Je verbundener Android-App ein Tracker mit dem letzten Standort, den die App
während einer Reise schickt (nur mit „Standort senden“).

Die Karte liest ihn wie den Tracker der Companion-App (ownerLocation) und zeigt
nie den Standort selbst, nur den Punkt auf der Strecke. Koordinaten und
Genauigkeit gehen nicht in den Verlauf von Home Assistant; nach „Senden
stoppen“, der Ankunft oder 30 Minuten ohne neuen Standort ist er leer.
"""

from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_GPS_ACCURACY, ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN, SIGNAL_LINK_LOCATION, SIGNAL_LINKS_CHANGED
from .links import tracker_unique_id


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    rt = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new = [lid for lid in rt.links.links if lid not in known]
        known.difference_update(lid for lid in list(known) if lid not in rt.links.links)
        if new:
            known.update(new)
            async_add_entities([AppTracker(rt, entry, lid) for lid in new])

    add_new()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_LINKS_CHANGED, add_new))


class AppTracker(TrackerEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:train-variant"
    _attr_has_entity_name = False
    # Kein Standortverlauf in Home Assistant
    _unrecorded_attributes = frozenset({ATTR_LATITUDE, ATTR_LONGITUDE, ATTR_GPS_ACCURACY, "speed", "bearing",
                                        "fix_time", "trip_id", "train_leg", "train_latitude", "train_longitude",
                                        "train_time", "train_gps", "train_speed", "train_speed_time",
                                        "train_speed_approx"})

    def __init__(self, rt, entry: ConfigEntry, link_id: str) -> None:
        self.rt = rt
        self.link_id = link_id
        link = rt.links.links[link_id]
        person = rt.name_of(link["userId"]) or "Person"
        device = link.get("device") or "Android"
        self._attr_unique_id = tracker_unique_id(entry.entry_id, link_id)
        self._attr_name = f"Reiseanzeige {person} ({device})"
        self.entity_id = f"device_tracker.{DOMAIN}_{slugify(person) or 'person'}_{slugify(device) or 'android'}"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{SIGNAL_LINK_LOCATION}_{self.link_id}", self.async_write_ha_state))

    @property
    def _fix(self) -> dict | None:
        return self.rt.links.locations.get(self.link_id)

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        return self._fix["lat"] if self._fix else None

    @property
    def longitude(self) -> float | None:
        return self._fix["lon"] if self._fix else None

    @property
    def location_accuracy(self) -> float:
        return self._fix["accuracy"] if self._fix else 0

    @property
    def extra_state_attributes(self) -> dict:
        fix = self._fix
        if not fix:
            return {}
        # fix_time: Zeit der Messung – die App schickt im Hintergrund gesammelt
        attrs = {"speed": fix.get("speed"), "bearing": fix.get("bearing"), "fix_time": fix["time"].isoformat(),
                 "trip_id": fix.get("tripId")}
        # Ergebnis der App: Zugpunkt auf der Strecke und Geschwindigkeit, wie die App sie
        # anzeigt (m/s); die Karte nutzt es vor ihrer eigenen Auswertung der Messung
        train = fix.get("train")
        if train:
            attrs |= {"train_leg": train["leg"], "train_latitude": train["lat"], "train_longitude": train["lon"],
                      "train_time": train["time"].isoformat(), "train_gps": train["gps"],
                      "train_speed": train.get("speed"),
                      "train_speed_time": train["speedTime"].isoformat() if train.get("speedTime") else None,
                      "train_speed_approx": train.get("speedApprox", False)}
        return attrs
