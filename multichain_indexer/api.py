"""The public Python API. The command line uses nothing else.

from multichain_indexer import Indexer

with Indexer("algorand") as indexer:
    account = indexer.account("ADDRESS")
    indexer.sync("ADDRESS")
    print(indexer.metrics(days=30))
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import TracebackType
from typing import Self, TypeVar

import multichain_indexer.adapters  # noqa: F401  (registers the built-in chains)
from multichain_indexer.core.base_indexer import ChainProfile
from multichain_indexer.core.cache import ResponseCache
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import InvalidInput
from multichain_indexer.core.export import export_transactions
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    MetricsReport,
    SyncResult,
    TransactionRecord,
)
from multichain_indexer.core.pipeline import Pipeline, ProgressCallback
from multichain_indexer.core.registry import create_indexer
from multichain_indexer.core.storage import SQLiteStorage

DEFAULT_TRANSACTION_LIMIT = 20
_T = TypeVar("_T")
Describable = TransactionRecord | AccountRecord | AssetRecord | ContractRecord


class AsyncIndexer:
    """Reads one network of one chain and keeps what it reads in a local database.

    Args:
        chain: Chain name, such as ``"algorand"``.
        network: Network name; the chain's test network when None.
        settings: Configuration; read from ``.env`` and the environment when None.
        database: Database file; overrides the configured one.

    Raises:
        UnsupportedChain: The chain is unknown or not available in this version.
        InvalidInput: The network is not one the chain has.
    """

    def __init__(
        self,
        chain: str,
        network: str | None = None,
        *,
        settings: Settings | None = None,
        database: Path | None = None,
    ) -> None:
        config = settings or Settings.from_env()
        self._cache = ResponseCache(config.cache_path) if config.cache_path else None
        self._indexer = create_indexer(chain, network, config, self._cache)
        self._storage = SQLiteStorage(database or config.database)
        self._pipeline = Pipeline(self._indexer, self._storage)

    @property
    def profile(self) -> ChainProfile:
        return self._indexer.profile

    @property
    def network(self) -> str:
        return self._indexer.network

    async def account(self, address: str) -> AccountRecord:
        """Fetch an account's current state and store it."""
        record = await self._indexer.fetch_account(address)
        self._storage.save_snapshot(self.network, record)
        return record

    async def account_transactions(
        self, address: str, *, limit: int = DEFAULT_TRANSACTION_LIMIT
    ) -> list[TransactionRecord]:
        """Fetch an account's most recent transactions from the node, newest first."""
        if limit < 1:
            raise InvalidInput("limit must be at least 1")
        collected: list[TransactionRecord] = []
        async for page in self._indexer.iter_account_transactions(address):
            collected.extend(page)
            if len(collected) >= limit:
                break
        return collected[:limit]

    async def asset(self, asset_id: str | int) -> AssetRecord:
        """Fetch a token or asset, such as an Algorand ASA, and store it."""
        record = await self._indexer.fetch_asset(str(asset_id))
        self._storage.save_snapshot(self.network, record)
        return record

    async def contract(self, contract_id: str | int) -> ContractRecord:
        """Fetch an application or contract, such as an Algorand app, and store it."""
        record = await self._indexer.fetch_contract(str(contract_id))
        self._storage.save_snapshot(self.network, record)
        return record

    async def sync(
        self, address: str, *, on_progress: ProgressCallback | None = None
    ) -> SyncResult:
        """Store what is new for an account since the last sync. Safe to re-run."""
        return await self._pipeline.sync_account(address, on_progress=on_progress)

    def stored_transactions(
        self, *, account: str | None = None, days: int | None = None
    ) -> list[TransactionRecord]:
        """Return transactions from the local database, newest first."""
        if days is not None:
            _require_positive_days(days)
        return self._pipeline.stored_transactions(account=account, days=days)

    def metrics(self, days: int = 30, *, account: str | None = None) -> MetricsReport:
        """Summarise stored transactions over the last ``days`` days."""
        _require_positive_days(days)
        return self._pipeline.metrics(days, account=account)

    def export(
        self,
        export_format: str,
        output: Path | None = None,
        *,
        account: str | None = None,
        days: int | None = None,
    ) -> str | None:
        """Export stored transactions as ``csv``, ``json`` or ``parquet``.

        Returns:
            The exported text when ``output`` is None, otherwise None.
        """
        records = self.stored_transactions(account=account, days=days)
        return export_transactions(records, export_format, output)

    def describe(self, record: Describable) -> dict[str, str]:
        """Return label and value pairs for display, in the chain's own terms."""
        return self._indexer.describe(record)

    async def aclose(self) -> None:
        """Release the network session and the database."""
        await self._indexer.aclose()
        self._storage.close()
        if self._cache:
            self._cache.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


