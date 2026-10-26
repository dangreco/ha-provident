# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import logging
from datetime import date
from typing import Any, override

import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from provident import (
    DecodeError,
    HTTPStatusError,
    InvalidMeterError,
    LoginRejectedError,
    TransportError,
)

from .const import (
    CONF_BACKFILL_DAYS,
    CONF_BILLED_RATES,
    CONF_INCLUDE_FEES,
    CONF_RATE_PLAN,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_INCLUDE_FEES,
    DOMAIN,
    MAX_BACKFILL_DAYS,
)
from .rates import BilledRate, Plan, RateHistory

_LOGGER = logging.getLogger(__name__)

_CLIENT_ERRORS = (DecodeError, HTTPStatusError, InvalidMeterError, TransportError)


async def _async_try_login(username: str, password: str) -> dict[str, Any]:
    from .coordinator import new_client

    client = new_client()
    await client.auth.login(username, password)
    meters = await client.account.meters()
    return {"meters": len(meters)}


async def _async_validate(
    username: str, password: str, context: str
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    errors: dict[str, str] = {}
    try:
        return await _async_try_login(username, password), errors
    except LoginRejectedError:
        errors["base"] = "invalid_auth"
    except _CLIENT_ERRORS as err:
        _LOGGER.warning("Provident connection failed during %s: %s", context, err)
        errors["base"] = "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error during Provident %s", context)
        errors["base"] = "unknown"
    return None, errors


class ProvidentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ProvidentOptionsFlow:
        return ProvidentOptionsFlow()

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            info, errors = await _async_validate(username, password, "setup")
            if info is not None:
                if not info["meters"]:
                    return self.async_abort(reason="no_meters")
                await self.async_set_unique_id(username)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Provident ({username})",
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_BACKFILL_DAYS: int(user_input[CONF_BACKFILL_DAYS]),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(include_username=True, include_backfill=True),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        username = user_input[CONF_USERNAME] if user_input else entry.data[CONF_USERNAME]
        password = user_input[CONF_PASSWORD] if user_input else ""
        if user_input is not None:
            info, errors = await _async_validate(username, password, "reconfigure")
            if info is not None:
                if not info["meters"]:
                    return self.async_abort(reason="no_meters")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_BACKFILL_DAYS: int(user_input[CONF_BACKFILL_DAYS]),
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(
                include_username=True,
                include_backfill=True,
                backfill_default=int(entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)),
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None and user_input.get(CONF_PASSWORD):
            info, errors = await _async_validate(
                reauth_entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                "reauth",
            )
            if info is not None:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_schema(include_username=False, include_backfill=False),
            description_placeholders={
                "username": reauth_entry.data[CONF_USERNAME],
            },
            errors=errors,
        )


def _schema(
    *,
    include_username: bool,
    include_backfill: bool,
    backfill_default: int = DEFAULT_BACKFILL_DAYS,
) -> probatio.Schema:
    fields: dict[Any, Any] = {}
    if include_username:
        fields[probatio.Required(CONF_USERNAME)] = str
    fields[probatio.Required(CONF_PASSWORD)] = str
    if include_backfill:
        fields[probatio.Optional(CONF_BACKFILL_DAYS, default=backfill_default)] = NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=MAX_BACKFILL_DAYS,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="days",
            )
        )
    return probatio.Schema(fields)


class ProvidentOptionsFlow(OptionsFlowWithReload):
    """Price plan for the cost statistic and price sensors; saving reloads the entry.

    The market plan adds a step for the rates printed on each bill, which Provident does not
    publish: add one per bill (the rate and the first day it covers) or remove a mistake.
    """

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._billed: RateHistory = RateHistory(())

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self.config_entry.options
        if user_input is not None:
            plan = Plan(user_input[CONF_RATE_PLAN])
            self._options = {
                CONF_RATE_PLAN: str(plan),
                CONF_INCLUDE_FEES: bool(user_input[CONF_INCLUDE_FEES]),
            }
            self._billed = RateHistory.from_list(options.get(CONF_BILLED_RATES, []))
            if plan is Plan.MARKET:
                return await self.async_step_billed_rates()
            return self._finish()
        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_RATE_PLAN, default=options.get(CONF_RATE_PLAN, str(Plan.NONE))
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[str(p) for p in Plan],
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_RATE_PLAN,
                    )
                ),
                probatio.Required(
                    CONF_INCLUDE_FEES,
                    default=options.get(CONF_INCLUDE_FEES, DEFAULT_INCLUDE_FEES),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_billed_rates(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            rates = list(self._billed.rates)
            remove = user_input.get(_REMOVE)
            start_text = user_input.get(_START)
            cents = user_input.get(_CENTS)
            if remove:
                rates = [r for r in rates if r.start.isoformat() != remove]
            if (start_text is None) != (cents is None):
                errors["base"] = "billed_rate_incomplete"
            elif start_text is not None and cents is not None:
                start = date.fromisoformat(start_text)
                # Re-entering a bill's start date corrects its rate.
                rates = [r for r in rates if r.start != start]
                rates.append(BilledRate(start=start, cents=float(cents)))
            if not errors:
                self._billed = RateHistory(tuple(rates))
                if user_input.get(_ADD_ANOTHER):
                    return await self.async_step_billed_rates()
                if not self._billed.rates:
                    errors["base"] = "billed_rates_required"
                else:
                    return self._finish()
        fields: dict[Any, Any] = {
            probatio.Optional(_START): DateSelector(),
            probatio.Optional(_CENTS): NumberSelector(
                NumberSelectorConfig(
                    min=0.1,
                    max=100,
                    step="any",
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="¢/kWh",
                )
            ),
        }
        if self._billed.rates:
            fields[probatio.Optional(_REMOVE)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=r.start.isoformat(), label=_describe(r))
                        for r in self._billed.rates
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        fields[probatio.Optional(_ADD_ANOTHER, default=False)] = BooleanSelector()
        listed = "\n".join(f"- {_describe(r)}" for r in self._billed.rates) or "- (none yet)"
        return self.async_show_form(
            step_id="billed_rates",
            data_schema=probatio.Schema(fields),
            errors=errors,
            description_placeholders={"rates": listed},
        )

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self._options, CONF_BILLED_RATES: self._billed.to_list()}
        )


_START = "start"
_CENTS = "cents"
_REMOVE = "remove"
_ADD_ANOTHER = "add_another"


def _describe(rate: BilledRate) -> str:
    return f"from {rate.start.isoformat()}: {rate.cents:g} ¢/kWh"
