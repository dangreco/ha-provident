# SPDX-License-Identifier: GPL-3.0-only

"""Rate rules: OEB periods and holidays, effective dates, tiers, fees, change detection."""

from __future__ import annotations

from datetime import date

import pytest
from provident import PublishedRates, RateEntry

from custom_components.provident.rates import (
    FEE_HOURS,
    Period,
    Plan,
    Price,
    RateSchedule,
    Tariff,
    holidays,
    is_ev_section,
    tariff_for,
    tier_threshold,
    time_period,
)

NOV1_2025 = date(2025, 11, 1)
NOV1_2024 = date(2024, 11, 1)


def price(current: float, previous: float | None = None) -> Price:
    return Price(
        current=current,
        current_effective=NOV1_2025,
        previous=previous,
        previous_effective=NOV1_2024 if previous is not None else None,
    )


def test_holidays_match_the_oeb_2026_schedule() -> None:
    # https://www.oeb.ca/.../holiday-schedule-time-use-and-ultra-low (2026 table).
    assert sorted(holidays(2026)) == [
        date(2026, 1, 1),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 18),
        date(2026, 7, 1),
        date(2026, 8, 3),
        date(2026, 9, 7),
        date(2026, 10, 12),
        date(2026, 12, 25),
        # Boxing Day is a Saturday: the next free weekday gets holiday pricing.
        date(2026, 12, 28),
    ]


def test_weekend_holidays_skip_to_the_next_free_weekday() -> None:
    # 2027: Christmas (Sat) -> Mon 27, Boxing Day (Sun) -> Tue 28.
    assert {date(2027, 12, 27), date(2027, 12, 28)} <= holidays(2027)
    # 2028: New Year's Day is a Saturday.
    assert date(2028, 1, 3) in holidays(2028)
    assert all(day.weekday() < 5 for year in range(2025, 2035) for day in holidays(year))


@pytest.mark.parametrize(
    ("day", "hour", "expected"),
    [
        # Winter weekday: on-peak mornings/evenings, mid-peak midday.
        (date(2026, 1, 14), 6, Period.OFF_PEAK),
        (date(2026, 1, 14), 7, Period.ON_PEAK),
        (date(2026, 1, 14), 10, Period.ON_PEAK),
        (date(2026, 1, 14), 11, Period.MID_PEAK),
        (date(2026, 1, 14), 16, Period.MID_PEAK),
        (date(2026, 1, 14), 17, Period.ON_PEAK),
        (date(2026, 1, 14), 18, Period.ON_PEAK),
        (date(2026, 1, 14), 19, Period.OFF_PEAK),
        # Summer weekday: the reverse.
        (date(2026, 7, 15), 7, Period.MID_PEAK),
        (date(2026, 7, 15), 11, Period.ON_PEAK),
        (date(2026, 7, 15), 16, Period.ON_PEAK),
        (date(2026, 7, 15), 17, Period.MID_PEAK),
        # Season boundaries.
        (date(2026, 4, 30), 12, Period.MID_PEAK),
        (date(2026, 5, 1), 12, Period.ON_PEAK),
        (date(2026, 10, 30), 12, Period.ON_PEAK),
        (date(2026, 11, 2), 12, Period.MID_PEAK),
        # Weekends and holidays are off-peak all day.
        (date(2026, 7, 18), 12, Period.OFF_PEAK),
        (date(2026, 7, 1), 12, Period.OFF_PEAK),
        (date(2026, 12, 28), 8, Period.OFF_PEAK),
    ],
)
def test_tou_periods(day: date, hour: int, expected: Period) -> None:
    assert time_period(Plan.TOU, day, hour) is expected


@pytest.mark.parametrize(
    ("day", "hour", "expected"),
    [
        (date(2026, 1, 14), 0, Period.ULTRA_LOW_OVERNIGHT),
        (date(2026, 1, 14), 6, Period.ULTRA_LOW_OVERNIGHT),
        (date(2026, 1, 14), 7, Period.MID_PEAK),
        (date(2026, 1, 14), 15, Period.MID_PEAK),
        (date(2026, 1, 14), 16, Period.ON_PEAK),
        (date(2026, 1, 14), 20, Period.ON_PEAK),
        (date(2026, 1, 14), 21, Period.MID_PEAK),
        (date(2026, 1, 14), 22, Period.MID_PEAK),
        (date(2026, 1, 14), 23, Period.ULTRA_LOW_OVERNIGHT),
        # Weekends/holidays: weekend off-peak by day, ultra-low overnight still applies.
        (date(2026, 7, 18), 12, Period.WEEKEND_OFF_PEAK),
        (date(2026, 7, 18), 23, Period.ULTRA_LOW_OVERNIGHT),
        (date(2026, 10, 12), 17, Period.WEEKEND_OFF_PEAK),
        # ULO is the same in summer and winter.
        (date(2026, 7, 15), 17, Period.ON_PEAK),
    ],
)
def test_ulo_periods(day: date, hour: int, expected: Period) -> None:
    assert time_period(Plan.ULO, day, hour) is expected


