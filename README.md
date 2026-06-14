# Hookii Neomow Map — native Home Assistant card + integration

Live, native SVG map of your Hookii Neomow robot mower(s) in Home Assistant —
**no iframe, no external service, works on every install type** (Home Assistant
OS, Supervised, **Container/Docker**, Core).

This is the v1.6 broad-support companion to the
[Hookii Bridge add-on](https://github.com/torvalstrom/hookii-bridge-ha-addon).
The bridge already auto-creates your mower entities (sensors, `lawn_mower`,
buttons, camera) via MQTT Discovery. This repo adds the **map**:

- **`custom_components/hookii_neomow/`** — a thin integration that subscribes to
  the bridge's republished MQTT map payloads (`DEVICE_MAP_V2`,
  `ALL_PATH_LIST_V2`, `ALL_PATH_INDEX_V2`, `STATUS`, `REGION_TASK`) and serves
  the geometry to the card over Home Assistant's authenticated websocket. It
  runs inside HA Core, so it needs no Supervisor and works on container installs.
- **`card/hookii-mower-map-card.js`** — a dependency-free Lovelace card that
  renders the yard boundary, cut/transit coverage, live trail and the mower's
  position + heading entirely client-side. No framework import, so it does not
  break when Home Assistant bumps its frontend.

## Why this exists

The bridge's built-in map is served over **HA Ingress**, which only exists on
HAOS/Supervised. Container users had to embed it via a raw URL (an external
reference). This card renders the same map natively from entity-adjacent
geometry, so **every** Home Assistant user gets the map with no reference and no
security trade-off.

## Requirements

1. The **Hookii Bridge** running (add-on on HAOS, or container on
   Docker/k3s/Compose) and publishing to your MQTT broker.
2. An **MQTT broker** + the Home Assistant **MQTT integration** configured
   (HAOS users: the Mosquitto add-on; container users: your own broker).

## Install (development / dogfood)

1. Copy `custom_components/hookii_neomow/` into your HA `config/custom_components/`
   and restart Home Assistant.
2. **Settings → Devices & Services → Add Integration → "Hookii Neomow Map"**,
   enter the topic prefix (default `hookii/details/device`) and your mowers as
   `label:serial[:color];…`.
3. Add `card/hookii-mower-map-card.js` as a Lovelace resource (module), then add
   the card:

   ```yaml
   type: custom:hookii-mower-map-card
   mower: garden     # the label you configured (optional if only one)
   rotate: 0         # degrees CCW to match your in-app orientation
   title: Garden
   ```

HACS one-click distribution lands once dogfooded. Status: **work in progress.**
