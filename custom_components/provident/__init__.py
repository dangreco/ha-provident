# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import ProvidentCoordinator, new_client
from .statistics import async_sync_statistics

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

PLATFORMS = [Platform.SENSOR]

type ProvidentConfigEntry = ConfigEntry[ProvidentCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ProvidentConfigEntry) -> bool:
    client = new_client()
    coordinator = ProvidentCoordinator(hass, entry, client)

    await coordinator.async_login_or_raise()

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    entry.async_on_unload(coordinator.async_add_listener(_make_stats_listener(hass, entry)))

    sync_task = hass.async_create_background_task(
        async_sync_statistics(hass, coordinator),
        f"{entry.domain} statistics sync for {entry.title}",
        eager_start=True,
    )

    def _cancel_sync() -> None:
        if not sync_task.done():
            sync_task.cancel()

    entry.async_on_unload(_cancel_sync)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _make_stats_listener(hass: HomeAssistant, entry: ProvidentConfigEntry) -> Callable[[], None]:
    def _sync_statistics() -> None:
        coordinator = entry.runtime_data
        if not coordinator.last_update_success:
            return
        hass.async_create_background_task(
            async_sync_statistics(hass, coordinator),
            f"{entry.domain} statistics sync for {entry.title}",
            eager_start=True,
        )

    return _sync_statistics


async def async_unload_entry(hass: HomeAssistant, entry: ProvidentConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ProvidentConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow removal only once the portal no longer lists the meter."""
    current = {m.meter.node_id for m in entry.runtime_data.data.meters}
    return not any(
        identifier[0] == DOMAIN and identifier[1] in current
        for identifier in device_entry.identifiers
    )
