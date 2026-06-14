"""MQTT subscription + per-mower geometry state for Hookii Neomow.

Subscribes (via Home Assistant's own MQTT client - no second broker
connection, no extra credentials) to the per-serial topics the Hookii Bridge
republishes, maintains the same per-mower state map_server.py does, and fires a
dispatcher signal whenever a mower's geometry changes so the websocket layer
can push a fresh snapshot to any connected card.

Why piggy-back on HA's MQTT integration instead of opening our own paho client
(as map_server.py does): inside HA the broker connection, auth and reconnect
are already managed by the `mqtt` integration we depend on. Re-using it means
zero extra config for the user (they wired MQTT up once) and no duplicate
watchdog/reconnect logic.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import geometry
from .const import (
    SIGNAL_MOWER_UPDATED,
    TRAIL_MAX,
    TRAIL_MIN_MOVE_CM,
)

_LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MowerState:
    """Latest captured state for a single mower."""

    def __init__(self, serial: str, label: str, color: str) -> None:
        self.serial = serial
        self.label = label
        self.color = color
        self.robot_x: int | None = None
        self.robot_y: int | None = None
        self.heading: float | None = None
        self.battery: Any = None
        self.work_status: Any = None
        self.online_status: Any = None
        self.last_update: str | None = None
        self.device_map: dict | None = None
        self.path_list: dict | None = None
        self.path_index: dict | None = None
        self.region_task: dict | None = None
        self.device_map_at: str | None = None
        self.path_list_at: str | None = None
        self.path_index_at: str | None = None
        self.trail: deque[list[int]] = deque(maxlen=TRAIL_MAX)

    def geometry(self) -> dict[str, Any]:
        """Assemble the raw-coordinate geometry snapshot for the card."""
        robot = None
        if self.robot_x is not None and self.robot_y is not None:
            robot = {"x": self.robot_x, "y": self.robot_y, "heading": self.heading}
        return {
            "serial": self.serial,
            "label": self.label,
            "color": self.color,
            "robot": robot,
            "battery": self.battery,
            "work_status": self.work_status,
            "online_status": self.online_status,
            "last_update": self.last_update,
            "boundary": geometry.extract_boundary(self.device_map),
            "path": geometry.extract_path_points(self.path_list, self.path_index),
            "trail": list(self.trail),
            "mowing_width_cm": geometry.extract_mowing_width_cm(self.region_task),
            "captures": {
                "device_map_at": self.device_map_at,
                "path_list_at": self.path_list_at,
                "path_index_at": self.path_index_at,
            },
        }


class NeomowCoordinator:
    """Owns the MQTT subscriptions and per-mower state for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        topic_prefix: str,
        mowers: list[dict[str, str]],
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.topic_prefix = topic_prefix.rstrip("/")
        self._unsubs: list[Callable[[], None]] = []
        self.mowers: dict[str, MowerState] = {}
        self._serial_to_label: dict[str, str] = {}
        for m in mowers:
            label = m["label"]
            self.mowers[label] = MowerState(m["serial"], label, m["color"])
            self._serial_to_label[m["serial"]] = label
        # Persist the big, slow-to-republish captures (boundary + cut paths) so
        # an HA restart does not blank the map for the minutes-to-hours until
        # the cloud next streams DEVICE_MAP_V2 / ALL_PATH_LIST_V2. Seedable: drop
        # the standalone neomow-viz's <label>_<TYPE>.json files in here.
        self._store_dir = hass.config.path("hookii_neomow_data")

    # Capture msgType -> MowerState attribute, for persistence round-trips.
    _PERSIST_KEYS = {
        "DEVICE_MAP_V2": "device_map",
        "ALL_PATH_LIST_V2": "path_list",
        "ALL_PATH_INDEX_V2": "path_index",
    }

    async def async_start(self) -> None:
        """Load persisted captures, then subscribe to one topic per mower."""
        await self.hass.async_add_executor_job(self._load_persisted)
        for state in self.mowers.values():
            topic = f"{self.topic_prefix}/{state.serial}"
            unsub = await mqtt.async_subscribe(self.hass, topic, self._on_message, 0)
            self._unsubs.append(unsub)
            _LOGGER.debug("subscribed %s -> %s", topic, state.label)

    def _load_persisted(self) -> None:
        """Restore captured boundary/path payloads from disk (executor thread)."""
        for label, state in self.mowers.items():
            for msg_type, attr in self._PERSIST_KEYS.items():
                path = os.path.join(self._store_dir, f"{label}_{msg_type}.json")
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, encoding="utf-8") as fh:
                        setattr(state, attr, json.load(fh))
                    setattr(state, f"{attr}_at", _now_iso())
                    _LOGGER.debug("loaded persisted %s for %s", msg_type, label)
                except (OSError, ValueError) as err:
                    _LOGGER.warning("load %s failed: %s", path, err)

    def _persist(self, label: str, msg_type: str, payload: dict) -> None:
        """Write a capture to disk (executor thread)."""
        try:
            os.makedirs(self._store_dir, exist_ok=True)
            tmp = os.path.join(self._store_dir, f"{label}_{msg_type}.json.tmp")
            final = os.path.join(self._store_dir, f"{label}_{msg_type}.json")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, final)
        except OSError as err:
            _LOGGER.warning("persist %s/%s failed: %s", label, msg_type, err)

    def _schedule_persist(self, label: str, msg_type: str, payload: dict) -> None:
        self.hass.async_create_task(
            self.hass.async_add_executor_job(self._persist, label, msg_type, payload)
        )

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _on_message(self, msg: mqtt.ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except (ValueError, TypeError):
            return
        serial = msg.topic.rsplit("/", 1)[-1]
        label = self._serial_to_label.get(serial)
        if not label:
            return
        state = self.mowers[label]
        if self._apply(state, payload):
            async_dispatcher_send(
                self.hass, f"{SIGNAL_MOWER_UPDATED}_{self.entry_id}", label
            )

    def _apply(self, state: MowerState, payload: dict[str, Any]) -> bool:
        """Update one mower from a decoded payload. Returns True if changed."""
        msg_type = payload.get("msgType", "?")

        if msg_type == "STATUS":
            status = payload.get("data", {}).get("STATUS", {})
            parsed = geometry.parse_status(status)
            if not parsed:
                return False
            state.robot_x = parsed["x"]
            state.robot_y = parsed["y"]
            state.heading = parsed["heading"]
            state.battery = parsed["battery"]
            state.work_status = parsed["work_status"]
            state.online_status = parsed["online_status"]
            state.last_update = parsed["last_update"] or _now_iso()
            # Trail: append only on a meaningful move.
            if not state.trail or (
                abs(state.trail[-1][0] - parsed["x"]) > TRAIL_MIN_MOVE_CM
                or abs(state.trail[-1][1] - parsed["y"]) > TRAIL_MIN_MOVE_CM
            ):
                state.trail.append([parsed["x"], parsed["y"]])
            return True

        if msg_type == "DEVICE_MAP_V2":
            state.device_map = payload
            state.device_map_at = _now_iso()
            self._schedule_persist(state.label, "DEVICE_MAP_V2", payload)
            _LOGGER.debug("DEVICE_MAP_V2 for %s", state.label)
            return True

        if msg_type == "ALL_PATH_LIST_V2":
            # Staleness guard: a transient empty/blank republish (mower coming
            # online, app reconnecting) must not clobber a good capture.
            new_count = geometry.path_point_count(payload)
            existing = geometry.path_point_count(state.path_list)
            if not existing or new_count >= existing * 0.10:
                state.path_list = payload
                state.path_list_at = _now_iso()
                self._schedule_persist(state.label, "ALL_PATH_LIST_V2", payload)
                return True
            _LOGGER.debug(
                "skipped stale ALL_PATH_LIST_V2 for %s (%d vs %d)",
                state.label, new_count, existing,
            )
            return False

        if msg_type == "ALL_PATH_INDEX_V2":
            state.path_index = payload
            state.path_index_at = _now_iso()
            self._schedule_persist(state.label, "ALL_PATH_INDEX_V2", payload)
            return True

        if msg_type == "REGION_TASK":
            state.region_task = payload
            return True

        return False
