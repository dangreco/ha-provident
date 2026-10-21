# SPDX-License-Identifier: GPL-3.0-only

"""Read-only tests against the real portal; run with ``make py-test-e2e``."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder  # type: ignore[attr-defined]
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.provident.const import DOMAIN

pytestmark = pytest.mark.e2e


@pytest.fixture
def real_credentials() -> dict[str, str]:
    username = os.environ.get("PROVIDENT_USERNAME")
    password = os.environ.get("PROVIDENT_PASSWORD")
    if not username or not password:
        pytest.skip("PROVIDENT_USERNAME / PROVIDENT_PASSWORD not set")
    return {CONF_USERNAME: username, CONF_PASSWORD: password}


async def test_real_portal_end_to_end(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    real_credentials: dict[str, str],
) -> None:
    from homeassistant import loader

    # The portal's suite is in Toronto.
    await hass.config.async_set_time_zone("America/Toronto")
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Provident (e2e)",
        data=real_credentials,
        unique_id=real_credentials[CONF_USERNAME],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    coordinator = entry.runtime_data
    assert coordinator.data.meters, "account has no meters"
    for meter_data in coordinator.data.meters:
        assert meter_data.register is not None
        assert not meter_data.register.error
        assert (meter_data.register.current or 0) > 0
        assert meter_data.daily.yesterday >= 0

    # 24 buckets per local day.
    client = coordinator.client
    today = coordinator.data.today
    for meter_data in coordinator.data.meters:
        series = await client.usage.series(
            meter_data.meter, today - timedelta(days=2), today + timedelta(days=1)
        )
        assert len(series[0].data) == 72

    from homeassistant.components.recorder.statistics import (
        statistics_during_period,
    )

    from custom_components.provident.statistics import statistic_id

    for meter_data in coordinator.data.meters:
        stat_id = statistic_id(meter_data.meter)
        rows = await hass.async_add_executor_job(
            statistics_during_period,  # type: ignore[arg-type]
            hass,
            dt_util.utcnow() - timedelta(days=40),
            None,
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        sums: list[float] = [row["sum"] for row in rows.get(stat_id, []) if row["sum"] is not None]
        assert len(sums) > 24 * 20, stat_id
        assert all(b >= a for a, b in zip(sums, sums[1:], strict=False)), stat_id


async def test_real_rates_price_the_real_meter(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    real_credentials: dict[str, str],
) -> None:
    """Live: fetch the published rates, price the account with TOU, check cost and sensors."""
    from homeassistant import loader
    from homeassistant.components.recorder.statistics import statistics_during_period
    from provident import Utility

    from custom_components.provident.const import CONF_INCLUDE_FEES, CONF_RATE_PLAN
    from custom_components.provident.rates import PLAN_PERIODS, Period, Plan
    from custom_components.provident.statistics import cost_statistic_id, statistic_id

    await hass.config.async_set_time_zone("America/Toronto")
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Provident (e2e)",
        data=real_credentials,
        options={CONF_RATE_PLAN: "tou", CONF_INCLUDE_FEES: True},
        unique_id=real_credentials[CONF_USERNAME],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    coordinator = entry.runtime_data
    rates = coordinator.rates
    assert rates is not None, "rates page not fetched"
    # Every plan fully published, plus both fees; nothing unrecognized on the live page.
    for plan, periods in PLAN_PERIODS.items():
        assert set(rates.energy[plan]) == set(periods), plan
    assert rates.admin_fee is not None and rates.meter_fee is not None
    assert not rates.unknown, rates.unknown

    electricity = [m for m in coordinator.data.meters if m.meter.utility == Utility.ELECTRICITY]
    assert electricity, "account has no electricity meter"
    for meter_data in coordinator.data.meters:
        priced = coordinator.tariff(meter_data.meter) is not None
        assert priced == (meter_data.meter.utility == Utility.ELECTRICITY), meter_data.meter

    for meter_data in electricity:
        meter = meter_data.meter
        rows: dict[str, list[Any]] = {}
        for stat_id in (statistic_id(meter), cost_statistic_id(meter)):
            result = await hass.async_add_executor_job(
                statistics_during_period,  # type: ignore[arg-type]
                hass,
                dt_util.utcnow() - timedelta(days=40),
                None,
                {stat_id},
                "hour",
                None,
                {"state", "sum"},
            )
            rows[stat_id] = list(result.get(stat_id, []))
        usage, cost = rows[statistic_id(meter)], rows[cost_statistic_id(meter)]
        assert len(cost) > 24 * 20, len(cost)
        by_start = {r["start"]: r["state"] for r in usage}
        fee_floor = 0.0
        for row in cost:
            # Fees alone make every hour cost something; energy never makes it negative.
            assert row["state"] > fee_floor, row
            kwh = by_start[row["start"]]
            # Bounded by the cheapest and dearest TOU price plus the hourly fee share.
            assert row["state"] <= kwh * 0.5 + 1.0, row
        sums = [r["sum"] for r in cost]
        assert all(b >= a for a, b in zip(sums, sums[1:], strict=False))

        slug = f"sensor.{meter.name}_electricity"
        price = hass.states.get(f"{slug}_current_price")
        period = hass.states.get(f"{slug}_price_period")
        assert price is not None and 0 < float(price.state) < 1, price
        assert period is not None and Period(period.state) in PLAN_PERIODS[Plan.TOU], period
