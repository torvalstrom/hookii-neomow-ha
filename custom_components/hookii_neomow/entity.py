"""Shared base entity for Hookii Neomow platforms."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_MOWER_UPDATED
from .coordinator import MowerState, NeomowCoordinator


class NeomowEntity(Entity):
    """Base: one device per mower, refreshed by the coordinator's dispatcher."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: NeomowCoordinator, label: str) -> None:
        self.coordinator = coordinator
        self.label = label
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._state.serial)},
            name=self._state.label,
            manufacturer="Hookii",
            model="Neomow",
        )

    @property
    def _state(self) -> MowerState:
        return self.coordinator.mowers[self.label]

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_MOWER_UPDATED}_{self.coordinator.entry_id}",
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self, label: str) -> None:
        if label == self.label:
            self.async_write_ha_state()
