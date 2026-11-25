"""Config flow to configure."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from json.decoder import JSONDecodeError
from typing import TYPE_CHECKING, Any

import aiohttp
import async_timeout
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from tenacity import RetryError

from .const import (
    CONF_MAX_WATERING_DURATION,
    CONF_MAX_WATERING_VOLUME,
    CONF_WATERING_SAFETY_LIMITS,
    DEFAULT_NAME,
    DOMAIN,
    GW_IP,
    NAME,
    NATIVE_MAX_WATERING_DURATION,
    NATIVE_MAX_WATERING_VOLUME,
    TAP_ID,
)
from .linktap_local import LinktapLocal

if TYPE_CHECKING:
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

_LOGGER = logging.getLogger(__name__)

# Gateways advertise themselves over mDNS as "LinkTapGw_<gateway id>" once
# "Enable mDNS responder" is switched on in the gateway's admin page.
ZEROCONF_PREFIX = "linktapgw_"

# One DNS/mDNS label. Underscores are allowed because the gateway's own mDNS
# name contains one (LinkTapGw_XXXX.local).
_HOST_LABEL = re.compile(r"^[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$")

_CONNECT_ERRORS = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    JSONDecodeError,
    RetryError,
    KeyError,
    TypeError,
)


def _is_ipv4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def _validated_gateway_host(value: str) -> str | None:
    """Return a normalized IPv4 address or hostname, or None if invalid.

    URLs, ports, paths and IPv6 addresses are rejected: the gateway is always
    reached as http://<host>/api.shtml.
    """
    candidate = value.strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        return str(address) if address.version == 4 else None

    hostname = candidate.rstrip(".")
    if not hostname or len(hostname) > 253:
        return None
    labels = hostname.split(".")
    if not all(_HOST_LABEL.match(label) for label in labels):
        return None
    # An all-numeric name is a mistyped IPv4 address, not a hostname.
    if all(label.isdigit() for label in labels):
        return None
    return hostname


async def _async_fetch_gateway_id(host: str) -> str:
    """Ask the gateway at ``host`` for its ID; raises on any connection error."""
    linker = LinktapLocal()
    linker.set_ip(host)
    async with async_timeout.timeout(15):
        gw_id = await linker.get_gw_id()
    if not gw_id:
        raise KeyError("gw_id")
    return str(gw_id)


@config_entries.HANDLERS.register(DOMAIN)
class LinktapFlowHandler(config_entries.ConfigFlow):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Create the LinkTap options flow."""
        return LinktapOptionsFlowHandler()

    def __init__(self) -> None:
        self._discovered_host: str | None = None
        self._discovered_gw_id: str | None = None

    async def async_step_user(self, user_input=None):
        """Handle a flow start."""
        _LOGGER.debug(f"Starting async_step_user of {DEFAULT_NAME}")
        errors = {}

        if user_input is not None:
            host = _validated_gateway_host(user_input[GW_IP])
            if host is None:
                errors[GW_IP] = "invalid_gateway_ip"
            else:
                self._async_abort_entries_match({GW_IP: host})
                try:
                    gw_id = await _async_fetch_gateway_id(host)
                except _CONNECT_ERRORS as err:
                    _LOGGER.debug("Cannot reach LinkTap gateway at %s: %s", host, err)
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(gw_id)
                    self._abort_if_unique_id_configured(
                        updates={GW_IP: host}, reload_on_update=False
                    )
                    normalized_input = dict(user_input)
                    normalized_input[GW_IP] = host
                    return self.async_create_entry(
                        title=DEFAULT_NAME, data=normalized_input
                    )

        default = (user_input or {}).get(GW_IP, "")
        schema = vol.Schema({vol.Required(GW_IP, default=default): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo):
        """Handle a gateway found over mDNS."""
        instance = discovery_info.name.split(".", 1)[0]
        if not instance.lower().startswith(ZEROCONF_PREFIX):
            return self.async_abort(reason="not_linktap_device")
        host = str(discovery_info.host)
        if not _is_ipv4(host):
            return self.async_abort(reason="not_ipv4_address")

        # The address may belong to an entry created before entries were keyed
        # by gateway ID; that entry is migrated when it next loads.
        self._async_abort_entries_match({GW_IP: host})

        # Ask the gateway itself for its ID rather than trusting the mDNS name.
        # This also confirms the Local HTTP API is enabled.
        try:
            gw_id = await _async_fetch_gateway_id(host)
        except _CONNECT_ERRORS as err:
            _LOGGER.debug("Discovered LinkTap gateway %s not reachable: %s", host, err)
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(gw_id)
        # Follow a DHCP address change, but never replace an address the user
        # entered as a hostname. The entry's update listener reloads it.
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, gw_id
        )
        updates = None
        if existing is not None and _is_ipv4(existing.data.get(GW_IP, "")):
            updates = {GW_IP: host}
        self._abort_if_unique_id_configured(updates=updates, reload_on_update=False)

        self._discovered_host = host
        self._discovered_gw_id = gw_id
        self.context["title_placeholders"] = {"name": f"LinkTap gateway {gw_id}"}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input=None):
        """Confirm adding a discovered gateway."""
        if user_input is not None:
            return self.async_create_entry(
                title=DEFAULT_NAME, data={GW_IP: self._discovered_host}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "gw_id": self._discovered_gw_id,
                "host": self._discovered_host,
            },
        )


