"""Backend interface and errors."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import aiohttp

from .models import DepartureBoard, Line, Stop

if TYPE_CHECKING:
    from .ratelimit import TokenBucket

REQUEST_TIMEOUT_S = 10


class BackendError(Exception):
    """Base error for all backend failures. Triggers failover."""


class BackendUnavailable(BackendError):
    """Timeout, connection error or 5xx."""


class RateLimited(BackendError):
    """HTTP 429."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__(f"rate limited, retry after {retry_after}")
        self.retry_after = retry_after


class ParseError(BackendError):
    """Response could not be parsed."""


class DepartureBackend(Protocol):
    """A source of stops, lines and departures."""

    name: str

    async def search_stops(self, query: str, limit: int = 8) -> list[Stop]: ...

    async def lines_at_stop(self, stop_id: str) -> list[Line]: ...

    async def departures(
        self,
        stop_id: str,
        *,
        direction_stop_id: str | None,
        duration_min: int,
        max_results: int,
    ) -> DepartureBoard: ...

    async def trip_next_stop(self, trip_id: str, after_stop_id: str) -> Stop | None: ...


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; fall back to exponential backoff


async def fetch_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    limiter: TokenBucket | None = None,
) -> Any:
    """Perform one request and map every failure onto a BackendError."""
    if limiter is not None:
        await limiter.acquire()
    try:
        async with session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
        ) as resp:
            if resp.status == 429:
                raise RateLimited(_retry_after(resp.headers.get("Retry-After")))
            if resp.status >= 500:
                raise BackendUnavailable(f"HTTP {resp.status} from {url}")
            if resp.status >= 400:
                raise BackendError(f"HTTP {resp.status} from {url}")
            try:
                return await resp.json(content_type=None)
            except ValueError as err:
                raise ParseError(f"invalid JSON from {url}") from err
    except TimeoutError as err:
        raise BackendUnavailable(f"timeout talking to {url}") from err
    except aiohttp.ClientError as err:
        raise BackendUnavailable(f"{type(err).__name__} talking to {url}") from err