class _LoopThread:
    """Runs an event loop on its own thread.

    The caller's thread may already have a running loop, as it does in Jupyter, and a
    second loop cannot run there. SQLite connections belong to the thread that opened
    them, so everything the blocking API does happens on this one thread.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coroutine: Coroutine[object, object, _T]) -> _T:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def call(self, function: Callable[[], _T]) -> _T:
        async def invoke() -> _T:
            return function()

        return self.run(invoke())

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()


class Indexer:
    """Blocking version of ``AsyncIndexer``, with the same methods and arguments."""

    def __init__(
        self,
        chain: str,
        network: str | None = None,
        *,
        settings: Settings | None = None,
        database: Path | None = None,
    ) -> None:
        self._thread = _LoopThread()
        try:
            self._inner = self._thread.call(
                lambda: AsyncIndexer(chain, network, settings=settings, database=database)
            )
        except BaseException:
            self._thread.close()
            raise

    @property
    def profile(self) -> ChainProfile:
        return self._inner.profile

    @property
    def network(self) -> str:
        return self._inner.network

    def account(self, address: str) -> AccountRecord:
        """See ``AsyncIndexer.account``."""
        return self._thread.run(self._inner.account(address))

    def account_transactions(
        self, address: str, *, limit: int = DEFAULT_TRANSACTION_LIMIT
    ) -> list[TransactionRecord]:
        """See ``AsyncIndexer.account_transactions``."""
        return self._thread.run(self._inner.account_transactions(address, limit=limit))

    def asset(self, asset_id: str | int) -> AssetRecord:
        """See ``AsyncIndexer.asset``."""
        return self._thread.run(self._inner.asset(asset_id))

    def contract(self, contract_id: str | int) -> ContractRecord:
        """See ``AsyncIndexer.contract``."""
        return self._thread.run(self._inner.contract(contract_id))

    def sync(self, address: str, *, on_progress: ProgressCallback | None = None) -> SyncResult:
        """See ``AsyncIndexer.sync``."""
        return self._thread.run(self._inner.sync(address, on_progress=on_progress))

    def stored_transactions(
        self, *, account: str | None = None, days: int | None = None
    ) -> list[TransactionRecord]:
        """See ``AsyncIndexer.stored_transactions``."""
        return self._thread.call(
            lambda: self._inner.stored_transactions(account=account, days=days)
        )

    def metrics(self, days: int = 30, *, account: str | None = None) -> MetricsReport:
        """See ``AsyncIndexer.metrics``."""
        return self._thread.call(lambda: self._inner.metrics(days, account=account))

    def export(
        self,
        export_format: str,
        output: Path | None = None,
        *,
        account: str | None = None,
        days: int | None = None,
    ) -> str | None:
        """See ``AsyncIndexer.export``."""
        return self._thread.call(
            lambda: self._inner.export(export_format, output, account=account, days=days)
        )

    def describe(self, record: Describable) -> dict[str, str]:
        """See ``AsyncIndexer.describe``."""
        return self._inner.describe(record)

    def close(self) -> None:
        """Release the network session and the database."""
        self._thread.run(self._inner.aclose())
        self._thread.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _require_positive_days(days: int) -> None:
    if days < 1:
        raise InvalidInput("days must be at least 1")
