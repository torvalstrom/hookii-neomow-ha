# Native cloud migration — learnings (2026-06-16)

The integration absorbed the `hookii-bridge-ha-addon` add-on's job. It used to
be an MQTT *consumer* (the add-on polled the Hookii cloud and republished to a
local MQTT broker; this integration subscribed and drew the map). It now talks
to the Hookii cloud **directly** (`api.HookiiCloudClient`), so there is no
add-on and no MQTT broker — it works on every HA install type (HAOS,
Supervised, Container, **Core**).

Verified live on `homeassistant.cscloud.dk` (a Container-install HA on k3s):
map card + `lawn_mower` (start/pause/dock) + battery/blade-rpm/coverage sensors,
all from the cloud. `lawn_mower.neomow_080190` = mowing, battery 50%, blade
1781 rpm, coverage 74%; `…080170` = docked.

## Hookii protocol gotchas (hit while porting bridge.py → api.py)

- **Full header set is mandatory.** Missing `hookii-agent` (or the rest) →
  `{"code":2,"msg":"hookii-agent参数错误"}`. `user-agent` must be the Flutter
  default `Dart/3.9 (dart:io)`. Keep `hookii_headers()` byte-identical to the
  add-on.
- **Login often omits the device list.** `/user/login/email` returns a JWT but
  no `deviceList` for some accounts → discover-by-login yields zero mowers. The
  config flow has an optional **serials** field as a fallback (same reason the
  add-on hardcodes `HOOKII_SERIALS_<label>`). A proper device-list endpoint is
  still TODO.
- **Cloud-MQTT password is per-environment**, username is constant
  (`hookii-iot`). beta vs prod use different passwords; derive from `env`.
  Per-user authz is the JWT in the heartbeat, not the MQTT login.
- **Single session.** The Hookii cloud allows ONE logical session per account
  (keyed on the heartbeat `push` value). The integration, the old add-on, and
  the phone app all compete — only one may run per account. Migration step:
  scale/stop the bridge before the integration takes the session.
- **STATUS is sparse + two-shaped** (A flat / B nested under
  `chassisData`/`taskInfo`, `battery` vs `electricity`, …). `status.py`
  normalises both and merges non-null fields so sensors don't flicker to
  "unknown". `geometry.parse_status` reads RAW fields, so the map needs no
  normalisation — only the entities do.
- **Command codes** (REST, not MQTT): start = precheck `cmd=7` then exec
  `cmd=6`; pause `3`; dock/return `1`; stop-keep `2`; stop-clear `8`; with
  `reqOprType=0`→`1` polling until the server finalises.

## HA / integration specifics

- `paho-mqtt` and `requests` are already bundled in the HA image — listed in
  `manifest.json` `requirements` anyway for non-bundled installs.
- `manifest.json`: dropped the `mqtt` dependency; `iot_class` → `cloud_push`,
  `integration_type` → `hub`.
- paho runs its own network thread → `on_message` fires off the event loop. The
  coordinator marshals each message with `hass.loop.call_soon_threadsafe`.
- Persist race: concurrent same-`(label,msg_type)` cloud bursts land on
  different `SyncWorker` threads and collided on a shared `.tmp`. Fixed with a
  unique tmp name per write.
- Config-entry schema bumped v1 → v2. With no `async_migrate_entry`, an old v1
  entry simply fails to load (logs "Migration handler not found") — it does NOT
  crash setup. Old entries should be deleted on upgrade (or a migration added).
- The bundled Lovelace card loads fine as a registered resource
  (`custom:hookii-mower-map-card`).

## Persistence seeding / mower labels

- Big captures (`DEVICE_MAP_V2`, `ALL_PATH_LIST_V2`, `ALL_PATH_INDEX_V2`) persist
  to `<config>/hookii_neomow_data/<label>_<TYPE>.json` and reload on start. The
  cloud re-streams them only every few minutes, so after a fresh add/restart the
  map is blank until then — seed by dropping the JSONs in (keyed by **label**).
- **Renaming a mower changes its persistence key** → it loses its boundary until
  reseeded/re-streamed. During this test the new auto-labels (`Neomow 080170`)
  were seeded from the old labels (`pond` etc.) by serial.

## Multi-account

- The add-on was multi-account (`tor` + `jannick` in one `HOOKII_ACCOUNTS`).
  The native integration is **per-account** — one config entry per Hookii login.
  Replacing a multi-account bridge = multiple config entries.

## Deploy/test method (k3s container HA)

- Copy into the pod via `tar -C … -cf - hookii_neomow | kubectl exec -i … -- tar -C /config/custom_components -xf -`
  (robust vs Windows `kubectl cp` colon-path quirks). Disable Git-Bash path
  mangling with `MSYS_NO_PATHCONV=1` for any `/config…` exec arg.
- Drive the config flow + verify entities/dashboards via the REST/WS API with a
  long-lived token. (The canonical token file is structured: read the
  `HA_TOKEN=` line, not the whole file.)

## Still open

- Per-mower friendly names in the config flow (vs `Neomow <last6>`).
- Auto device-list endpoint so serials aren't manual.
- Dashboard/automation migration from the add-on's MQTT entity names
  (`sensor.neomowxpro170_*`) to the native ones (`sensor.neomow_080170_*`).
- Reconcile the bundled card version vs `CARD_VERSION` on changes.
