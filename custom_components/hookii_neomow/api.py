"""Hookii cloud client for the native Neomow integration.

This is the half of the old `hookii-bridge-ha-addon` add-on (`bridge.py`) that
the native integration absorbs so it needs NO separate Docker add-on and NO
Home Assistant MQTT broker - it works on every HA install type (HAOS,
Supervised, Container, Core).

Two channels, exactly as the reverse-engineered Hookii protocol splits them:

  * TELEMETRY  - read-only Hookii **cloud MQTT** (8883, TLS, shared
    `hookii-iot` creds). The mower pushes STATUS / DEVICE_MAP_V2 /
    ALL_PATH_LIST_V2 / ALL_PATH_INDEX_V2 / REGION_TASK / NOTICE_ALARM to
    `hk/server/mower/push/<model>/<serial>`. A session-scoped heartbeat to
    `hk/app/mower/hb/<model>/<serial>` every 1.5s keeps the server streaming
    (and is the crux of the single-session constraint - see below).

  * CONTROL    - HTTPS REST to `<host>:10443/api/v1/mower/...` carrying the JWT
    from `/user/login/email` in the `hookii-token` header. start/pause/dock,
    schedule, snapshot, recover-alarm.

SINGLE SESSION: the Hookii cloud allows ONE logical session per account. The
running integration's heartbeat competes with the phone app and with the old
add-on if both are live - so only one of them may run against an account at a
time (the migration step is "stop the add-on, then add this integration").

paho-mqtt runs its own network thread; `on_message` therefore fires OFF the HA
event loop. This module stays HA-agnostic: it just invokes the caller-supplied
`on_telemetry(serial, payload)` callback from that thread. The coordinator is
responsible for marshalling back onto the loop (call_soon_threadsafe). REST
calls are synchronous `requests`; callers run them via
`hass.async_add_executor_job`.

Faithful port of bridge.py (protocol facts, command codes and the heartbeat
shape are PCAP-confirmed there); the local-broker republish + HA MQTT discovery
are dropped because the integration owns entities + the card natively.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import paho.mqtt.client as mqtt
import requests

from .status import normalise_status

_LOGGER = logging.getLogger(__name__)

# Headers the Hookii Android app sends on EVERY request (login + post-auth),
# reverse-engineered from pcap. Missing ANY of them -> the server rejects with
# {"code":2,"msg":"hookii-agent参数错误","data":null}. Keep this byte-identical
# to the add-on's bridge.py.
#  - hookii-token: "Hookii " (trailing space) before login, "Hookii <JWT>" after.
#  - hookii-agent: free-form "Android/<mfr> <model> <ver>/V<app>/<build>" - the
#    prefix shape matters, the exact device less so.
#  - app-time-zone-offset: minutes vs UTC.
#  - user-agent: the Flutter Dart HTTP client default.
HOOKII_USER_AGENT = "Dart/3.9 (dart:io)"
HOOKII_AGENT = "Android/Xiaomi 25010PN30G 16/V1.1.0/189"
HOOKII_APP_NAME = "Hookii App"
HOOKII_APP_LANG = "en"

# Cloud endpoints per environment. `prod` and `beta` differ only in host; the
# REST control port is 10443 and the cloud MQTT port is 8883 on both.
ENV_HOSTS = {
    "beta": "iot.beta.hookii.com",
    "prod": "iot.hookii.com",
}
REST_PORT = 10443
MQTT_PORT = 8883
# Shared IoT broker creds baked into the Hookii app (auth is really carried by
# the JWT inside the heartbeat - the username/password just open the socket, so
# the same pair works for every user of an environment). Username is constant;
# the password rotates per environment. Verified against a 2026-06-12 prod
# capture. Same values the public hookii-bridge add-on ships.
CLOUD_MQTT_USER = "hookii-iot"
CLOUD_MQTT_PASS_BY_ENV = {
    "beta": "ukLWdAbvRF3JVqNyTdAVJsMx",
    "prod": "CaV4C4qHBQxwWI#GomA2zuI&D#MxyaMF",
}

# Heartbeat cadence the Android app uses (PCAP-confirmed 1.5s). The `push`
# value is a FIXED per-session integer (observed 23), NOT a counter.
HEARTBEAT_SEC = 1.5
HEARTBEAT_PUSH = 23

# Default model code (Neomow X Pro = 0002); the real code is learned from the
# first inbound push topic per serial.
DEFAULT_MODEL = "0002"

# Force a reconnect if the cloud socket stays down this long (paho can wedge in
# a state where on_disconnect fired but on_connect never fires again).
RECONNECT_AFTER_DOWN_SEC = 300

# Command-poll cadence: the app submits a command (reqOprType=0) then polls
# (reqOprType=1) every ~2.5s until the server finalises it (result==1 / no
# waitingProgressInfo), up to 30s. Without polling the server may never execute
# a stuck recharge. PCAP-confirmed 2026-05-29.
CMD_POLL_INTERVAL = 2.5
CMD_POLL_TIMEOUT = 30.0


def _tz_offset_minutes() -> int:
    """Local timezone offset in minutes vs UTC."""
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


def hookii_headers(token: str = "") -> dict[str, str]:
    """Standard Hookii request headers. `token` is the JWT (empty pre-login).

    Every field is required by the server (see the constants above); omitting
    any returns code=2 "hookii-agent参数错误".
    """
    return {
        "hookii-token": f"Hookii {token}" if token else "Hookii ",
        "user-agent": HOOKII_USER_AGENT,
        "hookii-agent": HOOKII_AGENT,
        "accept-encoding": "gzip",
        "app-time-zone-offset": str(_tz_offset_minutes()),
        "content-type": "application/json",
        "app-language": HOOKII_APP_LANG,
        "app-name": HOOKII_APP_NAME,
    }


@dataclass
class HookiiConfig:
    """Resolved connection parameters for one account."""

    email: str
    password: str
    env: str = "beta"
    model: str = DEFAULT_MODEL

    @property
    def host(self) -> str:
        return ENV_HOSTS.get(self.env, ENV_HOSTS["beta"])

    @property
    def rest_base(self) -> str:
        return f"https://{self.host}:{REST_PORT}"

    @property
    def mqtt_pass(self) -> str:
        return CLOUD_MQTT_PASS_BY_ENV.get(self.env, CLOUD_MQTT_PASS_BY_ENV["beta"])


@dataclass
class HookiiAccount:
    """Mutable per-account auth state, refreshed by login()."""

    label: str
    jwt: str = ""
    app_user_id: str = ""
    serials: list[str] = field(default_factory=list)


class HookiiAuthError(RuntimeError):
    """Login failed (bad credentials, unexpected response shape)."""


_HEX32 = __import__("re").compile(r"^[0-9a-fA-F]{32}$")


def md5_upper(s: str) -> str:
    """MD5(cleartext) upper-hex. If `s` already looks like a 32-hex digest,
    treat it as pre-hashed and just upper-case it (so a user can store the
    hash instead of the cleartext password)."""
    import hashlib

    if _HEX32.match(s):
        return s.upper()
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def login(cfg: HookiiConfig, acct: HookiiAccount) -> None:
    """Refresh acct.jwt + acct.serials via POST /api/v1/user/login/email.

    Synchronous (run in an executor). Raises HookiiAuthError on failure.
    """
    url = f"{cfg.rest_base}/api/v1/user/login/email"
    body = {"email": cfg.email, "password": md5_upper(cfg.password)}
    try:
        r = requests.post(
            url, json=body, headers=hookii_headers(token=""), timeout=15, verify=False
        )
    except requests.RequestException as err:
        raise HookiiAuthError(f"login transport error: {err}") from err
    try:
        data = r.json()
    except ValueError as err:
        raise HookiiAuthError(
            f"login returned {r.status_code} non-JSON ({len(r.content)} bytes)"
        ) from err
    payload = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(payload, dict):
        raise HookiiAuthError(f"unexpected login response shape: {data!r}")
    acct.jwt = payload.get("token") or payload.get("jwt") or ""
    if not acct.jwt:
        raise HookiiAuthError(f"login response missing token: keys={list(payload.keys())}")
    acct.app_user_id = str(payload.get("appUserId") or payload.get("userId") or "")
    devs = payload.get("deviceList") or payload.get("devices") or []
    serials = [d.get("deviceSn") or d.get("sn") or d.get("serial") for d in devs]
    serials = [s for s in serials if s]
    if serials:
        acct.serials = serials
    _LOGGER.info(
        "[%s] login OK jwt-len=%d serials=%s", acct.label, len(acct.jwt), acct.serials
    )


# ---------------------------------------------------------------------------
# REST control channel
# ---------------------------------------------------------------------------


def _post(cfg: HookiiConfig, acct: HookiiAccount, path: str, body: dict) -> dict:
    """POST a command. Auto re-login + retry on HTTP 401 or app-level code=10
    (server-side token invalidation, e.g. another device signed in). Returns
    response.data (dict) or {} on failure."""
    url = f"{cfg.rest_base}{path}"
    for attempt in (1, 2):
        try:
            r = requests.post(
                url, json=body, headers=hookii_headers(acct.jwt), timeout=20, verify=False
            )
        except requests.RequestException:
            _LOGGER.exception("[%s] POST %s transport error (try %d)", acct.label, path, attempt)
            return {}
        if r.status_code == 401 and attempt == 1:
            _LOGGER.info("[%s] POST %s -> 401, re-login + retry", acct.label, path)
            try:
                login(cfg, acct)
            except HookiiAuthError:
                _LOGGER.exception("[%s] re-login failed during retry", acct.label)
                return {}
            continue
        try:
            data = r.json()
        except ValueError:
            _LOGGER.error("[%s] POST %s -> %s non-JSON", acct.label, path, r.status_code)
            return {}
        code = data.get("code") if isinstance(data, dict) else None
        if code == 10 and attempt == 1:
            _LOGGER.info("[%s] POST %s -> code=10 token-invalid, re-login + retry", acct.label, path)
            try:
                login(cfg, acct)
            except HookiiAuthError:
                return {}
            continue
        if code not in (0, 1):
            _LOGGER.warning(
                "[%s] POST %s -> code=%s msg=%s",
                acct.label, path, code, data.get("msg") if isinstance(data, dict) else "?",
            )
        return (data.get("data") if isinstance(data, dict) else {}) or {}
    return {}


def _cmd_start_stop(
    cfg: HookiiConfig, acct: HookiiAccount, serial: str, model: str,
    command: int, region_list: list | None = None, req_opr_type: int = 0,
) -> dict:
    body: dict = {
        "command": command,
        "serialNumber": serial,
        "modelCode": model,
        "reqOprType": req_opr_type,
    }
    if region_list is not None:
        body["regionList"] = region_list
    return _post(cfg, acct, "/api/v1/mower/cmd/start/stop/job", body)


def _cmd_start_stop_polled(
    cfg: HookiiConfig, acct: HookiiAccount, serial: str, model: str,
    command: int, region_list: list | None = None,
) -> dict:
    """Submit (reqOprType=0) then poll (reqOprType=1) until the server finalises
    the command or CMD_POLL_TIMEOUT elapses."""
    initial = _cmd_start_stop(cfg, acct, serial, model, command, region_list, req_opr_type=0)
    if not initial:
        return initial
    if initial.get("result") == 1 and not initial.get("waitingProgressInfo"):
        return initial
    deadline = time.time() + CMD_POLL_TIMEOUT
    last = initial
    polls = 0
    while time.time() < deadline:
        time.sleep(CMD_POLL_INTERVAL)
        polls += 1
        last = _cmd_start_stop(cfg, acct, serial, model, command, region_list, req_opr_type=1)
        if not last:
            return {}
        if last.get("result") == 1 or not last.get("waitingProgressInfo"):
            _LOGGER.info("[%s] cmd %s finalised after %d poll(s)", acct.label, command, polls)
            return last
    _LOGGER.warning("[%s] cmd %s poll timeout", acct.label, command)
    return last


def _capture_snapshot(cfg: HookiiConfig, acct: HookiiAccount, serial: str, model: str) -> bytes | None:
    """Trigger an on-demand camera snapshot and return the JPG bytes (or None).

    Two-step (PCAP-confirmed): POST /mower/capture/image -> {result, fileUrl};
    then GET fileUrl (a short-lived CDN URL on a different port) for the JPG."""
    data = _post(cfg, acct, "/api/v1/mower/capture/image",
                 {"serialNumber": serial, "modelCode": model})
    if not data or not data.get("result"):
        _LOGGER.info("[%s] snapshot declined: %s", acct.label, data)
        return None
    file_url = data.get("fileUrl")
    if not file_url:
        return None
    try:
        r = requests.get(file_url, headers=hookii_headers(acct.jwt), timeout=20, verify=False)
        return r.content if r.status_code == 200 else None
    except requests.RequestException:
        _LOGGER.exception("[%s] snapshot download failed", acct.label)
        return None


def _cmd_recover_alarm(cfg: HookiiConfig, acct: HookiiAccount, serial: str, model: str) -> dict:
    """Self-heal a remote-recoverable exception (e.g. docking failure 515).
    reqOprType 0 then poll 1 until the server returns code=61 (= done)."""
    body_base = {"serialNumber": serial, "modelCode": model, "response": None}

    def call(opr: int):
        url = f"{cfg.rest_base}/api/v1/mower/remote/recovery/alarm"
        for attempt in (1, 2):
            try:
                r = requests.post(url, json={**body_base, "reqOprType": opr},
                                  headers=hookii_headers(acct.jwt), timeout=20, verify=False)
            except requests.RequestException:
                return None, {}
            if r.status_code == 401 and attempt == 1:
                try: login(cfg, acct)
                except HookiiAuthError: return None, {}
                continue
            try: env = r.json()
            except ValueError: return None, {}
            code = env.get("code") if isinstance(env, dict) else None
            if code == 10 and attempt == 1:
                try: login(cfg, acct)
                except HookiiAuthError: return None, {}
                continue
            return code, (env.get("data") if isinstance(env, dict) else {}) or {}
        return None, {}

    code, data = call(0)
    if code == 61:
        return {"completed": True}
    deadline = time.time() + CMD_POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(CMD_POLL_INTERVAL)
        code, data = call(1)
        if code == 61:
            return {"completed": True}
        if code not in (0, 1):
            return data
    return data


class HookiiCloudClient:
    """Cloud-MQTT telemetry subscriber + REST command sender for one account.

    Call start()/stop() from an executor (paho's loop runs its own thread). On
    each inbound push it invokes ``on_telemetry(serial, payload_dict)`` from the
    paho thread; the coordinator marshals that onto the HA loop. Control methods
    (start/pause/dock/...) are synchronous REST and also belong in an executor.
    """

    def __init__(
        self,
        cfg: HookiiConfig,
        acct: HookiiAccount,
        on_telemetry: Callable[[str, dict], None],
    ) -> None:
        self.cfg = cfg
        self.acct = acct
        self._on_telemetry = on_telemetry
        self.client_id = f"Android_{cfg.email}_{int(time.time() * 1000)}"
        self._stop = threading.Event()
        self._client: mqtt.Client | None = None
        self._hb_thread: threading.Thread | None = None
        # Per-serial model code learned from the observed push topic.
        self._serial_model: dict[str, str] = {sn: cfg.model for sn in acct.serials}
        # Per-mower firmware-upgrade gate (robotStatus 6): drop commands so a
        # stray press can't interfere with an OTA flash.
        self._upgrading: dict[str, bool] = {}

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> None:
        c = mqtt.Client(
            client_id=self.client_id,
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        c.username_pw_set(CLOUD_MQTT_USER, self.cfg.mqtt_pass)
        # Hookii's cloud broker uses a self-signed chain (same reason their app
        # needs reFlutter to bypass validation). Fixed known host -> accept.
        tls_ctx = ssl.create_default_context()
        tls_ctx.check_hostname = False
        tls_ctx.verify_mode = ssl.CERT_NONE
        c.tls_set_context(tls_ctx)
        c.tls_insecure_set(True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_disconnect = self._on_disconnect
        c.connect_async(self.cfg.host, MQTT_PORT, keepalive=15)
        c.loop_start()
        self._client = c
        if not self._hb_thread or not self._hb_thread.is_alive():
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, name=f"hookii-hb-{self.acct.label}", daemon=True
            )
            self._hb_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._client:
            self._client.loop_stop()
            try:
                self._client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass

    # ---- callbacks ----------------------------------------------------

    def _on_connect(self, client, _userdata, _flags, rc, _props=None):
        if rc != mqtt.CONNACK_ACCEPTED:
            _LOGGER.error("[%s] cloud-mqtt connect failed rc=%s", self.acct.label, rc)
            return
        _LOGGER.info(
            "[%s] cloud-mqtt connected as %s, subscribing to %d serial(s)",
            self.acct.label, self.client_id, len(self.acct.serials),
        )
        for sn in self.acct.serials:
            # Wildcard model in the topic - we don't know every model code a
            # priori; the server filters by the JWT in our heartbeat.
            client.subscribe(f"hk/server/mower/push/+/{sn}", qos=1)

    def _on_disconnect(self, _client, _userdata, _flags, rc, _props=None):
        _LOGGER.warning("[%s] cloud-mqtt disconnected rc=%s (auto-reconnect)", self.acct.label, rc)

    def _on_message(self, _client, _userdata, msg):
        try:
            parts = msg.topic.split("/")  # hk/server/mower/push/<model>/<serial>
            if len(parts) < 6:
                return
            serial = parts[-1]
            observed_model = parts[-2]
            if self._serial_model.get(serial) != observed_model:
                self._serial_model[serial] = observed_model
            try:
                payload = json.loads(msg.payload)
            except (ValueError, TypeError):
                _LOGGER.warning("[%s] non-JSON payload on %s", self.acct.label, msg.topic)
                return
            if payload.get("msgType") == "STATUS":
                # Normalise THIS message's raw STATUS (fan chassisData/taskInfo
                # out, derive ha_state) BEFORE handing it on. Must be done on the
                # fresh per-message dict: normalise_status uses setdefault for the
                # fan-out, so running it on a persistent accumulator would freeze
                # the derived fields after the first message. The coordinator owns
                # the sparse-merge accumulation across messages.
                st_in = payload.get("data", {}).get("STATUS", {})
                if isinstance(st_in, dict):
                    normalise_status(st_in)
                    # robotStatus 6 == firmware upgrading -> gate commands.
                    self._upgrading[serial] = st_in.get("robotStatus") == 6
            self._on_telemetry(serial, payload)
        except Exception:  # noqa: BLE001 - never let one bad message kill the loop
            _LOGGER.exception("[%s] error processing inbound msg", self.acct.label)

    # ---- heartbeat ----------------------------------------------------

    def _heartbeat_loop(self) -> None:
        _LOGGER.info("[%s] heartbeat thread up (%.1fs)", self.acct.label, HEARTBEAT_SEC)
        while not self._stop.is_set():
            if not self._client or not self._client.is_connected():
                time.sleep(1)
                continue
            for sn in self.acct.serials:
                model = self._serial_model.get(sn, self.cfg.model)
                payload = json.dumps({
                    "ts": int(time.time() * 1000),
                    "msgType": "HEARTBEAT",
                    # push is a FIXED per-session value (not a counter); the
                    # server keys the logical session on it, which is why two
                    # heartbeat streams with different push values evict each
                    # other (the single-session constraint).
                    "data": {"push": HEARTBEAT_PUSH, "token": self.acct.jwt},
                })
                try:
                    self._client.publish(f"hk/app/mower/hb/{model}/{sn}", payload, qos=0)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("[%s] heartbeat publish failed for %s", self.acct.label, sn)
            deadline = time.time() + HEARTBEAT_SEC
            while time.time() < deadline:
                if self._stop.is_set():
                    return
                time.sleep(min(0.5, max(0.01, deadline - time.time())))

    # ---- control (synchronous REST; run in an executor) ---------------

    def model_for(self, serial: str) -> str:
        return self._serial_model.get(serial, self.cfg.model)

    def send_action(self, action: str, serial: str, region_list: list | None = None) -> bool:
        """Translate a high-level action into the proper command code(s).

        Returns False (and does nothing) while the mower is mid-OTA. Command
        codes are PCAP-confirmed in the protocol reference:
          start=precheck(7)+execute(6), pause=3, dock/return=1,
          stop_keep=2, stop_clear=8.
        """
        if self._upgrading.get(serial):
            _LOGGER.warning("[%s] dropping '%s' - firmware upgrade in progress", self.acct.label, action)
            return False
        model = self.model_for(serial)
        try:
            if action == "start":
                # Two-step: cmd=7 pre-check (resume-from-breakpoints is the safe
                # default), then cmd=6 execute.
                _cmd_start_stop(self.cfg, self.acct, serial, model, 7, region_list or [])
                _cmd_start_stop_polled(self.cfg, self.acct, serial, model, 6, region_list or [])
            elif action == "pause":
                _cmd_start_stop_polled(self.cfg, self.acct, serial, model, 3)
            elif action in ("return", "dock", "recharge"):
                _cmd_start_stop_polled(self.cfg, self.acct, serial, model, 1)
            elif action == "stop_keep":
                _cmd_start_stop_polled(self.cfg, self.acct, serial, model, 2)
            elif action == "stop_clear":
                _cmd_start_stop_polled(self.cfg, self.acct, serial, model, 8)
            elif action == "recover_alarm":
                _cmd_recover_alarm(self.cfg, self.acct, serial, model)
            else:
                _LOGGER.warning("[%s] unknown action %r", self.acct.label, action)
                return False
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[%s] action %s failed", self.acct.label, action)
            return False
        return True

    def capture_snapshot(self, serial: str) -> bytes | None:
        """Trigger + fetch an on-demand camera snapshot (synchronous REST)."""
        return _capture_snapshot(self.cfg, self.acct, serial, self.model_for(serial))


def _now_iso() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat()
