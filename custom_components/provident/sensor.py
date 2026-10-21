# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from provident import Meter

from .const import DOMAIN, MANUFACTURER, UTILITY_LABEL, meter_unit_info
from .coordinator import ProvidentCoordinator
from .models import MeterData

if TYPE_CHECKING:
    from . import ProvidentConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProvidentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known_node_ids: set[str] = set()

    @callback
    def _sync_meters() -> None:
        current = {m.meter.node_id: m.meter for m in coordinator.data.meters}

        new_entities = [
            entity
            for node_id, meter in current.items()
            if node_id not in known_node_ids
            for entity in _entities_for_meter(coordinator, meter)
        ]
        known_node_ids.update(current)
        if new_entities:
            async_add_entities(new_entities)

        # Prune only after a good refresh that listed meters; an empty answer must not wipe devices.
        if not coordinator.last_update_success or not current:
            return
        device_registry = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
            node_ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
            if node_ids and node_ids.isdisjoint(current):
                device_registry.async_update_device(
                    device.id, remove_config_entry_id=entry.entry_id
                )
                known_node_ids.difference_update(node_ids)

    _sync_meters()
    entry.async_on_unload(coordinator.async_add_listener(_sync_meters))


def _entities_for_meter(
    coordinator: ProvidentCoordinator, meter: Meter
) -> list[ProvidentSensorEntity]:
    return [
        ProvidentTotalUsageSensor(coordinator, meter),
        ProvidentTodaySensor(coordinator, meter),
        ProvidentYesterdaySensor(coordinator, meter),
    ]


def _device_info(meter: Meter) -> DeviceInfo:
    label = UTILITY_LABEL.get(meter.utility, meter.utility)
    return DeviceInfo(
        identifiers={(DOMAIN, meter.node_id)},
        name=f"{meter.name} {label}",
        manufacturer=MANUFACTURER,
        model=label,
        model_id=meter.utility,
        serial_number=meter.serial,
        entry_type=DeviceEntryType.SERVICE,
    )


class ProvidentSensorEntity(CoordinatorEntity[ProvidentCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ProvidentCoordinator, meter: Meter, key: str) -> None:
        super().__init__(coordinator)
        self.meter = meter
        self._attr_unique_id = f"{meter.node_id}_{key}"
        self._attr_device_info = _device_info(meter)

        device_class, unit, _unit_class = meter_unit_info(meter)
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit

    @property
    def meter_data(self) -> MeterData | None:
        assert self.coordinator.data is not None
        return self.coordinator.data.meter_data(self.meter.id)

    @property
    @override
    def available(self) -> bool:
        return super().available and self.meter_data is not None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, float | str]:
        return {
            "meter_serial": self.meter.serial,
            "utility": self.meter.utility,
        }


class ProvidentTotalUsageSensor(ProvidentSensorEntity, RestoreEntity):
    """Meter register; the portal value lags by up to a day."""

    _attr_translation_key = "total_usage"
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: ProvidentCoordinator, meter: Meter) -> None:
        super().__init__(coordinator, meter, "total_usage")

    @property
    @override
    def native_value(self) -> float | None:
        data = self.meter_data
        if data is None or data.register is None or data.register.error:
            return None
        return data.register.current

    @property
    @override
    def extra_state_attributes(self) -> dict[str, float | str]:
        attrs = dict(super().extra_state_attributes)
        data = self.meter_data
        if data is not None and data.register is not None:
            for key, value in (
                ("previous_reading", data.register.previous),
                ("period_total", data.register.total),
                ("daily_average", data.register.daily_average),
            ):
                if value is not None:
                    attrs[key] = value
        return attrs


class ProvidentTodaySensor(ProvidentSensorEntity):
    """Today's sum; the portal zero-fills unpublished hours, so it only grows."""

    _attr_translation_key = "today_usage"
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: ProvidentCoordinator, meter: Meter) -> None:
        super().__init__(coordinator, meter, "today_usage")

    @property
    @override
    def native_value(self) -> float | None:
        data = self.meter_data
        return None if data is None else data.daily.today


class ProvidentYesterdaySensor(ProvidentSensorEntity):
    _attr_translation_key = "yesterday_usage"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: ProvidentCoordinator, meter: Meter) -> None:
        super().__init__(coordinator, meter, "yesterday_usage")

    @property
    @override
    def native_value(self) -> float | None:
        data = self.meter_data
        return None if data is None else data.daily.yesterday
