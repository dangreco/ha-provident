# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING, TypeVar, override

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
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
    ReportRow,
    TransportError,
)

from .const import CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS, DOMAIN, UPDATE_INTERVAL
from .models import DailyUsage, MeterData, ProvidentData

if TYPE_CHECKING:
    from . import ProvidentConfigEntry

_LOGGER = logging.getLogger(__name__)


def new_client() -> AsyncClient:
    """Test seam: tests replace this with a fake-portal client."""
    return AsyncClient()


_SESSION_EXPIRED = (401, 403)

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
        self.statistics_lock = asyncio.Lock()

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

        meter_data: list[MeterData] = []
        for meter in meters:
            daily = await self._daily(meter, today)
            meter_data.append(MeterData(meter=meter, register=registers.get(meter.id), daily=daily))
        return ProvidentData(account=account, meters=tuple(meter_data), today=today)

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
