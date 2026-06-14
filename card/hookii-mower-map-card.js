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
        this._geom[msg.label] = msg.geometry;
        if (msg.label === this._activeLabel()) this._render();
      },
      { type: "hookii_neomow/subscribe" }
    );
  }

  _activeLabel() {
    if (this._config && this._config.mower) return this._config.mower;
    const labels = Object.keys(this._geom || {});
    return labels.length ? labels[0] : null;
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
    const g = label ? (this._geom || {})[label] : null;
    // Render whenever ANY geometry exists — a docked/offline mower (the common
    // case) has no live robot position but still has a yard boundary + the cut
    // paths it has driven, which is exactly what's worth showing.
    if (!g || !this._hasGeometry(g)) {
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

    // Bounds from path + boundary + robot, padded.
    const bounds = [];
    if (path.length) for (const p of path) bounds.push([p[0], p[1]]);
    else for (const poly of mowing) for (const p of poly) bounds.push(p);
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
  "%c HOOKII-MOWER-MAP-CARD %c v0.1.1 ",
  "color:#0f172a;background:#22c55e;font-weight:700;",
  "color:#22c55e;background:#0f172a;"
);
