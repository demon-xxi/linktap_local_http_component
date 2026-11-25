"""Tests for the LinktapFlowHandler config flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.linktap.config_flow import _validated_gateway_host
from custom_components.linktap.const import DOMAIN, GW_IP

GW = "1A2B3C4D5E6F7788"
FETCH_ID = "custom_components.linktap.config_flow._async_fetch_gateway_id"
SETUP_ENTRY = "custom_components.linktap.async_setup_entry"

# ---------------------------------------------------------------------------
# _validated_gateway_host  (pure function, no HA needed)
# ---------------------------------------------------------------------------


class TestValidatedGatewayHost:
    def test_valid_ipv4_is_returned_unchanged(self):
        assert _validated_gateway_host("192.168.1.100") == "192.168.1.100"

    def test_leading_and_trailing_whitespace_is_stripped(self):
        assert _validated_gateway_host("  10.0.0.1  ") == "10.0.0.1"

    def test_ipv6_address_is_rejected(self):
        assert _validated_gateway_host("::1") is None

    def test_empty_string_returns_none(self):
        assert _validated_gateway_host("") is None

    @pytest.mark.parametrize(
        "host",
        ["myhub.local", "LinkTapGw_1A2B3C4D.local", "linktap", "gw-01.home.arpa"],
    )
    def test_hostnames_are_accepted(self, host):
        assert _validated_gateway_host(host) == host

    def test_trailing_dot_is_removed(self):
        assert _validated_gateway_host("myhub.local.") == "myhub.local"

    @pytest.mark.parametrize(
        "value",
        [
            "http://192.168.1.123",
            "192.168.1.123:80",
            "192.168.1.123/api.shtml",
            "myhub.local:80",
            "http://myhub.local",
            "my hub.local",
            "-bad.local",
            "a..b",
            "192.168.1",
            "999.1.1.1",
            "x" * 64 + ".local",
        ],
    )
    def test_urls_ports_paths_and_bad_names_are_rejected(self, value):
        assert _validated_gateway_host(value) is None


# ---------------------------------------------------------------------------
# User flow
# ---------------------------------------------------------------------------


async def _start_user_flow(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


class TestUserFlow:
    async def test_form_is_shown_on_initial_load(
        self, hass, enable_custom_integrations
    ):
        result = await _start_user_flow(hass)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

    async def test_invalid_host_shows_error_and_rerenders_form(
        self, hass, enable_custom_integrations
    ):
        result = await _start_user_flow(hass)
        with patch(FETCH_ID, AsyncMock(return_value=GW)) as fetch:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={GW_IP: "http://not valid"}
            )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"].get(GW_IP) == "invalid_gateway_ip"
        fetch.assert_not_called()

    @pytest.mark.parametrize(
        ("entered", "stored"),
        [
            ("192.168.1.100", "192.168.1.100"),
            ("  10.0.0.5  ", "10.0.0.5"),
            ("LinkTapGw_1A2B.local", "LinkTapGw_1A2B.local"),
        ],
    )
    async def test_valid_host_creates_entry_keyed_by_gateway_id(
        self, hass, enable_custom_integrations, entered, stored
    ):
        with patch(FETCH_ID, AsyncMock(return_value=GW)) as fetch, patch(
            SETUP_ENTRY, AsyncMock(return_value=True)
        ):
            result = await _start_user_flow(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={GW_IP: entered}
            )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"][GW_IP] == stored
        assert result["result"].unique_id == GW
        fetch.assert_awaited_once_with(stored)

    async def test_unreachable_gateway_shows_error(
        self, hass, enable_custom_integrations
    ):
        result = await _start_user_flow(hass)
        with patch(FETCH_ID, AsyncMock(side_effect=aiohttp.ClientError)):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={GW_IP: "192.168.1.100"}
            )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_same_gateway_at_new_address_updates_entry(
        self, hass, enable_custom_integrations
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=GW, data={GW_IP: "192.168.1.100"}
        )
        entry.add_to_hass(hass)
        result = await _start_user_flow(hass)
        with patch(FETCH_ID, AsyncMock(return_value=GW)):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={GW_IP: "gw.local"}
            )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert entry.data[GW_IP] == "gw.local"

    async def test_same_address_as_legacy_entry_aborts(
        self, hass, enable_custom_integrations
    ):
        """Entries made before gateway-ID keys are matched by address."""
        MockConfigEntry(
            domain=DOMAIN, unique_id="0123abcd", data={GW_IP: "192.168.1.100"}
        ).add_to_hass(hass)
        result = await _start_user_flow(hass)
        with patch(FETCH_ID, AsyncMock(return_value=GW)) as fetch:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={GW_IP: "192.168.1.100"}
            )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Zeroconf discovery
# ---------------------------------------------------------------------------


def _discovery(host="192.168.1.50", name=f"LinkTapGw_{GW}._http._tcp.local."):
    return SimpleNamespace(name=name, host=host, port=80, type="_http._tcp.local.")


async def _start_zeroconf_flow(hass, info):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )


class TestZeroconfFlow:
    async def test_discovered_gateway_is_confirmed_and_added(
        self, hass, enable_custom_integrations
    ):
        with patch(FETCH_ID, AsyncMock(return_value=GW)), patch(
            SETUP_ENTRY, AsyncMock(return_value=True)
        ):
            result = await _start_zeroconf_flow(hass, _discovery())
            assert result["type"] == FlowResultType.FORM
            assert result["step_id"] == "zeroconf_confirm"
            assert result["description_placeholders"] == {
                "gw_id": GW,
                "host": "192.168.1.50",
            }
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={}
            )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {GW_IP: "192.168.1.50"}
        assert result["result"].unique_id == GW

    async def test_other_http_service_is_ignored(
        self, hass, enable_custom_integrations
    ):
        with patch(FETCH_ID, AsyncMock(return_value=GW)) as fetch:
            result = await _start_zeroconf_flow(
                hass, _discovery(name="Printer._http._tcp.local.")
            )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "not_linktap_device"
        fetch.assert_not_called()

    async def test_ipv6_only_discovery_is_ignored(
        self, hass, enable_custom_integrations
    ):
        result = await _start_zeroconf_flow(hass, _discovery(host="fe80::1"))
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "not_ipv4_address"

    async def test_unreachable_discovered_gateway_aborts(
        self, hass, enable_custom_integrations
    ):
        with patch(FETCH_ID, AsyncMock(side_effect=TimeoutError)):
            result = await _start_zeroconf_flow(hass, _discovery())
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "cannot_connect"

    async def test_new_dhcp_address_updates_ip_entry(
        self, hass, enable_custom_integrations
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=GW, data={GW_IP: "192.168.1.10"}
        )
        entry.add_to_hass(hass)
        with patch(FETCH_ID, AsyncMock(return_value=GW)):
            result = await _start_zeroconf_flow(hass, _discovery("192.168.1.50"))
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert entry.data[GW_IP] == "192.168.1.50"

    async def test_hostname_entry_is_never_overwritten(
        self, hass, enable_custom_integrations
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=GW, data={GW_IP: "gw.local"}
        )
        entry.add_to_hass(hass)
        with patch(FETCH_ID, AsyncMock(return_value=GW)):
            result = await _start_zeroconf_flow(hass, _discovery("192.168.1.50"))
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert entry.data[GW_IP] == "gw.local"

    async def test_legacy_entry_at_same_address_aborts(
        self, hass, enable_custom_integrations
    ):
        MockConfigEntry(
            domain=DOMAIN, unique_id="0123abcd", data={GW_IP: "192.168.1.50"}
        ).add_to_hass(hass)
        with patch(FETCH_ID, AsyncMock(return_value=GW)) as fetch:
            result = await _start_zeroconf_flow(hass, _discovery("192.168.1.50"))
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Gateway ID lookup
# ---------------------------------------------------------------------------


async def test_fetch_gateway_id_uses_host():
    from custom_components.linktap.config_flow import _async_fetch_gateway_id

    api = AsyncMock()
    api.set_ip = lambda host: setattr(api, "host", host)
    api.get_gw_id.return_value = GW
    with patch(
        "custom_components.linktap.config_flow.LinktapLocal", return_value=api
    ):
        assert await _async_fetch_gateway_id("gw.local") == GW
    assert api.host == "gw.local"


async def test_fetch_gateway_id_rejects_empty_id():
    from custom_components.linktap.config_flow import _async_fetch_gateway_id

    api = AsyncMock()
    api.set_ip = lambda host: None
    api.get_gw_id.return_value = ""
    with patch(
        "custom_components.linktap.config_flow.LinktapLocal", return_value=api
    ), pytest.raises(KeyError):
        await _async_fetch_gateway_id("gw.local")
