# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar, override

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from provident import (
    AccountDetails,
    AsyncClient,
    DecodeError,
    HTTPStatusError,
    InvalidMeterError,
    LoginRejectedError,
    Meter,
    ProvidentError,
    RatesSessionRequiredError,
    ReportRow,
    TransportError,
    Utility,
)

from .const import (
    CONF_BACKFILL_DAYS,
    CONF_INCLUDE_FEES,
    CONF_RATE_PLAN,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_INCLUDE_FEES,
    DOMAIN,
    RATES_REFRESH,
    UPDATE_INTERVAL,
    meter_unit_info,
)
from .models import DailyUsage, MeterData, ProvidentData
from .rates import KWH_PER_UNIT, Plan, RateSchedule, Tariff, tariff_for

if TYPE_CHECKING:
    from . import ProvidentConfigEntry

_LOGGER = logging.getLogger(__name__)


def new_client() -> AsyncClient:
    """Test seam: tests replace this with a fake-portal client."""
    return AsyncClient()


_SESSION_EXPIRED = (401, 403)

#: Wait before retrying the rates site after a failure (it is not needed every update).
_RATES_RETRY = timedelta(hours=1)

_STORE_VERSION = 1


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


_T = TypeVar("_T")

_CLIENT_ERRORS = (DecodeError, HTTPStatusError, InvalidMeterError, TransportError)


