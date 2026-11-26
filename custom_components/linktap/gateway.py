"""The LinkTap gateway as a device of its own.

The gateway gets a device entry that the taps hang off (``via_device``), with
its firmware version and admin page. When the gateway's mDNS responder is
enabled, its advertised MAC address is added as a device connection, so
Home Assistant merges it with the same device seen by network integrations
such as UniFi. A few diagnostic entities show whether the gateway answers,
how many timers it currently reaches over RF, and the address in use.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from json.decoder import JSONDecodeError
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityCategory
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from tenacity import RetryError

from .const import DOMAIN, MANUFACTURER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

GATEWAY_MODEL = "Gateway"
GATEWAY_NAME = "LinkTap Gateway"
# Firmware and the device list rarely change; tap polls already show whether
# the gateway answers.
GATEWAY_CONFIG_INTERVAL = timedelta(minutes=30)
MDNS_SERVICE_TYPE = "_http._tcp.local."
MDNS_TIMEOUT_MS = 3000

_CONNECT_ERRORS = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    JSONDecodeError,
    RetryError,
    KeyError,
    TypeError,
)


def gateway_device_info(gw_id: str) -> DeviceInfo:
    """Device info that attaches an entity to the gateway device."""
    return DeviceInfo(identifiers={(DOMAIN, gw_id)})


class LinktapGatewayCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keeps the gateway's configuration (firmware, device list) current."""

    def __init__(
        self,
        hass: HomeAssistant,
        linker,
        gw_id: str,
        host: str,
        config: dict[str, Any],
        tap_coordinators: list[DataUpdateCoordinator],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} gateway {gw_id}",
            update_interval=GATEWAY_CONFIG_INTERVAL,
        )
        self.linker = linker
        self.gw_id = gw_id
        self.host = host
        self.tap_coordinators = tap_coordinators
        self.mdns: dict[str, Any] = {}
        # Seeded with the configuration read during setup.
        self.data = config

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.linker.get_gw_config(self.gw_id)
        except _CONNECT_ERRORS as err:
            raise UpdateFailed(f"Error reading LinkTap gateway config: {err}") from err

    @property
    def firmware(self) -> str | None:
        version = (self.data or {}).get("ver")
        return str(version) if version else None

    @property
    def reachable(self) -> bool:
        """Whether the gateway currently answers.

        Tap polls run every few seconds, so they show an outage long before
        the slow config refresh does.
        """
        if self.tap_coordinators:
            return any(c.last_update_success for c in self.tap_coordinators)
        return self.last_update_success

    def linked_taps(self) -> tuple[int, list[str]]:
        """Return how many taps the gateway reaches, and the ones it does not."""
        linked = 0
        unlinked = []
        for coordinator in self.tap_coordinators:
            data = coordinator.data or {}
            if coordinator.last_update_success and data.get("is_rf_linked"):
                linked += 1
            else:
                unlinked.append(coordinator.tap_id)
        return linked, unlinked


@callback
def async_register_gateway_device(
    hass: HomeAssistant, entry: ConfigEntry, gateway: LinktapGatewayCoordinator
) -> dr.DeviceEntry:
    """Create or update the gateway's device entry."""
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, gateway.gw_id)},
        manufacturer=MANUFACTURER,
        model=GATEWAY_MODEL,
        name=GATEWAY_NAME,
        serial_number=gateway.gw_id,
        sw_version=gateway.firmware,
        configuration_url=f"http://{gateway.host}/",
    )


@callback
def async_link_taps_to_gateway(
    hass: HomeAssistant, gateway_device: dr.DeviceEntry, tap_ids: list[str]
) -> None:
    """Show each tap as connected through the gateway."""
    registry = dr.async_get(hass)
    for tap_id in tap_ids:
        device = registry.async_get_device(identifiers={(DOMAIN, tap_id)})
        if device is not None and device.via_device_id != gateway_device.id:
            registry.async_update_device(device.id, via_device_id=gateway_device.id)


@callback
def async_sync_firmware(
    hass: HomeAssistant, gateway: LinktapGatewayCoordinator
) -> None:
    """Update the device's firmware version when the gateway reports a new one."""
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, gateway.gw_id)})
    if (
        device is not None
        and gateway.firmware
        and device.sw_version != gateway.firmware
    ):
        registry.async_update_device(device.id, sw_version=gateway.firmware)


