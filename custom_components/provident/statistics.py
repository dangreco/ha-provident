# SPDX-License-Identifier: GPL-3.0-only

"""Hourly consumption and cost imported as external statistics with running sums."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from provident import Meter, Series

from .const import (
    CURRENCY,
    HOUR_CHUNK_DAYS,
    REFETCH_DAYS,
    UTILITY_LABEL,
    meter_unit_info,
)
from .coordinator import call_portal
from .rates import KWH_PER_UNIT, Plan, RateSchedule

if TYPE_CHECKING:
    from homeassistant.components.recorder.statistics import StatisticsRow

    from .coordinator import ProvidentCoordinator

_LOGGER = logging.getLogger(__name__)


def statistic_id(meter: Meter) -> str:
    return f"provident:{slugify(meter.serial)}_hourly_consumption"


def metadata(meter: Meter, unit: str, unit_class: str) -> StatisticMetaData:
    label = UTILITY_LABEL.get(meter.utility, meter.utility)
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{meter.name} {label} hourly usage",
        source="provident",
        statistic_id=statistic_id(meter),
        unit_class=unit_class,
        unit_of_measurement=unit,
    )


def cost_statistic_id(meter: Meter) -> str:
    return f"provident:{slugify(meter.serial)}_hourly_cost"


def cost_metadata(meter: Meter) -> StatisticMetaData:
    label = UTILITY_LABEL.get(meter.utility, meter.utility)
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{meter.name} {label} hourly cost",
        source="provident",
        statistic_id=cost_statistic_id(meter),
        unit_class=None,
        unit_of_measurement=CURRENCY,
    )


def bucket_start(day: date, index: int, tz: tzinfo) -> datetime:
    """UTC start of hour bucket ``index`` of local ``day``, on wall-clock hours."""
    local_midnight = datetime.combine(day, time(), tzinfo=tz)
    return (local_midnight + timedelta(hours=index)).astimezone(dt_util.UTC)


async def async_sync_statistics(hass: HomeAssistant, coordinator: ProvidentCoordinator) -> None:
    if hass.state is not CoreState.running:
        return
    if "recorder" not in hass.config.components:
        _LOGGER.debug("Recorder not loaded; skipping statistics import")
        return

    data = coordinator.data
    if coordinator.statistics_lock.locked():
        _LOGGER.debug("Statistics sync already running; skipping")
        return
    async with coordinator.statistics_lock:
        priced_before = dict(coordinator.priced)
        for meter_data in data.meters:
            meter = meter_data.meter
            _device_class, unit, unit_class = meter_unit_info(meter)
            if not unit_class:
                # The recorder cannot convert unmapped units.
                _LOGGER.warning(
                    "Skipping statistics for meter %s: unmapped unit %r",
                    meter.serial,
                    meter.unit,
                )
                continue
            try:
                await _sync_meter(hass, coordinator, meter, unit, unit_class, data.today)
            except Exception as err:
                # Background loop: log and retry next refresh (HA stopping mid-sync can raise).
                _LOGGER.warning("Statistics sync failed for meter %s: %s", meter.serial, err)
        # Commit rows so the next sync's get_last_statistics sees them.
        await get_instance(hass).async_block_till_done()
        if coordinator.priced != priced_before:
            await coordinator.async_save_state()


def _pricing_key(coordinator: ProvidentCoordinator) -> dict[str, Any]:
    """What a cost statistic was priced with; a change of either part re-prices it."""
    return {"plan": str(coordinator.plan), "include_fees": coordinator.include_fees}


async def _last_row(hass: HomeAssistant, stat_id: str) -> StatisticsRow | None:
    rows = (
        await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, stat_id, True, {"sum"}
        )
    ).get(stat_id)
    return rows[0] if rows else None


async def _sum_before(hass: HomeAssistant, stat_id: str, start: datetime) -> float:
    """Running sum of the newest row that starts before ``start`` (0 if none)."""
    rows = (
        await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            # Rows are contiguous, so the newest one before ``start`` is within a day.
            start - timedelta(days=1),
            start,
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
    ).get(stat_id, [])
    return float(rows[-1]["sum"] or 0.0) if rows else 0.0


def _window(
    last: StatisticsRow | None, earliest: date, today: date, tz: tzinfo
) -> tuple[date, float | None]:
    """First day to (re)import and, if the newest row predates it, that row's sum."""
    if last is None:
        return earliest, 0.0
    last_start = dt_util.utc_from_timestamp(float(last["start"]))
    first_day = max(
        earliest, min(today - timedelta(days=REFETCH_DAYS), last_start.astimezone(tz).date())
    )
    if last_start < bucket_start(first_day, 0, tz):
        # Newest row predates the window (capped by earliest): its sum is the baseline.
        return first_day, float(last["sum"] or 0.0)
    return first_day, None


