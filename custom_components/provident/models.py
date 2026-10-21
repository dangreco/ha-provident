# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provident import AccountDetails, Meter, ReportRow

    from .rates import RateSchedule


@dataclass(frozen=True)
class DailyUsage:
    """Today's sum is partial until the portal publishes all its hours."""

    yesterday: float
    today: float


@dataclass(frozen=True)
class MeterData:
    meter: Meter
    register: ReportRow | None
    daily: DailyUsage
    #: Usage so far this calendar month, for the tiered plan; ``None`` when not needed/known.
    month_to_date: float | None = None


@dataclass(frozen=True)
class ProvidentData:
    """``today`` fixes the local day for sensors and statistics."""

    account: AccountDetails
    meters: tuple[MeterData, ...]
    today: date
    #: Last good published rates; kept through rates-site outages.
    rates: RateSchedule | None = None

    def meter_data(self, meter_id: int) -> MeterData | None:
        return next((m for m in self.meters if m.meter.id == meter_id), None)