class ProvidentCoordinator(DataUpdateCoordinator[ProvidentData]):
    def __init__(
        self, hass: HomeAssistant, entry: ProvidentConfigEntry, client: AsyncClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=UPDATE_INTERVAL,
        )
        self._client = client
        self._username = entry.data[CONF_USERNAME]
        self._password = entry.data[CONF_PASSWORD]
        self.backfill_days: int = int(entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS))
        self.plan = Plan(entry.options.get(CONF_RATE_PLAN, Plan.NONE))
        self.include_fees: bool = bool(entry.options.get(CONF_INCLUDE_FEES, DEFAULT_INCLUDE_FEES))
        self.statistics_lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, store_key(entry.entry_id))
        self.rates: RateSchedule | None = None
        self._rates_fetched_at: datetime | None = None
        self._rates_retry_at: datetime | None = None
        #: Per meter serial: the tariff its cost statistic was last imported with.
        self.priced: dict[str, dict[str, Any]] = {}

    async def async_load_state(self) -> None:
        """Restore the last good rates and the pricing state of cost statistics."""
        stored = await self._store.async_load() or {}
        if rates := stored.get("rates"):
            self.rates = RateSchedule.from_dict(rates)
            fetched = stored.get("rates_fetched_at")
            self._rates_fetched_at = dt_util.parse_datetime(fetched) if fetched else None
        self.priced = dict(stored.get("priced", {}))

    async def async_save_state(self) -> None:
        await self._store.async_save(
            {
                "rates": self.rates.to_dict() if self.rates else None,
                "rates_fetched_at": (
                    self._rates_fetched_at.isoformat() if self._rates_fetched_at else None
                ),
                "priced": self.priced,
            }
        )

    async def async_remove_state(self) -> None:
        await self._store.async_remove()

    def tariff(self, meter: Meter) -> Tariff | None:
        """How this meter is priced, or ``None`` (no plan, unpriced utility, unknown unit)."""
        _device_class, unit, _unit_class = meter_unit_info(meter)
        if unit not in KWH_PER_UNIT:
            return None
        if meter.utility == Utility.ELECTRICITY:
            ev = False
        elif meter.utility == Utility.ELECTRIC_VEHICLE:
            ev = True
        else:
            return None
        return tariff_for(self.rates, self.plan, ev=ev, include_fees=self.include_fees)

    async def async_fetch_rates(self) -> RateSchedule | None:
        """Fetch the published rates now; ``None`` (and the old rates kept) on failure."""
        try:
            try:
                published = await self._client.rates.fetch()
            except RatesSessionRequiredError:
                await self._client.rates.login(self._username, self._password)
                published = await self._client.rates.fetch()
        except ProvidentError as err:
            self._rates_retry_at = dt_util.utcnow() + _RATES_RETRY
            if self.rates is None:
                _LOGGER.warning("Provident rates unavailable: %s", err)
            else:
                _LOGGER.warning("Provident rates unavailable; keeping the last ones: %s", err)
            return None
        schedule = RateSchedule.from_published(published)
        if schedule.is_empty():
            self._rates_retry_at = dt_util.utcnow() + _RATES_RETRY
            _LOGGER.warning(
                "Provident rates page had no recognizable rates (unrecognized rows: %s)",
                schedule.unknown,
            )
            return None
        if schedule.unknown:
            _LOGGER.debug("Unrecognized rows on the rates page: %s", schedule.unknown)
        self.rates = schedule
        self._rates_fetched_at = dt_util.utcnow()
        self._rates_retry_at = None
        await self.async_save_state()
        return schedule

    async def _maybe_refresh_rates(self) -> None:
        """Refresh rates at most daily, and only when a plan uses them."""
        if self.plan is Plan.NONE:
            return
        now = dt_util.utcnow()
        if self._rates_retry_at is not None and now < self._rates_retry_at:
            return
        fresh = self._rates_fetched_at is not None and now - self._rates_fetched_at < RATES_REFRESH
        if self.rates is not None and fresh:
            return
        await self.async_fetch_rates()

    @property
    def client(self) -> AsyncClient:
        return self._client

    async def login(self) -> None:
        await self._client.auth.login(self._username, self._password)

    async def async_login_or_raise(self) -> None:
        try:
            await self.login()
        except LoginRejectedError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="login_rejected",
                translation_placeholders={"reason": str(err.reason or "")},
            ) from err
        except _CLIENT_ERRORS as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

    @override
    async def _async_update_data(self) -> ProvidentData:
        try:
            return await self._fetch()
        except HTTPStatusError as err:
            if err.status not in _SESSION_EXPIRED:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="portal_status_error",
                    translation_placeholders={"status": str(err.status), "error": str(err)},
                ) from err
            _LOGGER.debug("Session expired; logging in again")
        except LoginRejectedError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="login_rejected",
                translation_placeholders={"reason": str(err.reason or "")},
            ) from err
        except _CLIENT_ERRORS as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="portal_error",
                translation_placeholders={"error": str(err)},
            ) from err

        await self.async_login_or_raise()

        try:
            return await self._fetch()
        except _CLIENT_ERRORS as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="portal_relogin_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _fetch(self) -> ProvidentData:
        account: AccountDetails = await self._client.account.details()
        meters: list[Meter] = await self._client.account.meters()

        # [yesterday, today) yields yesterday's closing reading.
        today = dt_util.now().date()
        registers = {
            row.meter_id: row
            for row in await self._report(meters, today - timedelta(days=1), today)
            if row.meter_id is not None
        }

        # HA's clock can be a day ahead of the portal's; retry errored meters a day earlier.
        stale = [m for m in meters if (r := registers.get(m.id)) is None or r.error]
        if stale:
            for row in await self._report(
                stale, today - timedelta(days=2), today - timedelta(days=1)
            ):
                if row.meter_id is not None and not row.error:
                    registers[row.meter_id] = row

        await self._maybe_refresh_rates()

        meter_data: list[MeterData] = []
        for meter in meters:
            daily = await self._daily(meter, today)
            month_to_date = None
            if self.plan is Plan.TIERED and self.tariff(meter) is not None:
                month_to_date = await self._month_to_date(meter, today, daily)
            meter_data.append(
                MeterData(
                    meter=meter,
                    register=registers.get(meter.id),
                    daily=daily,
                    month_to_date=month_to_date,
                )
            )
        return ProvidentData(
            account=account, meters=tuple(meter_data), today=today, rates=self.rates
        )

    async def _month_to_date(self, meter: Meter, today: date, daily: DailyUsage) -> float:
        """Usage this calendar month so far, in kWh (published hours only)."""
        total = daily.today
        month_start = today.replace(day=1)
        if month_start < today:
            series = await self._client.usage.series(meter, month_start, today)
            total += sum(series[0].data) if series else 0.0
        _device_class, unit, _unit_class = meter_unit_info(meter)
        return total * KWH_PER_UNIT[unit]

    async def _report(self, meters: list[Meter], start: date, end: date) -> list[ReportRow]:
        report = await self._client.usage.report(meters, start, end)
        return [row for rows in report.values() for row in rows]

    async def _daily(self, meter: Meter, today: date) -> DailyUsage:
        """Sum yesterday's and today's hourly buckets (24 per local day, DST days included)."""
        series = await self._client.usage.series(
            meter, today - timedelta(days=1), today + timedelta(days=1)
        )
        data = series[0].data if series else []
        yesterday = data[:24]
        today_hours = data[24:48]
        return DailyUsage(
            yesterday=round(sum(yesterday), 4),
            today=round(sum(today_hours), 4),
        )


async def call_portal[T](fn: Callable[[], Awaitable[T]], what: str) -> T:
    try:
        return await fn()
    except _CLIENT_ERRORS as err:
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="portal_call_failed",
            translation_placeholders={"what": what, "error": str(err)},
        ) from err
