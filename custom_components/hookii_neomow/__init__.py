"""Hookii Neomow Map integration.

Data plane for the native (no-iframe) Lovelace mower-map card: subscribes to
the Hookii Bridge's republished MQTT map payloads and serves per-mower geometry
to the card over Home Assistant's authenticated websocket. Entities themselves
come from the bridge's MQTT Discovery - this integration only adds the rich map
geometry the card needs (which is too large / too raw for entity attributes).
"""
from __future__ import annotations

import logging
import os

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_MOWERS, CONF_TOPIC_PREFIX, DEFAULT_TOPIC_PREFIX, DOMAIN
from .coordinator import NeomowCoordinator
from . import websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []

# The Lovelace card ships bundled inside this integration. We serve it from a
# static path and load it on the frontend so the user never has to install a
# second HACS item or register a Lovelace resource by hand - one HACS install
# (the integration) brings both the data plane AND the card.
CARD_FILENAME = "hookii-mower-map-card.js"
# Served from the package's frontend/ subdir (we register the DIRECTORY, which
# is the reliable static-path form, and it keeps the integration's .py source
# out of the public path).
CARD_DIR_URL = f"/{DOMAIN}_frontend"
# Bump CARD_VERSION on every hookii-mower-map-card.js change. The version query
# (a) cache-busts the browser/service-worker module cache and (b) gives the
# updated card a NEW module-map URL so customElements.define runs for it - a
# same-named custom element cannot be redefined in a live frontend session, so
# an unchanged URL would keep serving the previously-defined (old) card class.
CARD_VERSION = "0.2.2"
CARD_URL = f"{CARD_DIR_URL}/{CARD_FILENAME}?v={CARD_VERSION}"


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve + register the bundled Lovelace card (idempotent, non-fatal)."""
    if hass.data.get(f"{DOMAIN}_card_registered"):
        return
    card_dir = os.path.join(os.path.dirname(__file__), "frontend")
    try:
        try:
            # HA 2024.7+: async static path registration.
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(CARD_DIR_URL, card_dir, False)]
            )
        except ImportError:
            # Older HA: synchronous registration.
            hass.http.register_static_path(CARD_DIR_URL, card_dir, cache_headers=False)
        # Register as a Lovelace RESOURCE (loaded + awaited BEFORE the dashboard
        # renders) rather than add_extra_js_url (which is not awaited -> the card
        # element can be undefined at first render -> "Custom element doesn't
        # exist"). The versioned URL also avoids the plain-URL service-worker
        # cache pinning an old card. Falls back to add_extra_js_url in YAML-mode
        # lovelace where the resource collection is unavailable.
        await _async_register_resource(hass, CARD_URL)
        hass.data[f"{DOMAIN}_card_registered"] = True
        _LOGGER.debug("registered bundled card at %s", CARD_URL)
    except Exception:  # noqa: BLE001 - card is a nicety; never break entry setup
        _LOGGER.exception("failed to register bundled Lovelace card")


async def _async_register_resource(hass: HomeAssistant, url: str) -> None:
    """Add (or update) the card as a Lovelace module resource, idempotently."""
    try:
        lovelace = hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None)
        if resources is None and isinstance(lovelace, dict):
            resources = lovelace.get("resources")
        if resources is None or not hasattr(resources, "async_create_item"):
            # YAML-mode lovelace (or unavailable): resources are read-only there,
            # so fall back to the frontend extra-module mechanism.
            add_extra_js_url(hass, url)
            return
        if hasattr(resources, "loaded") and not resources.loaded:
            await resources.async_load()
            resources.loaded = True
        base = url.split("?", 1)[0]
        for item in resources.async_items():
            if (item.get("url") or "").split("?", 1)[0] == base:
                if item.get("url") != url:
                    await resources.async_update_item(
                        item["id"], {"res_type": "module", "url": url}
                    )
                return
        await resources.async_create_item({"res_type": "module", "url": url})
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Lovelace resource registration failed; using extra_js_url")
        add_extra_js_url(hass, url)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hookii Neomow Map from a config entry."""
    topic_prefix = entry.data.get(CONF_TOPIC_PREFIX, DEFAULT_TOPIC_PREFIX)
    mowers = entry.data.get(CONF_MOWERS, [])
    if not mowers:
        _LOGGER.error("config entry has no mowers configured")
        return False

    coordinator = NeomowCoordinator(hass, entry.entry_id, topic_prefix, mowers)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    websocket.async_register(hass)
    await _async_register_card(hass)

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: NeomowCoordinator | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        await coordinator.async_stop()
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
