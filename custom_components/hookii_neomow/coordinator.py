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


# Concise English texts (deliberately shorter/clearer than Hookii's own
# wording, which Tor finds confusing). The fault TEXT is resolved server-side by
# Hookii (the MQTT NOTICE_ALARM carries only the numeric errCode + i18n key, not
# the prose; the APK's en.json holds API/UI strings, not device-fault text), so
# this table is built from codes we confirm live rather than scraped. Codes
# marked CONFIRMED were observed end-to-end on Tor's mower 2026-06-20.
_ERRCODE_TEXT = {
    "801": "Stopped",            # CONFIRMED - stop button pressed
    "823": "Tilted",             # CONFIRMED - mower tilted/lifted off level
    "514": "Docking failed", "515": "Docking failed",
    "516": "Not charging at dock",   # greenhouse kissing-dock (Hookii push code)
}
# errCodes whose alarm should persist until the mower is *actually* OK again
# (charging/mowing), not merely until the motion-halt clears. Docking/charging
# faults leave the mower docked-but-idle, so a halt-cleared check would drop them
# too early; motion-halt faults (stop/tilt/slip) clear with the halt.
_DOCKING_CODES = {"514", "515", "516"}
# runStatusList values seen in normal operation (mowing/docked/charging).
_NORMAL_RUNSTATUS = {0, 5, 7}


