# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest
from homeassistant.components.recorder import Recorder  # type: ignore[attr-defined]
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.provident.const import (
    CONF_BACKFILL_DAYS,
    DEFAULT_BACKFILL_DAYS,
    REFETCH_DAYS,
)
from custom_components.provident.statistics import bucket_start

ELECTRICITY_ID = "provident:85006408_9_hourly_consumption"
COLD_WATER_ID = "provident:23166694_2_hourly_consumption"
HOT_WATER_ID = "provident:24119905_2_hourly_consumption"
ALL_IDS = {ELECTRICITY_ID, COLD_WATER_ID, HOT_WATER_ID}


def _during_period(
    hass: HomeAssistant,
    stat_id: str,
    types: set[str],
) -> Any:
    return hass.async_add_executor_job(
        cast(Any, statistics_during_period),
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {stat_id},
        "hour",
        None,
        types,
    )


async def _stats(
    hass: HomeAssistant, stat_id: str, types: set[str] | None = None
) -> list[dict[str, Any]]:
    """Rows for one statistic, oldest first."""
    rows: Any = await _during_period(hass, stat_id, types or {"state", "sum"})
    return list(rows.get(stat_id, []))


async def _sync(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def test_first_import_backfills(
    recorder_mock: Recorder,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
) -> None:
    frozen_now = datetime(2026, 10, 20, 12, 0, tzinfo=dt_util.UTC)
    freezer.move_to(frozen_now)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    await _sync(hass, mock_config_entry)

    coordinator = mock_config_entry.runtime_data
    window_day = coordinator.data.today - timedelta(days=DEFAULT_BACKFILL_DAYS)
    first = bucket_start(window_day, 0, dt_util.UTC)
    expected = {stat_id: int((frozen_now - first).total_seconds() // 3600) for stat_id in ALL_IDS}

    for stat_id in ALL_IDS:
        rows = await _stats(hass, stat_id)
        assert rows, stat_id
        assert len(rows) == expected[stat_id], (stat_id, len(rows), expected[stat_id])
        starts = [dt_util.utc_from_timestamp(r["start"]) for r in rows]
        assert all(b - a == timedelta(hours=1) for a, b in zip(starts, starts[1:], strict=False)), (
            stat_id
        )
        sums = [r["sum"] for r in rows]
        assert all(b >= a for a, b in zip(sums, sums[1:], strict=False))
        assert rows[-1]["start"] < frozen_now.timestamp()


async def test_states_match_portal(
    recorder_mock: Recorder,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
) -> None:
    frozen_now = datetime(2026, 10, 20, 12, 0, tzinfo=dt_util.UTC)
    freezer.move_to(frozen_now)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    await _sync(hass, mock_config_entry)

    coordinator = mock_config_entry.runtime_data
    today = coordinator.data.today
    rows = await _stats(hass, ELECTRICITY_ID)
    by_start = {row["start"]: row["state"] for row in rows}

    day = today - timedelta(days=DEFAULT_BACKFILL_DAYS)
    checked = 0
    while day <= today:
        for hour in range(24):
            start = (
                datetime.combine(day, datetime.min.time())
                .replace(tzinfo=dt_util.UTC, hour=hour)
                .timestamp()
            )
            if start in by_start:
                expected = portal.hour_value("Electricity", day, hour)
                if day >= today:  # delayed utility: unpublished today
                    expected = 0.0
                assert by_start[start] == pytest.approx(expected), (day, hour)
                checked += 1
        day += timedelta(days=1)
    assert checked > 24 * 7


async def test_incremental_sync_after_time_passes(
    recorder_mock: Recorder,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
) -> None:
    # Mid-day: no midnight crossing, no publication flips.
    freezer.move_to(datetime(2026, 10, 20, 10, 30, tzinfo=dt_util.UTC))
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    before = await _stats(hass, COLD_WATER_ID)
    last_sum = before[-1]["sum"]
    last_start = before[-1]["start"]

    freezer.tick(timedelta(hours=1))
    await _sync(hass, mock_config_entry)

    after = await _stats(hass, COLD_WATER_ID)
    assert len(after) == len(before) + 1
    assert after[-1]["start"] == last_start + 3600
    # One new hour plus the previous placeholder hour's late correction.
    assert after[-1]["sum"] - last_sum == pytest.approx(
        sum(r["state"] for r in after) - sum(r["state"] for r in before)
    )


async def test_restated_hour_is_corrected(
    recorder_mock: Recorder, hass: HomeAssistant, portal, setup_entry
) -> None:
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    coordinator = setup_entry.runtime_data
    yesterday = coordinator.data.today - timedelta(days=1)
    hour = 10
    old = portal.hour_value("Cold Water", yesterday, hour)
    portal.set_hour("Cold Water", yesterday, hour, old + 1.0)

    before = await _stats(hass, COLD_WATER_ID)
    await _sync(hass, setup_entry)
    after = await _stats(hass, COLD_WATER_ID)

    assert len(after) == len(before)
    day_start = bucket_start(yesterday, hour, dt_util.UTC).timestamp()
    row = next(r for r in after if r["start"] == day_start)
    assert row["state"] == pytest.approx(old + 1.0)
    delta = after[-1]["sum"] - before[-1]["sum"]
    assert delta == pytest.approx(1.0)


async def test_late_data_beyond_window_not_refetched(
    recorder_mock: Recorder, hass: HomeAssistant, portal, setup_entry
) -> None:
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    coordinator = setup_entry.runtime_data
    stale_day = coordinator.data.today - timedelta(days=REFETCH_DAYS + 5)
    old = portal.hour_value("Cold Water", stale_day, 12)
    portal.set_hour("Cold Water", stale_day, 12, old + 5.0)

    before = await _stats(hass, COLD_WATER_ID)
    await _sync(hass, setup_entry)
    after = await _stats(hass, COLD_WATER_ID)
    assert after == before


async def test_delayed_utility_catches_up(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer,
    portal,
    setup_entry,
) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    coordinator = setup_entry.runtime_data
    today = coordinator.data.today
    rows = await _stats(hass, ELECTRICITY_ID)
    today_midnight = (
        datetime.combine(today, datetime.min.time()).replace(tzinfo=dt_util.UTC).timestamp()
    )
    today_states = [r["state"] for r in rows if r["start"] >= today_midnight]
    # Elapsed hours of the unpublished day import as 0.0 until corrected.
    assert today_states and set(today_states) == {0.0}

    freezer.tick(timedelta(days=1, hours=6))
    await _sync(hass, setup_entry)

    rows = await _stats(hass, ELECTRICITY_ID)
    day_total = portal.day_value("Electricity", today)
    hour_states = [
        r["state"]
        for r in rows
        if datetime.combine(today, datetime.min.time()).replace(tzinfo=dt_util.UTC).timestamp()
        <= r["start"]
        < datetime.combine(today + timedelta(days=1), datetime.min.time())
        .replace(tzinfo=dt_util.UTC)
        .timestamp()
    ]
    assert len(hour_states) == 24
    assert sum(hour_states) == pytest.approx(day_total)


async def test_spring_forward_day_keeps_every_bucket(
    recorder_mock: Recorder,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
) -> None:
    """24 buckets on a 23-hour day: two share a UTC hour and are merged."""
    freezer.move_to(datetime(2026, 3, 10, 12, 0, tzinfo=dt_util.UTC))
    await hass.config.async_set_time_zone("America/Toronto")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    transition_day = date(2026, 3, 8)  # clocks jump 02:00 -> 03:00
    tz = dt_util.get_time_zone("America/Toronto")
    assert tz is not None
    day_start = bucket_start(transition_day, 0, tz)
    day_end = bucket_start(transition_day + timedelta(days=1), 0, tz)
    assert day_end - day_start == timedelta(hours=23)

    for stat_id, utility in ((ELECTRICITY_ID, "Electricity"), (COLD_WATER_ID, "Cold Water")):
        rows = [
            r
            for r in await _stats(hass, stat_id)
            if day_start.timestamp() <= r["start"] < day_end.timestamp()
        ]
        # The merged hour carries two buckets.
        assert len(rows) == 23, stat_id
        assert sum(r["state"] for r in rows) == pytest.approx(
            portal.day_value(utility, transition_day), abs=1e-3
        ), stat_id
        merged = next(
            r for r in rows if r["start"] == bucket_start(transition_day, 2, tz).timestamp()
        )
        assert merged["state"] == pytest.approx(
            portal.hour_value(utility, transition_day, 2)
            + portal.hour_value(utility, transition_day, 3),
            abs=1e-3,
        ), stat_id


async def test_fall_back_day_keeps_every_bucket(
    recorder_mock: Recorder,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
) -> None:
    """24 buckets on a 25-hour day: the extra UTC hour has no row."""
    freezer.move_to(datetime(2025, 11, 4, 12, 0, tzinfo=dt_util.UTC))
    await hass.config.async_set_time_zone("America/Toronto")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    transition_day = date(2025, 11, 2)  # clocks fall back 02:00 -> 01:00
    tz = dt_util.get_time_zone("America/Toronto")
    assert tz is not None
    day_start = bucket_start(transition_day, 0, tz)
    day_end = bucket_start(transition_day + timedelta(days=1), 0, tz)
    assert day_end - day_start == timedelta(hours=25)

    rows = [
        r
        for r in await _stats(hass, COLD_WATER_ID)
        if day_start.timestamp() <= r["start"] < day_end.timestamp()
    ]
    assert len(rows) == 24
    assert sum(r["state"] for r in rows) == pytest.approx(
        portal.day_value("Cold Water", transition_day), abs=1e-3
    )
    sums = [r["sum"] for r in rows]
    assert all(b >= a for a, b in zip(sums, sums[1:], strict=False))


async def test_new_meter_backfilled_after_discovery(
    recorder_mock: Recorder, hass: HomeAssistant, portal, setup_entry
) -> None:
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    gas = {
        "id": 700001,
        "node": "MP:700001",
        "name": "3105",
        "utility": "Gas",
        "unit": "m3",
        "serial": "99900001-1",
    }
    portal.meters.append(gas)
    await _sync(hass, setup_entry)

    rows = await _stats(hass, "provident:99900001_1_hourly_consumption")
    assert rows


async def test_unmapped_unit_skipped(
    recorder_mock: Recorder, hass: HomeAssistant, portal, setup_entry
) -> None:
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    portal.meters = [
        {**portal.meters[0], "unit": "therms", "utility": "Gas", "serial": "88800001-1"}
    ]
    await _sync(hass, setup_entry)
    await async_wait_recording_done(hass)

    assert await _stats(hass, "provident:88800001_1_hourly_consumption") == []


async def test_configured_backfill_days(
    recorder_mock: Recorder,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
) -> None:
    frozen_now = datetime(2026, 10, 20, 12, 0, tzinfo=dt_util.UTC)
    freezer.move_to(frozen_now)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_BACKFILL_DAYS: 7}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    first = bucket_start(
        mock_config_entry.runtime_data.data.today - timedelta(days=7), 0, dt_util.UTC
    )
    for stat_id in ALL_IDS:
        rows = await _stats(hass, stat_id)
        assert rows[0]["start"] == first.timestamp(), stat_id
        assert len(rows) == int((frozen_now - first).total_seconds() // 3600), stat_id


async def test_downtime_longer_than_refetch_window_has_no_gap(
    recorder_mock: Recorder,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
) -> None:
    freezer.move_to(datetime(2026, 10, 20, 10, 30, tzinfo=dt_util.UTC))
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    before = await _stats(hass, COLD_WATER_ID)
    last_sum = before[-1]["sum"]
    last_start = before[-1]["start"]

    freezer.tick(timedelta(days=REFETCH_DAYS + 3))
    await _sync(hass, mock_config_entry)

    after = await _stats(hass, COLD_WATER_ID)
    starts = [r["start"] for r in after]
    assert all(b - a == 3600 for a, b in zip(starts, starts[1:], strict=False))
    sums = [r["sum"] for r in after]
    assert all(b >= a for a, b in zip(sums, sums[1:], strict=False))
    # Every step adds exactly its own state: a baseline reset would break this at the window start.
    assert all(
        cur["sum"] - prev["sum"] == pytest.approx(cur["state"], abs=1e-3)
        for prev, cur in zip(after, after[1:], strict=False)
    )
    assert after[-1]["sum"] > last_sum
    assert after[-1]["start"] > last_start + 4 * 24 * 3600


async def test_raising_backfill_days_imports_older_days(
    recorder_mock: Recorder,
    freezer,
    hass: HomeAssistant,
    portal,
    patch_client: None,
    mock_config_entry,
) -> None:
    """A larger ``backfill_days`` fills the days before the first stored row, sums intact."""
    frozen_now = datetime(2026, 10, 20, 12, 0, tzinfo=dt_util.UTC)
    freezer.move_to(frozen_now)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_BACKFILL_DAYS: 7}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    coordinator = mock_config_entry.runtime_data
    before = {r["start"]: r["state"] for r in await _stats(hass, COLD_WATER_ID)}
    assert coordinator.backfilled[COLD_WATER_ID] == 7

    coordinator.backfill_days = 30
    await _sync(hass, mock_config_entry)

    rows = await _stats(hass, COLD_WATER_ID)
    first = bucket_start(coordinator.data.today - timedelta(days=30), 0, dt_util.UTC)
    assert rows[0]["start"] == first.timestamp()
    assert len(rows) == int((frozen_now - first).total_seconds() // 3600)
    starts = [r["start"] for r in rows]
    assert all(b - a == 3600 for a, b in zip(starts, starts[1:], strict=False))
    assert rows[0]["sum"] == pytest.approx(rows[0]["state"], abs=1e-3)
    assert all(
        cur["sum"] - prev["sum"] == pytest.approx(cur["state"], abs=1e-3)
        for prev, cur in zip(rows, rows[1:], strict=False)
    )
    # Hours that were already imported keep their values.
    assert all(
        r["state"] == pytest.approx(before[r["start"]], abs=1e-3)
        for r in rows
        if r["start"] in before
    )
    assert coordinator.backfilled[COLD_WATER_ID] == 30
    stored = await coordinator._store.async_load()
    assert stored is not None
    assert stored["backfilled"][COLD_WATER_ID] == 30
