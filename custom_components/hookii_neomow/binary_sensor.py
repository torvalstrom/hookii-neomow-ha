"""Binary sensors for Hookii Neomow (firmware-upgrade + error)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# How long after a snapshot arrives the "snapshot_fresh" sensor stays on, so the
# dashboard camera card shows the photo when it comes and auto-hides after.
_SNAPSHOT_FRESH_SECONDS = 30


def _snapshot_fresh(s: dict[str, Any]) -> bool:
    at = s.get("ha_snapshot_at")
    if not at:
        return False
    try:
        t = datetime.fromisoformat(at)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() < _SNAPSHOT_FRESH_SECONDS
    except (ValueError, TypeError):
        return False

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NeomowCoordinator
from .entity import NeomowEntity


@dataclass(frozen=True, kw_only=True)
class NeomowBinaryDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[dict[str, Any]], bool]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


BINARY_SENSORS: tuple[NeomowBinaryDescription, ...] = (
    NeomowBinaryDescription(
        key="upgrading",
        translation_key="upgrading",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cog-sync",
        is_on_fn=lambda s: bool(s.get("ha_upgrading")),
    ),
    # On for ~30s after a fresh snapshot arrives (set in coordinator.set_snapshot).
    # The dashboard camera card is conditional on this so the photo shows when it
    # comes and disappears after; re-evaluated by the ~1.5s STATUS stream.
    NeomowBinaryDescription(
        key="snapshot_fresh",
        translation_key="snapshot_fresh",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:camera-iris",
        is_on_fn=_snapshot_fresh,
    ),
    # Error/alarm — raised from live STATUS halt detection (robotStatus==4 /
    # "1" in runStatusList) and enriched with the precise errCode from
    # NOTICE_ALARM. Motion-halt faults clear when the halt clears; docking
    # faults (514/515/516) persist until the mower charges/mows again.
    # `alarm_code` is the numeric Hookii errCode when known (e.g. 801 stop,
    # 823 tilt, 516 not-charging-at-dock) so automations can branch on it.
    NeomowBinaryDescription(
        key="error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:alert-circle",
        is_on_fn=lambda s: bool(s.get("ha_alarm_active")),
        attrs_fn=lambda s: {
            "alarm_code": s.get("ha_alarm_code"),
            "alarm_label": s.get("ha_alarm_label"),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NeomowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NeomowBinarySensor(coordinator, label, desc)
        for label in coordinator.mowers
        for desc in BINARY_SENSORS
    )


class NeomowBinarySensor(NeomowEntity, BinarySensorEntity):
    entity_description: NeomowBinaryDescription

    def __init__(
        self, coordinator: NeomowCoordinator, label: str, desc: NeomowBinaryDescription
    ) -> None:
        super().__init__(coordinator, label)
        self.entity_description = desc
        self._attr_unique_id = f"{self._state.serial}_{desc.key}"

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self._state.status)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._state.status)
