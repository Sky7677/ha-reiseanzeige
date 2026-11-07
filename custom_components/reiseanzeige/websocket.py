"""WebSocket-Befehle für die Karte.

Die Karte spricht über die angemeldete Verbindung des Home-Assistant-Frontends
mit der Integration; der Benutzer kommt automatisch mit (connection.user).
Bearbeiten (Sitzplatz, Löschen, Standort anfordern) darf jeder angemeldete
Benutzer – Entscheidung vom 02.10.2026.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .core.board import departures
from .core.timetables import TimetablesError
from .core.trips import TripError
from .helpers import loaded_runtime


def _runtime(hass: HomeAssistant):
    rt = loaded_runtime(hass)
    if rt is None:
        raise TripError("NOT_CONFIGURED", "Reiseanzeige ist nicht eingerichtet")
    return rt


def _error(connection, msg_id: int, err: Exception) -> None:
    code = getattr(err, "code", "unknown_error")
    connection.send_error(msg_id, str(code).lower(), str(err))


@callback
def async_register(hass: HomeAssistant) -> None:
    for handler in (ws_me, ws_trips, ws_trip, ws_add, ws_delete, ws_seat, ws_locate, ws_board, ws_map_base):
        websocket_api.async_register_command(hass, handler)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/me"})
@callback
def ws_me(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
    except TripError as err:
        return _error(connection, msg["id"], err)
    user = connection.user
    owner = rt.owners.get(user.id) or {}
    connection.send_result(msg["id"], {
        "id": user.id,
        "name": user.name,
        "configured": True,
        # "+ Reise" in der Karte nur für Personen mit Handy (wie bisher: der
        # Flurbildschirm teilt nicht). Das Handy wird beim ersten Teilen erkannt.
        "canAdd": bool(owner.get("notify")),
        "hasPhone": bool(owner.get("notify")),
        "station": rt.default_station,
        # Ohne Zugangsdaten der Bahn: keine Echtzeit, keine Abfahrtstafel
        "realtime": rt.client is not None,
    })


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/trips"})
@callback
def ws_trips(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
    except TripError as err:
        return _error(connection, msg["id"], err)
    connection.send_result(msg["id"], {"trips": [rt.trip_view(t, light=True) for t in rt.trips.list()]})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/trip", vol.Required("trip_id"): str})
@callback
def ws_trip(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
        connection.send_result(msg["id"], rt.trip_view(rt.trips.get(msg["trip_id"])))
    except TripError as err:
        _error(connection, msg["id"], err)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/add", vol.Required("text"): vol.All(str, vol.Length(max=8000))})
@websocket_api.async_response
async def ws_add(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
    except TripError as err:
        return _error(connection, msg["id"], err)
    result = await rt.add_text(connection.user.id, msg["text"])
    if not result["ok"]:
        connection.send_error(msg["id"], str(result.get("code", "invalid")).lower(), result["message"])
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/delete", vol.Required("trip_id"): str})
@websocket_api.async_response
async def ws_delete(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
        connection.send_result(msg["id"], await rt.trips.delete_trip(msg["trip_id"]))
    except TripError as err:
        _error(connection, msg["id"], err)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/seat",
    vol.Required("trip_id"): str,
    vol.Required("leg"): vol.Coerce(int),
    vol.Optional("coach", default=""): str,
    vol.Optional("seat", default=""): str,
})
@websocket_api.async_response
async def ws_seat(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
        trip = await rt.trips.set_seat(msg["trip_id"], msg["leg"], msg["coach"], msg["seat"])
        connection.send_result(msg["id"], rt.trip_view(trip))
    except TripError as err:
        _error(connection, msg["id"], err)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/locate", vol.Required("trip_id"): str})
@websocket_api.async_response
async def ws_locate(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
        result = await rt.request_location(msg["trip_id"])
    except TripError as err:
        return _error(connection, msg["id"], err)
    if result["status"] == "cooldown":
        connection.send_result(msg["id"], result)  # Karte zeigt die Sperrzeit an
    elif result["status"] != "sent":
        connection.send_error(msg["id"], "not_available", "Standort lässt sich gerade nicht anfordern")
    else:
        connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/board", vol.Optional("station"): str})
@websocket_api.async_response
async def ws_board(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
    except TripError as err:
        return _error(connection, msg["id"], err)
    if rt.client is None:
        connection.send_result(msg["id"], {"status": "no-credentials"})
        return
    station = msg.get("station") or rt.default_station
    if not station:
        connection.send_result(msg["id"], {"status": "no-station"})
        return
    try:
        board = await departures(rt.client, station, dt_util.now(), replacements=rt.board_replacements, locate=rt.locate_path)
        connection.send_result(msg["id"], {"status": "ok", **board})
    except TimetablesError as err:
        connection.send_result(msg["id"], {"status": "error", "code": err.code, "message": str(err)})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/map_base"})
@callback
def ws_map_base(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
    except TripError as err:
        return _error(connection, msg["id"], err)
    connection.send_result(msg["id"], {"base": f"/api/{DOMAIN}/{rt.map_token}/"})
