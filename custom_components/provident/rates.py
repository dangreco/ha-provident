# SPDX-License-Identifier: GPL-3.0-only

"""Prices for Provident meters: the rates Provident publishes, applied with the OEB's rules.

Provident publishes each price with its current and previous effective dates; this module
decides which price applies to an hour (Ontario Regulated Price Plan periods, OEB holidays,
tiered thresholds) and what that hour costs. No Home Assistant imports: everything here is
pure and unit-tested directly.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provident import PublishedRates, RateEntry

#: Provident's fees are quoted per 30 days of billing.
FEE_HOURS = 30 * 24

#: Residential tier thresholds, kWh per 30 days.
SUMMER_TIER_THRESHOLD = 600.0
WINTER_TIER_THRESHOLD = 1000.0


class Plan(StrEnum):
    """Electricity price plan, chosen in the options (the page lists every plan)."""

    NONE = "none"
    TOU = "tou"
    ULO = "ulo"
    TIERED = "tiered"


class Period(StrEnum):
    OFF_PEAK = "off_peak"
    MID_PEAK = "mid_peak"
    ON_PEAK = "on_peak"
    ULTRA_LOW_OVERNIGHT = "ultra_low_overnight"
    WEEKEND_OFF_PEAK = "weekend_off_peak"
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


PLAN_PERIODS: dict[Plan, tuple[Period, ...]] = {
    Plan.TOU: (Period.OFF_PEAK, Period.MID_PEAK, Period.ON_PEAK),
    Plan.ULO: (
        Period.ULTRA_LOW_OVERNIGHT,
        Period.WEEKEND_OFF_PEAK,
        Period.MID_PEAK,
        Period.ON_PEAK,
    ),
    Plan.TIERED: (Period.TIER_1, Period.TIER_2),
}

#: Row labels of an EV section (compacted) that name a period.
_EV_LABELS: dict[str, Period] = {
    "offpeak": Period.OFF_PEAK,
    "midpeak": Period.MID_PEAK,
    "onpeak": Period.ON_PEAK,
    "ultralowovernight": Period.ULTRA_LOW_OVERNIGHT,
    "weekendoffpeak": Period.WEEKEND_OFF_PEAK,
    "tier1": Period.TIER_1,
    "tier2": Period.TIER_2,
}


def _words(text: str) -> list[str]:
    return "".join(c if c.isalnum() else " " for c in text.lower()).split()


def is_ev_section(heading: str) -> bool:
    words = _words(heading)
    return any(
        w in ("ev", "evs") or w.startswith(("vehicle", "charging", "charger")) for w in words
    )


@dataclass(frozen=True)
class Price:
    """A published value with the dates its current and previous values took effect."""

    current: float | None
    current_effective: date | None = None
    previous: float | None = None
    previous_effective: date | None = None

    def on(self, day: date) -> float | None:
        """The value in effect on ``day``; ``None`` before anything published took effect."""
        in_current = self.current_effective is None or day >= self.current_effective
        if in_current and self.current is not None:
            return self.current
        if in_current and self.current_effective is not None:
            return None
        if self.previous is not None and (
            self.previous_effective is None or day >= self.previous_effective
        ):
            return self.previous
        return None

    def changed_from(self, old: Price | None) -> date | None:
        """Earliest day whose value differs from ``old``'s; ``date.min`` if undated."""
        if old == self:
            return None
        if old is None:
            return self.previous_effective or self.current_effective or date.min
        days: list[date] = []
        if (old.previous, old.previous_effective) != (self.previous, self.previous_effective):
            days.append(self.previous_effective or date.min)
        if (old.current, old.current_effective) != (self.current, self.current_effective):
            days.append(self.current_effective or date.min)
        return min(days)

    @classmethod
    def from_entry(cls, entry: RateEntry) -> Price:
        return cls(
            current=entry.current,
            current_effective=entry.current_effective,
            previous=entry.previous,
            previous_effective=entry.previous_effective,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "current_effective": _iso(self.current_effective),
            "previous": self.previous,
            "previous_effective": _iso(self.previous_effective),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Price:
        return cls(
            current=data.get("current"),
            current_effective=_day(data.get("current_effective")),
            previous=data.get("previous"),
            previous_effective=_day(data.get("previous_effective")),
        )


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _day(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


@dataclass(frozen=True)
class RateSchedule:
    """Everything usable from the rates page: energy in ¢/kWh, fees in $ per 30 days."""

    energy: Mapping[Plan, Mapping[Period, Price]] = field(default_factory=dict)
    #: Read from an EV charging section, if the page has one.
    ev: Mapping[Period, Price] = field(default_factory=dict)
    admin_fee: Price | None = None
    meter_fee: Price | None = None
    #: Other sections as published (diagnostics only).
    sections: Mapping[str, Mapping[str, Price]] = field(default_factory=dict)
    unknown: Mapping[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_published(cls, rates: PublishedRates) -> RateSchedule:
        by_plan: dict[Plan, dict[Period, RateEntry | None]] = {
            Plan.TOU: {
                Period.OFF_PEAK: rates.tou_off_peak,
                Period.MID_PEAK: rates.tou_mid_peak,
                Period.ON_PEAK: rates.tou_on_peak,
            },
            Plan.ULO: {
                Period.ULTRA_LOW_OVERNIGHT: rates.ulo_ultra_low_overnight,
                Period.WEEKEND_OFF_PEAK: rates.ulo_weekend_off_peak,
                Period.MID_PEAK: rates.ulo_mid_peak,
                Period.ON_PEAK: rates.ulo_on_peak,
            },
            Plan.TIERED: {Period.TIER_1: rates.tier_1, Period.TIER_2: rates.tier_2},
        }
        energy = {
            plan: {p: Price.from_entry(e) for p, e in periods.items() if e is not None}
            for plan, periods in by_plan.items()
        }
        sections = {
            heading: {label: Price.from_entry(e) for label, e in rows.items()}
            for heading, rows in rates.sections.items()
        }
        ev: dict[Period, Price] = {}
        for heading, rows in sections.items():
            if not is_ev_section(heading):
                continue
            for label, price in rows.items():
                period = _EV_LABELS.get("".join(_words(label)))
                if period is not None:
                    ev.setdefault(period, price)
        return cls(
            energy={plan: periods for plan, periods in energy.items() if periods},
            ev=ev,
            admin_fee=Price.from_entry(rates.admin_fee) if rates.admin_fee else None,
            meter_fee=Price.from_entry(rates.meter_fee) if rates.meter_fee else None,
            sections=sections,
            unknown=dict(rates.unknown),
        )

    def is_empty(self) -> bool:
        return not (self.energy or self.ev or self.admin_fee or self.meter_fee)

    def _prices(self) -> dict[str, Price | None]:
        prices: dict[str, Price | None] = {
            f"{plan}.{period}": price
            for plan, periods in self.energy.items()
            for period, price in periods.items()
        }
        prices.update({f"ev.{period}": price for period, price in self.ev.items()})
        prices["fee.admin"] = self.admin_fee
        prices["fee.meter"] = self.meter_fee
        return prices

    def changed_from(self, old: RateSchedule | None) -> date | None:
        """Earliest day priced differently than under ``old``; ``None`` if nothing changed.

        ``None`` too when there is no ``old``: the first import prices everything anyway.
        """
        if old is None:
            return None
        new_prices, old_prices = self._prices(), old._prices()
        days = []
        for key in new_prices.keys() | old_prices.keys():
            new, previous = new_prices.get(key), old_prices.get(key)
            if new is None:
                # A price that disappeared from the page: keep what was imported.
                continue
            if (day := new.changed_from(previous)) is not None:
                days.append(day)
        return min(days, default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "energy": {
                plan: {period: price.to_dict() for period, price in periods.items()}
                for plan, periods in self.energy.items()
            },
            "ev": {period: price.to_dict() for period, price in self.ev.items()},
            "admin_fee": self.admin_fee.to_dict() if self.admin_fee else None,
            "meter_fee": self.meter_fee.to_dict() if self.meter_fee else None,
            "sections": {
                heading: {label: price.to_dict() for label, price in rows.items()}
                for heading, rows in self.sections.items()
            },
            "unknown": dict(self.unknown),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RateSchedule:
        return cls(
            energy={
                Plan(plan): {Period(p): Price.from_dict(v) for p, v in periods.items()}
                for plan, periods in data.get("energy", {}).items()
            },
            ev={Period(p): Price.from_dict(v) for p, v in data.get("ev", {}).items()},
            admin_fee=Price.from_dict(data["admin_fee"]) if data.get("admin_fee") else None,
            meter_fee=Price.from_dict(data["meter_fee"]) if data.get("meter_fee") else None,
            sections={
                heading: {label: Price.from_dict(v) for label, v in rows.items()}
                for heading, rows in data.get("sections", {}).items()
            },
            unknown={k: list(v) for k, v in data.get("unknown", {}).items()},
        )


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth_monday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(-first.weekday()) % 7 + 7 * (n - 1))


@cache
def holidays(year: int) -> frozenset[date]:
    """Weekdays priced as holidays by the OEB; weekend holidays move to the next free weekday."""
    victoria = date(year, 5, 24)
    actual = sorted(
        (
            date(year, 1, 1),  # New Year's Day
            _nth_monday(year, 2, 3),  # Family Day
            _easter(year) - timedelta(days=2),  # Good Friday
            victoria - timedelta(days=victoria.weekday()),  # Victoria Day
            date(year, 7, 1),  # Canada Day
            _nth_monday(year, 8, 1),  # Civic Holiday
            _nth_monday(year, 9, 1),  # Labour Day
            _nth_monday(year, 10, 2),  # Thanksgiving
            date(year, 12, 25),  # Christmas Day
            date(year, 12, 26),  # Boxing Day
        )
    )
    observed = {d for d in actual if d.weekday() < 5}
    for day in actual:
        if day.weekday() < 5:
            continue
        while day.weekday() >= 5 or day in observed:
            day += timedelta(days=1)
        observed.add(day)
    return frozenset(observed)


def is_off_peak_day(day: date) -> bool:
    return day.weekday() >= 5 or day in holidays(day.year)


def is_summer(day: date) -> bool:
    return 5 <= day.month <= 10


def tier_threshold(day: date) -> float:
    """kWh at the Tier 1 price in ``day``'s calendar month, prorated per 30 days."""
    base = SUMMER_TIER_THRESHOLD if is_summer(day) else WINTER_TIER_THRESHOLD
    return base * calendar.monthrange(day.year, day.month)[1] / 30


def time_period(plan: Plan, day: date, hour: int) -> Period:
    """Price period of local wall-clock ``hour`` (0-23) of ``day`` for TOU or ULO."""
    if plan is Plan.TOU:
        if is_off_peak_day(day) or hour < 7 or hour >= 19:
            return Period.OFF_PEAK
        midday = 11 <= hour < 17
        if is_summer(day):
            return Period.ON_PEAK if midday else Period.MID_PEAK
        return Period.MID_PEAK if midday else Period.ON_PEAK
    if plan is Plan.ULO:
        if hour < 7 or hour >= 23:
            return Period.ULTRA_LOW_OVERNIGHT
        if is_off_peak_day(day):
            return Period.WEEKEND_OFF_PEAK
        return Period.ON_PEAK if 16 <= hour < 21 else Period.MID_PEAK
    raise ValueError(f"{plan} has no time periods")


@dataclass(frozen=True)
class Tariff:
    """Prices one meter: energy by period (¢/kWh) plus fees ($ per 30 days) spread hourly."""

    plan: Plan
    energy: Mapping[Period, Price]
    fees: tuple[Price, ...] = ()

    def period(self, day: date, hour: int, month_to_date: float | None) -> Period | None:
        """``month_to_date``: kWh used earlier this calendar month (Tiered only)."""
        if self.plan is Plan.TIERED:
            if month_to_date is None:
                return None
            return Period.TIER_1 if month_to_date < tier_threshold(day) else Period.TIER_2
        return time_period(self.plan, day, hour)

    def _rate(self, period: Period, day: date) -> float | None:
        """$/kWh of ``period`` on ``day``."""
        price = self.energy.get(period)
        cents = price.on(day) if price else None
        return None if cents is None else cents / 100

    def price(self, day: date, hour: int, month_to_date: float | None) -> float | None:
        """Energy price in $/kWh at this hour, without fees."""
        period = self.period(day, hour, month_to_date)
        return None if period is None else self._rate(period, day)

    def fee_per_hour(self, day: date) -> float | None:
        total = 0.0
        for fee in self.fees:
            value = fee.on(day)
            if value is None:
                return None
            total += value
        return total / FEE_HOURS

    def costs(
        self, buckets: Iterable[tuple[date, int, float]], month_to_date: float | None
    ) -> list[float | None]:
        """Cost in $ of each chronological (local day, wall-clock hour, kWh) bucket.

        ``month_to_date`` is the kWh used before the first bucket in its month (Tiered only;
        ``None`` if unknown, which leaves that month's hours unpriced). Hours whose price did
        not take effect yet as far as the page shows are ``None``.
        """
        costs: list[float | None] = []
        month: tuple[int, int] | None = None
        for day, hour, kwh in buckets:
            if month is not None and (day.year, day.month) != month:
                month_to_date = 0.0
            month = (day.year, day.month)
            energy = self._energy_cost(day, hour, kwh, month_to_date)
            if month_to_date is not None:
                month_to_date += kwh
            fee = self.fee_per_hour(day)
            costs.append(None if energy is None or fee is None else energy + fee)
        return costs

    def _energy_cost(
        self, day: date, hour: int, kwh: float, month_to_date: float | None
    ) -> float | None:
        if self.plan is not Plan.TIERED:
            rate = self._rate(time_period(self.plan, day, hour), day)
            return None if rate is None else kwh * rate
        if month_to_date is None:
            return None
        tier_1 = max(0.0, min(kwh, tier_threshold(day) - month_to_date))
        cost = 0.0
        for period, amount in ((Period.TIER_1, tier_1), (Period.TIER_2, kwh - tier_1)):
            if amount <= 0:
                continue
            rate = self._rate(period, day)
            if rate is None:
                return None
            cost += amount * rate
        return cost


#: Energy units the tariffs (priced per kWh) can convert from.
KWH_PER_UNIT = {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0}


def tariff_for(
    schedule: RateSchedule | None, plan: Plan, *, ev: bool, include_fees: bool
) -> Tariff | None:
    """The tariff for one meter, or ``None`` when it cannot be priced.

    Suite electricity uses the chosen plan's published prices. An EV meter is priced only from
    an EV section of the page, read with the same plan; it never borrows suite prices.
    """
    if schedule is None or plan is Plan.NONE:
        return None
    energy = schedule.ev if ev else schedule.energy.get(plan, {})
    needed = PLAN_PERIODS[plan]
    if not all(period in energy for period in needed):
        return None
    fees: tuple[Price, ...] = ()
    if include_fees and not ev:
        if schedule.admin_fee is None or schedule.meter_fee is None:
            return None
        fees = (schedule.admin_fee, schedule.meter_fee)
    return Tariff(plan=plan, energy={p: energy[p] for p in needed}, fees=fees)
