"""Per-mower geometry state for Hookii Neomow, fed directly from Hookii cloud.

The integration owns the whole data plane now (solution B, 2026-06-16): instead
of subscribing to a local MQTT broker that a separate Hookii Bridge add-on
republished to, it connects to the Hookii cloud itself via
``api.HookiiCloudClient`` and applies the same telemetry messages. This removes
the add-on + the HA ``mqtt`` dependency, so the integration works on every HA
install type (HAOS, Supervised, Container, Core).

The message handling (STATUS / DEVICE_MAP_V2 / ALL_PATH_LIST_V2 /
ALL_PATH_INDEX_V2 / REGION_TASK) and the geometry parsing are unchanged - the
payloads are the same cloud messages the bridge used to pass through. Only the
transport changed: paho's network thread calls ``_on_cloud_message`` off the HA
event loop, so we marshal each message back onto the loop before touching state
or firing the dispatcher.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import geometry
from .api import HookiiAccount, HookiiCloudClient, HookiiConfig
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
        # Full last-known STATUS dict (entities read richer fields from here).
        self.status: dict[str, Any] = {}
        self.device_map: dict | None = None
        self.path_list: dict | None = None
        self.path_index: dict | None = None
        self.region_task: dict | None = None
        self.device_map_at: str | None = None
        self.path_list_at: str | None = None
        self.path_index_at: str | None = None
        self.trail: deque[list[int]] = deque(maxlen=TRAIL_MAX)
        # Last on-demand camera snapshot (set by the snapshot button/camera).
        self.snapshot: bytes | None = None
        self.snapshot_at: str | None = None

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
    """Owns the cloud connection and per-mower state for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        cfg: HookiiConfig,
        acct: HookiiAccount,
        mowers: list[dict[str, str]],
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.cfg = cfg
        self.acct = acct
        self.mowers: dict[str, MowerState] = {}
        self._serial_to_label: dict[str, str] = {}
        for m in mowers:
            label = m["label"]
            self.mowers[label] = MowerState(m["serial"], label, m["color"])
            self._serial_to_label[m["serial"]] = label
        self._client: HookiiCloudClient | None = None
        # Persist the big, slow-to-republish captures (boundary + cut paths) so
        # an HA restart does not blank the map for the minutes-to-hours until
        # the cloud next streams DEVICE_MAP_V2 / ALL_PATH_LIST_V2.
        self._store_dir = hass.config.path("hookii_neomow_data")

    _PERSIST_KEYS = {
        "DEVICE_MAP_V2": "device_map",
        "ALL_PATH_LIST_V2": "path_list",
        "ALL_PATH_INDEX_V2": "path_index",
    }

    async def async_start(self) -> None:
        """Load persisted captures, then connect to the Hookii cloud."""
        await self.hass.async_add_executor_job(self._load_persisted)
        self._client = HookiiCloudClient(self.cfg, self.acct, self._on_cloud_message)
        # paho's connect + loop_start are blocking-ish; run off the loop.
        await self.hass.async_add_executor_job(self._client.start)
        _LOGGER.info(
            "hookii cloud client started for %d mower(s)", len(self.mowers)
        )

    async def async_stop(self) -> None:
        if self._client is not None:
            await self.hass.async_add_executor_job(self._client.stop)
            self._client = None

    @property
    def client(self) -> HookiiCloudClient | None:
        return self._client

    def set_snapshot(self, label: str, data: bytes) -> None:
        """Store a freshly captured camera image and notify the camera entity."""
        state = self.mowers.get(label)
        if state is None:
            return
        state.snapshot = data
        state.snapshot_at = _now_iso()
        async_dispatcher_send(
            self.hass, f"{SIGNAL_MOWER_UPDATED}_{self.entry_id}", label
        )

    def _load_persisted(self) -> None:
        for label, state in self.mowers.items():
            for msg_type, attr in self._PERSIST_KEYS.items():
                path = os.path.join(self._store_dir, f"{label}_{msg_type}.json")
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, encoding="utf-8") as fh:
                        setattr(state, attr, json.load(fh))
                    setattr(state, f"{attr}_at", _now_iso())
                except (OSError, ValueError) as err:
                    _LOGGER.warning("load %s failed: %s", path, err)

    def _persist(self, label: str, msg_type: str, payload: dict) -> None:
        try:
            os.makedirs(self._store_dir, exist_ok=True)
            final = os.path.join(self._store_dir, f"{label}_{msg_type}.json")
            # Unique tmp per write: bursts of the same (label, msg_type) land on
            # different SyncWorker threads and would otherwise race the same
            # .tmp -> os.replace then fails "No such file" for the loser.
            tmp = f"{final}.{os.getpid()}.{uuid4().hex}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, final)
        except OSError as err:
            _LOGGER.warning("persist %s/%s failed: %s", label, msg_type, err)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _schedule_persist(self, label: str, msg_type: str, payload: dict) -> None:
        self.hass.async_add_executor_job(self._persist, label, msg_type, payload)

    # ---- cloud message ingress ----------------------------------------

    def _on_cloud_message(self, serial: str, payload: dict) -> None:
        """Called from paho's network thread - marshal onto the HA loop."""
        self.hass.loop.call_soon_threadsafe(self._handle, serial, payload)

    @callback
    def _handle(self, serial: str, payload: dict) -> None:
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
            # The cloud client already normalised this message's raw STATUS.
            # Accumulate it into the persistent per-mower status by merging
            # non-null fields, so a sparse packet can't blank a sensor and the
            # latest value of every field is always present. (Assignment-merge,
            # NOT setdefault - the values must track the newest message.)
            incoming = payload.get("data", {}).get("STATUS", {})
            if isinstance(incoming, dict):
                for k, v in incoming.items():
                    if v is not None:
                        state.status[k] = v
            parsed = geometry.parse_status(state.status)
            if not parsed:
                # Even without a position fix, a STATUS refresh can carry new
                # battery/work fields the entities want - signal a change.
                return bool(state.status)
            state.robot_x = parsed["x"]
            state.robot_y = parsed["y"]
            state.heading = parsed["heading"]
            state.battery = parsed["battery"]
            state.work_status = parsed["work_status"]
            state.online_status = parsed["online_status"]
            state.last_update = parsed["last_update"] or _now_iso()
            if not state.trail or (
                abs(state.trail[-1][0] - parsed["x"]) > TRAIL_MIN_MOVE_CM
                or abs(state.trail[-1][1] - parsed["y"]) > TRAIL_MIN_MOVE_CM
            ):
                state.trail.append([parsed["x"], parsed["y"]])
            # Self-clear a docking/obstacle alarm once the mower is clearly OK
            # again (charging at dock, or actively mowing).
            if state.status.get("ha_alarm_active") and (
                state.status.get("ha_is_charging") or state.status.get("ha_state") == "mowing"
            ):
                state.status["ha_alarm_active"] = False
                state.status["ha_alarm_code"] = None
            return True

        if msg_type == "NOTICE_ALARM":
            na = payload.get("data", {}).get("NOTICE_ALARM", {})
            err = na.get("errCode") if isinstance(na, dict) else None
            state.status["ha_alarm_active"] = bool(err)
            state.status["ha_alarm_code"] = err
            return True

        if msg_type == "DEVICE_MAP_V2":
            state.device_map = payload
            state.device_map_at = _now_iso()
            self._schedule_persist(state.label, "DEVICE_MAP_V2", payload)
            return True

        if msg_type == "ALL_PATH_LIST_V2":
            new_count = geometry.path_point_count(payload)
            existing = geometry.path_point_count(state.path_list)
            if not existing or new_count >= existing * 0.10:
                state.path_list = payload
                state.path_list_at = _now_iso()
                self._schedule_persist(state.label, "ALL_PATH_LIST_V2", payload)
                return True
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
