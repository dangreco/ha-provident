# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.provident.const import CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS, DOMAIN

from .fixtures.portal import LOGIN, PASSWORD, TREE, USERNAME

CREDENTIALS = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


def _login_requests(portal) -> list[tuple[str, str, dict[str, list[str]], bytes]]:
    return [r for r in portal.requests if r[1] == LOGIN]


async def test_user_flow_creates_entry(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=CREDENTIALS
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Provident ({USERNAME})"
    assert result["data"] == {**CREDENTIALS, CONF_BACKFILL_DAYS: 30}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    # One login for validation, one for entry setup.
    assert len(_login_requests(portal)) == 2
    assert any(r[1] == TREE for r in portal.requests)


async def test_user_flow_custom_backfill_days(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={**CREDENTIALS, CONF_BACKFILL_DAYS: 7}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BACKFILL_DAYS] == 7
    assert isinstance(result["data"][CONF_BACKFILL_DAYS], int)


async def test_user_flow_invalid_auth(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None
) -> None:
    portal.status_error = 500
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=CREDENTIALS
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_no_meters(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None
) -> None:
    portal.meters = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=CREDENTIALS
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_meters"


async def test_user_flow_duplicate_account(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=CREDENTIALS
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    # The portal password never changed, so reuse it.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
    assert entry is not None
    assert entry.data[CONF_PASSWORD] == PASSWORD


async def test_reconfigure_flow(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=CREDENTIALS
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
    assert entry is not None
    assert entry.data == {**CREDENTIALS, CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS}
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_changes_backfill_days(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={**CREDENTIALS, CONF_BACKFILL_DAYS: 90}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
    assert entry is not None
    assert entry.data[CONF_BACKFILL_DAYS] == 90
    assert entry.runtime_data.backfill_days == 90


async def test_flow_translations_exist(
    recorder_mock, hass: HomeAssistant, portal, patch_client: None
) -> None:
    from homeassistant.helpers.translation import async_get_translations

    translations: dict[str, Any] = await async_get_translations(hass, "en", "entity", {DOMAIN})
    config = await async_get_translations(hass, "en", "config", {DOMAIN})
    translations.update(config)

    needed = [
        "component.provident.config.step.user.data.username",
        "component.provident.config.step.user.data.password",
        "component.provident.config.step.reconfigure.data.username",
        "component.provident.config.step.reauth_confirm.data.password",
        "component.provident.config.error.invalid_auth",
        "component.provident.config.error.cannot_connect",
        "component.provident.config.error.unknown",
        "component.provident.config.abort.no_meters",
        "component.provident.config.abort.reauth_successful",
        "component.provident.config.abort.reconfigure_successful",
        "component.provident.entity.sensor.total_usage.name",
        "component.provident.entity.sensor.today_usage.name",
        "component.provident.entity.sensor.yesterday_usage.name",
    ]
    missing = [key for key in needed if key not in translations]
    assert not missing, f"missing translations: {missing}"
