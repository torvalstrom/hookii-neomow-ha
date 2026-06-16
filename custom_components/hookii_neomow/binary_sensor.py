"""Binary sensors for Hookii Neomow (firmware-upgrade + error)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


BINARY_SENSORS: tuple[NeomowBinaryDescription, ...] = (
    NeomowBinaryDescription(
        key="upgrading",
        translation_key="upgrading",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cog-sync",
        is_on_fn=lambda s: bool(s.get("ha_upgrading")),
    ),
    # Error/alarm — set by NOTICE_ALARM messages (coordinator._apply) and
    # self-cleared by STATUS when ha_is_charging or ha_state == "mowing".
    NeomowBinaryDescription(
        key="error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:alert-circle",
        is_on_fn=lambda s: bool(s.get("ha_alarm_active")),
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
