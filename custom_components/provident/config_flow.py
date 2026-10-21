# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import logging
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
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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
    CONF_INCLUDE_FEES,
    CONF_RATE_PLAN,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_INCLUDE_FEES,
    DOMAIN,
    MAX_BACKFILL_DAYS,
)
from .rates import Plan

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
                    data_updates={CONF_USERNAME: username, CONF_PASSWORD: password},
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(include_username=True, include_backfill=False),
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


def _schema(*, include_username: bool, include_backfill: bool) -> probatio.Schema:
    fields: dict[Any, Any] = {}
    if include_username:
        fields[probatio.Required(CONF_USERNAME)] = str
    fields[probatio.Required(CONF_PASSWORD)] = str
    if include_backfill:
        fields[probatio.Optional(CONF_BACKFILL_DAYS, default=DEFAULT_BACKFILL_DAYS)] = (
            NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=MAX_BACKFILL_DAYS,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="days",
                )
            )
        )
    return probatio.Schema(fields)


class ProvidentOptionsFlow(OptionsFlowWithReload):
    """Price plan for the cost statistic and price sensors; saving reloads the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_RATE_PLAN: str(Plan(user_input[CONF_RATE_PLAN])),
                    CONF_INCLUDE_FEES: bool(user_input[CONF_INCLUDE_FEES]),
                }
            )
        options = self.config_entry.options
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
