"""The sync's re-check of stored pending records, which marks reorged ones orphaned."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue

from multichain_indexer.core.base_indexer import BaseIndexer, ChainProfile
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    NativeData,
    Status,
    TransactionPage,
    TransactionRecord,
)
from multichain_indexer.core.pipeline import Pipeline
from multichain_indexer.core.storage import SQLiteStorage

NOW = datetime(2026, 11, 11, tzinfo=UTC)
ACCOUNT = "alice"

# The database schema exactly as version 0.4.0 created it, so a database written before
# orphan marking existed is shown to load and be re-checked.
SCHEMA_0_4_0 = """
CREATE TABLE IF NOT EXISTS transactions (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    time TEXT NOT NULL,
    sender TEXT NOT NULL,
    receiver TEXT,
    amount TEXT NOT NULL,
    fee TEXT NOT NULL,
    status TEXT NOT NULL,
    native TEXT NOT NULL,
    PRIMARY KEY (chain, network, tx_id)
);
CREATE INDEX IF NOT EXISTS transactions_by_time ON transactions (chain, network, time);
CREATE INDEX IF NOT EXISTS transactions_by_sender ON transactions (chain, network, sender, time);
CREATE TABLE IF NOT EXISTS account_transactions (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    account TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    PRIMARY KEY (chain, network, account, tx_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (chain, network, kind, item_id)
);
CREATE TABLE IF NOT EXISTS sync_state (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    account TEXT NOT NULL,
    checkpoint INTEGER,
    backfill_from INTEGER,
    backfill_to INTEGER,
    backfill_cursor TEXT,
    PRIMARY KEY (chain, network, account)
);
"""


class ToyNative(NativeData):
    height: int


def record(tx_id: str, status: Status = Status.PENDING, height: int = 90) -> TransactionRecord:
    return TransactionRecord(
        id=tx_id,
        time=NOW,
        sender=ACCOUNT,
        amount=1,
        fee=1,
        status=status,
        chain="toy",
        network="testnet",
        native=ToyNative(height=height),
    )


class ToyIndexer(BaseIndexer):
    """Serves a fixed history; ``dropped`` names what a reorg removed from the chain."""

    profile = ChainProfile("toy", "TOY", "units", 0, ("testnet",), "testnet", ToyNative)

    def __init__(self, history: Sequence[TransactionRecord], dropped: set[str]) -> None:
        super().__init__("testnet")
        self.history = list(history)
        self.dropped = dropped
        self.rechecked: list[list[str]] = []

    def validate_address(self, address: str) -> str:
        return address

    async def fetch_account(self, address: str) -> AccountRecord:
        raise NotImplementedError

    async def fetch_asset(self, asset_id: str) -> AssetRecord:
        raise NotImplementedError

    async def fetch_contract(self, contract_id: str) -> ContractRecord:
        raise NotImplementedError

    async def chain_head(self) -> int:
        return 100

    async def iter_account_transactions(
        self,
        address: str,
        *,
        from_height: int | None = None,
        to_height: int | None = None,
        cursor: str | None = None,
    ) -> AsyncIterator[TransactionPage]:
        records = tuple(
            item
            for item in self.history
            if from_height is None or item.native.height >= from_height  # type: ignore[attr-defined]
        )
        if records:
            yield TransactionPage(records=records)

    async def recheck_pending(
        self, records: Sequence[TransactionRecord]
    ) -> list[TransactionRecord]:
        self.rechecked.append([item.id for item in records])
        return [
            item.model_copy(update={"status": Status.ORPHANED})
            for item in records
            if item.id in self.dropped
        ]

    def chain_metrics(self, records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
        return {}

    def describe(
        self, record: TransactionRecord | AccountRecord | AssetRecord | ContractRecord
    ) -> dict[str, str]:
        return {}

    @property
    def requests_made(self) -> int:
        return 0

    async def aclose(self) -> None:
        return None

    def final_height(self, head: int) -> int:
        return head - 20


def statuses(storage: SQLiteStorage) -> dict[str, Status]:
    stored = storage.load_transactions("toy", "testnet", ToyNative, account=ACCOUNT)
    return {item.id: item.status for item in stored}


async def test_a_pending_record_a_reorg_dropped_is_marked_orphaned_not_deleted(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "toy.db")
    indexer = ToyIndexer([record("kept"), record("dropped")], dropped={"dropped"})
    pipeline = Pipeline(indexer, storage)
    first = await pipeline.sync_account(ACCOUNT)
    assert statuses(storage) == {"kept": Status.PENDING, "dropped": Status.PENDING}
    assert indexer.rechecked == []

    indexer.history = [record("kept", Status.FINAL)]
    second = await pipeline.sync_account(ACCOUNT)
    storage.close()

    assert indexer.rechecked == [["dropped"]]
    assert statuses(SQLiteStorage(tmp_path / "toy.db")) == {
        "kept": Status.FINAL,
        "dropped": Status.ORPHANED,
    }
    assert (first.records_fetched, second.records_fetched) == (2, 1)


async def test_records_the_sync_fetched_again_are_not_rechecked(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "toy.db")
    indexer = ToyIndexer([record("a"), record("b")], dropped=set())
    pipeline = Pipeline(indexer, storage)
    await pipeline.sync_account(ACCOUNT)
    await pipeline.sync_account(ACCOUNT)
    assert indexer.rechecked == []
    assert statuses(storage) == {"a": Status.PENDING, "b": Status.PENDING}


async def test_nothing_changed_leaves_the_records_as_they_are(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "toy.db")
    indexer = ToyIndexer([record("a")], dropped=set())
    pipeline = Pipeline(indexer, storage)
    await pipeline.sync_account(ACCOUNT)
    indexer.history = []
    await pipeline.sync_account(ACCOUNT)
    assert indexer.rechecked == [["a"]]
    assert statuses(storage) == {"a": Status.PENDING}


async def test_final_and_failed_records_are_never_rechecked(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "toy.db")
    indexer = ToyIndexer(
        [record("final", Status.FINAL), record("failed", Status.FAILED)], dropped=set()
    )
    pipeline = Pipeline(indexer, storage)
    await pipeline.sync_account(ACCOUNT)
    indexer.history = []
    await pipeline.sync_account(ACCOUNT)
    assert indexer.rechecked == []


async def test_a_database_written_by_0_4_0_loads_and_is_rechecked(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_0_4_0)
    with connection:
        for tx_id, status in (("old-final", "final"), ("old-pending", "pending")):
            connection.execute(
                "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "toy",
                    "testnet",
                    tx_id,
                    NOW.isoformat(),
                    ACCOUNT,
                    None,
                    "1",
                    "1",
                    status,
                    '{"height": 50}',
                ),
            )
            connection.execute(
                "INSERT INTO account_transactions VALUES (?, ?, ?, ?)",
                ("toy", "testnet", ACCOUNT, tx_id),
            )
        connection.execute(
            "INSERT INTO sync_state (chain, network, account, checkpoint) VALUES (?, ?, ?, ?)",
            ("toy", "testnet", ACCOUNT, 60),
        )
    connection.close()

    storage = SQLiteStorage(path)
    indexer = ToyIndexer([], dropped={"old-pending"})
    result = await Pipeline(indexer, storage).sync_account(ACCOUNT)
    assert result.from_height == 61
    assert indexer.rechecked == [["old-pending"]]
    assert statuses(storage) == {"old-final": Status.FINAL, "old-pending": Status.ORPHANED}


def test_storage_filters_by_status(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "toy.db")
    storage.save_transactions("toy", "testnet", ACCOUNT, [record("p"), record("f", Status.FINAL)])
    pending = storage.load_transactions(
        "toy", "testnet", ToyNative, account=ACCOUNT, status=Status.PENDING
    )
    assert [item.id for item in pending] == ["p"]


@pytest.mark.parametrize("status", [Status.PENDING, Status.FINAL])
async def test_chains_without_a_recheck_change_nothing(status: Status) -> None:
    class Plain(ToyIndexer):
        recheck_pending = BaseIndexer.recheck_pending  # type: ignore[assignment]

    assert await Plain([], set()).recheck_pending([record("a", status)]) == []