def _resolve_alarm(status: dict[str, Any]) -> tuple[Any, str]:
    """Return (code, label) for the current fault. `code` is the machine value
    (NOTICE errCode, or a derived marker for sensor-detected halts) kept for
    automations; `label` is a concise human text that always embeds the
    MQTT-reported code in parentheses, e.g. 'Lifted (822)' or 'Stopped (1)'."""
    notice = status.get("ha_notice_errcode")
    nc = str(notice) if notice else None
    ss = status.get("sensorStatus")
    ss = ss if isinstance(ss, dict) else {}
    rsl = status.get("runStatusList")
    halt_codes = [x for x in rsl if x not in _NORMAL_RUNSTATUS] if isinstance(rsl, list) else []
    # Code shown in parens: prefer the precise NOTICE errCode (e.g. 801/823),
    # else fall back to the runStatusList halt marker that STATUS reported.
    disp = nc or (",".join(str(x) for x in halt_codes) or "stopped")
    # Known errCode -> our own concise text.
    if nc and nc in _ERRCODE_TEXT:
        return nc, f"{_ERRCODE_TEXT[nc]} ({disp})"
    # Unknown/absent errCode: derive a meaningful description from the human
    # sensorStatus flags, still embedding the most precise code we have. The
    # alarm_code stays the numeric errCode when present (automations branch on
    # it); otherwise a stable string marker for the fault class.
    if ss.get("leftLiftHallSensor") or ss.get("rightLiftHallSensor"):
        return (nc or "lift"), f"Lifted ({disp})"
    if ss.get("leftLiftCollisionBarSensor") or ss.get("rightLiftCollisionBarSensor"):
        return (nc or "collision"), f"Bumper stuck ({disp})"
    if ss.get("leftDriveMotorStatus") or ss.get("rightDriveMotorStatus"):
        return (nc or "drive_motor"), f"Wheel fault - check for debris ({disp})"
    if ss.get("turnKnifeDiscMotorStatus") or ss.get("liftingKnifeDiscMotorStatus"):
        return (nc or "blade_motor"), f"Blade fault ({disp})"
    if nc:
        return nc, f"Error ({disp})"
    return "halt", f"Stopped - needs attention ({disp})"


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
        # createTime of the most recent NOTICE we have acted on. Unread notices
        # persist server-side, so NOTICE_ALARM's latestNotice can be stale; we
        # only adopt a notice whose createTime is newer than this. None means
        # "not yet baselined" - the first NOTICE_ALARM after start records the
        # current latest WITHOUT raising, so a pre-existing unread notice can't
        # fire a phantom alarm on every reconnect.
        self.last_notice_at: str | None = None
        # Mowing zones fetched via REST map/data (small areaId + areaName), the
        # ids the start command's regionList expects. Reliable, unlike rare MQTT.
        self.areas: list[dict[str, Any]] = []
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
        # Populate the mowing-zone list in the background (REST map/data; doesn't
        # block startup and survives the rare MQTT DEVICE_MAP_V2 never arriving).
        self.hass.async_create_task(self.async_refresh_areas())

    async def async_stop(self) -> None:
        if self._client is not None:
            await self.hass.async_add_executor_job(self._client.stop)
            self._client = None

    async def async_refresh_areas(self, label: str | None = None) -> None:
        """Refresh the cached mowing-zone list (REST map/data) for one or all
        mowers. Best-effort: failures leave the previous cache intact."""
        if self._client is None:
            return
        items = (
            [(label, self.mowers[label])]
            if label is not None and label in self.mowers
            else list(self.mowers.items())
        )
        for lbl, state in items:
            try:
                areas = await self.hass.async_add_executor_job(
                    self._client.get_areas, state.serial
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("[%s] area refresh failed", lbl)
                continue
            if areas:
                state.areas = areas
                async_dispatcher_send(
                    self.hass, f"{SIGNAL_MOWER_UPDATED}_{self.entry_id}", lbl
                )

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
        # Mirror into status so the snapshot_fresh binary_sensor (which reads the
        # status dict) sees it. Set when the IMAGE arrives, so the dashboard card
        # appears exactly then and auto-hides 30s later (the ~1.5s STATUS stream
        # re-evaluates the sensor, flipping it off just after the 30s mark).
        state.status["ha_snapshot_at"] = state.snapshot_at
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
        # Diagnostic: which cloud message types actually reach this account.
        # STATUS streams continuously; the map/path captures are rare + slow,
        # so "Waiting for map data" is ambiguous without this. Enable via
        #   logger: {logs: {custom_components.hookii_neomow: debug}}
        _LOGGER.debug("[%s] inbound cloud msg: %s", state.label, msg_type)

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
            # Live fault detection from STATUS (2026-06-20, validated by Tor
            # triggering stop/tilt/slip on a real mower). Faults set
            # robotStatus==4 and add "1" to runStatusList - and crucially do
            # NOT fire NOTICE_ALARM, so the integration was previously blind to
            # them (the reported "Problem: ok while the mower is stuck"). Note
            # workStatus keeps reporting "working" through a slip, which is why
            # the old "clear on mowing" heuristic was wrong. Drive the alarm
            # off the live STATUS so it persists until the mower itself reports
            # a normal state again - same semantics the Hookii app shows.
            rs = state.status.get("robotStatus")
            rsl = state.status.get("runStatusList")
            halted = rs == 4 or (isinstance(rsl, list) and 1 in rsl)
            notice = state.status.get("ha_notice_errcode")
            recovered = (
                state.status.get("ha_is_charging")
                or state.status.get("ha_state") == "mowing"
            )
            # Docking/charging NOTICE faults (514/515/516) leave the mower
            # docked-but-idle, so they must persist until it is actually OK again
            # (charging/mowing) - a halt-cleared check would drop them too early.
            # Motion-halt faults (stop/tilt/slip) clear as soon as the halt
            # clears, so we don't keep a stale alarm after the user resets it.
            notice_persists = (
                notice is not None and str(notice) in _DOCKING_CODES and not recovered
            )
            if halted or notice_persists:
                code, label = _resolve_alarm(state.status)
                state.status["ha_alarm_active"] = True
                state.status["ha_alarm_code"] = code
                state.status["ha_alarm_label"] = label
            else:
                state.status["ha_alarm_active"] = False
                state.status["ha_alarm_code"] = None
                state.status["ha_alarm_label"] = None
                state.status["ha_notice_errcode"] = None
                state.status["ha_notice_at"] = None
            return True

        if msg_type == "NOTICE_ALARM":
            # NOTICE_ALARM is an unread-notice SUMMARY, NOT a flat alarm:
            #   {"total": N, "latestNotice": {...}, "noticeList": [{...}]}
            # Each notice carries the real errCode + i18n title/content keys +
            # serialNumber + createTime. When total==0 it is just {"total": 0}
            # (no unread) - which earlier looked like "just a count" and left the
            # integration blind to NOTICE-only faults (e.g. docking 516). The
            # code is in the MQTT data all along; it just lives one level down.
            na = payload.get("data", {}).get("NOTICE_ALARM", {})
            latest = None
            if isinstance(na, dict) and na.get("total"):
                latest = na.get("latestNotice")
                if not isinstance(latest, dict):
                    nl = na.get("noticeList")
                    latest = nl[0] if isinstance(nl, list) and nl else None
            err = ct = None
            if isinstance(latest, dict):
                # The summary is per-device, but guard on serial anyway so a
                # stray notice can't raise an alarm on the wrong mower.
                sn = latest.get("serialNumber")
                if not sn or sn == state.serial:
                    err = latest.get("errCode")
                    ct = latest.get("createTime")
            if not err or not ct:
                return True
            if state.last_notice_at is None:
                # First NOTICE_ALARM since start: baseline only, don't raise a
                # phantom alarm for a pre-existing unread notice.
                state.last_notice_at = ct
                return True
            if ct <= state.last_notice_at:
                # Stale/duplicate (createTime is China-time "Y-m-d H:M:S", so a
                # lexicographic compare is chronological) - ignore.
                return True
            # A genuinely new fault. Remember it (+ when); the STATUS handler
            # owns clearing it once the mower recovers (docking alarms persist).
            state.last_notice_at = ct
            state.status["ha_notice_errcode"] = str(err)
            state.status["ha_notice_at"] = ct
            code, label = _resolve_alarm(state.status)
            state.status["ha_alarm_active"] = True
            state.status["ha_alarm_code"] = code
            state.status["ha_alarm_label"] = label
            return True

        if msg_type == "DEVICE_MAP_V2":
            if state.device_map_at is None:
                # One-time, default-level: lets a user confirm the map boundary
                # actually arrived (vs the card's "Waiting for map data") without
                # turning on debug logging. The map is pushed rarely + slowly, so
                # this can lag the first telemetry by minutes-to-hours.
                _LOGGER.info(
                    "[%s] first DEVICE_MAP_V2 received - map boundary now available",
                    state.label,
                )
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
