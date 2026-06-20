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
# mowing_height: optional per-call cutting-height override (mm) applied to every
# selected zone.
START_REGION_SCHEMA = {
    vol.Required("regions"): vol.All(cv.ensure_list, [vol.Any(cv.string, int)]),
    vol.Optional("mowing_height"): vol.All(vol.Coerce(int), vol.Range(min=20, max=100)),
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
        regions = geometry.extract_regions(self._state.region_task)
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

    async def async_start_region(
        self, regions: list, mowing_height: int | None = None
    ) -> None:
        """Start mowing only the named/identified zone(s), optionally overriding
        cutting height. Builds the cloud regionList from the live REGION_TASK zone
        list so each entry carries the regionId/regionIndex the cloud expects."""
        available = geometry.extract_regions(self._state.region_task)
        if not available:
            raise HomeAssistantError(
                "No zone data yet for this mower (waiting for REGION_TASK). "
                "Open the mower in the Hookii app once, then retry."
            )
        by_name = {
            str(r.get("regionName", "")).strip().lower(): r for r in available
        }
        by_id = {str(r.get("regionId")): r for r in available}
        selected: list[dict] = []
        unknown: list = []
        for req in regions:
            key = str(req).strip()
            r = by_name.get(key.lower()) or by_id.get(key)
            if r is None:
                unknown.append(req)
            elif r not in selected:
                selected.append(r)
        if unknown:
            names = ", ".join(
                sorted(n for n in (r.get("regionName") for r in available) if n)
            )
            raise ServiceValidationError(
                f"Unknown zone(s): {unknown}. Available zones: {names or '(none)'}"
            )
        region_list = []
        for r in selected:
            entry = {k: v for k, v in r.items() if k != "regionName" and v is not None}
            if mowing_height is not None:
                entry["mowingHeight"] = mowing_height
            region_list.append(entry)
        _LOGGER.info(
            "[%s] start_region -> %s (regionList=%s)",
            self._state.label,
            [r.get("regionName") for r in selected],
            region_list,
        )
        await self._send("start", region_list=region_list)
