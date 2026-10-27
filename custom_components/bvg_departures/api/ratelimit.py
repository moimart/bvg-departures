"""Token bucket shared by all config entries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time


class TokenBucket:
    """Allow `rate` requests per `period` seconds. Callers queue, never drop."""

    def __init__(
        self,
        rate: int = 60,
        period: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = float(rate)
        self._refill_per_s = rate / period
        self._tokens = float(rate)
        self._clock = clock
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(
            self._capacity, self._tokens + (now - self._updated) * self._refill_per_s
        )
        self._updated = now

    async def acquire(self) -> None:
        """Take one token, waiting for a refill if the bucket is empty."""
        async with self._lock:
            self._refill()
            while self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self._refill_per_s)
                self._refill()
            self._tokens -= 1
