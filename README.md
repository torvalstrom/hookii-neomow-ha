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
- **`custom_components/hookii_neomow/frontend/hookii-mower-map-card.js`** — a
  dependency-free Lovelace card that renders the yard boundary, cut/transit
  coverage, live trail and the mower's position + heading entirely client-side.
  It ships **inside** the integration (served + auto-registered on setup), so
  there is no second thing to install. No framework import, so it does not break
  when Home Assistant bumps its frontend.

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

## Install (via HACS — one item, card included)

The card is **bundled inside the integration** — installing the integration
serves and auto-registers the card, so there is no second HACS item and no
Lovelace resource to add by hand.

1. **HACS → ⋮ (top-right) → Custom repositories** → add
   `https://github.com/torvalstrom/hookii-neomow-ha` with category **Integration**
   → **Download** → restart Home Assistant.
2. **Settings → Devices & Services → Add Integration → "Hookii Neomow Map"** →
   enter the topic prefix (default `hookii/details/device`) and your mowers as
   `label:serial[:color];…`.
3. Add the card to any dashboard (it is already registered — just reference it):

   ```yaml
   type: custom:hookii-mower-map-card
   mower: garden     # the label you configured (optional if only one)
   rotate: 0         # degrees CCW to match your in-app orientation
   title: Garden
   ```

Updates flow through that single HACS item. (Default-store inclusion — so it's
findable by search without the custom-repo step — is pending a `home-assistant/brands` PR.)

## Card options

| Option | Default | Description |
|---|---|---|
| `mower` | first configured | The mower **label** to show (from the integration config). Optional if you only configured one mower. |
| `rotate` | `0` | Degrees counter-clockwise. Set to `90`/`180`/`270` so the map matches the orientation you see in the Hookii app. |
| `title` | — | Optional card header. |
| `aspect_ratio` | `1.4` | Width-to-height ratio of the map area. |

Add one card per mower. Example for a multi-mower setup:

```yaml
type: custom:hookii-mower-map-card
mower: greenhouse
title: Greenhouse
rotate: 90
```

## What the map shows

- **Yard boundary** (translucent green) once the cloud has streamed `DEVICE_MAP_V2`
  (can take minutes to hours after a mower first comes online), plus any
  **exclusion zones** (dark).
- **Coverage**: thick green where the mower **cut**, thin light-green for
  **transit** (moving without cutting).
- The **live trail** in the mower's colour and the **robot** itself with a
  heading arrow.
- A **docked or offline mower** still shows its yard + coverage — just without
  the live robot marker (so the map is useful even when the mower is parked).

The boundary + path captures are persisted, so an HA restart does not blank the
map while it waits for the next cloud stream.

## Troubleshooting

- **"Custom element doesn't exist: hookii-mower-map-card"** — fully close and
  reopen the app (or hard-refresh the browser) after installing/updating, so the
  bundled card is fetched.
- **Map says "Waiting for map data…"** — the bridge isn't publishing yet, or the
  topic prefix / mower serial don't match what the bridge uses. Check the bridge
  is running and that the MQTT integration is connected.
- **Map looks rotated wrong** — set `rotate` on the card to `90`, `180` or `270`.
- **Migrating from the old iframe map** (`neomow.cscloud.dk` / the bridge's
  Ingress page) — replace those `iframe`/`picture` cards with this card.
