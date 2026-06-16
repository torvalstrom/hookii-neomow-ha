"""Config flow for Hookii Neomow Map.

The integration talks to the Hookii cloud directly (no add-on, no MQTT broker),
so setup just needs the Hookii account: email + password + cloud environment.
On submit we log in, discover the account's mower serial numbers, and create one
entry per account with an auto-assigned label + colour per mower.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import HookiiAccount, HookiiAuthError, HookiiConfig, login
from .const import (
    CONF_COLOR,
    CONF_EMAIL,
    CONF_ENV,
    CONF_LABEL,
    CONF_MOWERS,
    CONF_PASSWORD,
    CONF_SERIAL,
    CONF_SERIALS,
    DEFAULT_ENV,
    DOMAIN,
    PALETTE,
)

_LOGGER = logging.getLogger(__name__)


def _mowers_from_serials(serials: list[str]) -> list[dict[str, str]]:
    """Auto-assign a friendly label + a stable palette colour per serial."""
    mowers: list[dict[str, str]] = []
    for i, sn in enumerate(serials):
        mowers.append({
            CONF_SERIAL: sn,
            CONF_LABEL: f"Neomow {sn[-6:]}",
            CONF_COLOR: PALETTE[i % len(PALETTE)],
        })
    return mowers


class NeomowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Hookii Neomow Map config flow."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            cfg = HookiiConfig(
                email=email,
                password=user_input[CONF_PASSWORD],
                env=user_input[CONF_ENV],
            )
            acct = HookiiAccount(label=email)
            try:
                await self.hass.async_add_executor_job(login, cfg, acct)
            except HookiiAuthError as err:
                _LOGGER.warning("Hookii login failed: %s", err)
                errors["base"] = "auth"
            else:
                # The login response often omits the device list (it does for
                # some accounts); fall back to the serials the user pasted.
                manual = [
                    s for s in re.split(r"[,\s]+", user_input.get(CONF_SERIALS, "").strip())
                    if s
                ]
                serials = acct.serials or manual
                if not serials:
                    errors["base"] = "no_mowers"
                else:
                    await self.async_set_unique_id(email.lower())
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Hookii Neomow ({email})",
                        data={
                            CONF_EMAIL: email,
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_ENV: user_input[CONF_ENV],
                            CONF_MOWERS: _mowers_from_serials(serials),
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Required(CONF_ENV, default=DEFAULT_ENV): SelectSelector(
                    SelectSelectorConfig(
                        options=["beta", "prod"],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_SERIALS, default=""): cv.string,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )
