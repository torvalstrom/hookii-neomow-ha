"""Lawn mower entity for Hookii Neomow (start / pause / dock / start-region)."""
from __future__ import annotations

import asyncio
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
        # service) know what to pass. Sourced from map/data areas (small areaId).
        areas = self._state.areas or geometry.extract_regions(
            self._state.region_task, self._state.device_map, self._state.status
        )
        return {
            "available_regions": [
                a.get("areaName") or a.get("regionName")
                for a in areas
                if a.get("areaName") or a.get("regionName")
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
        """Start mowing only the named/identified zone(s).

        The cloud's start/stop/job ``regionList`` uses the SMALL 0-based ``areaId``
        from calendar/param (the original captures show e.g. ``regionList:[0,1]``),
        NOT the large regionId from region/task/overview. Resolves zone names/ids to
        areaIds, then runs the whole-yard start sequence (cmd 7 pre-check + cmd 6
        execute) with that regionList. A sleeping mower wakes automatically as part
        of the start. To SWITCH zones the current job must be cancelled first (the
        cloud keeps the running job otherwise), so we stop+keep-progress and wait
        for the mower to leave its active/transit state before starting."""
        await self.coordinator.async_refresh_areas(self._state.label)
        available = self._state.areas
        if not available:
            raise HomeAssistantError(
                "No zone data for this mower (could not fetch the map). "
                "Try again in a moment."
            )
        by_name = {str(a.get("areaName", "")).strip().lower(): a for a in available}
        by_id = {str(a.get("areaId")): a for a in available}
        selected: list[dict] = []
        unknown: list = []
        for req in regions:
            a = by_name.get(str(req).strip().lower()) or by_id.get(str(req).strip())
            if a is None:
                unknown.append(req)
            elif a not in selected:
                selected.append(a)
        if unknown:
            names = ", ".join(
                sorted(n for n in (a.get("areaName") for a in available) if n)
            )
            raise ServiceValidationError(
                f"Unknown zone(s): {unknown}. Available zones: {names or '(none)'}"
            )
        region_ids = [a["areaId"] for a in selected]
        # If the mower is actively mowing or in transit, cancel that job first
        # (keeping breakpoint progress) so the new zone selection takes - the cloud
        # ignores a zone change while a job is running. robotStatus: 1/2 mowing,
        # 7 travelling, 9 docking, 10 returning are "active"; 0/3/4/5 are idle/dock.
        active = {1, 2, 7, 9, 10}
        if self._state.status.get("robotStatus") in active:
            _LOGGER.info(
                "[%s] start_region: cancelling current job before switching zones",
                self._state.label,
            )
            await self._send("stop_keep")
            for _ in range(30):  # up to ~60s for it to leave the active state
                await asyncio.sleep(2)
                if self._state.status.get("robotStatus") not in active:
                    break
            await asyncio.sleep(3)  # settle so the start isn't dropped
        _LOGGER.info(
            "[%s] start_region -> %s (regionList=%s, areaIds, rs=%s)",
            self._state.label,
            [a.get("areaName") for a in selected],
            region_ids,
            self._state.status.get("robotStatus"),
        )
        await self._send("start", region_list=region_ids)
