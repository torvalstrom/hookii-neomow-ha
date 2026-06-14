"""Websocket API bridging the coordinator's geometry to the Lovelace card.

The card opens one subscription (``hookii_neomow/subscribe``). On subscribe we
immediately push a full snapshot of every configured mower, then stream a fresh
snapshot for any mower whose geometry changes. This is HA-native (rides the
authenticated `/api/websocket` connection the frontend already holds) - no
extra port, no CORS, no iframe, works identically on HAOS and Container.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_MOWER_UPDATED, WS_TYPE_SUBSCRIBE


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the websocket command (idempotent across config entries)."""
    if hass.data.get(f"{DOMAIN}_ws_registered"):
        return
    websocket_api.async_register_command(hass, ws_subscribe)
    hass.data[f"{DOMAIN}_ws_registered"] = True


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_SUBSCRIBE})
@callback
def ws_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Stream geometry snapshots for every configured mower to the card."""
    coordinators = list(hass.data.get(DOMAIN, {}).values())

    @callback
    def _send(label: str, geometry: dict) -> None:
        connection.send_message(
            websocket_api.event_message(
                msg["id"], {"label": label, "geometry": geometry}
            )
        )

    # Per-entry dispatcher listeners -> push the changed mower.
    unsubs = []
    for coordinator in coordinators:
        @callback
        def _on_update(label: str, _coordinator=coordinator) -> None:
            state = _coordinator.mowers.get(label)
            if state is not None:
                _send(label, state.geometry())

        unsubs.append(
            async_dispatcher_connect(
                hass,
                f"{SIGNAL_MOWER_UPDATED}_{coordinator.entry_id}",
                _on_update,
            )
        )

    @callback
    def _unsubscribe() -> None:
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg["id"]] = _unsubscribe
    connection.send_result(msg["id"])

    # Initial full snapshot so the card paints immediately on load.
    for coordinator in coordinators:
        for label, state in coordinator.mowers.items():
            _send(label, state.geometry())