def test_tier_threshold_is_seasonal_and_prorated() -> None:
    assert tier_threshold(date(2026, 6, 10)) == pytest.approx(600 * 30 / 30)
    assert tier_threshold(date(2026, 7, 10)) == pytest.approx(600 * 31 / 30)
    assert tier_threshold(date(2026, 2, 10)) == pytest.approx(1000 * 28 / 30)
    assert tier_threshold(date(2026, 11, 1)) == pytest.approx(1000)


def test_price_follows_effective_dates() -> None:
    p = price(9.8, 7.6)
    assert p.on(date(2025, 11, 1)) == 9.8
    assert p.on(date(2025, 10, 31)) == 7.6
    assert p.on(date(2024, 11, 1)) == 7.6
    # Before anything published took effect: unknown, not a guess.
    assert p.on(date(2024, 10, 31)) is None
    assert Price(current=9.8).on(date(2000, 1, 1)) == 9.8


def test_tou_costs_by_hour_and_rate_change() -> None:
    tariff = Tariff(
        plan=Plan.TOU,
        energy={
            Period.OFF_PEAK: price(9.8, 7.6),
            Period.MID_PEAK: price(15.7, 12.2),
            Period.ON_PEAK: price(20.3, 15.8),
        },
    )
    costs = tariff.costs(
        [
            (date(2025, 10, 31), 12, 1.0),  # Fri, summer on-peak, previous price
            (date(2025, 11, 3), 12, 2.0),  # Mon, winter mid-peak, current price
            (date(2025, 11, 3), 8, 1.0),  # winter on-peak
            (date(2025, 11, 1), 12, 1.0),  # Sat off-peak
        ],
        0.0,
    )
    assert costs == pytest.approx([0.158, 2 * 0.157, 0.203, 0.098])


def test_fees_spread_hourly_and_follow_their_own_dates() -> None:
    admin = Price(11.90, date(2026, 4, 16), 11.69, date(2025, 11, 16))
    meter = Price(8.46, date(2026, 4, 16), 8.31, date(2025, 11, 16))
    tariff = Tariff(plan=Plan.TOU, energy={Period.OFF_PEAK: price(9.8)}, fees=(admin, meter))
    assert tariff.fee_per_hour(date(2026, 5, 1)) == pytest.approx((11.90 + 8.46) / FEE_HOURS)
    assert tariff.fee_per_hour(date(2026, 1, 1)) == pytest.approx((11.69 + 8.31) / FEE_HOURS)
    # Before the earliest fee: the whole hour is unpriced rather than energy-only.
    assert tariff.fee_per_hour(date(2025, 11, 15)) is None
    (cost,) = tariff.costs([(date(2026, 5, 2), 12, 0.0)], 0.0)  # Saturday, zero usage
    assert cost == pytest.approx((11.90 + 8.46) / FEE_HOURS)


def test_tiered_splits_the_hour_that_crosses_the_threshold() -> None:
    tariff = Tariff(plan=Plan.TIERED, energy={Period.TIER_1: price(12), Period.TIER_2: price(14.2)})
    june = date(2026, 6, 15)  # threshold 600 kWh
    (cost,) = tariff.costs([(june, 12, 10.0)], 595.0)
    assert cost == pytest.approx(5 * 0.12 + 5 * 0.142)
    assert tariff.period(june, 12, 595.0) is Period.TIER_1
    assert tariff.period(june, 12, 600.0) is Period.TIER_2
    # A new month starts over at tier 1.
    costs = tariff.costs([(date(2026, 6, 30), 23, 1.0), (date(2026, 7, 1), 0, 1.0)], 700.0)
    assert costs == pytest.approx([0.142, 0.12])
    # Unknown month-to-date: unpriced, not assumed.
    assert tariff.costs([(june, 12, 1.0)], None) == [None]
    assert tariff.price(june, 12, None) is None


