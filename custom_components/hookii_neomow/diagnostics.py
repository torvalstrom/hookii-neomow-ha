"""Diagnostics for Hookii Neomow.

Dumps each mower's full last-known STATUS dict so issues like "the Problem
sensor says OK but the mower is stuck" can be debugged against the raw cloud
fields without enabling MQTT-level logging.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import NeomowCoordinator

# Coordinates + serials are the only privacy-relevant fields; keep everything
# else (work/error/alarm/charge fields) so support reports are useful.
TO_REDACT = {
    "serialNumber", "sn", "deviceSn",
    "latitude", "longitude", "lat", "lng", "lon",
    "x", "y", "robotX", "robotY", "coordinate", "coordinates",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: NeomowCoordinator = hass.data[DOMAIN][entry.entry_id]
    mowers: dict[str, Any] = {}
    for label, state in coordinator.mowers.items():
        mowers[label] = {
            "serial_tail": (state.serial or "")[-4:],
            "work_status": state.work_status,
            "online_status": state.online_status,
            "last_update": state.last_update,
            "ha_alarm_active": state.status.get("ha_alarm_active"),
            "ha_alarm_code": state.status.get("ha_alarm_code"),
            # The field names are the key thing for debugging coverage gaps.
            "status_keys": sorted(state.status.keys()),
            "status": async_redact_data(state.status, TO_REDACT),
        }
    return {"mowers": mowers}
