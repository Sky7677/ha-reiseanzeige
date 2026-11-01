"""Kleine Hilfen ohne eigene Zuständigkeit."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN


def loaded_runtime(hass: HomeAssistant):
    """Laufzeit der (einzigen) geladenen Einrichtung oder None."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and getattr(entry, "runtime_data", None) is not None:
            return entry.runtime_data
    return None
