"""Verbindungen zur Android-Reiseanzeige (Nachrichtenformat v1, core/link_format.py;
Beschreibung in docs/format-v1.md).

Je verbundener App – eine Person, ein Handy – ein eigener Webhook. Verbunden
wird mit einem Einmal-Schlüssel, der 10 Minuten gilt:

  * Karte → „Android-App verbinden“ (Knopf oder QR-Code): Home Assistant
    erzeugt den Schlüssel (websocket.py, link_start).
  * Ohne zweiten Bildschirm: Die App teilt eine Kopplungsanfrage mit eigenem
    Schlüssel an die Companion-App; runtime.py meldet ihn hier an und schickt
    dem Handy eine Benachrichtigung mit Deep Link.

Die App meldet sich mit link.hello an der Einmal-Adresse und bekommt in der
Antwort ihre dauerhafte Adresse. Die steht damit weder in einer
Benachrichtigung noch im Ereignisprotokoll. Wer verbunden ist, bestimmt allein
die Adresse, an die eine Nachricht geht – die App schickt keinen Benutzer mit.

Standorte der App hält diese Klasse nur im Speicher (für die Tracker-Entität,
device_tracker.py); sie werden nie gespeichert.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from http import HTTPStatus

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_LINK_LOCATION, SIGNAL_LINKS_CHANGED, STORAGE_KEY_LINKS, STORAGE_VERSION
from .core.link_format import (
    MAX_BODY_BYTES, FormatError, error_response, parse_request, response,
)

_LOGGER = logging.getLogger(__name__)

PAIRING_TTL = 600            # Sekunden, die ein Einmal-Schlüssel gilt
RATE_LIMIT = 60              # Anfragen je Verbindung und Minute
LOCATION_MAX_AGE = timedelta(minutes=30)   # danach wird der Standort der Tracker-Entität geleert


def tracker_unique_id(entry_id: str, link_id: str) -> str:
    return f"{entry_id}_link_{link_id}"


class Links:
    """Gemerkte Verbindungen, offene Einmal-Schlüssel und ihre Webhooks."""

    def __init__(self, hass: HomeAssistant, rt, *, local_only: bool, app_url: str | None) -> None:
        self.hass = hass
        self.rt = rt
        self.local_only = local_only
        self.app_url = (app_url or "").strip().rstrip("/") or None
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY_LINKS)
        self.links: dict[str, dict] = {}
        self.locations: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}
        self._registered: set[str] = set()
        self._hits: dict[str, deque] = {}
        self._unsub_expiry: dict[str, object] = {}

    # --- Speicher und Webhooks ---

    async def load(self) -> None:
        data = await self._store.async_load() or {}
        self.links = data.get("links") or {}

    async def _save(self) -> None:
        await self._store.async_save({"links": self.links})

    def _register(self, webhook_id: str, name: str) -> None:
        if webhook_id in self._registered:
            return
        webhook.async_register(self.hass, DOMAIN, name, webhook_id, self.handle,
                               local_only=self.local_only, allowed_methods=["POST"])
        self._registered.add(webhook_id)

    def _unregister(self, webhook_id: str) -> None:
        if webhook_id in self._registered:
            webhook.async_unregister(self.hass, webhook_id)
            self._registered.discard(webhook_id)

    @callback
    def start(self) -> None:
        for link in self.links.values():
            self._register(link["webhookId"], f"Reiseanzeige-App {link.get('device') or ''}".strip())

    @callback
    def stop(self) -> None:
        for wid in list(self._registered):
            self._unregister(wid)
        for unsub in self._unsub_expiry.values():
            unsub()
        self._unsub_expiry.clear()
        self._pending.clear()

    # --- Abfragen ---

    def for_user(self, user_id: str) -> list[dict]:
        return sorted((l for l in self.links.values() if l["userId"] == user_id), key=lambda l: l["createdAt"])

    def linked(self, user_id: str | None) -> bool:
        return bool(user_id) and any(l["userId"] == user_id for l in self.links.values())

    def tracker_entity(self, link_id: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(
            "device_tracker", DOMAIN, tracker_unique_id(self.rt.entry.entry_id, link_id))

    def tracker_of_user(self, user_id: str) -> str | None:
        """Tracker der zuletzt aktiven Verbindung einer Person."""
        own = sorted(self.for_user(user_id), key=lambda l: l.get("lastSeenAt") or "", reverse=True)
        return self.tracker_entity(own[0]["id"]) if own else None

    def base_url(self) -> str | None:
        """Adresse, unter der die App Home Assistant erreicht: aus den Optionen, sonst die interne."""
        if self.app_url:
            return self.app_url
        try:
            return get_url(self.hass, allow_internal=True, allow_external=True, allow_cloud=False,
                           prefer_external=False).rstrip("/")
        except NoURLAvailableError:
            return None

    def public(self, link: dict) -> dict:
        """Für Karte und Diagnose: ohne Webhook-Adresse."""
        return {k: link.get(k) for k in ("id", "userId", "device", "appVersion", "createdAt", "lastSeenAt")} | {
            "tracker": self.tracker_entity(link["id"])}

    # --- Verbinden ---

    @callback
    def begin(self, user_id: str, *, token: str | None = None, pair_id: str | None = None) -> dict:
        """Einmal-Schlüssel anmelden (neu erzeugt oder von der App beim Teilen)."""
        token = token or secrets.token_hex(32)
        old = self._pending.pop(token, None)
        if old is not None and old["userId"] != user_id:
            raise ValueError("Schlüssel gehört einem anderen Benutzer")
        self._pending[token] = {"userId": user_id, "pairId": pair_id, "expires": time.monotonic() + PAIRING_TTL,
                                "linkId": None}
        self._register(token, "Reiseanzeige-App verbinden")
        if token in self._unsub_expiry:
            self._unsub_expiry.pop(token)()
        self._unsub_expiry[token] = async_call_later(self.hass, PAIRING_TTL, lambda _now: self._expire(token))
        return {"token": token, "expiresIn": PAIRING_TTL}

    @callback
    def _expire(self, token: str) -> None:
        self._unsub_expiry.pop(token, None)
        if self._pending.pop(token, None) is not None:
            self._unregister(token)

    async def _hello(self, token: str, msg: dict, app_version: str | None) -> dict:
        pending = self._pending[token]
        if pending["linkId"] and pending["linkId"] in self.links:   # Antwort ging verloren: dieselbe Adresse
            return self.links[pending["linkId"]]
        user_id, instance = pending["userId"], msg.get("appInstance")
        link = next((l for l in self.links.values()
                     if instance and l["userId"] == user_id and l.get("appInstance") == instance), None)
        now = dt_util.now().isoformat()
        if link is not None:
            # Dieselbe App verbindet sich neu: neue Adresse, Reisen bleiben
            self._unregister(link["webhookId"])
            link["webhookId"] = secrets.token_hex(32)
        else:
            link = {"id": uuid.uuid4().hex, "userId": user_id, "webhookId": secrets.token_hex(32),
                    "appInstance": instance, "createdAt": now}
            self.links[link["id"]] = link
        link.update(device=msg.get("device") or link.get("device"), appVersion=app_version or link.get("appVersion"),
                    lastSeenAt=now)
        pending["linkId"] = link["id"]
        await self._save()
        self._register(link["webhookId"], f"Reiseanzeige-App {link.get('device') or ''}".strip())
        _LOGGER.info("Android-App „%s“ von %s verbunden", link.get("device"), self.rt.name_of(user_id) or user_id)
        async_dispatcher_send(self.hass, SIGNAL_LINKS_CHANGED)
        await self.rt.on_linked(link)
        return link

    def link_info(self, link: dict, *, with_webhook: bool) -> dict:
        info = {"instance": self.hass.config.location_name, "user": self.rt.name_of(link["userId"]),
                "localOnly": self.local_only}
        return ({"webhookId": link["webhookId"]} | info) if with_webhook else info

    # --- Aufheben ---

    async def remove(self, link_id: str, *, reason: str) -> bool:
        link = self.links.pop(link_id, None)
        if link is None:
            return False
        self._unregister(link["webhookId"])
        self.locations.pop(link_id, None)
        await self._save()
        removed = await self.rt.trips.remove_link(link_id)
        reg = er.async_get(self.hass)
        if (eid := self.tracker_entity(link_id)) is not None:
            reg.async_remove(eid)
        _LOGGER.info("Android-App „%s“ getrennt (%s), %s Reisen entfernt", link.get("device"), reason, removed)
        async_dispatcher_send(self.hass, SIGNAL_LINKS_CHANGED)
        return True

    async def cleanup(self, now: datetime) -> None:
        """Alte Standorte entfernen (aus dem Takt der Laufzeit)."""
        for link_id, loc in list(self.locations.items()):
            if now - loc["time"] > LOCATION_MAX_AGE:
                self.clear_location(link_id)

    # --- Standort (nur im Speicher) ---

    @callback
    def set_location(self, link_id: str, fix: dict, trip_id: str) -> None:
        old = self.locations.get(link_id)
        if old and old["time"] >= fix["time"]:
            return
        self.locations[link_id] = fix | {"tripId": trip_id}
        async_dispatcher_send(self.hass, f"{SIGNAL_LINK_LOCATION}_{link_id}")

    @callback
    def clear_location(self, link_id: str) -> None:
        if self.locations.pop(link_id, None) is not None:
            async_dispatcher_send(self.hass, f"{SIGNAL_LINK_LOCATION}_{link_id}")

    # --- Webhook ---

    def _limited(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= RATE_LIMIT:
            return True
        hits.append(now)
        return False

    async def handle(self, hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return _json(error_response("too_large", "Anfrage zu groß"), HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = await request.content.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            return _json(error_response("too_large", "Anfrage zu groß"), HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        if self._limited(webhook_id):
            return _json(error_response("rate_limited", "Zu viele Anfragen"), HTTPStatus.TOO_MANY_REQUESTS)
        try:
            req = parse_request(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json(error_response("bad_request", "JSON erwartet"), HTTPStatus.BAD_REQUEST)
        except FormatError as err:
            return _json(error_response(err.code, str(err)), HTTPStatus.BAD_REQUEST)

        extra: dict = {}
        if webhook_id in self._pending:
            if not req.messages or req.messages[0]["type"] != "link.hello":
                return _json(error_response("bad_request", "Zuerst link.hello an die Einmal-Adresse"), HTTPStatus.BAD_REQUEST)
            link = await self._hello(webhook_id, req.messages[0], req.app_version)
            extra["link"] = self.link_info(link, with_webhook=True)
            messages = req.messages[1:]
        else:
            link = next((l for l in self.links.values() if l["webhookId"] == webhook_id), None)
            if link is None:   # gerade aufgehoben
                return web.Response(status=HTTPStatus.OK)
            link["lastSeenAt"] = dt_util.now().isoformat()
            if req.app_version:
                link["appVersion"] = req.app_version
            self._store.async_delay_save(lambda: {"links": self.links}, 600)
            messages = req.messages

        trips: dict[str, str] = {}
        for msg in messages:
            kind = msg["type"]
            if kind == "link.hello":
                link.update(device=msg.get("device") or link.get("device"))
                extra["link"] = self.link_info(link, with_webhook=False)
            elif kind == "link.bye":
                await self.remove(link["id"], reason="in der App aufgehoben")
                break
            else:
                trips[msg["tripId"]] = await self.rt.app_message(link, msg)
        return _json(response(trips, **extra), HTTPStatus.OK)


def _json(body: dict, status: HTTPStatus) -> web.Response:
    return web.json_response(body, status=status)
