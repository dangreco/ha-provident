# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from asyncio import Task
from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.const import Platform
from homeassistant.core import CoreState, Event, HomeAssistant
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

    sync_task = _create_stats_sync_task(hass, entry)

    def _cancel_sync() -> None:
        if sync_task is not None and not sync_task.done():
            sync_task.cancel()

    entry.async_on_unload(_cancel_sync)
    if hass.state is not CoreState.running:
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_hass_started(hass, entry)))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _make_stats_listener(hass: HomeAssistant, entry: ProvidentConfigEntry) -> Callable[[], None]:
    def _sync_statistics() -> None:
        _create_stats_sync_task(hass, entry)

    return _sync_statistics


def _on_hass_started(
    hass: HomeAssistant, entry: ProvidentConfigEntry
) -> Callable[[Event], None]:
    def _sync_statistics(_event: Event) -> None:
        _create_stats_sync_task(hass, entry)

    return _sync_statistics


def _create_stats_sync_task(hass: HomeAssistant, entry: ProvidentConfigEntry) -> Task[None] | None:
    coordinator = entry.runtime_data
    if not coordinator.last_update_success:
        return None
    return hass.async_create_background_task(
        async_sync_statistics(hass, coordinator),
        f"{entry.domain} statistics sync for {entry.title}",
        eager_start=True,
    )


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
