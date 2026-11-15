"""Every chain at once: metrics side by side, one export table, and parallel syncs.

from multichain_indexer import CrossChain

with CrossChain() as everything:
    everything.sync()
    for row in everything.metrics(days=30).rows:
        print(row.chain, row.network, row.transactions, row.total_fees_display)

A chain that fails never stops the others: its row, or its account's outcome, carries the
error instead. US dollar figures appear only for mainnet records and only when the user
has given a price in ``config.toml``; test network tokens have no market price.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from multichain_indexer.api import AsyncIndexer, _LoopThread
from multichain_indexer.core.base_indexer import ChainProfile
from multichain_indexer.core.config import Settings
from multichain_indexer.core.config_file import MAINNET
from multichain_indexer.core.errors import IndexerError, InvalidInput
from multichain_indexer.core.export import export_transactions
from multichain_indexer.core.models import MetricsReport, SyncResult, TransactionRecord
from multichain_indexer.db import Database, chosen_database, open_storage

ALL_CHAINS = "all"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChainMetrics(_Frozen):
    """One chain and network's metrics, in its own unit and in US dollars when priced."""

    chain: str
    network: str
    days: int = Field(ge=1)
    transactions: int = Field(default=0, ge=0)
    transactions_per_day: float = Field(default=0.0, ge=0)
    daily_transactions: dict[str, int] = Field(default_factory=dict)
    active_accounts: int = Field(default=0, ge=0)
    total_fees: int = Field(default=0, ge=0)
    total_fees_display: str = ""
    fees_usd: Decimal | None = None
    price_usd: Decimal | None = None
    price_date: date | None = None
    failure_rate: float | None = None
    error: str | None = None


class CrossChainReport(_Frozen):
    """Metrics for every chain and network in the database, over the same window."""

    generated_at: AwareDatetime
    days: int = Field(ge=1)
    rows: tuple[ChainMetrics, ...]


class SyncOutcome(_Frozen):
    """What syncing one account from ``config.toml`` gave: a result or an error."""

    chain: str
    network: str
    account: str
    result: SyncResult | None = None
    error: str | None = None


