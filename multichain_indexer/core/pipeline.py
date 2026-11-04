"""Fetch, store and summarise: the path every chain's data takes."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from multichain_indexer.core.base_indexer import BaseIndexer
from multichain_indexer.core.metrics import build_report
from multichain_indexer.core.models import MetricsReport, SyncResult, TransactionRecord
from multichain_indexer.core.storage import Storage

ProgressCallback = Callable[[int], None]


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
        """Fetch what is new since the last sync and store it.

        Re-running is safe at any point: writes are upserts, and the checkpoint only
        moves after every page has been stored. It never moves past the final height,
        so records still pending are fetched again next time.

        Args:
            address: The account to sync.
            on_progress: Called with the number of records stored so far.
        """
        account = self._indexer.validate_address(address)
        started = time.monotonic()
        requests_before = self._indexer.requests_made

        checkpoint = self._storage.load_checkpoint(self._chain, self._network, account)
        from_height = None if checkpoint is None else checkpoint + 1
        to_height = await self._indexer.chain_head()

        fetched = 0
        if from_height is None or from_height <= to_height:
            pages = self._indexer.iter_account_transactions(
                account, from_height=from_height, to_height=to_height
            )
            async for page in pages:
                fetched += self._storage.save_transactions(self._network, account, page)
                if on_progress is not None:
                    on_progress(fetched)

        final_height = self._indexer.final_height(to_height)
        self._storage.save_checkpoint(self._chain, self._network, account, final_height)
        return SyncResult(
            chain=self._chain,
            network=self._network,
            account=account,
            from_height=from_height,
            to_height=to_height,
            records_fetched=fetched,
            requests_made=self._indexer.requests_made - requests_before,
            elapsed_seconds=time.monotonic() - started,
        )

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
