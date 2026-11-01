"""Kartenkacheln über den Proxy: Abruf bei der OSMF mit eigener Kennung, Zwischenspeicher."""

from custom_components.reiseanzeige.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_vektorkachel_und_schrift(hass, iris, hass_client_no_auth, aioclient_mock):
    aioclient_mock.get("https://vector.openstreetmap.org/shortbread_v1/5/16/10.mvt", content=b"TILE")
    aioclient_mock.get("https://vector.openstreetmap.org/styles/shortbread/fonts/noto_sans_regular/0-255.pbf", content=b"FONT")
    entry = MockConfigEntry(domain=DOMAIN, data={"client_id": "cid", "api_key": "key"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    base = f"/api/{DOMAIN}/{entry.runtime_data.map_token}/"
    client = await hass_client_no_auth()
    r = await client.get(f"{base}map/vector/5/16/10.mvt")
    assert r.status == 200, await r.text()
    assert await r.read() == b"TILE"
    assert r.headers["Content-Type"].startswith("application/vnd.mapbox-vector-tile")
    r2 = await client.get(f"{base}map/vector/5/16/10.mvt")
    assert r2.status == 200 and aioclient_mock.call_count == 1, "zweiter Abruf aus dem Zwischenspeicher"
    r3 = await client.get(f"{base}map/fonts/noto_sans_regular/0-255.pbf")
    assert r3.status == 200
    ua = aioclient_mock.mock_calls[0][3]
    assert ua and "HA-Reiseanzeige" in ua.get("User-Agent", "")
