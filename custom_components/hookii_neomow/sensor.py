"""Sensor entities for Hookii Neomow (battery + telemetry)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, REVOLUTIONS_PER_MINUTE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NeomowCoordinator
from .entity import NeomowEntity


@dataclass(frozen=True, kw_only=True)
class NeomowSensorDescription(SensorEntityDescription):
    """A sensor described by how to pull its value out of the STATUS dict."""

    value_fn: Callable[[dict[str, Any]], Any]


def _num(status: dict[str, Any], key: str) -> Any:
    v = status.get(key)
    return v if isinstance(v, (int, float)) else None


SENSORS: tuple[NeomowSensorDescription, ...] = (
    NeomowSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: _num(s, "electricity"),
    ),
    NeomowSensorDescription(
        key="blade_rpm",
        translation_key="blade_rpm",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: _num(s, "knifeDiscMotorSpeed"),
    ),
    NeomowSensorDescription(
        key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: _num(s, "wifiSignal"),
    ),
    NeomowSensorDescription(
        key="mowing_coverage",
        translation_key="mowing_coverage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: _num(s, "mowingCoverage"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NeomowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NeomowSensor(coordinator, label, desc)
        for label in coordinator.mowers
        for desc in SENSORS
    )


class NeomowSensor(NeomowEntity, SensorEntity):
    """A single telemetry value off the mower's STATUS payload."""

    entity_description: NeomowSensorDescription

    def __init__(
        self, coordinator: NeomowCoordinator, label: str, desc: NeomowSensorDescription
    ) -> None:
        super().__init__(coordinator, label)
        self.entity_description = desc
        self._attr_unique_id = f"{self._state.serial}_{desc.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._state.status)
