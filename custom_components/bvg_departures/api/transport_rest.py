"""Backend for the community-run v6.bvg.transport.rest (hafas-rest-api)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from .base import ParseError, fetch_json
from .models import (
    Departure,
    DepartureBoard,
    Line,
    Stop,
    clean_stop_name,
    normalize_line_name,
)
from .ratelimit import TokenBucket

BASE_URL = "https://v6.bvg.transport.rest"
NAME = "transport_rest"


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _products(raw: dict[str, Any] | None) -> frozenset[str]:
    return frozenset(k for k, v in (raw or {}).items() if v)


def _line(raw: dict[str, Any]) -> Line:
    return Line(normalize_line_name(raw.get("name") or ""), raw.get("product") or "")


def _unique_lines(raw: list[dict[str, Any]] | None) -> list[Line]:
    seen: dict[tuple[str, str], Line] = {}
    for item in raw or []:
        line = _line(item)
        if line.name:
            seen.setdefault((line.product, line.name), line)
    return list(seen.values())


def _stop(raw: dict[str, Any]) -> Stop:
    return Stop(
        id=str(raw["id"]),
        name=clean_stop_name(raw.get("name") or str(raw["id"])),
        products=_products(raw.get("products")),
        lines=tuple(dict.fromkeys(line.name for line in _unique_lines(raw.get("lines")))),
    )


def parse_departure(raw: dict[str, Any]) -> Departure:
    """Parse one hafas-rest-api departure object."""
    planned = _dt(raw.get("plannedWhen"))
    when = _dt(raw.get("when"))
    if planned is None:
        # Cancelled departures carry only plannedWhen; everything else has one.
        if when is None:
            raise ParseError(f"departure {raw.get('tripId')} has no time")
        planned = when
    cancelled = bool(raw.get("cancelled"))
    if cancelled:
        when = None
    return Departure(
        trip_id=raw["tripId"],
        line=_line(raw.get("line") or {}),
        headsign=(raw.get("direction") or "").strip(),
        when=when,
        planned=planned,
        delay_s=raw.get("delay"),
        platform=raw.get("platform"),
        planned_platform=raw.get("plannedPlatform"),
        cancelled=cancelled,
        remarks=tuple(
            r.get("summary") or r.get("text") or ""
            for r in raw.get("remarks") or []
            if r.get("type") == "warning"
        ),
        stop_id=(raw.get("stop") or {}).get("id"),
    )


class TransportRestBackend:
    """hafas-rest-api v6 client."""

    name = NAME

    def __init__(
        self,
        session: aiohttp.ClientSession,
        limiter: TokenBucket | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._session = session
        self._limiter = limiter
        self._base = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        return await fetch_json(
            self._session, "GET", self._base + path, params=params, limiter=self._limiter
        )

    async def search_stops(self, query: str, limit: int = 8) -> list[Stop]:
        data = await self._get(
            "/locations",
            {
                "query": query,
                "results": str(limit),
                "stops": "true",
                "addresses": "false",
                "poi": "false",
                "linesOfStops": "true",
            },
        )
        try:
            return [
                _stop(item)
                for item in data
                if item.get("type") in ("stop", "station") and item.get("id")
            ]
        except (KeyError, TypeError, AttributeError) as err:
            raise ParseError("unexpected /locations response") from err

    async def lines_at_stop(self, stop_id: str) -> list[Line]:
        data = await self._get(f"/stops/{quote(stop_id)}", {"linesOfStops": "true"})
        try:
            return _unique_lines(data.get("lines"))
        except (KeyError, TypeError, AttributeError) as err:
            raise ParseError("unexpected /stops response") from err

    async def departures(
        self,
        stop_id: str,
        *,
        direction_stop_id: str | None,
        duration_min: int,
        max_results: int,
    ) -> DepartureBoard:
        params = {
            "duration": str(duration_min),
            "results": str(max_results),
            "remarks": "true",
            "linesOfStops": "false",
        }
        if direction_stop_id:
            params["direction"] = direction_stop_id
        data = await self._get(f"/stops/{quote(stop_id)}/departures", params)
        try:
            raw_deps = data["departures"]
            updated = data.get("realtimeDataUpdatedAt")
            deps = tuple(parse_departure(d) for d in raw_deps)
        except (KeyError, TypeError, AttributeError, ValueError) as err:
            raise ParseError("unexpected /departures response") from err
        return DepartureBoard(
            departures=deps,
            realtime_updated=datetime.fromtimestamp(updated, UTC) if updated else None,
            fetched_at=datetime.now(UTC),
            backend=NAME,
        )

    async def trip_next_stop(self, trip_id: str, after_stop_id: str) -> Stop | None:
        data = await self._get(
            f"/trips/{quote(trip_id, safe='')}",
            {"stopovers": "true", "remarks": "false", "polyline": "false"},
        )
        try:
            stopovers = data["trip"]["stopovers"]
            ids = [{s["stop"]["id"], (s["stop"].get("station") or {}).get("id")} for s in stopovers]
        except (KeyError, TypeError, AttributeError) as err:
            raise ParseError("unexpected /trips response") from err
        for i, stop_ids in enumerate(ids[:-1]):
            if after_stop_id in stop_ids:
                return _stop(stopovers[i + 1]["stop"])
        return None
