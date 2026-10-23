# Hookii Neomow — native Home Assistant integration

Control and monitor your Hookii Neomow robot mower(s) in Home Assistant, with a
live SVG map — **no add-on, no MQTT broker, no external service.** Works on every
install type (Home Assistant OS, Supervised, **Container/Docker**, Core).

You sign in with your Hookii app account and the integration connects **directly
to the Hookii cloud** (cloud-MQTT for live telemetry, HTTPS for control). It runs
entirely inside HA Core, so there's nothing else to install or host.

> **v0.3.0 is a major change.** Earlier versions were only a *map* that consumed
> MQTT data republished by the separate
> [Hookii Bridge add-on](https://github.com/torvalstrom/hookii-bridge-ha-addon).
> v0.3.0 absorbs that job: it talks to the Hookii cloud itself, so the add-on
> and the HA MQTT broker are **no longer needed**. Upgrading? See
> [Migrating](#migrating-from-the-mqtt-bridge-version).

## What you get

Per mower (one device each):

- **Live map card** — yard boundary, cut/transit coverage, live trail, and the
  mower's position + heading, rendered client-side (dependency-free Lovelace
  card, shipped inside the integration and auto-registered — nothing extra to
  install).
- **`lawn_mower` entity** — start / pause / return-to-dock, with live activity
  (mowing / docked / returning), plus a `hookii_neomow.start_region` service to mow
  only chosen zones (see [Mow specific zones](#mow-specific-zones)).
- **Sensors** — battery, blade RPM, charge current, work status, current region,
  cut area, mowing coverage, efficiency, task progress, mowing height, and the
  battery/blade/drive-motor temperatures. (Voltage, GPS satellites and firmware
  version ship disabled-by-default as diagnostics.)
- **Binary sensors** — firmware-upgrading and **error/fault** (see
  [Error & fault detection](#error--fault-detection)).
- **Buttons** — start, pause, return to dock, stop (keep/clear progress), clear
  exception, and **camera snapshot**.
- **Camera** — shows the latest on-demand snapshot (press the snapshot button;
  the mower's camera must be awake — a docked mower usually declines).

## Mow specific zones

**Where zones come from:** you define zones (regions/areas) in the **Hookii
mobile app** when you map your lawn - this integration *reads* them, it does not
create them. There is no separate "regions table" to fill in here.

**Where to see your zones in Home Assistant:** open **Developer Tools → States**,
find your `lawn_mower.neomow_<serial>` entity, and look at its
**`available_regions`** attribute (e.g. `["GardenNorth", "south", "mortenroad"]`).
That list is your table of zones.

**Why "Start" mows everything:** the normal start button
(`lawn_mower.start_mowing`) is a whole-yard mow - it covers all zones. To mow only
some zones, call the `hookii_neomow.start_region` service instead:

```yaml
service: hookii_neomow.start_region
target:
  entity_id: lawn_mower.neomow_<serial>
data:
  regions: ["south"]          # one or more zone names (or numeric area ids)
```

A sleeping mower wakes automatically. If it's already mowing another zone, the
current job is cancelled (keeping breakpoint progress) before the new zone starts.
A whole-yard mow is still just `lawn_mower.start_mowing`.

### Queue several zones with an automation

The cloud runs one zone at a time, so to chain zones start the next one when the
mower returns to the dock (its `activity` becomes `docked`):

```yaml
alias: Neomow zone queue (south then mortenroad)
triggers:
  - trigger: state
    entity_id: lawn_mower.neomow_<serial>
    to: docked
conditions:
  - condition: state
    entity_id: input_text.neomow_queue   # holds remaining zones, comma-separated
    state: "!="
    # ... pop the next zone from your queue helper and call start_region with it
actions:
  - service: hookii_neomow.start_region
    target: { entity_id: lawn_mower.neomow_<serial> }
    data:
      regions: "{{ states('input_text.neomow_queue').split(',')[0] }}"
  # ... then trim the consumed zone off input_text.neomow_queue
```

(Use any queue helper you like - an `input_text`, a `input_select`, or a script
variable. The key idea: trigger on `docked` so each zone starts from idle.)

## Error & fault detection

The **error** binary sensor (`binary_sensor.neomow_<serial>_problem`) turns on when
the mower needs attention, and exposes three attributes:

- `alarm_label` - a concise description with the Hookii error code in
  parentheses, e.g. `Emergency stop pressed (801)`, `Lifted (822)`,
  `Docking failed - check station power (516)`. Point dashboards and
  notifications at it. It follows the language Home Assistant is configured
  with (English and Danish are included).
- `alarm_code` - the raw code for automations to branch on (the numeric Hookii
  `errCode` when known, else a fault-class marker like `lift` / `halt`). The UI
  shows it translated, e.g. `Lifted (822)`; templates still get `822`.
- `alarm_description` - Hookii's own (English) notice text for the code, i.e.
  the sentence the Hookii app shows, with the instructions to clear it.

Two complementary signals feed it:

1. **Live STATUS** - a motion halt (`robotStatus == 4` / `1` in `runStatusList`) is
   detected immediately, including faults the cloud never raises a notice for
   (stop button, tilt, wheel slip). When no code is present the label is derived
   from the mower's own sensor flags (lift hall, bumper, drive/blade motor).
2. **Cloud notices** - the MQTT `NOTICE_ALARM` summary carries the precise
   `errCode` for the latest unread notice; docking/charging faults (`513` /
   `514` / `515` / `516` / `528`) that don't halt the mower are caught this way
   and persist until it charges or mows again. Stale unread notices are
   ignored - only a notice newer than the last one handled raises an alarm.
   Status and account notices (e.g. `511` Charging, `502` Mowing started,
   `16` Mower offline) never raise the alarm.

**Error code table.** `custom_components/hookii_neomow/error_codes.py` maps all
238 codes Hookii publishes notice texts for (101-1076 mower alarms plus 10-24
account/app notices) to a kind (fault / warning / info / notice), a short label
and Hookii's official English text. The official texts are the ones the Hookii
app downloads from the Hookii cloud's language service; the short labels and
the Danish translations are ours. Only code 107 has no official text.

A full dump of a mower's live state is available via **Settings → Devices &
Services → Hookii Neomow → ⋮ → Download diagnostics** (coordinates + serials
redacted).

## Install

1. **Install HACS** if you haven't — https://hacs.xyz (one-time, all install types).
2. **Add this repo to HACS:** HACS → ⋮ → **Custom repositories** → paste
   `https://github.com/torvalstrom/hookii-neomow-ha`, category **Integration** → **Add**.
3. Open the new **Hookii Neomow** entry → **Download** → **Restart Home Assistant**.
4. **Add the integration:** Settings → Devices & Services → **Add Integration** →
   search **Hookii Neomow** → sign in:
   - **Email / password** — your Hookii app login (the password is stored locally
     and only sent, MD5-hashed, to the Hookii cloud).
   - **Cloud environment** — `beta` for most accounts, `prod` if your account
     lives on the production cloud.
   - **Mower serial numbers** *(optional)* — only needed if no mowers are found
     automatically. Comma-separated, e.g. `HKX1EB100XXXXXXXXXX, HKX2EB100XXXXXXXXXX`.
     Find them in the Hookii app under each mower's details.

## Add the map to a dashboard

The card is registered automatically. Add a card of type
`custom:hookii-mower-map-card`. Options (all optional):

```yaml
type: custom:hookii-mower-map-card
title: Neomow Map
mower: "Neomow 080170"   # omit to show the first mower; matches the device name
rotate: 0                # degrees, to match your yard's orientation
aspect_ratio: "1.4"
```

## Important: single Hookii session

The Hookii cloud allows **one active session per account**. While this
integration is connected it holds that session, so the **Hookii phone app may get
signed out periodically** (and vice-versa). This is inherent to the Hookii cloud,
not a bug. Use a dedicated/secondary Hookii account if that's a problem.

## Migrating from the MQTT-bridge version

v0.3.0 no longer reads MQTT. After updating:

1. Remove the old config entry (it used an MQTT topic prefix) and **re-add** the
   integration with your Hookii login as above.
2. You can **stop/remove the Hookii Bridge add-on** and the dedicated MQTT broker
   — they're no longer used. (Keep MQTT if other integrations need it.)
3. Entity IDs change (they're now per-mower native entities). Update any
   dashboards/automations that referenced the old MQTT-discovered entities.

## How it works

- **Telemetry:** a per-account client connects to the Hookii cloud MQTT broker
  (TLS) and a session heartbeat keeps it streaming STATUS / DEVICE_MAP_V2 /
  ALL_PATH_LIST_V2 / ALL_PATH_INDEX_V2 / REGION_TASK, which become entities + map
  geometry (served to the card over HA's authenticated websocket).
- **Control:** commands (start/pause/dock/stop/recover/snapshot) go over HTTPS to
  the Hookii cloud REST API.
- Bundled `paho-mqtt` + `requests`; nothing host-specific, so it runs on Core /
  Container the same as HAOS.

Reverse-engineered, unofficial, and not affiliated with Hookii. Use at your own
risk.
