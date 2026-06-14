"""Hookii Neomow Map integration.

Data plane for the native (no-iframe) Lovelace mower-map card: subscribes to
the Hookii Bridge's republished MQTT map payloads and serves per-mower geometry
to the card over Home Assistant's authenticated websocket. Entities themselves
come from the bridge's MQTT Discovery - this integration only adds the rich map
geometry the card needs (which is too large / too raw for entity attributes).
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_MOWERS, CONF_TOPIC_PREFIX, DEFAULT_TOPIC_PREFIX, DOMAIN
from .coordinator import NeomowCoordinator
from . import websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []


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
