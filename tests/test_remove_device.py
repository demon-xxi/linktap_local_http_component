"""Tests for removing a LinkTap device from the device registry."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.linktap import (
    async_remove_config_entry_device,
    async_setup_entry,
)
from custom_components.linktap.const import DOMAIN, GW_IP
from tests.conftest import MOCK_GW_IP, MOCK_TAP_ID


@pytest.fixture
def config_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={GW_IP: MOCK_GW_IP}, options={})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def loaded_entry(hass, config_entry, mock_linktap_api):
    with patch(
        "custom_components.linktap.LinktapLocal", return_value=mock_linktap_api
    ), patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(return_value=True),
    ):
        await async_setup_entry(hass, config_entry)
    return config_entry


def _device(tap_id):
    return SimpleNamespace(identifiers={(DOMAIN, tap_id)})


async def test_refused_while_gateway_reports_device(hass, loaded_entry):
    device = _device(MOCK_TAP_ID)
    assert await async_remove_config_entry_device(hass, loaded_entry, device) is False


async def test_allowed_once_device_gone_from_gateway(hass, loaded_entry):
    device = _device("OLD_TAP")
    assert await async_remove_config_entry_device(hass, loaded_entry, device) is True


async def test_allowed_when_entry_not_loaded(hass, config_entry):
    device = _device("ANY")
    assert await async_remove_config_entry_device(hass, config_entry, device) is True
