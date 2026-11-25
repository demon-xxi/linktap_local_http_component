"""Receive local status pushes from LinkTap gateways.

The gateway's "HTTP Client" setting POSTs a JSON frame to a configured Server
URL whenever an end device changes state, and periodically as a keepalive.
Configure it as ``http://<home-assistant>:8123/api/linktap/push``.

Status pushes arrive either flat (the same shape as a cmd 3 poll reply) or as
a frame with the device status nested under ``dev_stat``, which may be a single
object or a list. Handshake (cmd 0) and date/time sync (cmd 13) frames expect
the current local date and time in the reply.

Polling is never disabled. Pushes only make updates faster and let the
coordinator poll less often while they keep arriving.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DATETIME_SYNC_CMD, HANDSHAKE_CMD, PUSH_DATA, PUSH_URL, STATUS_CMD

if TYPE_CHECKING:
    from . import LinktapCoordinator

try:  # The http component is a manifest dependency, but tests may run without it.
    from homeassistant.components.http import HomeAssistantView
except ImportError:  # pragma: no cover
    HomeAssistantView = object  # type: ignore[assignment,misc]

_LOGGER = logging.getLogger(__name__)

_GATEWAYS = "gateways"
_VIEW_REGISTERED = "view_registered"
# Frames the gateway sends for a status update. 103 is the unsolicited variant
# used by the MQTT protocol; accept it here too in case firmware reuses it.
_STATUS_CMDS = {STATUS_CMD, 103}


@callback
def async_register_gateway(
    hass: HomeAssistant,
    gw_id: str,
    addresses: set[str],
    coordinators: dict[str, "LinktapCoordinator"],
) -> None:
    """Route pushes from ``gw_id`` to its tap coordinators.

    ``addresses`` are the source IPs a push from this gateway may come from.
    Registers the HTTP view on first use.
    """
    push_data = hass.data.setdefault(PUSH_DATA, {_GATEWAYS: {}})
    push_data[_GATEWAYS][gw_id] = {
        "addresses": set(addresses),
        "coordinators": dict(coordinators),
    }

    if push_data.get(_VIEW_REGISTERED):
        return
    http = getattr(hass, "http", None)
    if http is None:
        _LOGGER.debug("HTTP component not loaded; LinkTap push endpoint not registered")
        return
    http.register_view(LinktapPushView())
    push_data[_VIEW_REGISTERED] = True


@callback
def async_unregister_gateway(hass: HomeAssistant, gw_id: str) -> None:
    """Stop routing pushes for ``gw_id``. The view itself stays registered."""
    push_data = hass.data.get(PUSH_DATA)
    if push_data:
        push_data[_GATEWAYS].pop(gw_id, None)


def extract_statuses(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """Return per-device status dicts from a push frame, in either shape."""
    raw = frame.get("dev_stat")
    if raw is None:
        statuses = [frame] if "dev_id" in frame else []
    elif isinstance(raw, dict):
        statuses = [raw]
    elif isinstance(raw, list):
        statuses = [item for item in raw if isinstance(item, dict)]
    else:
        statuses = []

    gw_id = frame.get("gw_id")
    result = []
    for status in statuses:
        status = dict(status)
        status.pop("cmd", None)
        status.pop("ret", None)
        if gw_id is not None:
            status.setdefault("gw_id", gw_id)
        result.append(status)
    return result


def datetime_reply(cmd: int, gw_id: str) -> dict[str, Any]:
    """Build the date/time reply the gateway expects for cmd 0 and cmd 13."""
    now = dt_util.now()
    return {
        "cmd": cmd,
        "gw_id": gw_id,
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "wday": now.isoweekday(),
    }


class LinktapPushView(HomeAssistantView):
    """Accept status pushes from configured LinkTap gateways."""

    url = PUSH_URL
    name = "api:linktap:push"
    # The gateway cannot send a Home Assistant token. Instead, a frame is only
    # acted on when its gw_id belongs to a configured gateway and it was sent
    # from that gateway's address. Pushes can only update reported status;
    # they cannot start or stop watering.
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Handle one frame from a gateway."""
        hass: HomeAssistant = request.app["hass"]
        try:
            frame = json.loads(await request.text())
        except ValueError:
            _LOGGER.debug("Ignoring LinkTap push with invalid JSON")
            return web.Response(status=400)
        if not isinstance(frame, dict):
            return web.Response(status=400)

        gw_id = frame.get("gw_id")
        gateway = hass.data.get(PUSH_DATA, {}).get(_GATEWAYS, {}).get(gw_id)
        if gateway is None:
            _LOGGER.debug("Ignoring LinkTap push for unknown gateway %s", gw_id)
            return web.Response(status=202)

        if request.remote not in gateway["addresses"]:
            _LOGGER.warning(
                "Rejected LinkTap push for gateway %s from unexpected address %s",
                gw_id,
                request.remote,
            )
            return web.Response(status=403)

        cmd = frame.get("cmd", STATUS_CMD)
        if cmd in (HANDSHAKE_CMD, DATETIME_SYNC_CMD):
            return web.json_response(datetime_reply(cmd, gw_id))
        if cmd not in _STATUS_CMDS:
            _LOGGER.debug("Ignoring LinkTap push cmd %s from gateway %s", cmd, gw_id)
            return web.Response(status=200)

        for status in extract_statuses(frame):
            coordinator = gateway["coordinators"].get(status.get("dev_id"))
            if coordinator is None:
                _LOGGER.debug(
                    "Ignoring LinkTap push for unknown device %s on gateway %s",
                    status.get("dev_id"),
                    gw_id,
                )
                continue
            coordinator.async_handle_push(status)
        return web.Response(status=200)
