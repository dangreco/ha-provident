# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .const import UTILITY_LABEL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import ProvidentConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ProvidentConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "rate_plan": str(coordinator.plan),
        "include_fees": coordinator.include_fees,
        # Published prices only; the rates page carries no personal data.
        "rates": coordinator.rates.to_dict() if coordinator.rates else None,
        "meters": [
            {
                "node_id": m.meter.node_id,
                "utility": UTILITY_LABEL.get(m.meter.utility, m.meter.utility),
                "unit": m.meter.unit,
                "serial": m.meter.serial,
                "register": m.register.current if m.register else None,
                "register_error": m.register.error if m.register else None,
                "today": m.daily.today,
                "yesterday": m.daily.yesterday,
                "priced": coordinator.tariff(m.meter) is not None,
            }
            for m in data.meters
        ],
    }