class LinktapOptionsFlowHandler(OptionsFlow):
    """Configure optional per-device instant-watering safety ceilings.

    The integration already registers a ConfigEntry update listener in __init__.py
    which reloads the entry after options change. Use plain OptionsFlow here so
    the same change is not also reloaded by OptionsFlowWithReload.
    """

    def __init__(self) -> None:
        self._tap_id: str | None = None
        self._tap_name: str | None = None

    def _entry_conf(self) -> dict[str, Any]:
        """Return the loaded runtime configuration for this config entry."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(
            self.config_entry.entry_id, {}
        )
        return entry_data.get("conf", {})

    def _taps(self) -> list[dict[str, Any]]:
        """Return taps currently loaded for this config entry."""
        return self._entry_conf().get("taps", [])

    def _volume_unit(self) -> str:
        """Return the gateway's configured native volume unit."""
        return self._entry_conf().get("vol_unit", "L")

    def _ha_device_name(self, tap: dict[str, Any]) -> str:
        """Return the Home Assistant device name, falling back to gateway name."""
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, tap[TAP_ID])}
        )
        if device is not None:
            return device.name_by_user or device.name or tap[NAME]
        return tap[NAME]

    async def async_step_init(self, user_input=None):
        """Choose a tap when needed, then configure its limits."""
        taps = self._taps()
        if not taps:
            return self.async_abort(reason="integration_not_loaded")

        if len(taps) == 1:
            self._tap_id = taps[0][TAP_ID]
            self._tap_name = self._ha_device_name(taps[0])
            return await self.async_step_limits()

        if user_input is not None:
            self._tap_id = user_input["tap_id"]
            selected = next(
                (tap for tap in taps if tap[TAP_ID] == self._tap_id),
                None,
            )
            if selected is None:
                return self.async_abort(reason="tap_not_found")
            self._tap_name = self._ha_device_name(selected)
            return await self.async_step_limits()

        options = [
            selector.SelectOptionDict(
                value=tap[TAP_ID],
                label=self._ha_device_name(tap),
            )
            for tap in taps
        ]
        schema = vol.Schema(
            {
                vol.Required("tap_id"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_limits(self, user_input=None):
        """Configure independent optional safety ceilings for one tap."""
        if self._tap_id is None:
            return await self.async_step_init()

        all_limits = dict(
            self.config_entry.options.get(CONF_WATERING_SAFETY_LIMITS, {})
        )
        current = dict(all_limits.get(self._tap_id, {}))

        if user_input is not None:
            updated = {}
            if CONF_MAX_WATERING_DURATION in user_input:
                updated[CONF_MAX_WATERING_DURATION] = int(
                    user_input[CONF_MAX_WATERING_DURATION]
                )
            if CONF_MAX_WATERING_VOLUME in user_input:
                updated[CONF_MAX_WATERING_VOLUME] = int(
                    user_input[CONF_MAX_WATERING_VOLUME]
                )

            if updated:
                all_limits[self._tap_id] = updated
            else:
                all_limits.pop(self._tap_id, None)

            new_options = dict(self.config_entry.options)
            if all_limits:
                new_options[CONF_WATERING_SAFETY_LIMITS] = all_limits
            else:
                new_options.pop(CONF_WATERING_SAFETY_LIMITS, None)

            # Existing integration update listener reloads the entry.
            return self.async_create_entry(data=new_options)

        schema = vol.Schema(
            {
                vol.Optional(CONF_MAX_WATERING_DURATION): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5,
                        max=NATIVE_MAX_WATERING_DURATION,
                        step=5,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Optional(CONF_MAX_WATERING_VOLUME): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10,
                        max=NATIVE_MAX_WATERING_VOLUME,
                        step=10,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement=self._volume_unit(),
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="limits",
            data_schema=self.add_suggested_values_to_schema(schema, current),
            description_placeholders={
                "tap_name": self._tap_name or self._tap_id,
            },
        )
