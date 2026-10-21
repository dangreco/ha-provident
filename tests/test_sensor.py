# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry
from pytest_homeassistant_custom_component.common import snapshot_platform

from custom_components.provident.const import DOMAIN

from .fixtures.portal import DELAYED_UTILITIES

TODAY_SENSORS = {
    "sensor.3105_electricity_total_usage": ("energy", "kWh"),
    "sensor.3105_cold_water_total_usage": ("water", "m³"),
    "sensor.3105_hot_water_total_usage": ("water", "m³"),
}


async def test_entities_created(recorder_mock, hass: HomeAssistant, setup_entry) -> None:
    await hass.async_block_till_done()

    coordinator = setup_entry.runtime_data

    for entity_id, (device_class, unit) in TODAY_SENSORS.items():
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.attributes["device_class"] == device_class
        assert state.attributes["unit_of_measurement"] == unit
        assert state.attributes["state_class"] == "total_increasing"

    electricity = hass.states.get("sensor.3105_electricity_total_usage")
    assert electricity is not None
    data = coordinator.data.meter_data(658637)
    assert data is not None
    assert float(electricity.state) == data.register.current
    assert electricity.attributes["previous_reading"] == data.register.previous
    assert electricity.attributes["period_total"] == data.register.total

    today_state = hass.states.get("sensor.3105_electricity_usage_today")
    assert today_state is not None
    assert float(today_state.state) == (
        0.0 if "Electricity" in DELAYED_UTILITIES else data.daily.today
    )
    yesterday_state = hass.states.get("sensor.3105_electricity_usage_yesterday")
    assert yesterday_state is not None
    assert float(yesterday_state.state) == data.daily.yesterday

    water = coordinator.data.meter_data(692294)
    assert water is not None
    water_today = hass.states.get("sensor.3105_cold_water_usage_today")
    assert water_today is not None
    assert float(water_today.state) == water.daily.today
    # Water registers still sit at the end of yesterday.
    water_register = hass.states.get("sensor.3105_cold_water_total_usage")
    assert water_register is not None
    assert float(water_register.state) == water.register.current


async def test_yesterday_is_diagnostic(recorder_mock, hass: HomeAssistant, setup_entry) -> None:
    registry = entity_registry.async_get(hass)
    entry = registry.async_get("sensor.3105_electricity_usage_yesterday")
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    for entity_id in (
        "sensor.3105_electricity_total_usage",
        "sensor.3105_electricity_usage_today",
    ):
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.entity_category is None


async def test_devices_registered(recorder_mock, hass: HomeAssistant, setup_entry) -> None:
    registry = device_registry.async_get(hass)
    for node, serial, model in (
        ("MP:658637", "85006408-9", "Electricity"),
        ("MP:692294", "23166694-2", "Cold Water"),
        ("MP:692262", "24119905-2", "Hot Water"),
    ):
        device = registry.async_get_device_by_identifier((DOMAIN, node), setup_entry.entry_id)
        assert device is not None
        assert device.serial_number == serial
        assert device.manufacturer == "Provident (MeterConnex)"
        assert device.name == f"3105 {model}"
        assert device.entry_type is device_registry.DeviceEntryType.SERVICE


async def test_unique_ids_stable(recorder_mock, hass: HomeAssistant, setup_entry) -> None:
    registry = entity_registry.async_get(hass)
    unique_ids = {
        entry.unique_id for entry in registry.entities.values() if entry.platform == DOMAIN
    }
    assert {
        "MP:658637_total_usage",
        "MP:658637_today_usage",
        "MP:658637_yesterday_usage",
        "MP:692294_total_usage",
        "MP:692294_today_usage",
        "MP:692294_yesterday_usage",
        "MP:692262_total_usage",
        "MP:692262_today_usage",
        "MP:692262_yesterday_usage",
    } <= unique_ids


