# SPDX-License-Identifier: GPL-3.0-only

"""Published rates end to end: options, cost statistic, price sensors, outages, diagnostics."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest
from homeassistant.components.recorder import Recorder  # type: ignore[attr-defined]
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.provident.const import (
    CONF_INCLUDE_FEES,
    CONF_RATE_PLAN,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
)
from custom_components.provident.diagnostics import async_get_config_entry_diagnostics
from custom_components.provident.rates import FEE_HOURS, Period, Plan, time_period
from custom_components.provident.statistics import bucket_start

from .fixtures.portal import RATES_HTML, RATES_LOGIN, RATES_PAGE

ELECTRICITY_USAGE = "provident:85006408_9_hourly_consumption"
ELECTRICITY_COST = "provident:85006408_9_hourly_cost"
WATER_COST = "provident:23166694_2_hourly_cost"
PRICE = "sensor.3105_electricity_current_price"
PERIOD = "sensor.3105_electricity_price_period"

# The fixture page: TOU 9.8/15.7/20.3 (previous 7.6/12.2/15.8, Nov 1 2025/2024);
# fees 11.90 + 8.46 from 2026-04-16 (previous 11.69 + 8.31 from 2025-11-16).
TOU_NOW = {Period.OFF_PEAK: 0.098, Period.MID_PEAK: 0.157, Period.ON_PEAK: 0.203}
FEES_NOW = (11.90 + 8.46) / FEE_HOURS
FROZEN = datetime(2026, 10, 20, 12, 0, tzinfo=dt_util.UTC)


async def _rows(hass: HomeAssistant, stat_id: str) -> list[dict[str, Any]]:
    rows: Any = await hass.async_add_executor_job(
        cast(Any, statistics_during_period),
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return list(rows.get(stat_id, []))


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def _sync(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await _settle(hass)


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, plan: Plan, *, include_fees: bool = True
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_RATE_PLAN: str(plan), CONF_INCLUDE_FEES: include_fees}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)


def _expected_tou(day: date, hour: int, kwh: float, *, fees: bool) -> float:
    return kwh * TOU_NOW[time_period(Plan.TOU, day, hour)] + (FEES_NOW if fees else 0.0)


@pytest.fixture
def frozen(freezer) -> None:
    freezer.move_to(FROZEN)


async def test_no_plan_means_no_rates_traffic_and_no_cost(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await _settle(hass)

    assert not [r for r in portal.requests if r[1] in (RATES_LOGIN, RATES_PAGE)]
    assert await _rows(hass, ELECTRICITY_COST) == []
    assert hass.states.get(PRICE) is None
    assert hass.states.get(PERIOD) is None


async def test_tou_cost_matches_usage_times_published_prices(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TOU)

    usage = {r["start"]: r["state"] for r in await _rows(hass, ELECTRICITY_USAGE)}
    cost = await _rows(hass, ELECTRICITY_COST)
    assert len(cost) == len(usage) > 24 * 20
    checked = 0
    for row in cost:
        start = dt_util.utc_from_timestamp(row["start"])
        local = start.astimezone(dt_util.get_default_time_zone())
        assert row["state"] == pytest.approx(
            _expected_tou(local.date(), local.hour, usage[row["start"]], fees=True), abs=1e-4
        ), local
        checked += 1
    assert checked == len(usage)
    sums = [r["sum"] for r in cost]
    assert all(b >= a for a, b in zip(sums, sums[1:], strict=False))
    assert sums[-1] == pytest.approx(sum(r["state"] for r in cost), abs=1e-3)
    # Water is metered by volume: never priced.
    assert await _rows(hass, WATER_COST) == []
    ids = await hass.async_add_executor_job(list_statistic_ids, hass)
    meta = next(i for i in ids if i["statistic_id"] == ELECTRICITY_COST)
    assert meta["statistics_unit_of_measurement"] == "CAD"
    assert meta["name"] == "3105 Electricity hourly cost"


async def test_fees_can_be_left_out(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TOU, include_fees=False)
    usage = {r["start"]: r["state"] for r in await _rows(hass, ELECTRICITY_USAGE)}
    for row in await _rows(hass, ELECTRICITY_COST):
        local = dt_util.utc_from_timestamp(row["start"]).astimezone(dt_util.get_default_time_zone())
        assert row["state"] == pytest.approx(
            _expected_tou(local.date(), local.hour, usage[row["start"]], fees=False), abs=1e-4
        )


async def test_hours_before_any_published_price_are_not_costed(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal,
    freezer,
    patch_client,
    mock_config_entry,
) -> None:
    # Backfill reaches 2025-11-03; the fees' previous value only starts 2025-11-16.
    freezer.move_to(datetime(2025, 12, 3, 12, 0, tzinfo=dt_util.UTC))
    portal.origin = date(2025, 11, 1)
    await _setup(hass, mock_config_entry, Plan.TOU)
    cost = await _rows(hass, ELECTRICITY_COST)
    first = dt_util.utc_from_timestamp(cost[0]["start"]).astimezone(dt_util.get_default_time_zone())
    assert first == datetime(2025, 11, 16, tzinfo=dt_util.get_default_time_zone())
    assert len(await _rows(hass, ELECTRICITY_USAGE)) > len(cost)


async def test_tiered_cost_uses_month_to_date(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TIERED, include_fees=False)
    cost = await _rows(hass, ELECTRICITY_COST)
    usage = await _rows(hass, ELECTRICITY_USAGE)
    # The fake meter uses well under the threshold: every hour is tier 1 (12¢).
    assert sum(r["state"] for r in cost) == pytest.approx(
        0.12 * sum(r["state"] for r in usage), abs=1e-3
    )
    period = hass.states.get(PERIOD)
    assert period is not None and period.state == Period.TIER_1


async def test_tiered_hour_crossing_the_threshold_is_split(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    # October threshold: 600 * 31/30 = 620 kWh. Oct 1 uses its usual few kWh; Oct 2 00:00
    # brings the month to 1 kWh below the threshold, and 01:00 (2 kWh) crosses it.
    today = FROZEN.date()
    portal.origin = today - timedelta(days=DEFAULT_BACKFILL_DAYS)
    oct1 = sum(portal.hour_value("Electricity", date(2026, 10, 1), h) for h in range(24))
    filler = round(620 - oct1 - 1, 3)
    portal.set_hour("Electricity", date(2026, 10, 2), 0, filler)
    portal.set_hour("Electricity", date(2026, 10, 2), 1, 2.0)
    await _setup(hass, mock_config_entry, Plan.TIERED, include_fees=False)

    rows = {r["start"]: r["state"] for r in await _rows(hass, ELECTRICITY_COST)}
    tz = dt_util.get_default_time_zone()
    first = rows[bucket_start(date(2026, 10, 2), 0, tz).timestamp()]
    crossing = rows[bucket_start(date(2026, 10, 2), 1, tz).timestamp()]
    after = rows[bucket_start(date(2026, 10, 2), 2, tz).timestamp()]
    left = 620 - oct1 - filler
    assert first == pytest.approx(filler * 0.12, abs=1e-3)
    assert crossing == pytest.approx(left * 0.12 + (2 - left) * 0.142, abs=1e-3)
    later = portal.hour_value("Electricity", date(2026, 10, 2), 2)
    assert after == pytest.approx(later * 0.142, abs=1e-4)


async def test_changing_the_plan_reprices_the_history(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TOU, include_fees=False)
    tou_total = (await _rows(hass, ELECTRICITY_COST))[-1]["sum"]

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_RATE_PLAN: "ulo", CONF_INCLUDE_FEES: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await _settle(hass)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.plan is Plan.ULO
    cost = await _rows(hass, ELECTRICITY_COST)
    usage = await _rows(hass, ELECTRICITY_USAGE)
    # Cleared and re-imported: one row per hour again, the running sum restarted.
    assert len(cost) == len(usage)
    assert cost[-1]["sum"] == pytest.approx(sum(r["state"] for r in cost), abs=1e-3)
    assert cost[-1]["sum"] != pytest.approx(tou_total)

    # Switching tracking off drops the price sensors; existing cost rows are kept.
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_RATE_PLAN: "none", CONF_INCLUDE_FEES: True}
    )
    await _settle(hass)
    price = hass.states.get(PRICE)
    assert price is None or price.state == "unavailable"


async def test_rate_change_reprices_from_its_effective_date(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TOU, include_fees=False)
    before = {r["start"]: r["state"] for r in await _rows(hass, ELECTRICITY_COST)}

    # Provident restates the current TOU on-peak (effective 2026-10-10) a day later.
    portal.rates_html = RATES_HTML.replace("<td>2025-11-01</td>\n\t\t\t\t<td>20.3</td>", "X", 1)
    portal.rates_html = RATES_HTML.replace("20.3", "30.0")
    coordinator = mock_config_entry.runtime_data
    coordinator._rates_fetched_at = dt_util.utcnow() - timedelta(days=2)
    await _sync(hass, mock_config_entry)

    after = await _rows(hass, ELECTRICITY_COST)
    tz = dt_util.get_default_time_zone()
    changed = 0
    for row in after:
        local = dt_util.utc_from_timestamp(row["start"]).astimezone(tz)
        if time_period(Plan.TOU, local.date(), local.hour) is Period.ON_PEAK:
            assert row["state"] == pytest.approx(before[row["start"]] * 30.0 / 20.3, abs=1e-4)
            changed += 1
        else:
            assert row["state"] == pytest.approx(before[row["start"]], abs=1e-6)
    # The whole backfill is after 2025-11-01, so every on-peak hour was re-priced.
    assert changed > 50
    assert after[-1]["sum"] == pytest.approx(sum(r["state"] for r in after), abs=1e-3)


async def test_price_and_period_sensors_follow_the_clock(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal,
    freezer,
    patch_client,
    mock_config_entry,
) -> None:
    await hass.config.async_set_time_zone("America/Toronto")
    # Wed 2026-01-14 10:59 local: winter on-peak, then mid-peak from 11:00.
    freezer.move_to(datetime(2026, 1, 14, 15, 59, tzinfo=dt_util.UTC))
    await _setup(hass, mock_config_entry, Plan.TOU)
    price, period = hass.states.get(PRICE), hass.states.get(PERIOD)
    assert price is not None and period is not None
    assert float(price.state) == pytest.approx(0.203)
    assert price.attributes["unit_of_measurement"] == "CAD/kWh"
    assert period.state == "on_peak"
    assert period.attributes["options"] == ["off_peak", "mid_peak", "on_peak"]

    freezer.move_to(datetime(2026, 1, 14, 16, 0, 1, tzinfo=dt_util.UTC))
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass, datetime(2026, 1, 14, 16, 0, 0, tzinfo=dt_util.UTC))
    await hass.async_block_till_done()
    assert hass.states.get(PERIOD).state == "mid_peak"  # type: ignore[union-attr]
    assert float(hass.states.get(PRICE).state) == pytest.approx(0.157)  # type: ignore[union-attr]


async def test_rates_session_expiry_signs_in_again(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TOU)
    logins = len([r for r in portal.requests if r[1] == RATES_LOGIN])
    assert logins == 1

    portal.rates_session = "r2"  # the site forgets every session
    coordinator = mock_config_entry.runtime_data
    coordinator._rates_fetched_at = dt_util.utcnow() - timedelta(days=2)
    await _sync(hass, mock_config_entry)

    assert len([r for r in portal.requests if r[1] == RATES_LOGIN]) == logins + 1
    assert coordinator.last_update_success
    assert coordinator.rates is not None
    # The login form posts location id + customer number (portal username + password).
    body = next(r[3] for r in portal.requests if r[1] == RATES_LOGIN)
    assert body == b"CustomerNumber=hunter2&LocationID=demo"


async def test_rates_daily_not_every_update(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TOU)
    pages = len([r for r in portal.requests if r[1] == RATES_PAGE])
    for _ in range(3):
        await _sync(hass, mock_config_entry)
    assert len([r for r in portal.requests if r[1] == RATES_PAGE]) == pages


async def test_rates_outage_keeps_usage_and_last_rates(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TOU)
    coordinator = mock_config_entry.runtime_data
    known = coordinator.rates

    portal.rates_status_error = 503
    coordinator._rates_fetched_at = dt_util.utcnow() - timedelta(days=2)
    await _sync(hass, mock_config_entry)

    assert coordinator.last_update_success
    assert coordinator.rates == known
    assert hass.states.get(PRICE).state != "unavailable"  # type: ignore[union-attr]
    # One failed attempt, then a back-off rather than hammering the site every update.
    attempts = len([r for r in portal.requests if r[1] == RATES_PAGE])
    await _sync(hass, mock_config_entry)
    assert len([r for r in portal.requests if r[1] == RATES_PAGE]) == attempts


async def test_rates_survive_a_restart_while_the_site_is_down(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TOU)
    known = mock_config_entry.runtime_data.rates
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    portal.rates_status_error = 503
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await _settle(hass)
    assert mock_config_entry.runtime_data.rates == known
    assert hass.states.get(PRICE).state != "unavailable"  # type: ignore[union-attr]


async def test_unrecognized_page_never_prices(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    portal.rates_html = "<p>We have moved!</p>"
    await _setup(hass, mock_config_entry, Plan.TOU)
    assert mock_config_entry.runtime_data.last_update_success
    assert await _rows(hass, ELECTRICITY_COST) == []
    assert hass.states.get(PRICE) is None


async def test_options_flow_defaults_and_translations(
    recorder_mock: Recorder, hass: HomeAssistant, portal, patch_client, mock_config_entry
) -> None:
    from homeassistant.helpers.translation import async_get_translations

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    data_schema = result["data_schema"]
    assert data_schema is not None
    schema = data_schema.schema
    defaults = {str(k): k.default() for k in schema}
    assert defaults == {CONF_RATE_PLAN: "none", CONF_INCLUDE_FEES: True}

    options = await async_get_translations(hass, "en", "options", {DOMAIN})
    selectors = await async_get_translations(hass, "en", "selector", {DOMAIN})
    entity = await async_get_translations(hass, "en", "entity", {DOMAIN})
    for key in (
        "component.provident.options.step.init.data.rate_plan",
        "component.provident.options.step.init.data.include_fees",
    ):
        assert key in options, key
    for plan in Plan:
        assert f"component.provident.selector.rate_plan.options.{plan}" in selectors
    for period in Period:
        assert f"component.provident.entity.sensor.price_period.state.{period}" in entity


async def test_diagnostics_include_the_rates(
    recorder_mock: Recorder, hass: HomeAssistant, portal, frozen, patch_client, mock_config_entry
) -> None:
    await _setup(hass, mock_config_entry, Plan.TOU)
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    assert diagnostics["rate_plan"] == "tou"
    assert diagnostics["rates"]["energy"]["tou"]["on_peak"]["current"] == 20.3
    assert diagnostics["rates"]["unknown"] == {}
    priced = {m["utility"]: m["priced"] for m in diagnostics["meters"]}
    assert priced == {"Electricity": True, "Cold Water": False, "Hot Water": False}
    assert "hunter2" not in repr(diagnostics)
