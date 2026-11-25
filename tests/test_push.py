"""Tests for local push handling."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.linktap import (
    LinktapCoordinator,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.linktap.const import (
    DOMAIN,
    GW_ID,
    GW_IP,
    PUSH_DATA,
    PUSH_POLL_INTERVAL_SECONDS,
    PUSH_URL,
    PUSH_WATCHDOG_SECONDS,
)
from custom_components.linktap.push import (
    LinktapPushView,
    async_register_gateway,
    async_resolve_addresses,
    async_unregister_gateway,
    datetime_reply,
    extract_statuses,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tests.conftest import MOCK_GW_ID, MOCK_GW_IP, MOCK_TAP_ID, MOCK_TAP_STATUS


@pytest.fixture
def coordinator(hass, mock_linktap_api):
    conf = {GW_IP: MOCK_GW_IP, GW_ID: MOCK_GW_ID}
    c = LinktapCoordinator(hass, mock_linktap_api, conf, MOCK_TAP_ID)
    c.data = {**MOCK_TAP_STATUS, "dev_id": MOCK_TAP_ID}
    c.last_update_success = True
    return c


@pytest.fixture
def registered(hass, coordinator):
    async_register_gateway(
        hass, MOCK_GW_ID, MOCK_GW_IP, {MOCK_GW_IP}, {MOCK_TAP_ID: coordinator}
    )
    return coordinator


def _request(hass, body, remote=MOCK_GW_IP):
    request = MagicMock()
    request.app = {"hass": hass}
    request.remote = remote
    text = body if isinstance(body, str) else json.dumps(body)
    request.text = AsyncMock(return_value=text)
    return request


def _status_frame(**fields):
    return {
        "cmd": 3,
        "gw_id": MOCK_GW_ID,
        "dev_stat": {"dev_id": MOCK_TAP_ID, **fields},
    }


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


class TestExtractStatuses:
    def test_flat_payload(self):
        frame = {"gw_id": "G", "dev_id": "D", "is_watering": True, "ret": 0}
        assert extract_statuses(frame) == [
            {"gw_id": "G", "dev_id": "D", "is_watering": True}
        ]

    def test_nested_object(self):
        frame = {"cmd": 3, "gw_id": "G", "dev_stat": {"dev_id": "D", "volume": 1}}
        assert extract_statuses(frame) == [{"dev_id": "D", "volume": 1, "gw_id": "G"}]

    def test_nested_list(self):
        frame = {
            "cmd": 3,
            "gw_id": "G",
            "dev_stat": [{"dev_id": "D1"}, "junk", {"dev_id": "D2"}],
        }
        assert [s["dev_id"] for s in extract_statuses(frame)] == ["D1", "D2"]

    @pytest.mark.parametrize(
        "frame", [{"gw_id": "G"}, {"gw_id": "G", "dev_stat": None}, {"dev_stat": 5}]
    )
    def test_no_status(self, frame):
        assert extract_statuses(frame) == []


def test_datetime_reply_format():
    fixed = datetime(2026, 11, 25, 3, 4, 5, tzinfo=timezone.utc)  # a Wednesday
    with patch("custom_components.linktap.push.dt_util.now", return_value=fixed):
        assert datetime_reply(13, "G") == {
            "cmd": 13,
            "gw_id": "G",
            "date": "20261125",
            "time": "030405",
            "wday": 3,
        }


# ---------------------------------------------------------------------------
# View behaviour
# ---------------------------------------------------------------------------


class TestPushView:
    async def test_status_push_updates_coordinator(self, hass, registered):
        listener = MagicMock()
        unsubscribe = registered.async_add_listener(listener)

        resp = await LinktapPushView().post(
            _request(hass, _status_frame(is_watering=True, volume=12.5))
        )
        unsubscribe()

        assert resp.status == 200
        assert registered.data["is_watering"] is True
        assert registered.data["volume"] == 12.5
        # Fields the push did not carry keep their polled values.
        assert registered.data["battery"] == MOCK_TAP_STATUS["battery"]
        assert registered.push_active
        listener.assert_called()

    async def test_flat_push_updates_coordinator(self, hass, registered):
        frame = {"gw_id": MOCK_GW_ID, "dev_id": MOCK_TAP_ID, "is_watering": True}
        resp = await LinktapPushView().post(_request(hass, frame))
        assert resp.status == 200
        assert registered.data["is_watering"] is True

    async def test_frame_without_cmd_is_status(self, hass, registered):
        frame = _status_frame(is_watering=True)
        del frame["cmd"]
        await LinktapPushView().post(_request(hass, frame))
        assert registered.data["is_watering"] is True

    async def test_invalid_json(self, hass, registered):
        resp = await LinktapPushView().post(_request(hass, "{not json"))
        assert resp.status == 400

    async def test_non_object_json(self, hass, registered):
        resp = await LinktapPushView().post(_request(hass, "[1, 2]"))
        assert resp.status == 400

    async def test_unknown_gateway(self, hass, registered):
        frame = {**_status_frame(is_watering=True), "gw_id": "OTHER"}
        resp = await LinktapPushView().post(_request(hass, frame))
        assert resp.status == 202
        assert registered.data["is_watering"] is False

    async def test_wrong_source_address_rejected(self, hass, registered):
        resp = await LinktapPushView().post(
            _request(hass, _status_frame(is_watering=True), remote="10.9.9.9")
        )
        assert resp.status == 403
        assert registered.data["is_watering"] is False
        assert not registered.push_active

    async def test_unknown_device_ignored(self, hass, registered):
        frame = {"cmd": 3, "gw_id": MOCK_GW_ID, "dev_stat": {"dev_id": "NOPE"}}
        resp = await LinktapPushView().post(_request(hass, frame))
        assert resp.status == 200
        assert not registered.push_active

    @pytest.mark.parametrize("cmd", [0, 13])
    async def test_handshake_and_time_sync_get_datetime(self, hass, registered, cmd):
        resp = await LinktapPushView().post(
            _request(hass, {"cmd": cmd, "gw_id": MOCK_GW_ID, "end_dev": [MOCK_TAP_ID]})
        )
        body = json.loads(resp.body)
        assert resp.status == 200
        assert body["cmd"] == cmd
        assert body["gw_id"] == MOCK_GW_ID
        assert len(body["date"]) == 8 and len(body["time"]) == 6
        assert 1 <= body["wday"] <= 7

    async def test_other_commands_ignored(self, hass, registered):
        resp = await LinktapPushView().post(
            _request(hass, {"cmd": 9, "gw_id": MOCK_GW_ID, "dev_id": MOCK_TAP_ID})
        )
        assert resp.status == 200
        assert not registered.push_active

    async def test_corrupt_volume_keeps_last_good_status(self, hass, registered):
        before = registered.data
        resp = await LinktapPushView().post(
            _request(hass, _status_frame(is_watering=True, volume=-5))
        )
        assert resp.status == 200
        assert registered.data is before
        # The push channel still counts as alive.
        assert registered.push_active

    async def test_unregistered_gateway_ignored(self, hass, registered):
        async_unregister_gateway(hass, MOCK_GW_ID)
        resp = await LinktapPushView().post(
            _request(hass, _status_frame(is_watering=True))
        )
        assert resp.status == 202


# ---------------------------------------------------------------------------
# Adaptive polling
# ---------------------------------------------------------------------------


class TestAdaptivePolling:
    def test_polls_normally_without_push(self, coordinator):
        assert not coordinator.push_active
        assert coordinator._next_update_interval() == coordinator._poll_interval

    def test_relaxes_after_push(self, coordinator):
        coordinator.async_handle_push({"dev_id": MOCK_TAP_ID, "is_watering": True})
        assert coordinator.update_interval == timedelta(
            seconds=PUSH_POLL_INTERVAL_SECONDS
        )

    async def test_reverts_when_pushes_stop(self, coordinator, mock_linktap_api):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch("custom_components.linktap.dt_util.utcnow", return_value=start):
            coordinator.async_handle_push({"dev_id": MOCK_TAP_ID})
            await coordinator.async_refresh()
        assert coordinator.update_interval == timedelta(
            seconds=PUSH_POLL_INTERVAL_SECONDS
        )

        later = start + timedelta(seconds=PUSH_WATCHDOG_SECONDS + 1)
        with patch("custom_components.linktap.dt_util.utcnow", return_value=later):
            assert not coordinator.push_active
            await coordinator.async_refresh()
        assert coordinator.update_interval == coordinator._poll_interval


# ---------------------------------------------------------------------------
# Setup wiring
# ---------------------------------------------------------------------------


async def test_setup_and_unload_register_gateway(hass, mock_linktap_api):
    entry = MockConfigEntry(domain=DOMAIN, data={GW_IP: MOCK_GW_IP})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.linktap.LinktapLocal", return_value=mock_linktap_api
    ), patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
    ), patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    ):
        await async_setup_entry(hass, entry)
        gateway = hass.data[PUSH_DATA]["gateways"][MOCK_GW_ID]
        assert gateway["addresses"] == {MOCK_GW_IP}
        assert set(gateway["coordinators"]) == {MOCK_TAP_ID}

        await async_unload_entry(hass, entry)
        assert MOCK_GW_ID not in hass.data[PUSH_DATA]["gateways"]


def test_view_registered_once(hass, coordinator):
    """The view is added on first registration only, even with several gateways."""
    hass.http = MagicMock()
    async_register_gateway(hass, "GW1", "10.0.0.1", {"10.0.0.1"}, {"T1": coordinator})
    async_register_gateway(hass, "GW2", "10.0.0.2", {"10.0.0.2"}, {"T2": coordinator})

    hass.http.register_view.assert_called_once()
    view = hass.http.register_view.call_args.args[0]
    assert isinstance(view, LinktapPushView)
    assert view.url == PUSH_URL
    assert view.requires_auth is False
    assert set(hass.data[PUSH_DATA]["gateways"]) == {"GW1", "GW2"}


def test_view_not_registered_without_http(hass, coordinator):
    """Without the http component, routing still works and nothing crashes."""
    hass.http = None
    async_register_gateway(
        hass, MOCK_GW_ID, MOCK_GW_IP, {MOCK_GW_IP}, {MOCK_TAP_ID: coordinator}
    )
    assert not hass.data[PUSH_DATA].get("view_registered")


# ---------------------------------------------------------------------------
# Gateways configured by hostname
# ---------------------------------------------------------------------------


async def test_resolve_ip_literal_skips_dns(hass):
    with patch.object(hass.loop, "getaddrinfo") as getaddrinfo:
        assert await async_resolve_addresses(hass, "192.168.1.5") == {"192.168.1.5"}
    getaddrinfo.assert_not_called()


async def test_resolve_hostname(hass):
    infos = [
        (2, 1, 6, "", ("192.168.1.5", 0)),
        (10, 1, 6, "", ("fe80::1", 0, 0, 0)),
    ]
    with patch.object(hass.loop, "getaddrinfo", AsyncMock(return_value=infos)):
        assert await async_resolve_addresses(hass, "gw.local") == {
            "192.168.1.5",
            "fe80::1",
        }


async def test_resolve_failure_returns_empty(hass):
    with patch.object(hass.loop, "getaddrinfo", AsyncMock(side_effect=OSError)):
        assert await async_resolve_addresses(hass, "gw.local") == set()


async def test_hostname_gateway_reresolved_after_address_change(hass, coordinator):
    async_register_gateway(
        hass, MOCK_GW_ID, "gw.local", {"192.168.1.5"}, {MOCK_TAP_ID: coordinator}
    )
    gateway = hass.data[PUSH_DATA]["gateways"][MOCK_GW_ID]
    frame = _status_frame(is_watering=True)

    # Just resolved: a push from a new address is not re-resolved yet.
    resolver = AsyncMock(return_value={"192.168.1.9"})
    with patch("custom_components.linktap.push.async_resolve_addresses", resolver):
        resp = await LinktapPushView().post(_request(hass, frame, remote="192.168.1.9"))
    assert resp.status == 403
    resolver.assert_not_called()

    # Once the cache is stale, the hostname is resolved again and accepted.
    gateway["resolved_at"] -= 61
    with patch("custom_components.linktap.push.async_resolve_addresses", resolver):
        resp = await LinktapPushView().post(_request(hass, frame, remote="192.168.1.9"))
    assert resp.status == 200
    assert coordinator.data["is_watering"] is True
    assert gateway["addresses"] == {"192.168.1.9"}


async def test_ip_gateway_never_reresolved(hass, registered):
    gateway = hass.data[PUSH_DATA]["gateways"][MOCK_GW_ID]
    gateway["resolved_at"] -= 3600
    resolver = AsyncMock()
    with patch("custom_components.linktap.push.async_resolve_addresses", resolver):
        resp = await LinktapPushView().post(
            _request(hass, _status_frame(is_watering=True), remote="10.9.9.9")
        )
    assert resp.status == 403
    resolver.assert_not_called()
