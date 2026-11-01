"""Gemeinsames für die Tests in Home Assistant."""

from unittest.mock import patch

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.components.frontend import DATA_EXTRA_JS_URL_ES5, DATA_EXTRA_MODULE_URL, UrlManager

from fake_iris import november_world


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def fake_frontend(hass):
    """Frontend als geladen markieren (die Testumgebung hat dessen Dateien nicht)."""
    hass.config.components.add("frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *a: None, [])
    hass.data[DATA_EXTRA_JS_URL_ES5] = UrlManager(lambda *a: None, [])
    yield


@pytest.fixture(autouse=True)
async def http_like_home_assistant(hass):
    """Webserver wie im echten Betrieb: Home Assistant verhindert beim Start, dass
    der Router eingefroren wird, damit Integrationen auch später Adressen anmelden
    können (homeassistant/components/http: HomeAssistantHTTP.start). Der Testserver
    tut das nicht – je nach Umgebung schlug sonst register_view fehl."""
    assert await async_setup_component(hass, "http", {})
    hass.http.app._router.freeze = lambda: None  # noqa: SLF001
    yield


@pytest.fixture(autouse=True)
def no_railnet_download(request):
    """Kein Gleisnetz aus dem Netz laden – außer in Tests mit railnet_release."""
    if "railnet_release" in request.fixturenames:
        yield
        return
    with patch("custom_components.reiseanzeige.railnet_store.RailNetStore.refresh_if_due", return_value=False):
        yield


@pytest.fixture
def railnet_release(hass, aioclient_mock, tmp_path):
    """Release des Gleisnetzes wie auf GitHub: manifest.json und die gepackte Datei."""
    from types import SimpleNamespace

    from custom_components.reiseanzeige.railnet_store import MANIFEST_URL, RELEASES
    from railnet_fixture import release, world

    hass.config.config_dir = str(tmp_path)  # Datei landet in tmp_path/.storage
    rel = SimpleNamespace(mock=aioclient_mock, path=tmp_path / ".storage" / "reiseanzeige.gleisnetz.bin.gz")

    def publish(version="2026-11-02", corrupt=False):
        gz, manifest = release(world(), version)
        if corrupt:
            manifest["sha256"] = "0" * 64
        aioclient_mock.clear_requests()
        aioclient_mock.get(MANIFEST_URL, json=manifest)
        aioclient_mock.get(RELEASES + manifest["file"], content=gz)
        rel.manifest = manifest

    rel.publish = publish
    publish()
    return rel


@pytest.fixture
def iris():
    """Nachgebaute Timetables-API statt echter Anfragen."""
    world = november_world()
    with patch("custom_components.reiseanzeige.runtime.aiohttp_fetch", lambda session: world), \
         patch("custom_components.reiseanzeige.config_flow.aiohttp_fetch", lambda session: world), \
         patch("custom_components.reiseanzeige.stations.StationDirectory.refresh_if_due", return_value=None):
        yield world
