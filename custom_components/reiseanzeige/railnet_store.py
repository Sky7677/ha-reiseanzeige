"""Gleisnetz herunterladen, zwischenspeichern und laden.

Quelle: Releases von https://github.com/Sky7677/reiseanzeige-gleisnetz, monatlich
aus OpenStreetMap gebaut (Daten: ODbL 1.0, © OpenStreetMap-Mitwirkende).
Zuerst wird nur das kleine Manifest geholt (Version, Größe, SHA-256); die Datei
selbst nur bei neuer Version. Sie liegt gepackt in .storage und wird beim Start
geladen. Prüfung wöchentlich, nach einem Fehlschlag frühestens nach 6 Stunden.
Ohne Gleisnetz läuft alles weiter, nur ohne Streckenverlauf.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Callable

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_VERSION, USER_AGENT
from .core.places import Places
from .core.railnet.fileformat import FORMAT_VERSION
from .core.railnet.router import RailNet

_LOGGER = logging.getLogger(__name__)

RELEASES = "https://github.com/Sky7677/reiseanzeige-gleisnetz/releases/latest/download/"
MANIFEST_URL = RELEASES + "manifest.json"
CHECK_EVERY = timedelta(days=7)
RETRY_AFTER = timedelta(hours=6)
MAX_SIZE = 400 * 1024 * 1024
FILE_NAME = "reiseanzeige.gleisnetz.bin.gz"


def _load(path: str) -> tuple[RailNet, Places]:
    """Datei laden und grob prüfen. Jeder Fehler hier heißt: Datei unbrauchbar."""
    with open(path, "rb") as f:
        data = gzip.decompress(f.read())
    net = RailNet(data)
    if net.header.get("format") != FORMAT_VERSION or len(net.e_from) != len(net.e_len) or \
            len(net.shape_start) != len(net.e_len) + 1 or len(net.adj_start) != len(net.node_lat) + 1:
        raise RailNetError("Gleisnetz-Datei passt nicht zum erwarteten Aufbau")
    return net, Places(net.stations, net.routes)


class RailNetStore:
    def __init__(self, hass: HomeAssistant, enabled: bool = True) -> None:
        self.hass = hass
        self.enabled = enabled
        self.path = hass.config.path(".storage", FILE_NAME)
        self._meta: Store[dict] = Store(hass, STORAGE_VERSION, "reiseanzeige.gleisnetz")
        self.net: RailNet | None = None
        self.places: Places | None = None
        self.version: str | None = None
        self.manifest: dict | None = None
        self.checked_at: datetime | None = None
        self.failed_at: datetime | None = None   # letzter Fehlschlag: frühestens RETRY_AFTER danach erneut
        self.error: str | None = None
        self.listeners: list[Callable[[], object]] = []

    # --- Start ---

    async def load(self) -> None:
        meta = await self._meta.async_load() or {}
        self.manifest = meta.get("manifest")
        self.checked_at = dt_util.parse_datetime(meta["checkedAt"]) if meta.get("checkedAt") else None
        if not self.enabled:
            await self.hass.async_add_executor_job(self._remove)
            return
        if self.manifest and await self.hass.async_add_executor_job(os.path.exists, self.path):
            try:
                self.net, self.places = await self.hass.async_add_executor_job(_load, self.path)
                self.version = self.manifest.get("version")
                _LOGGER.info("Gleisnetz %s geladen: %s Kanten, %s Bahnhöfe mit Nummer",
                             self.version, len(self.net.e_len), len(self.places))
            except Exception as err:  # noqa: BLE001 – eine kaputte Datei darf den Start nie verhindern
                _LOGGER.warning("Gespeichertes Gleisnetz unbrauchbar, wird neu geladen: %s", err)
                await self.hass.async_add_executor_job(self._remove)
                self.manifest, self.checked_at = None, None

    def _remove(self) -> None:
        for p in (self.path, self.path + ".part"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    # --- Aktualisieren ---

    def due(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        if self.failed_at and now - self.failed_at < RETRY_AFTER:
            return False
        return self.net is None or self.checked_at is None or now - self.checked_at >= CHECK_EVERY

    async def refresh_if_due(self) -> bool:
        """Neue Version holen, falls fällig. True, wenn ein neues Netz geladen wurde."""
        now = dt_util.now()
        if not self.due(now):
            return False
        session = async_get_clientsession(self.hass)
        headers = {"User-Agent": USER_AGENT}
        try:
            async with session.get(MANIFEST_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as res:
                if res.status >= 400:
                    raise RailNetError(f"Manifest: HTTP {res.status}")
                manifest = await res.json(content_type=None)
            if not isinstance(manifest, dict):
                raise RailNetError("Manifest unlesbar")
            if manifest.get("format") != FORMAT_VERSION:
                raise RailNetError(f"Format {manifest.get('format')} wird von dieser Version nicht unterstützt – bitte die Integration aktualisieren")
            if self.net is not None and manifest.get("version") == self.version:
                self.checked_at, self.error, self.failed_at = now, None, None
                await self._save()
                return False
            await self._download(session, headers, manifest)
            net, places = await self.hass.async_add_executor_job(_load, self.path + ".part")
            await self.hass.async_add_executor_job(os.replace, self.path + ".part", self.path)
        except Exception as err:  # noqa: BLE001 – jeder Fehler: altes Netz behalten, in 6 h erneut
            self.error, self.failed_at = str(err) or type(err).__name__, now
            _LOGGER.warning("Gleisnetz nicht aktualisiert, %s: %s", "bisheriges bleibt in Gebrauch" if self.net else "Karte ohne Streckenverlauf", self.error)
            await self.hass.async_add_executor_job(self._remove_part)
            return False
        self.net, self.places = net, places
        self.manifest, self.version, self.checked_at = manifest, manifest.get("version"), now
        self.error, self.failed_at = None, None
        await self._save()
        _LOGGER.info("Gleisnetz %s geladen (%s MB): %s Kanten, %s Bahnhöfe mit Nummer",
                     self.version, round(manifest.get("size", 0) / 1e6, 1), len(net.e_len), len(places))
        for listener in self.listeners:
            await listener()
        return True

    async def _download(self, session: aiohttp.ClientSession, headers: dict, manifest: dict) -> None:
        name = os.path.basename(str(manifest.get("file") or ""))
        size, digest = manifest.get("size"), manifest.get("sha256")
        if not name or not isinstance(size, int) or not 0 < size <= MAX_SIZE or not digest:
            raise RailNetError("Manifest unvollständig")
        part = self.path + ".part"
        h = hashlib.sha256()
        total = 0
        await self.hass.async_add_executor_job(lambda: os.makedirs(os.path.dirname(part), exist_ok=True))
        f = await self.hass.async_add_executor_job(open, part, "wb")
        try:
            timeout = aiohttp.ClientTimeout(total=1800, sock_read=120)
            async with session.get(RELEASES + name, headers=headers, timeout=timeout) as res:
                if res.status >= 400:
                    raise RailNetError(f"{name}: HTTP {res.status}")
                buf = bytearray()
                async for chunk in res.content.iter_chunked(256 * 1024):
                    total += len(chunk)
                    if total > size:
                        raise RailNetError(f"{name}: größer als angekündigt")
                    h.update(chunk)
                    buf += chunk
                    if len(buf) >= 4 * 1024 * 1024:
                        await self.hass.async_add_executor_job(f.write, bytes(buf))
                        buf.clear()
                if buf:
                    await self.hass.async_add_executor_job(f.write, bytes(buf))
        finally:
            await self.hass.async_add_executor_job(f.close)
        if total != size or h.hexdigest() != digest:
            raise RailNetError(f"{name}: Prüfsumme oder Größe stimmt nicht")

    def _remove_part(self) -> None:
        try:
            os.remove(self.path + ".part")
        except FileNotFoundError:
            pass

    async def _save(self) -> None:
        await self._meta.async_save({
            "manifest": self.manifest,
            "checkedAt": self.checked_at.isoformat() if self.checked_at else None,
        })

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "loaded": self.net is not None,
            "version": self.version,
            "source": (self.manifest or {}).get("source"),
            "counts": (self.manifest or {}).get("counts"),
            "checkedAt": self.checked_at.isoformat() if self.checked_at else None,
            "error": self.error,
        }


class RailNetError(Exception):
    pass
