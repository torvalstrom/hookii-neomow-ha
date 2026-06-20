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
        """Start mowing only the named/identified zone(s).

        Mirrors the Hookii app's zone flow. A zone-start only takes when the mower
        is not already mowing (otherwise the cloud keeps the current job), so if it
        is mowing we stop first (keeping breakpoint progress), wait for it to leave
        the mowing state, then issue the same start sequence the whole-yard start
        uses (cmd 7 pre-check + cmd 6 execute) but with regionList set to the chosen
        regionIds instead of empty (= all). NB: a zone with no pending mowing task
        will be rejected by the cloud with "no tasks pending in the selected
        zones"."""
        await self.coordinator.async_refresh_regions(self._state.label)
        available = self._state.regions or geometry.extract_regions(
            self._state.region_task, self._state.device_map, self._state.status
        )
        if not available:
            raise HomeAssistantError(
                "No zone data for this mower (could not fetch the zone list). "
                "Try again in a moment."
            )
        by_name = {str(r.get("regionName", "")).strip().lower(): r for r in available}
        by_id = {str(r.get("regionId")): r for r in available}
        selected: list[dict] = []
        unknown: list = []
        for req in regions:
            r = by_name.get(str(req).strip().lower()) or by_id.get(str(req).strip())
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
        # DISABLED actuation: zone-start is a stateful multi-step handshake in the
        # app (wake if sleeping -> cancel current task -> save breakpoints ->
        # select areas -> start) that our isolated REST commands don't faithfully
        # replicate. Firing cmd 7+6 with a regionList of bare ids leaves the mower
        # in a "No tasks to be executed in mowing area" locked fault and/or sends
        # it home to charge. Until the exact app payloads are captured (decrypted
        # pcap), refuse to actuate - but the resolution above still validates the
        # zone name and surfaces available_regions.
        region_ids = [r["regionId"] for r in selected]  # noqa: F841
        raise ServiceValidationError(
            "Zone start is not enabled yet: the mower's zone-start is a multi-step "
            "app handshake we have not fully reverse-engineered, and firing it can "
            "wedge the mower. Zone names are valid (see available_regions); use "
            "lawn_mower.start_mowing for a whole-yard start in the meantime."
        )
