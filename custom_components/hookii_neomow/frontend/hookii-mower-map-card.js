/*
 * Hookii Neomow Map card — native, dependency-free Lovelace card.
 *
 * Renders the live yard/mower SVG client-side from geometry streamed by the
 * `hookii_neomow` integration over Home Assistant's authenticated websocket
 * (`hookii_neomow/subscribe`). No iframe, no external host, no MQTT in the
 * browser — works identically on HAOS and Container/Core installs.
 *
 * Deliberately written as a plain custom element (no LitElement / framework
 * import) so it does not couple to HA frontend internals — the main cause of
 * custom cards breaking across monthly HA releases. The only HA API it touches
 * is the stable, documented `hass.connection.subscribeMessage`.
 *
 * The rendering is a faithful port of the bridge's map_server.py render_svg():
 * translucent mowing polygons, exclusion fills, thick green cut swaths
 * (stroke width = mowing width in data units so adjacent rows merge), thin
 * transit paths, the live trail, and the robot marker + heading arrow.
 *
 * Config:
 *   type: custom:hookii-mower-map-card
 *   mower: garden        # label as configured in the integration (optional if
 *                        # only one mower is configured)
 *   rotate: 0            # degrees CCW, to match your in-app orientation
 *   title: Garden        # optional card header
 *   aspect_ratio: 1.4    # width/height of the map area (default 1.4)
 */

const BG = "#0f172a";

// Module-level last-known geometry per label. This survives for the LIFE OF THE
// PAGE across element recreations - HA tears down + rebuilds lovelace card
// ELEMENTS whenever the websocket drops+reconnects, giving each rebuilt card an
// empty per-instance _geom. The per-instance localStorage cache is meant to
// cover that, but with several large maps (boundary + cut-paths + trail) a
// setItem can silently blow the ~5MB quota and never persist - so a rebuilt card
// missed the cache and flashed "Waiting for map data…" for the 5-15s reconnect.
// This in-memory map has no quota and never fails, so a rebuilt element repaints
// its last map INSTANTLY. localStorage stays as the cross-reload / cross-tab tier.
const LAST_GOOD = {};

