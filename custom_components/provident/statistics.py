# SPDX-License-Identifier: GPL-3.0-only

"""Hourly consumption imported as external statistics with a running sum."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING

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
    HOUR_CHUNK_DAYS,
    REFETCH_DAYS,
    UTILITY_LABEL,
    meter_unit_info,
)
from .coordinator import call_portal

if TYPE_CHECKING:
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


async def _sync_meter(
    hass: HomeAssistant,
    coordinator: ProvidentCoordinator,
    meter: Meter,
    unit: str,
    unit_class: str,
    today: date,
) -> None:
    stat_id = statistic_id(meter)
    tz = dt_util.get_time_zone(hass.config.time_zone)
    assert tz is not None
    meta = metadata(meter, unit, unit_class)
    now = dt_util.utcnow()

    earliest = today - timedelta(days=coordinator.backfill_days)
    last = (
        await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, stat_id, True, {"sum"}
        )
    ).get(stat_id)
    if not last:
        window_first_day = earliest
        window_first_start = bucket_start(window_first_day, 0, tz)
        running_sum = 0.0
    else:
        last_start = dt_util.utc_from_timestamp(float(last[0]["start"]))
        last_day = last_start.astimezone(tz).date()
        window_first_day = max(earliest, min(today - timedelta(days=REFETCH_DAYS), last_day))
        window_first_start = bucket_start(window_first_day, 0, tz)
        if last_start < window_first_start:
            # Newest row predates the window (capped by earliest): its sum is the baseline.
            running_sum = float(last[0]["sum"] or 0.0)
        else:
            prev = (
                await get_instance(hass).async_add_executor_job(
                    statistics_during_period,
                    hass,
                    window_first_start - timedelta(hours=1),
                    window_first_start,
                    {stat_id},
                    "hour",
                    None,
                    {"sum"},
                )
            ).get(stat_id, [])
            running_sum = float(prev[0]["sum"] or 0.0) if prev else 0.0

    # Spring-forward: two buckets share a UTC hour and the recorder keeps one row per start, so merge.
    by_start: dict[datetime, float] = {}
    day = window_first_day
    while day <= today:
        chunk_end = min(day + timedelta(days=HOUR_CHUNK_DAYS), today + timedelta(days=1))
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
                if start >= now:
                    continue
                by_start[start] = by_start.get(start, 0.0) + value
        day = chunk_end

    stats: list[StatisticData] = []
    for start in sorted(by_start):
        running_sum += by_start[start]
        stats.append(
            StatisticData(
                start=start,
                state=round(by_start[start], 4),
                sum=round(running_sum, 4),
            )
        )

    if stats:
        async_add_external_statistics(hass, meta, stats)
        _LOGGER.debug("Imported %d hourly statistics for %s", len(stats), stat_id)


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
