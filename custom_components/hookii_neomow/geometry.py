"""Hookii Neomow geometry parsing.

This is a faithful port of the parsing logic in the bridge's
``hookii_bridge/map_server.py`` (the standalone FastAPI visualizer), with the
FastAPI/SVG rendering stripped out. The integration owns the *data plane*: it
subscribes to the same local-broker MQTT payloads the bridge republishes
(STATUS / DEVICE_MAP_V2 / ALL_PATH_LIST_V2 / ALL_PATH_INDEX_V2 / REGION_TASK)
and turns them into raw-coordinate geometry that the Lovelace card renders
client-side.

Coordinates stay in the mower's own local frame (integers, cm-ish). The card
does the bounding box, the Y-flip projection and the optional ROTATE_DEG
display rotation - so this module never touches screen space.

Kept deliberately close to map_server.py so the two cannot drift in how they
read the (occasionally quirky) Hookii cloud schema. Notable schema facts that
are easy to get wrong and are encoded here:

- The cut/transit classification (May 2026 firmware) lives on the SEGMENT in
  ``ALL_PATH_INDEX_V2.indexInfoList`` as ``{startIndex, endIndex, info}``, NOT
  on the per-point dict (whose ``info`` is now always 0). We project the
  segment ``info`` down to per-point info before decimating.
- The boundary polygons are nested under
  ``DEVICE_MAP_V2.mapDataList[0].{mowingAreaElementList, exclusionAreaElementList}``
  with ``elementPointList`` of ``{x, y}`` dicts - not any top-level key.
"""
from __future__ import annotations

from typing import Any

# Decimate very dense path-point lists to keep the websocket payload + the
# client-side SVG manageable. Matches map_server.py's 4000-point cap.
MAX_PATH_POINTS = 4000

# Neomow X Pro default cutting width (cm) used for the cut-swath stroke when
# the cloud has not supplied a REGION_TASK mowingWidth yet.
MOWING_WIDTH_DEFAULT_CM = 25.0


def parse_status(status: dict[str, Any]) -> dict[str, Any] | None:
    """Extract position/heading/battery from a STATUS payload's data.STATUS.

    Returns None when the payload carries no usable position (the caller keeps
    the previous fix), mirroring map_server.handle_status's early-return.
    """
    x = status.get("robotX")
    y = status.get("robotY")
    heading = status.get("robotNavigation")
    if heading is None:
        heading = status.get("robotNav")  # older field name
    if x is None or y is None:
        return None
    try:
        x = int(x)
        y = int(y)
    except (TypeError, ValueError):
        return None
    return {
        "x": x,
        "y": y,
        "heading": heading,
        "battery": status.get("electricity"),
        "work_status": status.get("workStatus"),
        "online_status": status.get("onlineStatus"),
        "last_update": status.get("updateTime"),
    }


def extract_boundary(device_map: dict[str, Any] | None) -> dict[str, list]:
    """Pull mowing + exclusion polygons from a DEVICE_MAP_V2 payload.

    Returns ``{"mowing": [[[x, y], ...], ...], "exclusion": [...]}`` in raw
    coordinates. Empty lists when nothing is captured yet.
    """
    out: dict[str, list] = {"mowing": [], "exclusion": []}
    if not device_map:
        return out
    try:
        d = device_map.get("data", {}).get("DEVICE_MAP_V2", {})
        if not isinstance(d, dict):
            return out

        # Modern shape (May 2026 cloud): nested under mapDataList[0]
        for map_entry in d.get("mapDataList", []) or []:
            if not isinstance(map_entry, dict):
                continue
            for src_key, dst_key in (
                ("mowingAreaElementList", "mowing"),
                ("exclusionAreaElementList", "exclusion"),
            ):
                for area in map_entry.get(src_key, []) or []:
                    pts = area.get("elementPointList") if isinstance(area, dict) else None
                    if not isinstance(pts, list) or not pts:
                        continue
                    poly = [
                        [p.get("x", p.get("posX", 0)), p.get("y", p.get("posY", 0))]
                        for p in pts
                        if isinstance(p, dict)
                    ]
                    if len(poly) >= 3:
                        out[dst_key].append(poly)

        # Legacy flat-key fallback (pre-May-2026 firmware).
        if not out["mowing"]:
            for key in ("boundary", "boundaryPoints", "regionPoints",
                        "borderPoints", "points"):
                pts = d.get(key)
                if isinstance(pts, list) and pts:
                    poly = [
                        [p.get("x", p.get("posX", 0)), p.get("y", p.get("posY", 0))]
                        for p in pts
                        if isinstance(p, dict)
                    ]
                    if len(poly) >= 3:
                        out["mowing"].append(poly)
                        break
    except (AttributeError, TypeError, KeyError):
        pass
    return out


