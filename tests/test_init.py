# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from provident import AsyncClient

from custom_components.provident.const import DOMAIN

from .fixtures.portal import LOGIN, USERNAME


async def test_setup_and_unload(recorder_mock, hass: HomeAssistant, setup_entry) -> None:
    entry = setup_entry
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.data.account.login == USERNAME
    assert len(coordinator.data.meters) == 3
    assert entry.runtime_data.last_update_success

    assert await hass.config_entries.async_unload(entry.entry_id)
    unloaded = hass.config_entries.async_get_entry(entry.entry_id)
    assert unloaded is not None
    assert unloaded.state is ConfigEntryState.NOT_LOADED


async def test_setup_login_rejected_starts_reauth(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None, mock_config_entry
) -> None:
    portal.reject_login = "Invalid Username or Password"
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR

    issue_reg = ir.async_get(hass)
    issues = [
        issue
        for issue in issue_reg.issues.values()
        if issue.issue_domain == DOMAIN and issue.translation_key == "config_entry_reauth"
    ]
    assert issues


async def test_setup_transport_failure_retries(
    recorder_mock, hass: HomeAssistant, patch_client: None, mock_config_entry
) -> None:
    with patch(
        "custom_components.provident.new_client",
        lambda: AsyncClient(base_url="http://127.0.0.1:9/", connect_timeout=1),
    ):
        mock_config_entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_expired_session_relogins(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None, setup_entry
) -> None:
    portal.session_token = "rotated"
    coordinator = setup_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    logins = [r for r in portal.requests if r[1] == LOGIN]
    # One login at setup, one after expiry.
    assert len(logins) == 2


async def test_meters_change_updates_data(
    recorder_mock, hass: HomeAssistant, portal, setup_entry
) -> None:
    coordinator = setup_entry.runtime_data
    assert len(coordinator.data.meters) == 3

    portal.meters = portal.meters[:1]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(coordinator.data.meters) == 1


async def test_register_falls_back_when_ha_clock_is_ahead_of_portal(
    recorder_mock,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
) -> None:
    from datetime import UTC, datetime

    # The portal (UTC) is still on Oct 20 while HA's zone (UTC+14) is on Oct 21.
    freezer.move_to(datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    await hass.config.async_set_time_zone("Pacific/Kiritimati")
    await hass.async_block_till_done()

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    coordinator = mock_config_entry.runtime_data
    assert coordinator.data.today.isoformat() == "2026-10-21"
    for meter_data in coordinator.data.meters:
        assert meter_data.register is not None
        assert not meter_data.register.error, meter_data.meter.serial
        assert meter_data.register.current is not None


async def test_transport_error_is_retried_once(
    recorder_mock, hass: HomeAssistant, portal, setup_entry
) -> None:
    await hass.async_block_till_done()
    coordinator = setup_entry.runtime_data
    portal.graph_failures[portal.meters[0]["node"]] = 1

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert portal.graph_failures[portal.meters[0]["node"]] == 0


async def test_failing_meter_keeps_its_last_data(
    recorder_mock, hass: HomeAssistant, portal, setup_entry
) -> None:
    """One meter failing twice does not blank the others."""
    await hass.async_block_till_done()
    coordinator = setup_entry.runtime_data
    failing = portal.meters[0]
    before = coordinator.data.meter_data(failing["id"])
    assert before is not None
    portal.graph_failures[failing["node"]] = 2

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert len(coordinator.data.meters) == len(portal.meters)
    after = coordinator.data.meter_data(failing["id"])
    assert after is not None
    assert after.daily == before.daily


async def test_failing_meter_without_same_day_data_fails_the_update(
    recorder_mock, freezer, hass: HomeAssistant, portal, setup_entry
) -> None:
    """Stale data from an earlier day is not served as if it were current."""
    await hass.async_block_till_done()
    coordinator = setup_entry.runtime_data
    freezer.tick(timedelta(days=1))
    portal.graph_failures[portal.meters[0]["node"]] = 2

    await coordinator.async_refresh()

    assert not coordinator.last_update_success
