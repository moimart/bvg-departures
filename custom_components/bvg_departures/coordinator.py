"""One coordinator per stop: fetches each distinct direction once per cycle."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.base import BackendError, DepartureBackend, RateLimited
from .api.models import Departure, DepartureBoard, normalize_line_name
from .const import (
    BACKOFF_S,
    CONF_DIRECTION_STOP_ID,
    CONF_HEADSIGNS,
    CONF_INCLUDE_CANCELLED,
    CONF_LEAD_MIN,
    CONF_LINE_NAME,
    CONF_MATCH_MODE,
    CONF_PRODUCT,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_WINDOW,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_WINDOW,
    DOMAIN,
    MATCH_HEADSIGN,
    MATCH_NEXT_STOP,
    MAX_SLOW_INTERVAL_S,
    STALE_AFTER,
    SUBENTRY_DEPARTURE,
)

_LOGGER = logging.getLogger(__name__)

type BoardKey = str | None
type BvgConfigEntry = ConfigEntry[BvgCoordinator]

FILTERED_RESULTS = 50
UNFILTERED_RESULTS = 200


@dataclass(frozen=True)
class DepartureConfig:
    """What one subentry is watching."""

    line_name: str
    product: str
    direction_stop_id: str | None
    match_mode: str = MATCH_NEXT_STOP
    headsigns: tuple[str, ...] = ()
    lead_min: int = 0
    include_cancelled: bool = False

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> DepartureConfig:
        """Build from subentry data."""
        return cls(
            line_name=normalize_line_name(data[CONF_LINE_NAME]),
            product=data[CONF_PRODUCT],
            direction_stop_id=data.get(CONF_DIRECTION_STOP_ID),
            match_mode=data.get(CONF_MATCH_MODE, MATCH_NEXT_STOP),
            headsigns=tuple(data.get(CONF_HEADSIGNS) or ()),
            lead_min=int(data.get(CONF_LEAD_MIN, 0)),
            include_cancelled=bool(data.get(CONF_INCLUDE_CANCELLED, False)),
        )

    @property
    def board_key(self) -> BoardKey:
        """Which board this subentry reads: its direction, or the unfiltered one."""
        if self.match_mode == MATCH_HEADSIGN or not self.direction_stop_id:
            return None
        return self.direction_stop_id


def match_departures(
    board: DepartureBoard | None, cfg: DepartureConfig, now: datetime
) -> list[Departure]:
    """Departures of `board` relevant to `cfg`, soonest first (spec 8.4)."""
    if board is None:
        return []
    earliest = now + timedelta(minutes=cfg.lead_min)
    headsigns = set(cfg.headsigns)
    return sorted(
        (
            d
            for d in board.departures
            if d.line.name == cfg.line_name
            and d.line.product == cfg.product
            and (cfg.include_cancelled or not d.cancelled)
            and d.effective >= earliest
            and (cfg.match_mode != MATCH_HEADSIGN or d.headsign in headsigns)
        ),
        key=lambda d: d.effective,
    )


@dataclass
class Stats:
    """Counters for diagnostics."""

    requests: int = 0
    failures: int = 0
    backend_used: dict[str, str] = field(default_factory=dict)


class BvgCoordinator(DataUpdateCoordinator[dict[BoardKey, DepartureBoard]]):
    """Polls the departures of one stop."""

    config_entry: BvgConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: BvgConfigEntry, backend: DepartureBackend
    ) -> None:
        self.backend = backend
        self.stop_id: str = entry.data[CONF_STOP_ID]
        self.stop_name: str = entry.data[CONF_STOP_NAME]
        self.base_interval = timedelta(
            seconds=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        )
        self.window_min: int = entry.options.get(CONF_WINDOW, DEFAULT_WINDOW)
        self.departure_configs: dict[str, DepartureConfig] = {
            sub.subentry_id: DepartureConfig.from_data(sub.data)
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_DEPARTURE
        }
        self.stop_device_id = ""  # set in async_setup_entry
        self.boards: dict[BoardKey, DepartureBoard] = {}
        self.last_success: datetime | None = None
        self.failing_since: datetime | None = None
        self.consecutive_failures = 0
        self.stats = Stats()
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.stop_name}",
            # No subentries -> nothing to show -> don't poll.
            update_interval=self.base_interval if self.departure_configs else None,
        )

    @property
    def issue_id(self) -> str:
        """Repairs issue id for this stop."""
        return f"api_unreachable_{self.config_entry.entry_id}"

    def config_for(self, subentry: ConfigSubentry) -> DepartureConfig:
        """Config of a subentry, as loaded at setup."""
        return self.departure_configs[subentry.subentry_id]

    def is_stale(self, now: datetime) -> bool:
        """Whether the last good fetch is older than STALE_AFTER."""
        return self.last_success is None or now - self.last_success > STALE_AFTER

    async def _fetch(self, key: BoardKey) -> DepartureBoard:
        self.stats.requests += 1
        board = await self.backend.departures(
            self.stop_id,
            direction_stop_id=key,
            duration_min=self.window_min,
            max_results=FILTERED_RESULTS if key else UNFILTERED_RESULTS,
        )
        self.stats.backend_used[str(key)] = board.backend
        return board

    async def _async_update_data(self) -> dict[BoardKey, DepartureBoard]:
        keys = list(dict.fromkeys(c.board_key for c in self.departure_configs.values()))
        if not keys:
            return {}
        results = await asyncio.gather(*(self._fetch(k) for k in keys), return_exceptions=True)
        errors: list[BackendError] = []
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, BackendError):
                errors.append(result)
            elif isinstance(result, BaseException):
                raise result
            else:
                self.boards[key] = result

        now = dt_util.utcnow()
        if errors:
            self._on_failure(errors, now)
        else:
            self._on_success(now)

        if not self.boards:
            raise UpdateFailed(f"Could not fetch departures: {errors[0]}")
        # On failure we keep serving the last good boards; entities prune
        # departures that have left and decide availability themselves.
        return dict(self.boards)

    def _on_success(self, now: datetime) -> None:
        if self.failing_since is not None:
            _LOGGER.info("Departures for %s are back", self.stop_name)
        self.last_success = now
        self.failing_since = None
        self.consecutive_failures = 0
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        self.update_interval = self._interval_after_success(now)

    def _on_failure(self, errors: list[BackendError], now: datetime) -> None:
        self.stats.failures += 1
        self.consecutive_failures += 1
        if self.failing_since is None:
            self.failing_since = now
        retry_after = max(
            (e.retry_after for e in errors if isinstance(e, RateLimited) and e.retry_after),
            default=None,
        )
        delay = (
            retry_after
            if retry_after is not None
            else BACKOFF_S[min(self.consecutive_failures, len(BACKOFF_S)) - 1]
        )
        self.update_interval = timedelta(seconds=delay)
        _LOGGER.warning(
            "Fetching departures for %s failed (%s); retrying in %ss",
            self.stop_name,
            errors[0],
            int(delay),
        )
        if now - self.failing_since > STALE_AFTER:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="api_unreachable",
                translation_placeholders={"stop": self.stop_name},
            )

    def _interval_after_success(self, now: datetime) -> timedelta:
        """Poll slowly while nothing relevant leaves within 20 minutes (spec 8.2)."""
        soonest: datetime | None = None
        for cfg in self.departure_configs.values():
            candidates = match_departures(self.boards.get(cfg.board_key), cfg, now)
            if candidates and (soonest is None or candidates[0].effective < soonest):
                soonest = candidates[0].effective
        if soonest is None or soonest - now <= timedelta(minutes=20):
            return self.base_interval
        slow = min(timedelta(seconds=MAX_SLOW_INTERVAL_S), soonest - now - timedelta(minutes=10))
        return max(self.base_interval, slow)
