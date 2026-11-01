"""Offene Daten herunterladen, zwischenspeichern und laden: Gleisnetz und Fahrplan.

Quelle: Releases von https://github.com/Sky7677/reiseanzeige-gleisnetz
  * Gleisnetz: monatlich aus OpenStreetMap (ODbL 1.0, © OpenStreetMap-Mitwirkende),
    Prüfung wöchentlich.
  * Fahrplan: täglich aus den GTFS-Feeds von gtfs.de (Daten DELFI e.V., CC BY 4.0),
    30 Tage voraus, Prüfung alle 12 Stunden.
Zuerst wird nur das kleine Manifest geholt (Version, Größe, SHA-256); die Datei
selbst nur bei neuer Version. Sie liegt gepackt in .storage und wird beim Start
geladen. Nach einem Fehlschlag frühestens nach 6 Stunden erneut; die bisherige
Fassung bleibt dann in Gebrauch. Ohne diese Daten läuft alles weiter, nur ohne
Streckenverlauf bzw. ohne Zwischenhalte vor dem Fahrplanabgleich.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_VERSION, USER_AGENT
from .core.places import Places, norm_key
from .core.railnet.fileformat import FORMAT_VERSION
from .core.railnet.plan import Timetable
from .core.railnet.router import RailNet

_LOGGER = logging.getLogger(__name__)

RELEASES = "https://github.com/Sky7677/reiseanzeige-gleisnetz/releases/latest/download/"
MANIFEST_URL = RELEASES + "manifest.json"
TT_RELEASES = "https://github.com/Sky7677/reiseanzeige-gleisnetz/releases/download/fahrplan/"
TT_MANIFEST_URL = TT_RELEASES + "fahrplan-manifest.json"
RETRY_AFTER = timedelta(hours=6)
MAX_SIZE = 400 * 1024 * 1024


class RailNetError(Exception):
    pass


def _read_gz(path: str) -> bytes:
    with open(path, "rb") as f:
        return gzip.decompress(f.read())


def _load_railnet(path: str) -> tuple[RailNet, Places]:
    """Datei laden und grob prüfen. Jeder Fehler hier heißt: Datei unbrauchbar."""
    net = RailNet(_read_gz(path))
    if net.header.get("format") != FORMAT_VERSION or len(net.e_from) != len(net.e_len) or \
            len(net.shape_start) != len(net.e_len) + 1 or len(net.adj_start) != len(net.node_lat) + 1:
        raise RailNetError("Gleisnetz-Datei passt nicht zum erwarteten Aufbau")
    return net, Places(net.stations, net.routes)


def _load_timetable(path: str) -> Timetable:
    tt = Timetable(_read_gz(path), key=norm_key)
    if len(tt.pat_start) != len(tt.pat_route) + 1 or len(tt.place_start) != len(tt.places) + 1 \
            or len(tt.pat_trip_start) != len(tt.pat_route) + 1:
        raise RailNetError("Fahrplan-Datei passt nicht zum erwarteten Aufbau")
    return tt


class DataStore:
    """Gemeinsamer Ablauf: Manifest prüfen, Datei mit Prüfsumme laden, ersetzen."""

    label = "Daten"
    kind: str | None = None
    releases = ""
    manifest_url = ""
    file_name = ""
    meta_key = ""
    check_every = timedelta(days=7)
    without = "ohne diese Daten"

    def __init__(self, hass: HomeAssistant, enabled: bool = True) -> None:
        self.hass = hass
        self.enabled = enabled
        self.path = hass.config.path(".storage", self.file_name)
        self._meta: Store[dict] = Store(hass, STORAGE_VERSION, self.meta_key)
        self.version: str | None = None
        self.manifest: dict | None = None
        self.checked_at: datetime | None = None
        self.failed_at: datetime | None = None   # letzter Fehlschlag: frühestens RETRY_AFTER danach erneut
        self.error: str | None = None
        self.listeners: list[Callable[[], object]] = []

    # --- je Art ---

    def _loader(self) -> Callable[[str], Any]:
        raise NotImplementedError

    def _apply(self, loaded: Any) -> None:
        raise NotImplementedError

    @property
    def loaded(self) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        return ""

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
                self._apply(await self.hass.async_add_executor_job(self._loader(), self.path))
                self.version = self.manifest.get("version")
                _LOGGER.info("%s %s geladen: %s", self.label, self.version, self.describe())
            except Exception as err:  # noqa: BLE001 – eine kaputte Datei darf den Start nie verhindern
                _LOGGER.warning("Gespeicherte Datei (%s) unbrauchbar, wird neu geladen: %s", self.label, err)
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
        return not self.loaded or self.checked_at is None or now - self.checked_at >= self.check_every

    async def refresh_if_due(self) -> bool:
        """Neue Version holen, falls fällig. True, wenn eine neue Fassung geladen wurde."""
        now = dt_util.now()
        if not self.due(now):
            return False
        session = async_get_clientsession(self.hass)
        headers = {"User-Agent": USER_AGENT}
        try:
            async with session.get(self.manifest_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as res:
                if res.status >= 400:
                    raise RailNetError(f"Manifest: HTTP {res.status}")
                manifest = await res.json(content_type=None)
            if not isinstance(manifest, dict):
                raise RailNetError("Manifest unlesbar")
            if manifest.get("format") != FORMAT_VERSION or (self.kind and manifest.get("kind", self.kind) != self.kind):
                raise RailNetError(f"Format {manifest.get('format')} wird von dieser Version nicht unterstützt – bitte die Integration aktualisieren")
            if self.loaded and manifest.get("version") == self.version and manifest.get("sha256") == (self.manifest or {}).get("sha256"):
                self.checked_at, self.error, self.failed_at = now, None, None
                await self._save()
                return False
            await self._download(session, headers, manifest)
            loaded = await self.hass.async_add_executor_job(self._loader(), self.path + ".part")
            await self.hass.async_add_executor_job(os.replace, self.path + ".part", self.path)
        except Exception as err:  # noqa: BLE001 – jeder Fehler: alte Fassung behalten, in 6 h erneut
            self.error, self.failed_at = str(err) or type(err).__name__, now
            _LOGGER.warning("%s nicht aktualisiert, %s: %s", self.label,
                            "bisherige Fassung bleibt in Gebrauch" if self.loaded else self.without, self.error)
            await self.hass.async_add_executor_job(self._remove_part)
            return False
        self._apply(loaded)
        self.manifest, self.version, self.checked_at = manifest, manifest.get("version"), now
        self.error, self.failed_at = None, None
        await self._save()
        _LOGGER.info("%s %s geladen (%s MB): %s", self.label, self.version,
                     round(manifest.get("size", 0) / 1e6, 1), self.describe())
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
            async with session.get(self.releases + name, headers=headers, timeout=timeout) as res:
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
        m = self.manifest or {}
        out = {
            "enabled": self.enabled,
            "loaded": self.loaded,
            "version": self.version,
            "source": m.get("source"),
            "counts": m.get("counts"),
            "checkedAt": self.checked_at.isoformat() if self.checked_at else None,
            "error": self.error,
        }
        if "window_start" in m:
            out["windowStart"] = m["window_start"]
        return out


class RailNetStore(DataStore):
    label = "Gleisnetz"
    kind = "railnet"
    releases = RELEASES
    manifest_url = MANIFEST_URL
    file_name = "reiseanzeige.gleisnetz.bin.gz"
    meta_key = "reiseanzeige.gleisnetz"
    check_every = timedelta(days=7)
    without = "Karte ohne Streckenverlauf"

    def __init__(self, hass: HomeAssistant, enabled: bool = True) -> None:
        super().__init__(hass, enabled)
        self.net: RailNet | None = None
        self.places: Places | None = None

    def _loader(self):
        return _load_railnet

    def _apply(self, loaded) -> None:
        self.net, self.places = loaded

    @property
    def loaded(self) -> bool:
        return self.net is not None

    def describe(self) -> str:
        return f"{len(self.net.e_len)} Kanten, {len(self.places)} Bahnhöfe mit Nummer" if self.net else ""


class TimetableStore(DataStore):
    label = "Fahrplan"
    kind = "timetable"
    releases = TT_RELEASES
    manifest_url = TT_MANIFEST_URL
    file_name = "reiseanzeige.fahrplan.bin.gz"
    meta_key = "reiseanzeige.fahrplan"
    check_every = timedelta(hours=12)
    without = "Zwischenhalte erst ab dem Fahrplanabgleich"

    def __init__(self, hass: HomeAssistant, enabled: bool = True) -> None:
        super().__init__(hass, enabled)
        self.timetable: Timetable | None = None

    def _loader(self):
        return _load_timetable

    def _apply(self, loaded) -> None:
        self.timetable = loaded

    @property
    def loaded(self) -> bool:
        return self.timetable is not None

    def describe(self) -> str:
        tt = self.timetable
        return f"{len(tt.trip_start)} Fahrten ab {tt.window_start}" if tt else ""
