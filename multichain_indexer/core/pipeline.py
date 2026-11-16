"""Fetch, store and summarise: the path every chain's data takes."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from multichain_indexer.core.base_indexer import BaseIndexer
from multichain_indexer.core.errors import InvalidInput
from multichain_indexer.core.metrics import build_report
from multichain_indexer.core.models import MetricsReport, Status, SyncResult, TransactionRecord
from multichain_indexer.core.storage import Backfill, Storage

ProgressCallback = Callable[[int], None]

logger = logging.getLogger(__name__)


@dataclass
class _Stored:
    """What one sync stored: how many records the pages held, and which transactions."""

    count: int = 0
    ids: set[str] = field(default_factory=set)


class Pipeline:
    """Connects one adapter to one store."""

    def __init__(self, indexer: BaseIndexer, storage: Storage) -> None:
        self._indexer = indexer
        self._storage = storage
        self._chain = indexer.profile.name
        self._network = indexer.network

    async def sync_account(
        self, address: str, *, on_progress: ProgressCallback | None = None
    ) -> SyncResult:
        """Store an account's history, resuming an interrupted sync where it stopped.

        A sync walks a fixed range of heights, newest first. Each page's records and the
        cursor for the next page are saved in one database transaction, so after a crash
        the stored cursor always points at the first page not yet stored. A resumed sync
        finishes that same range. The checkpoint only moves once the range is complete,
        and never past the final height, so records still pending are fetched again.

        Args:
            address: The account to sync.
            on_progress: Called with the number of records stored so far in this run.
        """
        account = self._indexer.validate_address(address)
        started = time.monotonic()
        requests_before = self._indexer.requests_made

        state = self._storage.load_sync_state(self._chain, self._network, account)
        resumed = state.backfill is not None
        backfill = state.backfill or await self._new_backfill(state.checkpoint)

        stored = _Stored()
        if backfill.from_height is None or backfill.from_height <= backfill.to_height:
            stored = await self._store_range(account, backfill, on_progress)
        await self._recheck_pending(account, stored.ids)

        final_height = self._indexer.final_height(backfill.to_height)
        self._storage.finish_sync(self._chain, self._network, account, final_height)
        return SyncResult(
            chain=self._chain,
            network=self._network,
            account=account,
            from_height=backfill.from_height,
            to_height=backfill.to_height,
            resumed=resumed,
            records_fetched=stored.count,
            requests_made=self._indexer.requests_made - requests_before,
            elapsed_seconds=time.monotonic() - started,
        )

    async def _new_backfill(self, checkpoint: int | None) -> Backfill:
        from_height = None if checkpoint is None else checkpoint + 1
        return Backfill(from_height, await self._indexer.chain_head(), None)

    async def _store_range(
        self, account: str, backfill: Backfill, on_progress: ProgressCallback | None
    ) -> _Stored:
        try:
            return await self._store_pages(account, backfill, on_progress)
        except InvalidInput:
            if backfill.cursor is None:
                raise
        # A node may stop honouring an old cursor. Walking the same range again from its
        # start costs requests but cannot lose anything, because writes are upserts.
        logger.warning("stored cursor was rejected, restarting range", extra={"account": account})
        restart = Backfill(backfill.from_height, backfill.to_height, None)
        return await self._store_pages(account, restart, on_progress)

    async def _store_pages(
        self, account: str, backfill: Backfill, on_progress: ProgressCallback | None
    ) -> _Stored:
        stored = _Stored()
        pages = self._indexer.iter_account_transactions(
            account,
            from_height=backfill.from_height,
            to_height=backfill.to_height,
            cursor=backfill.cursor,
        )
        async for page in pages:
            progress = (
                Backfill(backfill.from_height, backfill.to_height, page.next_cursor)
                if page.next_cursor
                else None
            )
            stored.count += self._storage.save_transactions(
                self._chain, self._network, account, page.records, backfill=progress
            )
            stored.ids.update(record.id for record in page.records)
            if on_progress is not None:
                on_progress(stored.count)
        return stored

    async def _recheck_pending(self, account: str, fetched: set[str]) -> None:
        """Ask the adapter about pending records this sync did not fetch again, and store
        what changed, such as a transaction a reorg dropped, which becomes orphaned."""
        stale = [
            record
            for record in self._storage.load_transactions(
                self._chain,
                self._network,
                self._indexer.profile.transaction_model,
                account=account,
                status=Status.PENDING,
            )
            if record.id not in fetched
        ]
        if not stale:
            return
        changed = await self._indexer.recheck_pending(stale)
        self._storage.save_transactions(self._chain, self._network, account, changed)
        orphaned = sum(1 for record in changed if record.status is Status.ORPHANED)
        if orphaned:
            logger.info("pending records orphaned", extra={"account": account, "count": orphaned})

    def stored_transactions(
        self, *, account: str | None = None, days: int | None = None
    ) -> list[TransactionRecord]:
        """Return stored records, optionally for one account and the last ``days`` days."""
        since = None if days is None else datetime.now(UTC) - timedelta(days=days)
        return self._storage.load_transactions(
            self._chain,
            self._network,
            self._indexer.profile.transaction_model,
            account=account,
            since=since,
        )

    def metrics(self, days: int, *, account: str | None = None) -> MetricsReport:
        """Summarise stored records over the last ``days`` days."""
        records = self.stored_transactions(account=account, days=days)
        return build_report(
            records,
            chain=self._chain,
            network=self._network,
            days=days,
            account=account,
            native=self._indexer.chain_metrics(records),
        )
