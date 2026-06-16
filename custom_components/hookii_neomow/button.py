"""Command buttons for Hookii Neomow (start / pause / dock / stop)."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NeomowCoordinator
from .entity import NeomowEntity

_LOGGER = logging.getLogger(__name__)

# (entity key, action passed to cloud client, icon)
BUTTONS: tuple[tuple[str, str, str], ...] = (
    ("start", "start", "mdi:play"),
    ("pause", "pause", "mdi:pause"),
    ("dock", "dock", "mdi:home-import-outline"),
    ("stop_keep", "stop_keep", "mdi:stop"),
    ("stop_clear", "stop_clear", "mdi:stop-circle-outline"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NeomowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NeomowButton(coordinator, label, key, action, icon)
        for label in coordinator.mowers
        for key, action, icon in BUTTONS
    )


class NeomowButton(NeomowEntity, ButtonEntity):
    def __init__(
        self, coordinator: NeomowCoordinator, label: str, key: str, action: str, icon: str
    ) -> None:
        super().__init__(coordinator, label)
        self._action = action
        self.entity_description = ButtonEntityDescription(
            key=key, translation_key=key, icon=icon
        )
        self._attr_unique_id = f"{self._state.serial}_{key}"

    async def async_press(self) -> None:
        client = self.coordinator.client
        if client is None:
            _LOGGER.warning("cloud client not ready; dropping '%s'", self._action)
            return
        await self.hass.async_add_executor_job(
            client.send_action, self._action, self._state.serial
        )