def extract_path_points(
    path_list: dict[str, Any] | None,
    path_index: dict[str, Any] | None,
) -> list[list]:
    """Return ``[[x, y, info], ...]`` where info=1 is a cut swath, 0 is transit.

    The cut/transit classification is projected from the segment-level
    ``ALL_PATH_INDEX_V2.indexInfoList`` down to per-point info before decimation
    (see module docstring for the schema rationale). Decimated to
    ``MAX_PATH_POINTS`` keeping path + info in lockstep so cut/transit
    boundaries stay aligned.
    """
    if not path_list:
        return []
    try:
        pl = (
            path_list.get("data", {}).get("ALL_PATH_LIST_V2", {})
            .get("pathList", [])
        )
        if not pl:
            return []
        points = pl[0].get("pathPointList", [])
        if not points:
            return []

        point_info: list[int | None] = [None] * len(points)
        idx_segs: list = []
        if path_index:
            try:
                idx_list = (
                    path_index.get("data", {}).get("ALL_PATH_INDEX_V2", {})
                    .get("pathIndexList", [])
                )
                if idx_list:
                    idx_segs = idx_list[0].get("indexInfoList", []) or []
            except (AttributeError, TypeError, KeyError, IndexError):
                idx_segs = []

        for seg in idx_segs:
            try:
                start = int(seg.get("startIndex", 0))
                end = int(seg.get("endIndex", 0))
                info = int(seg.get("info", 0))
            except (TypeError, ValueError):
                continue
            for i in range(max(0, start), min(end, len(point_info))):
                point_info[i] = info

        # Fall back to per-point info (legacy), then 0.
        for i, p in enumerate(points):
            if point_info[i] is None:
                point_info[i] = p.get("info", 0)

        if len(points) > MAX_PATH_POINTS:
            step = len(points) // MAX_PATH_POINTS
            points = points[::step]
            point_info = point_info[::step]

        return [
            [p.get("x", 0), p.get("y", 0), info]
            for p, info in zip(points, point_info)
        ]
    except (AttributeError, TypeError, KeyError, IndexError):
        return []


def path_point_count(path_list: dict[str, Any] | None) -> int:
    """Number of raw path points in an ALL_PATH_LIST_V2 payload (0 on miss).

    Used by the staleness guard so a transient empty/blank republish does not
    overwrite a good capture (mirrors map_server's lenient 10% threshold).
    """
    if not path_list:
        return 0
    try:
        return len(
            path_list["data"]["ALL_PATH_LIST_V2"]["pathList"][0]
            .get("pathPointList", [])
        )
    except (AttributeError, TypeError, KeyError, IndexError):
        return 0


def extract_mowing_width_cm(region_task: dict[str, Any] | None) -> float:
    """Cutting width (cm) from a REGION_TASK payload, default when absent."""
    try:
        rt = (region_task or {}).get("data", {}).get("REGION_TASK", {})
        mw = rt.get("mowingWidth") if isinstance(rt, dict) else None
        if isinstance(mw, (int, float)) and mw > 0:
            return float(mw)
    except (AttributeError, TypeError, KeyError):
        pass
    return MOWING_WIDTH_DEFAULT_CM
