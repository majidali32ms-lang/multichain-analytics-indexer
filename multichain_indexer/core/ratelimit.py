"""Paces requests to one endpoint so public nodes are not hammered."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Spaces request starts evenly and honours ``Retry-After`` pauses."""

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1 / requests_per_second
        self._next_slot = 0.0

    async def acquire(self) -> None:
        """Wait for this request's turn."""
        # Nothing awaits between reading and advancing the slot, so concurrent
        # callers on one event loop each get a slot of their own.
        now = time.monotonic()
        slot = max(now, self._next_slot)
        self._next_slot = slot + self._interval
        if slot > now:
            await asyncio.sleep(slot - now)

    def pause(self, seconds: float) -> None:
        """Hold back every later request for at least ``seconds``."""
        self._next_slot = max(self._next_slot, time.monotonic() + seconds)
