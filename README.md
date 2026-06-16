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
  (mowing / docked / returning).
- **Sensors** — battery, blade RPM, charge current, work status, current region,
  cut area, mowing coverage, efficiency, task progress, mowing height, and the
  battery/blade/drive-motor temperatures. (Voltage, GPS satellites and firmware
  version ship disabled-by-default as diagnostics.)
- **Binary sensors** — firmware-upgrading, error/alarm.
- **Buttons** — start, pause, return to dock, stop (keep/clear progress), clear
  exception, and **camera snapshot**.
- **Camera** — shows the latest on-demand snapshot (press the snapshot button;
  the mower's camera must be awake — a docked mower usually declines).

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
     automatically. Comma-separated, e.g. `HKX1EB100JD25010115, HKX2EB100JD24080170`.
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
