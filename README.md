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

## Quick start (step by step)

**Before you start** you need two things already working: the
[**Hookii Bridge**](https://github.com/torvalstrom/hookii-bridge-ha-addon)
(add-on on HAOS, or a container on Docker/Compose/k3s) publishing to your MQTT
broker, and Home Assistant's **MQTT integration** connected to that broker. The
bridge auto-creates your mower entities; this repo adds the map.

1. **Install HACS** if you don't have it yet — https://hacs.xyz (one-time, works
   on every HA install type).
2. **Add this repository to HACS:** HACS → ⋮ (top-right) → **Custom repositories**
   → paste `https://github.com/torvalstrom/hookii-neomow-ha`, category
   **Integration** → **Add**.
3. **Download:** open the new *Hookii Neomow Map* entry → **Download** →
   **Restart Home Assistant** when prompted.
4. **Add the integration:** Settings → Devices & Services → **Add Integration**
   → search **Hookii Neomow Map** → enter the MQTT topic prefix (default
   `hookii/details/device`) and your mowers as `label:serial[:color]` separated
   by `;` — e.g. `garden:HKX1EB100JD25010115:#22c55e;pond:HKX2EB100JD24080170`.
5. **Add the card to a dashboard:** edit any dashboard → **Add card** → search
   **Hookii Neomow Map** (or paste the YAML below). The card ships **inside** the
   integration and is already registered — there is no second HACS item and no
   Lovelace resource to add by hand.

   ```yaml
   type: custom:hookii-mower-map-card
   mower: garden     # the label you configured (optional if only one mower)
   rotate: 0         # 90/180/270 to match the orientation in the Hookii app
   title: Garden
   ```

The map appears as soon as the bridge streams the first data. Add one card per
mower. Updates later flow through that single HACS item.

> Don't have HACS yet, or prefer not to use it? You can also copy
> `custom_components/hookii_neomow/` into your HA `config/custom_components/`
> folder manually and restart — then continue from step 4.

## Why this exists

The bridge's built-in map is served over **HA Ingress**, which only exists on
HAOS/Supervised. Container users had to embed it via a raw URL (an external
reference). This card renders the same map natively from entity-adjacent
geometry, so **every** Home Assistant user gets the map with no reference and no
security trade-off.

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