class AsyncCrossChain:
    """Works on every chain and network at once, through one database.

    Args:
        settings: Configuration; read from ``config.toml``, ``.env`` and the environment
            when None.
        database: SQLite file or PostgreSQL URL; overrides ``MCI_DATABASE_URL`` and the
            configured file.
        migrate: Bring the database to the current schema first; see ``AsyncIndexer``.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        database: Database | None = None,
        migrate: bool = True,
    ) -> None:
        self._settings = settings or Settings.from_env()
        self._database = chosen_database(self._settings, database)
        self._migrate = migrate

    def networks(self) -> list[tuple[str, str]]:
        """Return every chain and network with stored transactions."""
        storage = open_storage(self._database, migrate=self._migrate)
        try:
            return storage.stored_networks()
        finally:
            storage.close()

    async def metrics(self, days: int = 30) -> CrossChainReport:
        """Summarise every stored chain and network over the last ``days`` days."""
        if days < 1:
            raise InvalidInput("days must be at least 1")
        rows = await asyncio.gather(
            *(self._chain_metrics(chain, network, days) for chain, network in self.networks())
        )
        return CrossChainReport(generated_at=datetime.now(UTC), days=days, rows=tuple(rows))

    async def sync(self, network: str | None = None) -> list[SyncOutcome]:
        """Sync every account listed under ``[accounts]`` in ``config.toml``, chains in parallel.

        Args:
            network: Sync only the accounts listed for this network. When None, every
                network except mainnet, which is synced only when named.
        """
        groups = [
            (chain, listed_network, accounts)
            for chain, networks in self._settings.accounts.items()
            for listed_network, accounts in networks.items()
            if _selected(listed_network, network)
        ]
        if not groups:
            raise InvalidInput(
                "no accounts to sync: list them under [accounts.<chain>] in config.toml"
            )
        outcomes = await asyncio.gather(*(self._sync_group(*group) for group in groups))
        return [outcome for group in outcomes for outcome in group]

    async def export(
        self, export_format: str, output: Path | None = None, *, days: int | None = None
    ) -> str | None:
        """Export every stored chain's transactions as one table, shared fields as columns.

        Returns:
            The exported text when ``output`` is None, otherwise None.
        """
        records: list[TransactionRecord] = []
        for chain, network in self.networks():
            async with self._open(chain, network) as indexer:
                records.extend(indexer.stored_transactions(days=days))
        return export_transactions(records, export_format, output)

    def _open(self, chain: str, network: str) -> AsyncIndexer:
        return AsyncIndexer(
            chain,
            network,
            settings=self._settings,
            database=self._database,
            migrate=self._migrate,
        )

    async def _chain_metrics(self, chain: str, network: str, days: int) -> ChainMetrics:
        try:
            async with self._open(chain, network) as indexer:
                return self._row(indexer.profile, indexer.metrics(days))
        except IndexerError as exc:
            return ChainMetrics(chain=chain, network=network, days=days, error=str(exc))

    def _row(self, profile: ChainProfile, report: MetricsReport) -> ChainMetrics:
        price = self._settings.prices.get(report.chain) if report.network == MAINNET else None
        fees_usd = (
            None
            if price is None
            else Decimal(report.total_fees).scaleb(-profile.native_decimals) * price.usd
        )
        return ChainMetrics(
            chain=report.chain,
            network=report.network,
            days=report.days,
            transactions=report.transactions,
            transactions_per_day=round(report.transactions / report.days, 2),
            daily_transactions=report.transactions_per_day,
            active_accounts=report.active_accounts,
            total_fees=report.total_fees,
            total_fees_display=profile.format_native(report.total_fees),
            fees_usd=fees_usd,
            price_usd=None if price is None else price.usd,
            price_date=None if price is None else price.date,
            failure_rate=report.failure_rate,
        )

    async def _sync_group(
        self, chain: str, network: str, accounts: Sequence[str]
    ) -> list[SyncOutcome]:
        try:
            indexer = self._open(chain, network)
        except IndexerError as exc:
            return [_failed(chain, network, account, exc) for account in accounts]
        outcomes: list[SyncOutcome] = []
        async with indexer:
            for account in accounts:
                try:
                    result = await indexer.sync(account)
                except IndexerError as exc:
                    outcomes.append(_failed(chain, network, account, exc))
                    continue
                outcomes.append(
                    SyncOutcome(chain=chain, network=network, account=account, result=result)
                )
        return outcomes


class CrossChain:
    """Blocking version of ``AsyncCrossChain``, with the same methods and arguments."""

    def __init__(
        self, *, settings: Settings | None = None, database: Database | None = None
    ) -> None:
        self._thread = _LoopThread()
        self._inner = AsyncCrossChain(settings=settings, database=database)

    def networks(self) -> list[tuple[str, str]]:
        """See ``AsyncCrossChain.networks``."""
        return self._thread.call(self._inner.networks)

    def metrics(self, days: int = 30) -> CrossChainReport:
        """See ``AsyncCrossChain.metrics``."""
        return self._thread.run(self._inner.metrics(days))

    def sync(self, network: str | None = None) -> list[SyncOutcome]:
        """See ``AsyncCrossChain.sync``."""
        return self._thread.run(self._inner.sync(network))

    def export(
        self, export_format: str, output: Path | None = None, *, days: int | None = None
    ) -> str | None:
        """See ``AsyncCrossChain.export``."""
        return self._thread.run(self._inner.export(export_format, output, days=days))

    def close(self) -> None:
        """Stop the event loop thread."""
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


def _selected(listed: str, wanted: str | None) -> bool:
    return listed == wanted if wanted is not None else listed != MAINNET


def _failed(chain: str, network: str, account: str, error: IndexerError) -> SyncOutcome:
    return SyncOutcome(chain=chain, network=network, account=account, error=str(error))
