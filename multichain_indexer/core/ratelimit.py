"""Paces requests so public nodes and free API plans are not hammered.

A provider's free plan limits its key, not each chain that uses it: Blockscout, for one,
serves Ethereum, Arbitrum and OP Mainnet on one key. Chains synced side by side must then
share one limiter, so ``shared_limiter`` keeps one per key, or per host for keyless
endpoints, for the whole process.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from urllib.parse import urlsplit

from pydantic import SecretStr

_KEY_DIGEST_LENGTH = 16

_shared: dict[str, RateLimiter] = {}
_shared_lock = threading.Lock()


class RateLimiter:
    """Spaces request starts evenly and honours ``Retry-After`` pauses.

    Safe to share between event loops on different threads, as the blocking ``Indexer``
    runs each one on a thread of its own.
    """

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1 / requests_per_second
        self._next_slot = 0.0
        self._lock = threading.Lock()

    @property
    def requests_per_second(self) -> float:
        return 1 / self._interval

    async def acquire(self) -> None:
        """Wait for this request's turn."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        if slot > now:
            await asyncio.sleep(slot - now)

    def pause(self, seconds: float) -> None:
        """Hold back every later request for at least ``seconds``."""
        with self._lock:
            self._next_slot = max(self._next_slot, time.monotonic() + seconds)

    def slow_to(self, requests_per_second: float) -> None:
        """Lower the rate to ``requests_per_second`` if that is slower than the current one."""
        with self._lock:
            self._interval = max(self._interval, 1 / requests_per_second)


def shared_limiter(scope: str, requests_per_second: float) -> RateLimiter:
    """Return this process's one limiter for ``scope``, from ``key_scope`` or ``host_scope``.

    When two users of a scope ask for different rates, the slower one holds for both.
    """
    with _shared_lock:
        limiter = _shared.get(scope)
        if limiter is None:
            limiter = _shared[scope] = RateLimiter(requests_per_second)
            return limiter
    limiter.slow_to(requests_per_second)
    return limiter


def key_scope(service: str, key: SecretStr) -> str:
    """Name the scope of an API key. Only a short digest of the key is kept."""
    digest = hashlib.sha256(key.get_secret_value().encode()).hexdigest()
    return f"{service}:{digest[:_KEY_DIGEST_LENGTH]}"


def host_scope(base_url: SecretStr) -> str:
    """Name the scope of a keyless endpoint: its host and port, never its path or query."""
    parts = urlsplit(base_url.get_secret_value())
    host = (parts.hostname or "").lower()
    return f"host:{host}:{parts.port}" if parts.port else f"host:{host}"
