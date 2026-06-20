"""Lawn mower entity for Hookii Neomow (start / pause / dock / start-region)."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import geometry
from .const import DOMAIN
from .coordinator import NeomowCoordinator
from .entity import NeomowEntity

_LOGGER = logging.getLogger(__name__)

_STATE_TO_ACTIVITY = {
    "mowing": LawnMowerActivity.MOWING,
    "docked": LawnMowerActivity.DOCKED,
    "returning": LawnMowerActivity.RETURNING,
}

SERVICE_START_REGION = "start_region"
# regions: list of zone names (case-insensitive) or numeric regionIds.
START_REGION_SCHEMA = {
    vol.Required("regions"): vol.All(cv.ensure_list, [vol.Any(cv.string, int)]),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NeomowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NeomowLawnMower(coordinator, label) for label in coordinator.mowers
    )
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_START_REGION, START_REGION_SCHEMA, "async_start_region"
    )


class NeomowLawnMower(NeomowEntity, LawnMowerEntity):
    """A single Neomow as an HA lawn_mower entity."""

    _attr_name = None  # use the device name
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )

    def __init__(self, coordinator: NeomowCoordinator, label: str) -> None:
        super().__init__(coordinator, label)
        self._attr_unique_id = f"{self._state.serial}_mower"

    @property
    def activity(self) -> LawnMowerActivity | None:
        status = self._state.status
        if status.get("ha_upgrading"):
            return LawnMowerActivity.ERROR
        return _STATE_TO_ACTIVITY.get(status.get("ha_state"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Surface the selectable zone names so users (and the start_region
        # service) know what to pass. Populated once a REGION_TASK has arrived.
        regions = self._state.regions or geometry.extract_regions(
            self._state.region_task, self._state.device_map, self._state.status
        )
        return {
            "available_regions": [
                r["regionName"] for r in regions if r.get("regionName")
            ]
        }

    async def _send(self, action: str, region_list: list | None = None) -> None:
        client = self.coordinator.client
        if client is None:
            raise HomeAssistantError("Hookii cloud client not ready")
        await self.hass.async_add_executor_job(
            client.send_action, action, self._state.serial, region_list
        )

    async def async_start_mowing(self) -> None:
        await self._send("start")

    async def async_pause(self) -> None:
        await self._send("pause")

    async def async_dock(self) -> None:
        await self._send("dock")

    async def async_start_region(self, regions: list) -> None:
        """DISABLED pending a confirmed regionList format.

        The cloud's start/stop/job command accepts a regionList of bare regionIds
        syntactically (code 1) but it leaves the mower in a "no tasks pending in
        the selected zones" fault state - the actual zone-start the app uses is a
        3-step flow (start -> select zones -> resume) whose exact payload we have
        not captured. Sending the unconfirmed command can wedge the mower, so this
        service refuses to fire until the format is verified from a real capture.
        The available_regions attribute (zone enumeration) still works."""
        raise ServiceValidationError(
            "Zone start is not supported yet: the cloud regionList format is "
            "still being worked out and firing it can leave the mower in a "
            "no-task fault state. Zone names are available via the mower's "
            "available_regions attribute; start the whole yard with "
            "lawn_mower.start_mowing in the meantime."
        )
