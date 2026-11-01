"""Kartenproxy: Vektor- und Rasterkacheln sowie Schriften der OpenStreetMap
Foundation für die Dashboard-Karte.

Gründe wie beim Kartenproxy, den Home Assistant seit 2026.9 selbst mitbringt:
Die OSMF verlangt erkennbare Anfragen (User-Agent), die ein Browser nicht
senden kann, und Kacheln sollen mindestens eine Woche zwischengespeichert
werden. Sobald Home Assistants eigener Proxy verfügbar ist, kann dieser hier
entfallen.

Zugriff: Die Adresse enthält einen Zufallsschlüssel, den nur angemeldete
Benutzer über die WebSocket-Verbindung erhalten (wie das access_token bei
Kamerabildern). Ohne passenden Schlüssel gibt es 404 – die Instanz wird so
nicht zum offenen Kachel-Proxy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import OrderedDict
from pathlib import Path

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, USER_AGENT
from .helpers import loaded_runtime

_LOGGER = logging.getLogger(__name__)

VECTOR_URL = "https://vector.openstreetmap.org"
RASTER_URL = "https://tile.openstreetmap.org"
TILE_MAX_AGE = 7 * 24 * 60 * 60
ASSET_MAX_AGE = 30 * 24 * 60 * 60
CACHE_MAX_BYTES = 32 * 1024 * 1024
MAX_FETCH_BYTES = 8 * 1024 * 1024
STYLES_DIR = Path(__file__).parent / "map_styles"

ROUTES = [
    (re.compile(r"^vector/(\d{1,2})/(\d+)/(\d+)\.mvt$"), lambda m: f"{VECTOR_URL}/shortbread_v1/{m[1]}/{m[2]}/{m[3]}.mvt", "application/vnd.mapbox-vector-tile", TILE_MAX_AGE),
    (re.compile(r"^raster/(\d{1,2})/(\d+)/(\d+)\.png$"), lambda m: f"{RASTER_URL}/{m[1]}/{m[2]}/{m[3]}.png", "image/png", TILE_MAX_AGE),
    (re.compile(r"^fonts/([a-z0-9_,]{1,100})/(\d{1,5}-\d{1,5})\.pbf$"), lambda m: f"{VECTOR_URL}/styles/shortbread/fonts/{m[1]}/{m[2]}.pbf", "application/x-protobuf", ASSET_MAX_AGE),
]
STYLE_RE = re.compile(r"^style/([a-z]+-(?:light|dark))\.json$")


class ByteLRU:
    """LRU nach Bytes; abgelaufene Einträge bleiben als Reserve bei Ausfällen."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes, self.bytes = max_bytes, 0
        self._d: OrderedDict[str, tuple[float, bytes]] = OrderedDict()

    def get(self, key: str):
        e = self._d.get(key)
        if e:
            self._d.move_to_end(key)
        return e

    def set(self, key: str, at: float, body: bytes) -> None:
        old = self._d.pop(key, None)
        if old:
            self.bytes -= len(old[1])
        self._d[key] = (at, body)
        self.bytes += len(body)
        while self.bytes > self.max_bytes and self._d:
            _, (_, b) = self._d.popitem(last=False)
            self.bytes -= len(b)


class MapProxyView(HomeAssistantView):
    url = f"/api/{DOMAIN}/{{token}}/map/{{path:.+}}"
    name = f"api:{DOMAIN}:map"
    requires_auth = False  # Schutz über den Schlüssel in der Adresse

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.cache = ByteLRU(CACHE_MAX_BYTES)
        self._pending: dict[str, asyncio.Future] = {}

    def _token_ok(self, token: str) -> bool:
        rt = loaded_runtime(self.hass)
        return rt is not None and secrets.compare_digest(token, rt.map_token)

    async def _fetch(self, url: str) -> tuple[int, bytes]:
        session = async_get_clientsession(self.hass)
        async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=10)) as res:
            if res.status == 404:
                return 404, b""
            if res.status >= 400:
                raise aiohttp.ClientError(f"HTTP {res.status}")
            # Vollständig lesen, mit Obergrenze. (content.read(n) liefert nur das
            # erste verfügbare Stück – das ergab abgeschnittene Kacheln.)
            chunks, size = [], 0
            async for chunk in res.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_FETCH_BYTES:
                    raise aiohttp.ClientError("Antwort zu groß")
                chunks.append(chunk)
            return 200, b"".join(chunks)

    async def _serve(self, key: str, url: str, max_age: int):
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < max_age:
            return 200, hit[1]
        fut = self._pending.get(key)
        if fut is None:
            fut = asyncio.ensure_future(self._fetch(url))
            self._pending[key] = fut
            fut.add_done_callback(lambda _f: self._pending.pop(key, None))
        try:
            status, body = await asyncio.shield(fut)
        except (aiohttp.ClientError, TimeoutError) as err:
            if hit:
                return 200, hit[1]
            _LOGGER.debug("Karte: %s nicht abrufbar (%s)", url, err)
            return 502, b""
        if status == 200:
            self.cache.set(key, time.time(), body)
        return status, body

    async def get(self, request: web.Request, token: str, path: str) -> web.Response:
        if not self._token_ok(token):
            return web.Response(status=404)
        m = STYLE_RE.match(path)
        if m:
            file = STYLES_DIR / f"{m[1]}.json"
            if not file.is_file():
                return web.Response(status=404)
            body = await self.hass.async_add_executor_job(file.read_bytes)
            return web.Response(body=body, content_type="application/json", headers={"Cache-Control": "private, max-age=3600"})
        for regex, upstream, ctype, max_age in ROUTES:
            mm = regex.match(path)
            if not mm:
                continue
            status, body = await self._serve(path, upstream(mm), max_age)
            if status != 200:
                return web.Response(status=status)
            return web.Response(body=body, content_type=ctype, headers={"Cache-Control": f"private, max-age={max_age}"})
        return web.Response(status=404)
