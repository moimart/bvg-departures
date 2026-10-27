"""Constants for BVG Departures."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "bvg_departures"
SUBENTRY_DEPARTURE: Final = "departure"

CONF_STOP_ID: Final = "stop_id"
CONF_STOP_NAME: Final = "stop_name"

CONF_BACKEND: Final = "backend"
CONF_UPDATE_INTERVAL: Final = "update_interval_s"
CONF_WINDOW: Final = "window_min"
DEFAULT_BACKEND: Final = "auto"
DEFAULT_UPDATE_INTERVAL: Final = 60
DEFAULT_WINDOW: Final = 90

CONF_LINE_NAME: Final = "line_name"
CONF_PRODUCT: Final = "product"
CONF_DIRECTION_STOP_ID: Final = "direction_stop_id"
CONF_DIRECTION_LABEL: Final = "direction_label"
CONF_NEXT_STOP_NAME: Final = "next_stop_name"
CONF_MATCH_MODE: Final = "match_mode"
CONF_HEADSIGNS: Final = "headsigns"
CONF_LEAD_MIN: Final = "lead_min"
CONF_INCLUDE_CANCELLED: Final = "include_cancelled"

MATCH_NEXT_STOP: Final = "next_stop"
MATCH_HEADSIGN: Final = "headsign"

# Setup-time lookahead used to discover lines and directions.
DISCOVERY_WINDOW_MIN: Final = 120

STALE_AFTER: Final = timedelta(minutes=15)
LOCAL_TICK: Final = timedelta(seconds=30)
BACKOFF_S: Final = (60, 120, 240, 600)
MAX_SLOW_INTERVAL_S: Final = 300

# S, U, Tram, Bus, other (spec 7.2)
PRODUCT_ORDER: Final = ("suburban", "subway", "tram", "bus", "ferry", "express", "regional")
PRODUCT_LABELS: Final = {
    "suburban": "S-Bahn",
    "subway": "U-Bahn",
    "tram": "Tram",
    "bus": "Bus",
    "ferry": "Fähre",
    "express": "IC/ICE",
    "regional": "RB/RE",
}