async def _sync_meter(
    hass: HomeAssistant,
    coordinator: ProvidentCoordinator,
    meter: Meter,
    unit: str,
    unit_class: str,
    today: date,
) -> None:
    tz = dt_util.get_time_zone(hass.config.time_zone)
    assert tz is not None
    earliest = today - timedelta(days=coordinator.backfill_days)
    usage_id = statistic_id(meter)
    usage_first, usage_base = _window(await _last_row(hass, usage_id), earliest, today, tz)

    tariff = coordinator.tariff(meter)
    cost_id = cost_statistic_id(meter)
    cost_first: date | None = None
    cost_base: float | None = None
    if tariff is not None:
        cost_first, cost_base = await _cost_window(hass, coordinator, meter, earliest, today, tz)

    fetch_first = usage_first if cost_first is None else min(usage_first, cost_first)
    buckets = await _hourly(coordinator, meter, fetch_first, today, tz)

    usage_start = bucket_start(usage_first, 0, tz)
    if usage_base is None:
        usage_base = await _sum_before(hass, usage_id, usage_start)
    usage_stats = _running(
        [(start, value) for start, _day, _hour, value in buckets if start >= usage_start],
        usage_base,
    )
    if usage_stats:
        async_add_external_statistics(hass, metadata(meter, unit, unit_class), usage_stats)
        _LOGGER.debug("Imported %d hourly statistics for %s", len(usage_stats), usage_id)

    if tariff is None or cost_first is None:
        return
    cost_start = bucket_start(cost_first, 0, tz)
    in_window = [b for b in buckets if b[0] >= cost_start]
    month_to_date: float | None = 0.0
    if tariff.plan is Plan.TIERED:
        month_to_date = await _month_before(coordinator, meter, cost_first, tz)
    kwh = KWH_PER_UNIT[unit]
    costs = tariff.costs(
        ((day, hour, value * kwh) for _s, day, hour, value in in_window), month_to_date
    )
    priced: list[tuple[datetime, float]] = []
    for (start, *_rest), cost in zip(in_window, costs, strict=True):
        if cost is None:
            if priced:
                # Later hours without a published price: stop at the gap rather than skip it.
                break
            continue
        priced.append((start, cost))
    if cost_base is None:
        cost_base = await _sum_before(hass, cost_id, cost_start)
    cost_stats = _running(_merge(priced), cost_base)
    if cost_stats:
        async_add_external_statistics(hass, cost_metadata(meter), cost_stats)
        _LOGGER.debug("Imported %d hourly costs for %s", len(cost_stats), cost_id)
    coordinator.priced[meter.serial] = {
        **_pricing_key(coordinator),
        "rates": coordinator.rates.to_dict() if coordinator.rates else None,
    }


