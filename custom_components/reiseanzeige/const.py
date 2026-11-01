"""Konstanten der Integration Reiseanzeige."""

DOMAIN = "reiseanzeige"
VERSION = "0.4.0"

CONF_CLIENT_ID = "client_id"
CONF_API_KEY = "api_key"
CONF_STATION = "station"            # Standard-Bahnhof der Abfahrtstafel
CONF_GRACE_MINUTES = "grace_minutes"
CONF_REFRESH_SECONDS = "refresh_seconds"
CONF_RAILNET = "railnet"            # Streckenverlauf aus dem Gleisnetz (OpenStreetMap)
CONF_TIMETABLE = "timetable"        # Zwischenhalte aus dem Fahrplan (GTFS, gtfs.de)

DEFAULT_GRACE_MINUTES = 60
DEFAULT_REFRESH_SECONDS = 90
DEFAULT_RAILNET = True
DEFAULT_TIMETABLE = True

SHARE_EVENT = "mobile_app.share"
SIGNAL_TRIPS_CHANGED = f"{DOMAIN}_trips_changed"
SIGNAL_NEW_OWNER = f"{DOMAIN}_new_owner"

STORAGE_VERSION = 1
STORAGE_KEY_TRIPS = f"{DOMAIN}.trips"
STORAGE_KEY_OWNERS = f"{DOMAIN}.owners"

TICK_SECONDS = 15
NOTIFY_TITLE = "Reiseanzeige"
USER_AGENT = f"HA-Reiseanzeige/{VERSION} (Home Assistant integration)"

# Karte: Pfad der ausgelieferten Kartendatei
CARD_URL = f"/{DOMAIN}/reiseanzeige-card.js"
