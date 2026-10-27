"""Backend factory and failover wrapper."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import TypeVar

import aiohttp

from .base import BackendError, DepartureBackend, RateLimited
from .hafas_mgate import MgateBackend
from .models import DepartureBoard, Line, Stop
from .ratelimit import TokenBucket
from .transport_rest import TransportRestBackend

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

BACKEND_AUTO = "auto"
BACKEND_TRANSPORT_REST = TransportRestBackend.name
BACKEND_MGATE = MgateBackend.name
BACKENDS = (BACKEND_AUTO, BACKEND_TRANSPORT_REST, BACKEND_MGATE)


class FailoverBackend:
    """Try the primary; on any BackendError retry the same call on the fallback."""

    name = BACKEND_AUTO

    def __init__(self, primary: DepartureBackend, fallback: DepartureBackend) -> None:
        self.primary = primary
        self.fallback = fallback
        self.failovers = 0

    async def _run(self, call: Callable[[DepartureBackend], Awaitable[_T]]) -> _T:
        try:
            result = await call(self.primary)
        except BackendError as primary_err:
            _LOGGER.debug(
                "%s failed (%s), trying %s", self.primary.name, primary_err, self.fallback.name
            )
            self.failovers += 1
            try:
                return await call(self.fallback)
            except BackendError as fallback_err:
                # Report the primary's error: a 429 Retry-After governs when the
                # next attempt makes sense.
                if isinstance(primary_err, RateLimited):
                    raise primary_err from fallback_err
                raise
        return result

    async def search_stops(self, query: str, limit: int = 8) -> list[Stop]:
        return await self._run(lambda b: b.search_stops(query, limit))

    async def lines_at_stop(self, stop_id: str) -> list[Line]:
        return await self._run(lambda b: b.lines_at_stop(stop_id))

    async def departures(
        self,
        stop_id: str,
        *,
        direction_stop_id: str | None,
        duration_min: int,
        max_results: int,
    ) -> DepartureBoard:
        return await self._run(
            lambda b: b.departures(
                stop_id,
                direction_stop_id=direction_stop_id,
                duration_min=duration_min,
                max_results=max_results,
            )
        )

    async def trip_next_stop(self, trip_id: str, after_stop_id: str) -> Stop | None:
        return await self._run(lambda b: b.trip_next_stop(trip_id, after_stop_id))


def create_backend(
    session: aiohttp.ClientSession, mode: str, limiter: TokenBucket
) -> DepartureBackend:
    """Build the backend for a config entry. The limiter guards transport.rest."""
    rest = TransportRestBackend(session, limiter)
    if mode == BACKEND_TRANSPORT_REST:
        return rest
    mgate = MgateBackend(session)
    if mode == BACKEND_MGATE:
        return mgate
    return FailoverBackend(rest, mgate)
