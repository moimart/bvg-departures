"""Diagnostics: configuration, last boards (truncated) and backend stats."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .api import FailoverBackend
from .coordinator import BvgConfigEntry

MAX_DEPARTURES = 10


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BvgConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a stop."""
    coordinator = entry.runtime_data
    backend = coordinator.backend
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "subentries": [
            {"title": s.title, "unique_id": s.unique_id, "data": dict(s.data)}
            for s in entry.subentries.values()
        ],
        "boards": {
            str(key): {
                "backend": board.backend,
                "fetched_at": board.fetched_at.isoformat(),
                "realtime_updated": board.realtime_updated.isoformat()
                if board.realtime_updated
                else None,
                "count": len(board.departures),
                "departures": [
                    {
                        "line": d.line.name,
                        "product": d.line.product,
                        "headsign": d.headsign,
                        "when": d.when.isoformat() if d.when else None,
                        "planned": d.planned.isoformat(),
                        "cancelled": d.cancelled,
                    }
                    for d in board.departures[:MAX_DEPARTURES]
                ],
            }
            for key, board in coordinator.boards.items()
        },
        "backend": {
            "mode": backend.name,
            "failovers": backend.failovers if isinstance(backend, FailoverBackend) else None,
            **asdict(coordinator.stats),
            "last_success": coordinator.last_success.isoformat()
            if coordinator.last_success
            else None,
            "failing_since": coordinator.failing_since.isoformat()
            if coordinator.failing_since
            else None,
            "update_interval_s": coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None,
        },
    }
