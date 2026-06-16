"""Sensor entities for Hookii Neomow (battery + telemetry).

Field set + entity-key suffixes mirror the add-on's MQTT-discovery sensors so a
dashboard built on the add-on remaps to the native entities by just swapping the
device prefix. All values come from the (normalised) STATUS payload.
"""
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
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    REVOLUTIONS_PER_MINUTE,
    UnitOfElectricCurrent,
    UnitOfLength,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NeomowCoordinator
from .entity import NeomowEntity

DIAG = EntityCategory.DIAGNOSTIC


@dataclass(frozen=True, kw_only=True)
class NeomowSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


def _v(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda s: s.get(key)


def _num(key: str) -> Callable[[dict[str, Any]], Any]:
    def fn(s: dict[str, Any]) -> Any:
        x = s.get(key)
        return x if isinstance(x, (int, float)) else None
    return fn


SENSORS: tuple[NeomowSensorDescription, ...] = (
    NeomowSensorDescription(
        key="battery", device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0, value_fn=_num("electricity")),
    NeomowSensorDescription(
        key="blade_rpm", translation_key="blade_rpm", icon="mdi:saw-blade",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
        value_fn=_num("knifeDiscMotorSpeed")),
    NeomowSensorDescription(
        key="charge_current", translation_key="charge_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT, entity_category=DIAG,
        suggested_display_precision=1, value_fn=_num("chargeCurrent")),
    NeomowSensorDescription(
        key="work_status", translation_key="work_status", icon="mdi:robot-mower",
        value_fn=_v("workStatus")),
    NeomowSensorDescription(
        key="wifi_signal", translation_key="wifi_signal", icon="mdi:wifi",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=DIAG, suggested_display_precision=0, value_fn=_num("wifiSignal")),
    NeomowSensorDescription(
        key="mowing_height", translation_key="mowing_height", icon="mdi:height",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
        value_fn=_num("mowingHeight")),
    # taskInfo-derived (fanned out by normalise_status)
    NeomowSensorDescription(
        key="current_region", translation_key="current_region", icon="mdi:map-marker",
        value_fn=_v("regionName")),
    NeomowSensorDescription(
        key="cut_area", translation_key="cut_area", icon="mdi:grass",
        native_unit_of_measurement="m²", state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, value_fn=lambda s: s.get("mowedArea", s.get("cutArea"))),
    NeomowSensorDescription(
        key="mowing_coverage", translation_key="mowing_coverage",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, value_fn=_num("mowingCoverage")),
    NeomowSensorDescription(
        key="efficiency", translation_key="efficiency", icon="mdi:speedometer",
        native_unit_of_measurement="m²/h", state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, value_fn=_num("mowingEfficiency")),
    NeomowSensorDescription(
        key="task_progress", translation_key="task_progress", icon="mdi:progress-check",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, value_fn=_num("taskProgress")),
    # Temperatures
    NeomowSensorDescription(
        key="temp_battery", translation_key="temp_battery",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT, entity_category=DIAG,
        suggested_display_precision=1, value_fn=_num("batteryTemp")),
    NeomowSensorDescription(
        key="temp_blade_motor", translation_key="temp_blade_motor",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT, entity_category=DIAG,
        suggested_display_precision=1, value_fn=_num("knifeDiscMotorTemp")),
    NeomowSensorDescription(
        key="temp_motor_left", translation_key="temp_motor_left",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT, entity_category=DIAG,
        suggested_display_precision=1, value_fn=_num("leftDriveMotorTemp")),
    NeomowSensorDescription(
        key="temp_motor_right", translation_key="temp_motor_right",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT, entity_category=DIAG,
        suggested_display_precision=1, value_fn=_num("rightDriveMotorTemp")),
    # Extras (full add-on parity)
    NeomowSensorDescription(
        key="voltage", translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V", state_class=SensorStateClass.MEASUREMENT,
        entity_category=DIAG, entity_registry_enabled_default=False,
        suggested_display_precision=1, value_fn=_num("voltage")),
    NeomowSensorDescription(
        key="satellites", translation_key="satellites", icon="mdi:satellite-variant",
        state_class=SensorStateClass.MEASUREMENT, entity_category=DIAG,
        entity_registry_enabled_default=False, suggested_display_precision=0,
        value_fn=_num("satellite")),
    NeomowSensorDescription(
        key="firmware", translation_key="firmware", icon="mdi:chip",
        entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=_v("version")),
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
