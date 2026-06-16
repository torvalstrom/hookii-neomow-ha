"""Constants for the Hookii Neomow map integration."""
from __future__ import annotations

DOMAIN = "hookii_neomow"

# Config-entry keys
CONF_EMAIL = "email"            # Hookii account login
CONF_PASSWORD = "password"      # Hookii account password (MD5'd before send)
CONF_ENV = "env"                # "beta" | "prod" cloud environment
CONF_SERIALS = "serials"        # manual serial fallback (login omits device list)
CONF_MOWERS = "mowers"          # list of {serial, label, color}
CONF_SERIAL = "serial"
CONF_LABEL = "label"
CONF_COLOR = "color"

# Legacy (pre-2026-06-16, MQTT-bridge era) - kept so old entries still load.
CONF_TOPIC_PREFIX = "topic_prefix"

DEFAULT_ENV = "beta"

# The bridge republishes per-serial under this prefix by default:
#   <prefix>/<serial>  ->  {msgType: STATUS|DEVICE_MAP_V2|ALL_PATH_LIST_V2|...}
DEFAULT_TOPIC_PREFIX = "hookii/details/device"

# Per-mower default palette, matched to map_server.py so colours are stable
# across the standalone visualizer and the card.
PALETTE = ["#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#ec4899", "#06b6d4"]

# Trail ring-buffer length (recent live positions), matches map_server TRAIL_MAX.
TRAIL_MAX = 2000

# Only append a trail point when the mower moved more than this many cm since
# the last sample (de-noises a parked mower's GPS jitter).
TRAIL_MIN_MOVE_CM = 5

# Websocket command/event types used between the integration and the card.
WS_TYPE_SUBSCRIBE = f"{DOMAIN}/subscribe"
WS_EVENT = f"{DOMAIN}_update"

# dispatcher signal fired whenever a mower's geometry changes.
SIGNAL_MOWER_UPDATED = f"{DOMAIN}_mower_updated"
