"""Config flow for Hookii Neomow Map.

Single UI step: the local-broker topic prefix (pre-filled with the bridge's
default) and a mower list in the same ``label:serial[:color]`` format the
bridge already uses, so users can paste the value straight from their bridge
config. MQTT itself is a declared dependency - the user wires the broker up
once via HA's MQTT integration; we never ask for broker credentials here.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_COLOR,
    CONF_LABEL,
    CONF_MOWERS,
    CONF_SERIAL,
    CONF_TOPIC_PREFIX,
    DEFAULT_TOPIC_PREFIX,
    DOMAIN,
    PALETTE,
)

CONF_MOWERS_SPEC = "mowers_spec"


def parse_mowers_spec(spec: str) -> list[dict[str, str]]:
    """Parse ``label:serial[:color];label:serial[:color];...`` into dicts.

    Raises ValueError if no usable mower is found, so the flow can surface a
    clear error instead of creating an empty (useless) entry.
    """
    mowers: list[dict[str, str]] = []
    for i, raw in enumerate(spec.split(";")):
        raw = raw.strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split(":")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError(f"malformed mower entry: {raw!r}")
        label, serial = parts[0], parts[1]
        color = parts[2] if len(parts) >= 3 and parts[2] else PALETTE[i % len(PALETTE)]
        mowers.append({CONF_LABEL: label, CONF_SERIAL: serial, CONF_COLOR: color})
    if not mowers:
        raise ValueError("no mowers parsed")
    return mowers


class NeomowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Hookii Neomow Map config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                mowers = parse_mowers_spec(user_input[CONF_MOWERS_SPEC])
            except ValueError:
                errors["base"] = "invalid_mowers"
            else:
                await self.async_set_unique_id(
                    user_input[CONF_TOPIC_PREFIX]
                    + "|"
                    + ",".join(m[CONF_SERIAL] for m in mowers)
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Hookii Neomow Map",
                    data={
                        CONF_TOPIC_PREFIX: user_input[CONF_TOPIC_PREFIX].rstrip("/"),
                        CONF_MOWERS: mowers,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TOPIC_PREFIX, default=DEFAULT_TOPIC_PREFIX
                ): cv.string,
                vol.Required(CONF_MOWERS_SPEC): cv.string,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )
