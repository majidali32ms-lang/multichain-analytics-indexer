"""Follow mode: run the incremental sync again and again, storing what arrives.

Each round is an ordinary sync, so it resumes, never duplicates and re-checks pending
records exactly as ``sync`` does. A round that fails on the network is logged and the next
one tries again; any other error stops the follow, because it would fail every time.

The default pause between rounds suits each chain's block time and the measured cost of
asking public endpoints for their head: one request on Algorand, Solana and Bitcoin, two
on NEAR and the EVM chains, with Solana's public RPC allowing only one request every two
seconds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from multichain_indexer.core.errors import InvalidInput, NetworkError
from multichain_indexer.core.models import SyncResult

DEFAULT_INTERVAL_SECONDS = 30.0
INTERVAL_SECONDS = {
    "algorand": 10.0,
    "solana": 30.0,
    "near": 30.0,
    "ethereum": 15.0,
    "arbitrum": 15.0,
    "optimism": 15.0,
    "base": 15.0,
    "polygon": 15.0,
    "bnb": 15.0,
    "opbnb": 15.0,
    "bitcoin": 60.0,
}

SyncOnce = Callable[[str], Awaitable[SyncResult]]
SyncCallback = Callable[[SyncResult], None]
Sleep = Callable[[float], Awaitable[None]]

logger = logging.getLogger(__name__)


@dataclass
class FollowSummary:
    """What a follow did, round by round, until it stopped."""

    rounds: int = 0
    failed_rounds: int = 0
    records_fetched: int = 0
    requests_made: int = 0
    results: list[SyncResult] = field(default_factory=list)

    def add(self, result: SyncResult) -> None:
        self.rounds += 1
        self.records_fetched += result.records_fetched
        self.requests_made += result.requests_made
        self.results.append(result)


def default_interval(chain: str) -> float:
    """Return the pause between rounds for ``chain``; plugin chains get the general default."""
    return INTERVAL_SECONDS.get(chain, DEFAULT_INTERVAL_SECONDS)


async def follow_account(
    sync_once: SyncOnce,
    address: str,
    *,
    interval: float,
    on_sync: SyncCallback | None = None,
    rounds: int | None = None,
    sleep: Sleep = asyncio.sleep,
) -> FollowSummary:
    """Sync ``address`` every ``interval`` seconds until cancelled or ``rounds`` are done.

    Raises:
        InvalidInput: ``interval`` or ``rounds`` is not positive.
        IndexerError: Any failure other than a network one, such as a bad address.
    """
    if interval <= 0:
        raise InvalidInput("the follow interval must be more than 0 seconds")
    if rounds is not None and rounds < 1:
        raise InvalidInput("rounds must be at least 1")
    summary = FollowSummary()
    while rounds is None or summary.rounds + summary.failed_rounds < rounds:
        try:
            result = await sync_once(address)
        except NetworkError as exc:
            summary.failed_rounds += 1
            logger.warning("follow round failed, trying again", extra={"error": str(exc)})
        else:
            summary.add(result)
            if on_sync is not None:
                on_sync(result)
        if rounds is None or summary.rounds + summary.failed_rounds < rounds:
            await sleep(interval)
    return summary
