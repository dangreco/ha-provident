# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.provident.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacted(recorder_mock, hass: HomeAssistant, setup_entry) -> None:
    entry = setup_entry
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    text = repr(diagnostics)

    assert "hunter2" not in text
    assert diagnostics["entry"]["data"]["username"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["password"] == "**REDACTED**"
    assert diagnostics["meters"][0]["node_id"] == "MP:658637"
    assert diagnostics["meters"][0]["register"] is not None
    assert diagnostics["meters"][0]["utility"] == "Electricity"