async def async_lookup_mdns(hass: HomeAssistant, gw_id: str) -> dict[str, Any]:
    """Read the gateway's mDNS record: its TXT properties, host name and IPs.

    Returns an empty dict when zeroconf is unavailable or the gateway's mDNS
    responder is off.
    """
    try:
        from homeassistant.components import zeroconf
        from zeroconf.asyncio import AsyncServiceInfo

        aiozc = await zeroconf.async_get_async_instance(hass)
    except Exception as err:  # noqa: BLE001 - zeroconf is optional here
        _LOGGER.debug("Zeroconf unavailable for LinkTap gateway lookup: %s", err)
        return {}

    info = AsyncServiceInfo(MDNS_SERVICE_TYPE, f"LinkTapGw_{gw_id}.{MDNS_SERVICE_TYPE}")
    try:
        found = await info.async_request(aiozc.zeroconf, MDNS_TIMEOUT_MS)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("mDNS lookup of LinkTap gateway %s failed: %s", gw_id, err)
        return {}
    if not found:
        _LOGGER.debug("LinkTap gateway %s did not answer over mDNS", gw_id)
        return {}

    properties = {}
    for key, value in (info.properties or {}).items():
        if value is None:
            continue
        key = key.decode() if isinstance(key, bytes) else str(key)
        value = value.decode() if isinstance(value, bytes) else str(value)
        properties[key] = value
    return {
        "properties": properties,
        "hostname": info.server.rstrip(".") if info.server else None,
        "addresses": info.parsed_addresses(),
    }


def mac_from_mdns(mdns: dict[str, Any]) -> str | None:
    """Return the gateway's MAC address from its TXT record, if valid."""
    properties = mdns.get("properties") or {}
    raw = properties.get("MAC") or properties.get("mac")
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isalnum()).lower()
    if len(digits) != 12 or any(ch not in "0123456789abcdef" for ch in digits):
        return None
    return dr.format_mac(":".join(digits[i : i + 2] for i in range(0, 12, 2)))


async def async_add_mdns_details(
    hass: HomeAssistant, gateway: LinktapGatewayCoordinator, device_id: str
) -> None:
    """Add the MAC from mDNS to the gateway device, so it merges with UniFi etc."""
    gateway.mdns = await async_lookup_mdns(hass, gateway.gw_id)
    mac = mac_from_mdns(gateway.mdns)
    if mac is None:
        return
    registry = dr.async_get(hass)
    if registry.async_get(device_id) is None:
        return
    registry.async_update_device(
        device_id, merge_connections={(dr.CONNECTION_NETWORK_MAC, mac)}
    )
    gateway.async_update_listeners()


class LinktapGatewayEntity(Entity):
    """Base for diagnostic entities on the gateway device.

    Listens to the gateway coordinator and to every tap coordinator, since
    the tap polls carry reachability and RF link state.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, gateway: LinktapGatewayCoordinator, key: str) -> None:
        self.gateway = gateway
        self._attr_unique_id = f"{gateway.gw_id}_gateway_{key}"
        self._attr_device_info = gateway_device_info(gateway.gw_id)

    async def async_added_to_hass(self) -> None:
        for coordinator in (self.gateway, *self.gateway.tap_coordinators):
            self.async_on_remove(coordinator.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class LinktapGatewayConnectivity(LinktapGatewayEntity, BinarySensorEntity):
    """On while the gateway answers Home Assistant."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Connectivity"

    def __init__(self, gateway: LinktapGatewayCoordinator) -> None:
        super().__init__(gateway, "connectivity")

    @property
    def is_on(self) -> bool:
        return self.gateway.reachable


class LinktapGatewayLinkedTaps(LinktapGatewayEntity, SensorEntity):
    """How many paired timers the gateway currently reaches over RF."""

    _attr_name = "Linked timers"
    _attr_icon = "mdi:access-point-network"

    def __init__(self, gateway: LinktapGatewayCoordinator) -> None:
        super().__init__(gateway, "linked_timers")

    @property
    def available(self) -> bool:
        return self.gateway.reachable

    @property
    def native_value(self) -> int:
        return self.gateway.linked_taps()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        linked, unlinked = self.gateway.linked_taps()
        return {
            "total": len(self.gateway.tap_coordinators),
            "unlinked": unlinked,
        }


class LinktapGatewayAddress(LinktapGatewayEntity, SensorEntity):
    """The address Home Assistant uses to reach the gateway."""

    _attr_name = "Address"
    _attr_icon = "mdi:ip-network"

    def __init__(self, gateway: LinktapGatewayCoordinator) -> None:
        super().__init__(gateway, "address")

    @property
    def native_value(self) -> str:
        return self.gateway.host

    @callback
    def _handle_update(self) -> None:
        # One gateway entity keeps the device's firmware version current.
        async_sync_firmware(self.hass, self.gateway)
        super()._handle_update()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mdns = self.gateway.mdns
        attributes: dict[str, Any] = {}
        if mdns.get("hostname"):
            attributes["mdns_hostname"] = mdns["hostname"]
        if mdns.get("addresses"):
            attributes["ip_addresses"] = mdns["addresses"]
        mac = mac_from_mdns(mdns)
        if mac:
            attributes["mac"] = mac
        return attributes
