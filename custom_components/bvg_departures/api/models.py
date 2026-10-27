"""Backend-independent domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

PRODUCTS: tuple[str, ...] = (
    "suburban",
    "subway",
    "tram",
    "bus",
    "ferry",
    "express",
    "regional",
)

_BUS_PREFIX = re.compile(r"^BUS\s+")
_WHITESPACE = re.compile(r"\s+")
_CITY_SUFFIX = re.compile(r"\s*\(Berlin\)")


def normalize_line_name(name: str) -> str:
    """Normalize a line name: trimmed, uppercase, without a "Bus " prefix."""
    name = _WHITESPACE.sub(" ", name.strip().upper())
    return _BUS_PREFIX.sub("", name)


def clean_stop_name(name: str) -> str:
    """Drop the redundant " (Berlin)" VBB puts in every stop name."""
    return _CITY_SUFFIX.sub("", name.strip())


@dataclass(frozen=True)
class Stop:
    """A stop or station."""

    id: str
    name: str
    products: frozenset[str]
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Line:
    """A line, e.g. M76 (bus)."""

    name: str
    product: str


@dataclass(frozen=True)
class Departure:
    """A single departure from the monitored stop."""

    trip_id: str
    line: Line
    headsign: str
    when: datetime | None
    planned: datetime
    delay_s: int | None
    platform: str | None
    planned_platform: str | None
    cancelled: bool
    remarks: tuple[str, ...]
    # The (sub-)stop the departure actually leaves from. Bus trips list the
    # bay (e.g. 900070702) rather than the station (900070301).
    stop_id: str | None = None

    @property
    def effective(self) -> datetime:
        """Best known departure time."""
        return self.when or self.planned


@dataclass(frozen=True)
class DepartureBoard:
    """Departures fetched in one request."""

    departures: tuple[Departure, ...]
    realtime_updated: datetime | None
    fetched_at: datetime
    backend: str
