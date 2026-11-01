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


@pytest.fixture
def iris():
    """Nachgebaute Timetables-API statt echter Anfragen."""
    world = november_world()
    with patch("custom_components.reiseanzeige.runtime.aiohttp_fetch", lambda session: world), \
         patch("custom_components.reiseanzeige.config_flow.aiohttp_fetch", lambda session: world), \
         patch("custom_components.reiseanzeige.stations.StationDirectory.refresh_if_due", return_value=None):
        yield world
