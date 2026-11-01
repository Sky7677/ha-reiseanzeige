"""Wer hat geteilt – und auf welchem Handy? Ohne Einrichtung.

Das Teilen-Ereignis der Companion-App enthält den Benutzer (context.user_id)
und die Kennung des Handys (Android: device_id, iOS: sourceDeviceID). Daraus
ermittelt die Integration
  * den Benachrichtigungsdienst dieses Handys (notify.mobile_app_<name>) für
    Bestätigungen, Hinweise und die Standort-Anforderung,
  * dessen device_tracker für die GPS-Korrektur auf der Karte,
und merkt sich beides je Benutzer. Wer nie geteilt hat, hat keinen Eintrag.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .const import STORAGE_KEY_OWNERS, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

MOBILE_APP = "mobile_app"


def device_id_of(event_data: dict) -> str | None:
    return event_data.get("device_id") or event_data.get("sourceDeviceID")


def resolve_device(hass: HomeAssistant, app_device_id: str) -> dict | None:
    """Kennung der Companion-App → {entry, notify, tracker} oder None.

    Ohne Geräteliste: Jede Einrichtung der Companion-App (mobile_app) trägt die
    Kennung des Handys und seinen Namen; Benachrichtigungsdienst und
    device_tracker gehören zu dieser Einrichtung. Das funktioniert unabhängig
    von den Änderungen an der Geräteliste (HA 2026.9: Kennungen nur noch je
    Einrichtung eindeutig).
    """
    entry = next((e for e in hass.config_entries.async_entries(MOBILE_APP)
                  if e.data.get("device_id") == app_device_id), None)
    if entry is None:
        return None
    name = entry.data.get("device_name") or entry.title
    notify = f"mobile_app_{slugify(name)}" if name else None
    if notify and not hass.services.has_service("notify", notify):
        notify = None
    tracker = next(
        (e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
         if e.domain == "device_tracker" and not e.disabled),
        None,
    )
    return {"entry": entry.entry_id, "appDeviceId": app_device_id, "notify": notify, "tracker": tracker,
            "deviceName": name}


class Owners:
    """Gemerkte Handys je Benutzer."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY_OWNERS)
        self.data: dict[str, dict] = {}

    async def load(self) -> None:
        self.data = await self._store.async_load() or {}

    async def learn(self, user_id: str, event_data: dict) -> dict | None:
        """Aus einem Teilen-Ereignis lernen. Liefert den (neuen) Eintrag."""
        app_id = device_id_of(event_data)
        info = resolve_device(self.hass, app_id) if app_id else None
        if info is None:
            return self.data.get(user_id)
        if self.data.get(user_id) != info:
            self.data[user_id] = info
            await self._store.async_save(self.data)
        return info

    def get(self, user_id: str) -> dict | None:
        return self.data.get(user_id)

    def notify_service(self, user_id: str) -> str | None:
        return (self.data.get(user_id) or {}).get("notify")

    def tracker(self, user_id: str) -> str | None:
        return (self.data.get(user_id) or {}).get("tracker")