async def test_new_meter_gets_entities_and_device(
    recorder_mock,
    hass: HomeAssistant,
    portal,
    setup_entry,
) -> None:
    gas = {
        "id": 700001,
        "node": "MP:700001",
        "name": "3105",
        "utility": "Gas",
        "unit": "m3",
        "serial": "99900001-1",
    }
    assert hass.states.get("sensor.3105_gas_total_usage") is None

    portal.meters.append(gas)
    await setup_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.3105_gas_total_usage")
    assert state is not None
    assert state.attributes["device_class"] == "gas"
    device = device_registry.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "MP:700001"), setup_entry.entry_id
    )
    assert device is not None

    await setup_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    registry = entity_registry.async_get(hass)
    gas_entities = [e for e in registry.entities.values() if e.unique_id.startswith("MP:700001_")]
    assert len(gas_entities) == 3


async def test_vanished_meter_device_and_entities_are_removed(
    recorder_mock,
    hass: HomeAssistant,
    portal,
    setup_entry,
) -> None:
    device_reg = device_registry.async_get(hass)
    entity_reg = entity_registry.async_get(hass)
    assert device_reg.async_get_device_by_identifier((DOMAIN, "MP:692262"), setup_entry.entry_id)

    portal.meters = [m for m in portal.meters if m["utility"] != "Hot Water"]
    await setup_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert (
        device_reg.async_get_device_by_identifier((DOMAIN, "MP:692262"), setup_entry.entry_id)
        is None
    )
    assert hass.states.get("sensor.3105_hot_water_total_usage") is None
    assert not [e for e in entity_reg.entities.values() if e.unique_id.startswith("MP:692262_")]
    assert hass.states.get("sensor.3105_electricity_total_usage") is not None


async def test_empty_portal_answer_never_wipes_devices(
    recorder_mock,
    hass: HomeAssistant,
    portal,
    setup_entry,
) -> None:
    portal.meters = []
    await setup_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    device_reg = device_registry.async_get(hass)
    assert device_reg.async_get_device_by_identifier((DOMAIN, "MP:658637"), setup_entry.entry_id)
    assert hass.states.get("sensor.3105_electricity_total_usage") is not None


async def test_user_can_only_delete_devices_the_portal_dropped(
    recorder_mock,
    hass: HomeAssistant,
    portal,
    setup_entry,
) -> None:
    from custom_components.provident import async_remove_config_entry_device

    device_reg = device_registry.async_get(hass)
    live = device_reg.async_get_device_by_identifier((DOMAIN, "MP:658637"), setup_entry.entry_id)
    assert live is not None
    assert not await async_remove_config_entry_device(hass, setup_entry, live)

    stale = device_reg.async_get_or_create(
        config_entry_id=setup_entry.entry_id, identifiers={(DOMAIN, "MP:1")}
    )
    assert await async_remove_config_entry_device(hass, setup_entry, stale)


async def test_register_sensor_unknown_on_error_row(
    recorder_mock, hass: HomeAssistant, setup_entry
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from custom_components.provident.models import ProvidentData
    from custom_components.provident.sensor import ProvidentTotalUsageSensor

    coordinator = setup_entry.runtime_data
    meter = next(m for m in coordinator.data.meters if m.meter.id == 692294)
    errored = replace(
        meter,
        register=SimpleNamespace(
            current=1.0, previous=0.0, total=1.0, daily_average=None, error=True
        ),
    )
    coordinator.data = ProvidentData(
        account=coordinator.data.account,
        meters=(errored,),
        today=coordinator.data.today,
    )

    entity = ProvidentTotalUsageSensor(coordinator, meter.meter)
    entity.hass = hass
    assert entity.native_value is None


async def test_snapshot_platform(
    recorder_mock,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
    snapshot,
    entity_registry: entity_registry.EntityRegistry,
) -> None:
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    freezer.move_to(_dt(2026, 10, 20, 12, 0, tzinfo=_UTC))
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_energy_dashboard_requirements(
    recorder_mock, hass: HomeAssistant, setup_entry
) -> None:
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    for entity_id in (
        "sensor.3105_electricity_total_usage",
        "sensor.3105_cold_water_total_usage",
    ):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["device_class"] in (
            SensorDeviceClass.ENERGY.value,
            SensorDeviceClass.WATER.value,
        )
        assert state.attributes["state_class"] == SensorStateClass.TOTAL_INCREASING.value
        assert "last_reset" not in state.attributes
