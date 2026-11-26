"""Diagnostics for the LinkTap integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, GW_ID, GW_IP, NAME, TAP_ID


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the gateway's configuration and the last status of each tap.

    Nothing here is secret: the local API has no credentials.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    conf = entry_data.get("conf", {})
    gateway = conf.get("gateway")
    return {
        "entry": {
            "address": entry.data.get(GW_IP),
            "unique_id": entry.unique_id,
            "source": entry.source,
            "options": dict(entry.options),
        },
        "gateway": {
            "id": conf.get(GW_ID),
            "address": conf.get(GW_IP),
            "reachable": gateway.reachable if gateway else None,
            "config": gateway.data if gateway else None,
            "mdns": gateway.mdns if gateway else None,
        },
        "taps": [
            {
                "id": tap[TAP_ID],
                "name": tap[NAME],
                "last_update_success": tap["coordinator"].last_update_success,
                "status": tap["coordinator"].data,
            }
            for tap in conf.get("taps", [])
        ],
    }