async def _cost_window(
    hass: HomeAssistant,
    coordinator: ProvidentCoordinator,
    meter: Meter,
    earliest: date,
    today: date,
    tz: tzinfo,
) -> tuple[date, float | None]:
    """Where the cost statistic must be (re)imported from.

    A new plan or fee choice clears the statistic and prices the whole backfill again; a
    rates change re-prices from the first day it affects; otherwise only the usual window.
    """
    cost_id = cost_statistic_id(meter)
    previous = coordinator.priced.get(meter.serial)
    if previous is not None and {k: previous.get(k) for k in ("plan", "include_fees")} != (
        _pricing_key(coordinator)
    ):
        await _clear(hass, cost_id)
        coordinator.priced.pop(meter.serial, None)
        return earliest, 0.0
    first, base = _window(await _last_row(hass, cost_id), earliest, today, tz)
    if previous is not None and coordinator.rates is not None:
        old = RateSchedule.from_dict(previous["rates"]) if previous.get("rates") else None
        changed = coordinator.rates.changed_from(old)
        if changed is not None and changed < first:
            first = max(earliest, changed)
            base = None
    return first, base


async def _clear(hass: HomeAssistant, stat_id: str) -> None:
    instance = get_instance(hass)
    instance.async_clear_statistics([stat_id])
    await instance.async_block_till_done()


async def _month_before(
    coordinator: ProvidentCoordinator, meter: Meter, day: date, tz: tzinfo
) -> float:
    """kWh used in ``day``'s month before ``day`` (for the tiered threshold)."""
    month_start = day.replace(day=1)
    if month_start == day:
        return 0.0
    _device_class, unit, _unit_class = meter_unit_info(meter)
    buckets = await _hourly(coordinator, meter, month_start, day - timedelta(days=1), tz)
    return sum(value for *_rest, value in buckets) * KWH_PER_UNIT[unit]


async def _hourly(
    coordinator: ProvidentCoordinator, meter: Meter, first: date, last: date, tz: tzinfo
) -> list[tuple[datetime, date, int, float]]:
    """Published hourly buckets of local days ``first``..``last``, up to now, oldest first.

    Each is ``(UTC start, local day, wall-clock hour, value)``; on spring-forward days two
    buckets share a UTC start and are kept apart here (prices differ by hour), merged later.
    """
    now = dt_util.utcnow()
    buckets: list[tuple[datetime, date, int, float]] = []
    day = first
    while day <= last:
        chunk_end = min(day + timedelta(days=HOUR_CHUNK_DAYS), last + timedelta(days=1))
        series = await call_portal(
            _fetch_chunk(coordinator, meter, day, chunk_end),
            f"hourly series for meter {meter.serial}",
        )
        chunk = list(series[0].data) if series else []
        # The portal returns 24 buckets per local day, DST days included.
        expected = (chunk_end - day).days * 24
        if not chunk:
            _LOGGER.debug("No hourly data for meter %s from %s to %s", meter.serial, day, chunk_end)
        elif len(chunk) != expected:
            _LOGGER.warning(
                "Unexpected hourly series length for meter %s (%s..%s): got %d, expected %d",
                meter.serial,
                day,
                chunk_end,
                len(chunk),
                expected,
            )
        else:
            for i, value in enumerate(chunk):
                local_day = day + timedelta(days=i // 24)
                start = bucket_start(local_day, i % 24, tz)
                if start < now:
                    buckets.append((start, local_day, i % 24, value))
        day = chunk_end
    return buckets


def _merge(values: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Sum values sharing a UTC start (spring-forward): the recorder keeps one row per hour."""
    merged: dict[datetime, float] = {}
    for start, value in values:
        merged[start] = merged.get(start, 0.0) + value
    return sorted(merged.items())


def _running(values: list[tuple[datetime, float]], base: float) -> list[StatisticData]:
    stats: list[StatisticData] = []
    total = base
    for start, value in _merge(values):
        total += value
        stats.append(StatisticData(start=start, state=round(value, 4), sum=round(total, 4)))
    return stats


async def _series_for(
    coordinator: ProvidentCoordinator, meter: Meter, start: date, end: date
) -> list[Series]:
    return await coordinator.client.usage.series(meter, start, end)


def _fetch_chunk(
    coordinator: ProvidentCoordinator, meter: Meter, start: date, end: date
) -> Callable[[], Awaitable[list[Series]]]:
    async def _fetch() -> list[Series]:
        return await _series_for(coordinator, meter, start, end)

    return _fetch
