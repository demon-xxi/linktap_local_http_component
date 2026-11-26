"""Tests for the gateway device and its diagnostic entities."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.linktap import async_setup_entry
from custom_components.linktap.const import DOMAIN, GW_IP
from custom_components.linktap.diagnostics import async_get_config_entry_diagnostics
from custom_components.linktap.gateway import (
    CONF_MAC,
    GATEWAY_MODEL,
    SETUP_MDNS_TIMEOUT_MS,
    LinktapGatewayAddress,
    LinktapGatewayConnectivity,
    LinktapGatewayCoordinator,
    LinktapGatewayLinkedTaps,
    async_add_mdns_details,
    async_gateway_mac,
    async_lookup_mdns,
    async_register_gateway_device,
    async_sync_firmware,
    mac_from_mdns,
)
from tests.conftest import (
    MOCK_GW_CONFIG,
    MOCK_GW_ID,
    MOCK_GW_IP,
    MOCK_TAP_ID,
    MOCK_TAP_NAME,
)

MAC = "02:4b:41:23:b2:39"


def _tap(tap_id, linked=True, ok=True):
    return SimpleNamespace(
        tap_id=tap_id,
        data={"is_rf_linked": linked},
        last_update_success=ok,
        async_add_listener=MagicMock(return_value=lambda: None),
    )


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={GW_IP: MOCK_GW_IP}, options={})
    entry.add_to_hass(hass)
    return entry


def _gateway(hass, taps=(), config=None, api=None):
    return LinktapGatewayCoordinator(
        hass,
        api or AsyncMock(),
        MOCK_GW_ID,
        MOCK_GW_IP,
        dict(config or MOCK_GW_CONFIG),
        list(taps),
    )


# ---------------------------------------------------------------------------
# MAC parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["02:4B:41:23:B2:39", "024B4123B239", "02-4b-41-23-b2-39"],
)
def test_mac_formats_are_normalised(raw):
    assert mac_from_mdns({"properties": {"MAC": raw}}) == MAC


@pytest.mark.parametrize(
    "mdns",
    [
        {},
        {"properties": {}},
        {"properties": {"MAC": "nope"}},
        {"properties": {"MAC": "02:4b:41"}},
    ],
)
def test_missing_or_bad_mac(mdns):
    assert mac_from_mdns(mdns) is None


# ---------------------------------------------------------------------------
# mDNS lookup
# ---------------------------------------------------------------------------


def _patch_zeroconf(found=True, error=None, instance_error=None):
    """Stand in for the zeroconf library and Home Assistant's zeroconf component.

    Neither is installed in the test environment; Home Assistant always ships
    both.
    """
    info = MagicMock()
    info.async_request = AsyncMock(return_value=found, side_effect=error)
    info.properties = {
        b"MAC": b"02:4B:41:23:B2:39",
        b"ID": MOCK_GW_ID.encode(),
        b"x": None,
    }
    info.server = f"LinkTapGw_{MOCK_GW_ID}.local."
    info.parsed_addresses.return_value = [MOCK_GW_IP]
    service_info = MagicMock(return_value=info)
    zc_asyncio = ModuleType("zeroconf.asyncio")
    zc_asyncio.AsyncServiceInfo = service_info
    ha_zeroconf = ModuleType("homeassistant.components.zeroconf")
    ha_zeroconf.async_get_async_instance = AsyncMock(
        return_value=MagicMock(), side_effect=instance_error
    )
    modules = {
        "zeroconf": ModuleType("zeroconf"),
        "zeroconf.asyncio": zc_asyncio,
        "homeassistant.components.zeroconf": ha_zeroconf,
    }
    return patch.dict(sys.modules, modules), service_info


async def test_lookup_reads_txt_hostname_and_addresses(hass):
    modules, service_info = _patch_zeroconf()
    with modules:
        mdns = await async_lookup_mdns(hass, MOCK_GW_ID)
    assert service_info.call_args.args[1] == f"LinkTapGw_{MOCK_GW_ID}._http._tcp.local."
    assert mdns == {
        "properties": {"MAC": "02:4B:41:23:B2:39", "ID": MOCK_GW_ID},
        "hostname": f"LinkTapGw_{MOCK_GW_ID}.local",
        "addresses": [MOCK_GW_IP],
    }


async def test_lookup_empty_when_gateway_silent(hass):
    modules, _ = _patch_zeroconf(found=False)
    with modules:
        assert await async_lookup_mdns(hass, MOCK_GW_ID) == {}


async def test_lookup_empty_on_error(hass):
    modules, _ = _patch_zeroconf(error=OSError("no multicast"))
    with modules:
        assert await async_lookup_mdns(hass, MOCK_GW_ID) == {}


async def test_lookup_empty_without_zeroconf(hass):
    modules, _ = _patch_zeroconf(instance_error=RuntimeError("not loaded"))
    with modules:
        assert await async_lookup_mdns(hass, MOCK_GW_ID) == {}


async def test_lookup_empty_when_zeroconf_not_installed(hass):
    with patch.dict(sys.modules, {"zeroconf": None, "zeroconf.asyncio": None}):
        assert await async_lookup_mdns(hass, MOCK_GW_ID) == {}


# ---------------------------------------------------------------------------
# Device registry
# ---------------------------------------------------------------------------


async def test_gateway_device_registered(hass, entry):
    device = async_register_gateway_device(hass, entry, _gateway(hass))
    assert device.identifiers == {(DOMAIN, MOCK_GW_ID)}
    assert device.model == GATEWAY_MODEL
    assert device.serial_number == MOCK_GW_ID
    assert device.sw_version == "4.38"
    assert device.configuration_url == f"http://{MOCK_GW_IP}/"


async def test_mdns_mac_added_as_connection(hass, entry):
    gateway = _gateway(hass)
    device = async_register_gateway_device(hass, entry, gateway)
    mdns = {
        "properties": {"MAC": "02:4B:41:23:B2:39"},
        "hostname": "h",
        "addresses": [],
    }
    with patch(
        "custom_components.linktap.gateway.async_lookup_mdns",
        AsyncMock(return_value=mdns),
    ):
        await async_add_mdns_details(hass, gateway, device.id)
    device = dr.async_get(hass).async_get(device.id)
    assert (dr.CONNECTION_NETWORK_MAC, MAC) in device.connections
    assert gateway.mdns == mdns


async def test_no_connection_without_mac(hass, entry):
    gateway = _gateway(hass)
    device = async_register_gateway_device(hass, entry, gateway)
    with patch(
        "custom_components.linktap.gateway.async_lookup_mdns",
        AsyncMock(return_value={}),
    ):
        await async_add_mdns_details(hass, gateway, device.id)
    assert dr.async_get(hass).async_get(device.id).connections == set()


async def test_firmware_change_updates_device(hass, entry):
    gateway = _gateway(hass)
    device = async_register_gateway_device(hass, entry, gateway)
    gateway.data = {**MOCK_GW_CONFIG, "ver": "4.50"}
    async_sync_firmware(hass, gateway)
    assert dr.async_get(hass).async_get(device.id).sw_version == "4.50"


async def test_setup_creates_gateway_and_links_taps(hass, entry, mock_linktap_api):
    """Taps created during platform setup end up behind the gateway device."""
    registry = dr.async_get(hass)

    async def forward(entry_, platforms):
        registry.async_get_or_create(
            config_entry_id=entry_.entry_id,
            identifiers={(DOMAIN, MOCK_TAP_ID)},
            name=MOCK_TAP_NAME,
        )
        return True

    with patch(
        "custom_components.linktap.LinktapLocal", return_value=mock_linktap_api
    ), patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(side_effect=forward),
    ), patch(
        "custom_components.linktap.gateway.async_lookup_mdns",
        AsyncMock(return_value={}),
    ):
        assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    gateway_device = registry.async_get_device(identifiers={(DOMAIN, MOCK_GW_ID)})
    tap_device = registry.async_get_device(identifiers={(DOMAIN, MOCK_TAP_ID)})
    assert gateway_device is not None
    assert tap_device.via_device_id == gateway_device.id
    assert hass.data[DOMAIN][entry.entry_id]["conf"]["gateway"].tap_coordinators


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


async def test_config_refresh(hass):
    api = AsyncMock()
    api.get_gw_config.return_value = {**MOCK_GW_CONFIG, "ver": "5.0"}
    gateway = _gateway(hass, api=api)
    assert await gateway._async_update_data() == {**MOCK_GW_CONFIG, "ver": "5.0"}
    api.get_gw_config.assert_awaited_once_with(MOCK_GW_ID)


async def test_config_refresh_failure(hass):
    api = AsyncMock()
    api.get_gw_config.side_effect = aiohttp.ClientError
    with pytest.raises(UpdateFailed):
        await _gateway(hass, api=api)._async_update_data()


def test_reachable_follows_tap_polls(hass):
    assert _gateway(hass, [_tap("a", ok=False), _tap("b", ok=True)]).reachable
    assert not _gateway(hass, [_tap("a", ok=False)]).reachable


def test_reachable_without_taps_uses_config_refresh(hass):
    gateway = _gateway(hass)
    gateway.last_update_success = False
    assert not gateway.reachable


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def test_connectivity_entity(hass):
    gateway = _gateway(hass, [_tap("a", ok=True)])
    entity = LinktapGatewayConnectivity(gateway)
    assert entity.is_on
    assert entity.entity_category == EntityCategory.DIAGNOSTIC
    assert entity.unique_id == f"{MOCK_GW_ID}_gateway_connectivity"
    assert entity.device_info["identifiers"] == {(DOMAIN, MOCK_GW_ID)}


def test_linked_timers_entity(hass):
    taps = [_tap("a"), _tap("b", linked=False), _tap("c", ok=False)]
    entity = LinktapGatewayLinkedTaps(_gateway(hass, taps))
    assert entity.native_value == 1
    assert entity.extra_state_attributes == {"total": 3, "unlinked": ["b", "c"]}
    assert entity.available


def test_linked_timers_unavailable_when_gateway_unreachable(hass):
    entity = LinktapGatewayLinkedTaps(_gateway(hass, [_tap("a", ok=False)]))
    assert not entity.available


def test_address_entity(hass):
    gateway = _gateway(hass)
    entity = LinktapGatewayAddress(gateway)
    assert entity.native_value == MOCK_GW_IP
    assert entity.extra_state_attributes == {}
    gateway.mdns = {
        "properties": {"MAC": "024B4123B239"},
        "hostname": "LinkTapGw_X.local",
        "addresses": [MOCK_GW_IP],
    }
    assert entity.extra_state_attributes == {
        "mdns_hostname": "LinkTapGw_X.local",
        "ip_addresses": [MOCK_GW_IP],
        "mac": MAC,
    }


async def test_entities_listen_to_gateway_and_taps(hass):
    taps = [_tap("a"), _tap("b")]
    gateway = _gateway(hass, taps)
    entity = LinktapGatewayConnectivity(gateway)
    entity.hass = hass
    with patch.object(
        gateway, "async_add_listener", MagicMock(return_value=lambda: None)
    ) as gw_listen:
        await entity.async_added_to_hass()
    gw_listen.assert_called_once()
    for tap in taps:
        tap.async_add_listener.assert_called_once()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


async def test_diagnostics(hass, entry):
    tap_coordinator = _tap(MOCK_TAP_ID)
    gateway = _gateway(hass, [tap_coordinator])
    gateway.mdns = {"hostname": "LinkTapGw_X.local"}
    hass.data[DOMAIN] = {
        entry.entry_id: {
            "conf": {
                "ID of Gateway": MOCK_GW_ID,
                GW_IP: MOCK_GW_IP,
                "taps": [
                    {
                        "ID of Tap": MOCK_TAP_ID,
                        "Friendly Name of Tap": MOCK_TAP_NAME,
                        "coordinator": tap_coordinator,
                    }
                ],
                "gateway": gateway,
            },
        }
    }
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["address"] == MOCK_GW_IP
    assert diag["gateway"]["id"] == MOCK_GW_ID
    assert diag["gateway"]["reachable"] is True
    assert diag["gateway"]["config"]["ver"] == "4.38"
    assert diag["gateway"]["mdns"] == {"hostname": "LinkTapGw_X.local"}
    assert diag["taps"] == [
        {
            "id": MOCK_TAP_ID,
            "name": MOCK_TAP_NAME,
            "last_update_success": True,
            "status": {"is_rf_linked": True},
        }
    ]


# ---------------------------------------------------------------------------
# Joining a device another integration registered for the same MAC
# ---------------------------------------------------------------------------


async def test_gateway_joins_existing_network_device(hass, entry):
    """A UniFi-style client device with the same MAC becomes the gateway."""
    unifi_entry = MockConfigEntry(domain="unifi")
    unifi_entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    unifi_device = registry.async_get_or_create(
        config_entry_id=unifi_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        default_name="LinkTap_Gateway_B239",
    )

    device = async_register_gateway_device(hass, entry, _gateway(hass), MAC)

    assert device.id == unifi_device.id
    assert device.identifiers == {(DOMAIN, MOCK_GW_ID)}
    assert set(device.config_entries) == {entry.entry_id, unifi_entry.entry_id}
    assert device.name == "LinkTap Gateway"


async def test_stored_mac_is_used_without_lookup(hass, entry):
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MAC: MAC})
    lookup = AsyncMock()
    with patch("custom_components.linktap.gateway.async_lookup_mdns", lookup):
        assert await async_gateway_mac(hass, entry, _gateway(hass)) == MAC
    lookup.assert_not_called()


async def test_mac_found_over_mdns_is_stored(hass, entry):
    gateway = _gateway(hass)
    mdns = {
        "properties": {"MAC": "02:4B:41:23:B2:39"},
        "hostname": "h",
        "addresses": [],
    }
    with patch(
        "custom_components.linktap.gateway.async_lookup_mdns",
        AsyncMock(return_value=mdns),
    ) as lookup:
        assert await async_gateway_mac(hass, entry, gateway) == MAC
    assert lookup.await_args.args[2] == SETUP_MDNS_TIMEOUT_MS
    assert entry.data[CONF_MAC] == MAC
    assert gateway.mdns == mdns


async def test_no_mac_without_mdns(hass, entry):
    with patch(
        "custom_components.linktap.gateway.async_lookup_mdns",
        AsyncMock(return_value={}),
    ):
        assert await async_gateway_mac(hass, entry, _gateway(hass)) is None
    assert CONF_MAC not in entry.data


async def test_setup_joins_network_device_by_mac(hass, entry, mock_linktap_api):
    unifi_entry = MockConfigEntry(domain="unifi")
    unifi_entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    unifi_device = registry.async_get_or_create(
        config_entry_id=unifi_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )
    mdns = {"properties": {"MAC": MAC}, "hostname": "h", "addresses": [MOCK_GW_IP]}
    lookup = AsyncMock(return_value=mdns)
    with patch(
        "custom_components.linktap.LinktapLocal", return_value=mock_linktap_api
    ), patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    ), patch(
        "custom_components.linktap.gateway.async_lookup_mdns", lookup
    ):
        assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    gateway_device = registry.async_get_device(identifiers={(DOMAIN, MOCK_GW_ID)})
    assert gateway_device.id == unifi_device.id
    assert entry.data[CONF_MAC] == MAC
    # Found during setup, so no second lookup in the background.
    lookup.assert_awaited_once()