class HookiiMowerMapCard extends HTMLElement {
  setConfig(config) {
    this._config = Object.assign(
      { rotate: 0, aspect_ratio: 1.4 },
      config || {}
    );
    this._buildShell();
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._subscribed) this._subscribe();
  }

  connectedCallback() {
    if (this._hass && !this._subscribed) this._subscribe();
  }

  disconnectedCallback() {
    if (this._unsub) {
      this._unsub.then((u) => u && u()).catch(() => {});
      this._unsub = null;
      this._subscribed = false;
    }
  }

  getCardSize() {
    return 6;
  }

  // --- data plane -----------------------------------------------------------

  _subscribe() {
    if (!this._hass || !this._hass.connection) return;
    this._subscribed = true;
    this._geom = this._geom || {};
    this._unsub = this._hass.connection.subscribeMessage(
      (msg) => {
        if (!msg || !msg.label) return;
        if (msg.geometry) {
          // Full snapshot: initial paint + rare boundary/path changes.
          // Keep last-known: never let an EMPTY snapshot replace a good cached
          // map. On an HA restart the integration can briefly answer a subscribe
          // before its persisted captures finish loading; that empty snapshot
          // used to blank the card ("Waiting for map data…") and stick until the
          // cloud next streamed a full map (rare - minutes to hours).
          const prev = this._geom[msg.label];
          if (prev && this._hasGeometry(prev) && !this._hasGeometry(msg.geometry)) {
            // ignore empty snapshot, keep the last good geometry
          } else {
            this._geom[msg.label] = msg.geometry;
            // Mirror the last good map to (1) the module-level in-memory store so
            // a rebuilt card element repaints instantly with no quota risk, and
            // (2) localStorage so a full page reload / new tab also repaints the
            // last-known map before the websocket answers. We NEVER revert to
            // "Waiting for map data…" once a real map has been captured.
            if (this._hasGeometry(msg.geometry)) {
              LAST_GOOD[msg.label] = msg.geometry;
              this._saveCache(msg.label, msg.geometry);
            }
          }
        } else if (msg.partial) {
          // Light delta (v0.3.17+): robot/status only, ~200 bytes instead of
          // the full multi-KB geometry on every STATUS. Merge into the cached
          // snapshot; ignore if none arrived yet (subscribe sends one first).
          const g = this._geom[msg.label];
          if (!g) return;
          LAST_GOOD[msg.label] = g; // keep the in-memory store pointing at the live object
          g.robot = msg.robot;
          g.battery = msg.battery;
          g.work_status = msg.work_status;
          g.online_status = msg.online_status;
          g.last_update = msg.last_update;
          if (msg.trail_last) {
            g.trail = g.trail || [];
            const last = g.trail[g.trail.length - 1];
            if (!last || last[0] !== msg.trail_last[0] || last[1] !== msg.trail_last[1]) {
              g.trail.push(msg.trail_last);
              if (g.trail.length > 2000) g.trail.shift();
            }
          }
          // Keep the cached last-known robot marker + trail reasonably fresh so
          // a reload after the mower goes quiet shows where it actually stopped,
          // not a stale spot. Throttled so ~1.5s deltas don't thrash storage.
          this._saveCacheThrottled(msg.label);
        } else {
          return;
        }
        if (msg.label === this._activeLabel()) this._render();
      },
      { type: "hookii_neomow/subscribe" }
    );
  }

  _activeLabel() {
    if (this._config && this._config.mower) return this._config.mower;
    const labels = Object.keys(this._geom || {});
    if (labels.length) return labels[0];
    // Fresh/recreated card element (HA rebuilds lovelace cards whenever the
    // websocket drops+reconnects) has an EMPTY in-memory _geom, so with no
    // `mower:` in the config we used to resolve NO label at all -> _render could
    // not even look up the durable cache -> it blanked to "Waiting for map
    // data…" for the 5-15s until the websocket re-answered, then snapped back.
    // Fall back to the last label we persisted so a recreated card repaints the
    // cached map INSTANTLY. This is the missing half of the durable cache.
    const persisted = this._persistedLabels();
    return persisted.length ? persisted[0] : null;
  }

  // --- DOM shell ------------------------------------------------------------

  _buildShell() {
    if (this._card) return;
    this._card = document.createElement("ha-card");
    this._body = document.createElement("div");
    this._body.style.cssText =
      "position:relative;width:100%;background:" + BG + ";overflow:hidden;";
    this._card.appendChild(this._body);
    this.innerHTML = "";
    this.appendChild(this._card);
  }

  _render() {
    if (!this._body) return;
    if (this._config.title) this._card.setAttribute("header", this._config.title);
    const ar = Number(this._config.aspect_ratio) || 1.4;
    this._body.style.aspectRatio = ar + " / 1";

    const label = this._activeLabel();
    // Instant repaint when this element has nothing in memory yet (a fresh card,
    // or - the common case - a card element HA just rebuilt on a websocket
    // reconnect): pull the last-known map from the module-level in-memory store
    // first (survives element recreation, no quota), then fall back to the
    // localStorage cache (survives a full page reload). Either way we never sit
    // on "Waiting for map data…" once a real map has been captured this session.
    if (label && (!this._geom || !this._geom[label])) {
      this._geom = this._geom || {};
      const mem = LAST_GOOD[label];
      if (mem) {
        this._geom[label] = mem;
      } else {
        const cached = this._loadCache(label);
        if (cached) {
          this._geom[label] = cached;
          LAST_GOOD[label] = cached;
        }
      }
    }
    const g = label ? (this._geom || {})[label] : null;
    // Render whenever ANY geometry exists — a docked/offline mower (the common
    // case) has no live robot position but still has a yard boundary + the cut
    // paths it has driven, which is exactly what's worth showing.
    if (!g || !this._hasGeometry(g)) {
      // Diagnostic: if this still fires with a map already seen this session,
      // the console tells us exactly which fallback missed.
      console.debug(
        "[hookii-map] waiting - label=%s geom=%s mem=%s cache=%s",
        label,
        g ? "empty" : "none",
        label && LAST_GOOD[label] ? "yes" : "no",
        label && this._loadCache(label) ? "yes" : "no"
      );
      this._body.innerHTML = this._placeholder("Waiting for map data…");
      return;
    }
    this._body.innerHTML = this._svg(g);
  }

  _hasGeometry(g) {
    const b = g.boundary || {};
    return !!(
      g.robot ||
      (g.path && g.path.length) ||
      (b.mowing && b.mowing.length) ||
      (b.exclusion && b.exclusion.length)
    );
  }

  // --- durable cache (localStorage) ----------------------------------------
  // The in-memory _geom is lost whenever the card element is torn down (page
  // reload, tab switch, HA restart). Mirroring the last good map to
  // localStorage means the card repaints it instantly on next load and NEVER
  // reverts to "Waiting for map data…" once a real map has been captured -
  // even if the integration is momentarily down and answers slowly (or not).

  _cacheKey(label) {
    return "hookii_map_v1:" + label;
  }

  _labelIndexKey() {
    return "hookii_map_labels_v1";
  }

  // Remember every label we have cached so a freshly-recreated card (empty
  // in-memory _geom, no `mower:` config) can still resolve WHICH cache key to
  // repaint from — see _activeLabel().
  _rememberLabel(label) {
    try {
      const s = localStorage.getItem(this._labelIndexKey());
      const arr = s ? JSON.parse(s) : [];
      if (!arr.includes(label)) {
        arr.push(label);
        localStorage.setItem(this._labelIndexKey(), JSON.stringify(arr));
      }
    } catch (e) {
      // best-effort
    }
  }

  _persistedLabels() {
    try {
      const s = localStorage.getItem(this._labelIndexKey());
      return s ? JSON.parse(s) : [];
    } catch (e) {
      return [];
    }
  }

  _saveCache(label, geometry) {
    try {
      localStorage.setItem(this._cacheKey(label), JSON.stringify(geometry));
      this._rememberLabel(label);
      (this._lastCacheSave || (this._lastCacheSave = {}))[label] = Date.now();
    } catch (e) {
      // storage full / disabled (private mode) — cache is best-effort. The
      // module-level LAST_GOOD covers same-session recreation regardless. Log
      // once so a persistent quota problem is diagnosable from the console.
      if (!this._quotaWarned) {
        this._quotaWarned = true;
        console.debug("[hookii-map] localStorage cache save failed (%s) - using in-memory only", (e && e.name) || e);
      }
    }
  }

  _saveCacheThrottled(label) {
    const now = Date.now();
    const last = (this._lastCacheSave || (this._lastCacheSave = {}))[label] || 0;
    if (now - last < 15000) return;
    const g = this._geom && this._geom[label];
    if (g && this._hasGeometry(g)) this._saveCache(label, g);
  }

  _loadCache(label) {
    try {
      const s = localStorage.getItem(this._cacheKey(label));
      return s ? JSON.parse(s) : null;
    } catch (e) {
      return null;
    }
  }

  _placeholder(text) {
    return (
      '<div style="position:absolute;inset:0;display:flex;align-items:center;' +
      'justify-content:center;color:#94a3b8;font-family:var(--paper-font-body1_-_font-family,sans-serif);' +
      'font-size:14px;padding:16px;text-align:center;">' +
      this._esc(text) +
      "</div>"
    );
  }

  // --- SVG rendering (port of map_server.render_svg) ------------------------

  _svg(g) {
    const rot = (Number(this._config.rotate) || 0) * (Math.PI / 180);
    const cos = Math.cos(rot);
    const sin = Math.sin(rot);
    const rotate = (x, y) =>
      rot === 0 ? [x, y] : [x * cos - y * sin, x * sin + y * cos];

    const mowing = (g.boundary.mowing || []).map((poly) =>
      poly.map((p) => rotate(p[0], p[1]))
    );
    const exclusion = (g.boundary.exclusion || []).map((poly) =>
      poly.map((p) => rotate(p[0], p[1]))
    );
    const path = (g.path || []).map((p) => {
      const r = rotate(p[0], p[1]);
      return [r[0], r[1], p[2]];
    });
    const trail = (g.trail || []).map((p) => rotate(p[0], p[1]));
    const robot = g.robot ? rotate(g.robot.x, g.robot.y) : null;

    // Bounds from the FULL mowing territory + path + robot, padded. We always
    // include every mowing zone (not just the current path's bbox) so the view
    // frames the WHOLE map - all zones - instead of zooming into the single
    // zone the mower is currently working. Exclusion zones sit inside the
    // mowing area so they need not extend the bounds.
    const bounds = [];
    for (const poly of mowing) for (const p of poly) bounds.push(p);
    if (path.length) for (const p of path) bounds.push([p[0], p[1]]);
    if (robot) bounds.push(robot);

    let minX, maxX, minY, maxY;
    if (bounds.length > 1) {
      minX = Math.min(...bounds.map((p) => p[0]));
      maxX = Math.max(...bounds.map((p) => p[0]));
      minY = Math.min(...bounds.map((p) => p[1]));
      maxY = Math.max(...bounds.map((p) => p[1]));
      const pad = 200;
      minX -= pad; maxX += pad; minY -= pad; maxY += pad;
    } else {
      minX = -1000; maxX = 1000; minY = -1000; maxY = 1000;
    }
    const spanX = Math.max(maxX - minX, 2000);
    const spanY = Math.max(maxY - minY, 2000);
    const W = Math.round(spanX);
    const H = Math.round(spanY);
    const px = Math.max(spanX, spanY) / 800;
    const toSvg = (x, y) => [x - minX, maxY - y]; // flip Y

    const out = [];
    out.push(
      '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" ' +
        'viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="xMidYMid meet" ' +
        'style="display:block;">'
    );
    out.push('<rect width="' + W + '" height="' + H + '" fill="' + BG + '"/>');

    const polyPoints = (poly) =>
      poly
        .map((p) => {
          const s = toSvg(p[0], p[1]);
          return s[0].toFixed(1) + "," + s[1].toFixed(1);
        })
        .join(" ");

    // Mowing territory (translucent green) then exclusion zones (dark).
    for (const poly of mowing) {
      out.push(
        '<polygon points="' + polyPoints(poly) + '" fill="#86efac33" ' +
          'stroke="#86efac55" stroke-width="' + px.toFixed(1) +
          '" stroke-linejoin="round"/>'
      );
    }
    for (const poly of exclusion) {
      out.push(
        '<polygon points="' + polyPoints(poly) + '" fill="#0f172acc" ' +
          'stroke="#475569" stroke-width="' + px.toFixed(1) +
          '" stroke-linejoin="round"/>'
      );
    }

    // Path coverage: split into cut (info==1) vs transit segments.
    if (path.length) {
      const cut = [];
      const transit = [];
      let cur = [];
      let curInfo = null;
      for (const p of path) {
        if (p[2] !== curInfo) {
          if (cur.length) (curInfo === 1 ? cut : transit).push(cur);
          cur = [];
          curInfo = p[2];
        }
        cur.push([p[0], p[1]]);
      }
      if (cur.length) (curInfo === 1 ? cut : transit).push(cur);

      const mowW = Number(g.mowing_width_cm) || 25;
      const cutStroke = Math.max(mowW * 1.4, px * 2);
      const segPoints = (seg) =>
        seg
          .map((p) => {
            const s = toSvg(p[0], p[1]);
            return s[0].toFixed(0) + "," + s[1].toFixed(0);
          })
          .join(" ");
      for (const seg of cut) {
        if (seg.length < 2) continue;
        out.push(
          '<polyline points="' + segPoints(seg) + '" fill="none" ' +
            'stroke="#22c55e" stroke-width="' + cutStroke.toFixed(0) +
            '" stroke-linecap="round" stroke-linejoin="round" opacity="0.85"/>'
        );
      }
      for (const seg of transit) {
        if (seg.length < 2) continue;
        out.push(
          '<polyline points="' + segPoints(seg) + '" fill="none" ' +
            'stroke="#86efac" stroke-width="' + px.toFixed(1) +
            '" opacity="0.4"/>'
        );
      }
    }

    // Live trail in the mower's colour.
    if (trail.length > 1) {
      const pts = trail
        .map((p) => {
          const s = toSvg(p[0], p[1]);
          return s[0].toFixed(0) + "," + s[1].toFixed(0);
        })
        .join(" ");
      out.push(
        '<polyline points="' + pts + '" fill="none" stroke="' + this._esc(g.color) +
          '" stroke-width="' + (px * 2).toFixed(1) + '" opacity="0.7"/>'
      );
    }

    // Robot marker + heading arrow — only when the mower is reporting a live
    // position. A docked/offline mower renders the yard + coverage without it.
    if (robot) {
      const rsvg = toSvg(robot[0], robot[1]);
      const r = px * 10;
      out.push(
        '<circle cx="' + rsvg[0].toFixed(0) + '" cy="' + rsvg[1].toFixed(0) +
          '" r="' + r.toFixed(0) + '" fill="' + this._esc(g.color) +
          '" stroke="#fff" stroke-width="' + (px * 2).toFixed(1) + '"/>'
      );
      if (g.robot.heading !== null && g.robot.heading !== undefined) {
        const a = (Number(g.robot.heading) + (Number(this._config.rotate) || 0)) *
          (Math.PI / 180);
        const ahx = Math.sin(a) * px * 18;
        const ahy = -Math.cos(a) * px * 18;
        out.push(
          '<line x1="' + rsvg[0].toFixed(0) + '" y1="' + rsvg[1].toFixed(0) +
            '" x2="' + (rsvg[0] + ahx).toFixed(0) + '" y2="' + (rsvg[1] + ahy).toFixed(0) +
            '" stroke="#fff" stroke-width="' + (px * 3).toFixed(1) + '"/>'
        );
      }
    }

    out.push("</svg>");
    return out.join("");
  }

  _esc(s) {
    return String(s == null ? "" : s).replace(/[<>&"]/g, (c) =>
      ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c])
    );
  }

  static getStubConfig() {
    return { type: "custom:hookii-mower-map-card" };
  }
}

customElements.define("hookii-mower-map-card", HookiiMowerMapCard);

// Register in the card picker.
window.customCards = window.customCards || [];
window.customCards.push({
  type: "hookii-mower-map-card",
  name: "Hookii Neomow Map",
  description: "Live native SVG map of your Hookii Neomow mower(s).",
  preview: false,
});

console.info(
  "%c HOOKII-MOWER-MAP-CARD %c v0.2.4 ",
  "color:#0f172a;background:#22c55e;font-weight:700;",
  "color:#22c55e;background:#0f172a;"
);
