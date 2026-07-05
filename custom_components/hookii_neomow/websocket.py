"""Websocket API bridging the coordinator's geometry to the Lovelace card.

The card opens one subscription (``hookii_neomow/subscribe``). On subscribe we
immediately push a full snapshot of every configured mower; afterwards we
stream:

- ``full`` updates (complete geometry snapshot) ONLY when the large map data
  actually changes (DEVICE_MAP_V2 / ALL_PATH_LIST_V2 / ALL_PATH_INDEX_V2 /
  REGION_TASK - rare, minutes-to-hours apart), and
- ``partial`` updates (robot position + battery + status + newest trail
  point, ~200 bytes) for the ~1.5s STATUS stream, throttled to at most one
  per mower per ``LIGHT_THROTTLE_S`` with a trailing send so the final
  position is never dropped.

v0.3.16 and earlier pushed the FULL snapshot (boundary + entire cut-path
history + 2000-point trail, easily 50-150 KB) on EVERY STATUS to EVERY
subscription. A phone showing the 4-card Neomow dashboard received megabytes
per second, hit HA's 4096-pending-message websocket cutoff ("Client unable to
keep up with pending messages") and was disconnected - the whole HA app
appeared dead while a mower was mowing, and the wasted upload bandwidth
starved other services for remote clients.

This is HA-native (rides the authenticated `/api/websocket` connection the
frontend already holds) - no extra port, no CORS, no iframe, works
identically on HAOS and Container.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_MOWER_UPDATED, WS_TYPE_SUBSCRIBE

# Minimum seconds between partial (robot/status) pushes per mower per
# subscription. The robot moves ~0.5 m/s; 2s keeps the marker fluid while
# cutting the STATUS-rate flood by ~3x and the per-event size by ~500x.
LIGHT_THROTTLE_S = 2.0
# Minimum seconds between FULL geometry pushes per mower per subscription.
# While a mower mows, ALL_PATH_INDEX_V2 grows by a point every ~1.5s - real
# content changes, but pushing the 100KB+ snapshot at that rate is the same
# flood the light/full split exists to prevent. The 2s partials carry the
# live robot + trail; a full path resync every 15s is visually seamless.
FULL_THROTTLE_S = 15.0


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
    """Stream geometry snapshots + throttled robot deltas to the card."""
    coordinators = list(hass.data.get(DOMAIN, {}).values())

    # Per-subscription throttle bookkeeping (this closure IS the subscription).
    last_light: dict[str, float] = {}  # label -> loop-time of last push (any kind)
    last_full: dict[str, float] = {}  # label -> loop-time of last FULL push
    # label -> (TimerHandle, kind) of a scheduled trailing send. A pending
    # 'full' supersedes lights; a pending 'light' upgrades to 'full'.
    pending: dict[str, tuple[object, str]] = {}

    @callback
    def _send(payload: dict) -> None:
        try:
            connection.send_message(websocket_api.event_message(msg["id"], payload))
        except Exception:  # noqa: BLE001 - connection may be tearing down
            pass

    @callback
    def _send_full(label: str, coordinator) -> None:
        state = coordinator.mowers.get(label)
        if state is None:
            return
        now = hass.loop.time()
        last_full[label] = now
        last_light[label] = now  # a full carries everything a light does
        _send({"label": label, "geometry": state.geometry()})

    @callback
    def _send_light(label: str, coordinator) -> None:
        state = coordinator.mowers.get(label)
        if state is None:
            return
        last_light[label] = hass.loop.time()
        _send({"label": label, "partial": True, **state.light_state()})

    # Per-entry dispatcher listeners -> push the changed mower.
    unsubs = []
    for coordinator in coordinators:

        @callback
        def _on_update(label: str, kind: str = "full", _coordinator=coordinator) -> None:
            now = hass.loop.time()
            cur = pending.get(label)

            def _schedule(wait: float, send_kind: str) -> None:
                @callback
                def _trailing(_label=label, _c=_coordinator, _k=send_kind) -> None:
                    pending.pop(_label, None)
                    if _k == "full":
                        _send_full(_label, _c)
                    else:
                        _send_light(_label, _c)

                pending[label] = (hass.loop.call_later(wait, _trailing), send_kind)

            if kind == "full":
                if cur is not None and cur[1] == "full":
                    return  # a full is already on its way
                wait = last_full.get(label, -FULL_THROTTLE_S) + FULL_THROTTLE_S - now
                if cur is not None:
                    cur[0].cancel()  # type: ignore[attr-defined]
                    pending.pop(label, None)
                if wait <= 0:
                    _send_full(label, _coordinator)
                else:
                    _schedule(wait, "full")
                return

            # light update
            if cur is not None:
                return  # a light or full trailing send already covers this
            wait = last_light.get(label, -LIGHT_THROTTLE_S) + LIGHT_THROTTLE_S - now
            if wait <= 0:
                _send_light(label, _coordinator)
            else:
                _schedule(wait, "light")

        unsubs.append(
            async_dispatcher_connect(
                hass,
                f"{SIGNAL_MOWER_UPDATED}_{coordinator.entry_id}",
                _on_update,
            )
        )

    @callback
    def _unsubscribe() -> None:
        for timer, _kind in pending.values():
            timer.cancel()  # type: ignore[attr-defined]
        pending.clear()
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg["id"]] = _unsubscribe
    connection.send_result(msg["id"])

    # Initial full snapshot so the card paints immediately on load.
    for coordinator in coordinators:
        for label in coordinator.mowers:
            _send_full(label, coordinator)