def _published(**overrides: object) -> PublishedRates:
    """A stand-in with the PublishedRates surface (the real one has no constructor)."""

    def entry(current: float, previous: float) -> RateEntry:
        return _Entry(current, NOV1_2025, previous, NOV1_2024)  # type: ignore[return-value]

    values: dict[str, object] = {
        "tier_1": entry(12, 9.3),
        "tier_2": entry(14.2, 11),
        "tou_off_peak": entry(9.8, 7.6),
        "tou_mid_peak": entry(15.7, 12.2),
        "tou_on_peak": entry(20.3, 15.8),
        "ulo_ultra_low_overnight": entry(3.9, 2.8),
        "ulo_weekend_off_peak": entry(9.8, 7.6),
        "ulo_mid_peak": entry(15.7, 12.2),
        "ulo_on_peak": entry(39.1, 28.4),
        "admin_fee": _Entry(11.90, date(2026, 4, 16), 11.69, date(2025, 11, 16)),
        "meter_fee": _Entry(8.46, date(2026, 4, 16), 8.31, date(2025, 11, 16)),
        "sections": {},
        "unknown": {},
    }
    values.update(overrides)
    return type("Published", (), values)()  # type: ignore[no-any-return]


class _Entry:
    def __init__(self, current: float, ce: date, previous: float, pe: date) -> None:
        self.current, self.current_effective = current, ce
        self.previous, self.previous_effective = previous, pe


def test_schedule_from_published_and_round_trip() -> None:
    schedule = RateSchedule.from_published(_published())
    assert schedule.energy[Plan.ULO][Period.ON_PEAK].current == 39.1
    assert schedule.energy[Plan.TOU][Period.ON_PEAK].current == 20.3
    assert schedule.admin_fee is not None and schedule.admin_fee.previous == 11.69
    assert RateSchedule.from_dict(schedule.to_dict()) == schedule


def test_ev_meters_are_priced_only_from_an_ev_section() -> None:
    schedule = RateSchedule.from_published(_published())
    assert tariff_for(schedule, Plan.TOU, ev=True, include_fees=True) is None
    ev_rows = {
        "Off-Peak": _Entry(8.0, NOV1_2025, 7.0, NOV1_2024),
        "Mid-Peak": _Entry(12.0, NOV1_2025, 11.0, NOV1_2024),
        "On-Peak": _Entry(25.0, NOV1_2025, 22.0, NOV1_2024),
    }
    with_ev = RateSchedule.from_published(_published(sections={"EV Charging Rates": ev_rows}))
    tariff = tariff_for(with_ev, Plan.TOU, ev=True, include_fees=True)
    assert tariff is not None
    assert tariff.price(date(2026, 1, 14), 8, 0.0) == pytest.approx(0.25)
    # Provident's suite fees are not charged on an EV meter.
    assert tariff.fees == ()
    # A water section never prices anything.
    water = RateSchedule.from_published(
        _published(sections={"Hot Water": {"Rate": _Entry(4.2, NOV1_2025, 4.0, NOV1_2024)}})
    )
    assert water.ev == {}
    assert is_ev_section("Electric Vehicle Charging") and not is_ev_section("Hot Water")


def test_tariff_needs_every_period_and_both_fees() -> None:
    schedule = RateSchedule.from_published(_published(tou_mid_peak=None))
    assert tariff_for(schedule, Plan.TOU, ev=False, include_fees=False) is None
    assert tariff_for(schedule, Plan.ULO, ev=False, include_fees=False) is not None
    no_fees = RateSchedule.from_published(_published(meter_fee=None))
    assert tariff_for(no_fees, Plan.TOU, ev=False, include_fees=True) is None
    assert tariff_for(no_fees, Plan.TOU, ev=False, include_fees=False) is not None
    assert tariff_for(None, Plan.TOU, ev=False, include_fees=False) is None
    assert tariff_for(schedule, Plan.NONE, ev=False, include_fees=False) is None


def test_schedule_change_reports_the_first_day_priced_differently() -> None:
    old = RateSchedule.from_published(_published())
    assert old.changed_from(old) is None
    assert old.changed_from(None) is None
    # The Nov 1 repricing: current becomes previous, a new current takes effect.
    nov2026 = date(2026, 11, 1)
    new = RateSchedule.from_published(
        _published(tou_on_peak=_Entry(18.9, nov2026, 20.3, NOV1_2025))
    )
    assert new.changed_from(old) == NOV1_2025
    fee = RateSchedule.from_published(
        _published(admin_fee=_Entry(12.10, date(2026, 10, 16), 11.90, date(2026, 4, 16)))
    )
    assert fee.changed_from(old) == date(2026, 4, 16)
    # A price that vanished from the page keeps what was imported.
    assert RateSchedule.from_published(_published(tou_on_peak=None)).changed_from(old) is None
