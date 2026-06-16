"""STATUS payload normalisation for the native Neomow integration.

Faithful port of the add-on bridge's ``normalise_status``: the Hookii cloud
alternates between a "full" (Shape A) and a "compact" (Shape B) STATUS layout,
and nests some telemetry under ``chassisData`` / ``taskInfo``. This flattens
both into one shape and derives HA-friendly fields (``ha_state``,
``ha_is_charging``, ``ha_upgrading``) so the lawn_mower + sensor entities read a
single stable schema. Mutates the STATUS dict in place (non-clobbering).
"""
from __future__ import annotations

from typing import Any


def normalise_status(status: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw ``data.STATUS`` dict in place and return it."""
    if not isinstance(status, dict):
        return status

    # chassisData / taskInfo fan-out -> top level (non-clobbering).
    for nested_key in ("chassisData", "taskInfo"):
        nested = status.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                status.setdefault(k, v)

    # Blade rpm: publish magnitude (sign encodes CW/CCW direction).
    rpm = status.get("knifeDiscMotorSpeed")
    if isinstance(rpm, (int, float)):
        status["knifeDiscMotorSpeed"] = abs(rpm)

    # Shape B -> Shape A aliases.
    if "battery" in status and "electricity" not in status:
        status["electricity"] = status["battery"]
    wtsi = status.get("workTimeStatusInfo")
    if isinstance(wtsi, dict) and "workStatus" not in status:
        ws = wtsi.get("workStatus")
        if ws is not None:
            status["workStatus"] = ws
    if "chargeDischargeCurrent" in status:
        cdc = status["chargeDischargeCurrent"]
        status.setdefault("chargeCurrent", cdc)
        status.setdefault("dischargeCurrent", cdc)
    if "fourGSignal" in status and "networkSignal" not in status:
        status["networkSignal"] = status["fourGSignal"]

    # Derived HA state from robotStatus (+ workingMode fallback).
    # Sign convention: chargeCurrent > 0 => current into battery => charging.
    rs = status.get("robotStatus")
    wm = status.get("workingMode")
    cc = status.get("chargeCurrent")
    ha_state: str | None = None
    ha_is_charging = False
    if rs == 5:
        ha_state, ha_is_charging = "docked", True
    elif rs in (0, 3, 4):
        ha_state = "docked"
    elif rs in (9, 10):
        ha_state = "returning"
    elif rs == 7:
        ha_state = "returning" if wm == 1 else "mowing"
    elif rs in (1, 2):
        ha_state = "mowing"
    elif rs is None:
        if wm == 0:
            ha_state = "docked"
            if isinstance(cc, (int, float)) and cc > 0:
                ha_is_charging = True
        elif wm == 1:
            ha_state = "returning"
        elif wm == 2:
            ha_state = "mowing"
    if ha_state is not None:
        status["ha_state"] = ha_state
        status["ha_is_charging"] = ha_is_charging
    # robotStatus 6 = firmware OTA in progress.
    status["ha_upgrading"] = (rs == 6)
    return status
