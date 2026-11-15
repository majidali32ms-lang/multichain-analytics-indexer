"""The storage contract every backend must keep."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from multichain_indexer.core.models import NativeData, Status, TransactionRecord
from multichain_indexer.core.storage import Backfill, SQLiteStorage, Storage

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
# Larger than any 64-bit integer, as a NEAR fee in yoctoNEAR or an EVM amount in wei can be.
HUGE = 2**70 + 3


class ToyNative(NativeData):
    height: int
    memo: str | None = None


def record(
    tx_id: str,
    *,
    chain: str = "toy",
    network: str = "testnet",
    status: Status = Status.FINAL,
    age: timedelta = timedelta(),
) -> TransactionRecord:
    return TransactionRecord(
        id=tx_id,
        time=NOW - age,
        sender="alice",
        receiver="bob",
        amount=HUGE,
        fee=7,
        status=status,
        chain=chain,
        network=network,
        native=ToyNative(height=5, memo="ünïcode"),
    )


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[Storage]:
    opened = SQLiteStorage(tmp_path / "storage.db")
    yield opened
    opened.close()


def test_records_round_trip_exactly(storage: Storage) -> None:
    saved = record("a")
    storage.save_transactions("toy", "testnet", "alice", [saved])
    assert storage.load_transactions("toy", "testnet", ToyNative) == [saved]


def test_saving_again_updates_and_never_duplicates(storage: Storage) -> None:
    storage.save_transactions("toy", "testnet", "alice", [record("a", status=Status.PENDING)])
    storage.save_transactions("toy", "testnet", "alice", [record("a")])
    loaded = storage.load_transactions("toy", "testnet", ToyNative)
    assert [(item.id, item.status) for item in loaded] == [("a", Status.FINAL)]


def test_loads_filter_by_account_time_and_status(storage: Storage) -> None:
    storage.save_transactions(
        "toy",
        "testnet",
        "alice",
        [record("new"), record("old", age=timedelta(days=40), status=Status.PENDING)],
    )
    storage.save_transactions("toy", "testnet", "carol", [record("other")])

    def ids(**filters: object) -> list[str]:
        loaded = storage.load_transactions("toy", "testnet", ToyNative, **filters)  # type: ignore[arg-type]
        return [item.id for item in loaded]

    assert ids(account="alice") == ["new", "old"]
    assert ids(since=NOW - timedelta(days=30)) == ["new", "other"]
    assert ids(status=Status.PENDING) == ["old"]


def test_stored_networks_lists_each_chain_and_network_once(storage: Storage) -> None:
    assert storage.stored_networks() == []
    storage.save_transactions(
        "toy", "testnet", "alice", [record("a"), record("b"), record("m", network="mainnet")]
    )
    storage.save_transactions("algo", "testnet", "alice", [record("c", chain="algo")])
    assert storage.stored_networks() == [
        ("algo", "testnet"),
        ("toy", "mainnet"),
        ("toy", "testnet"),
    ]


def test_backfill_is_saved_with_its_records_and_cleared_when_finished(storage: Storage) -> None:
    progress = Backfill(from_height=10, to_height=90, cursor="page-2")
    storage.save_transactions("toy", "testnet", "alice", [record("a")], backfill=progress)
    assert storage.load_sync_state("toy", "testnet", "alice").backfill == progress

    storage.finish_sync("toy", "testnet", "alice", 84)
    state = storage.load_sync_state("toy", "testnet", "alice")
    assert (state.checkpoint, state.backfill) == (84, None)
    assert storage.load_sync_state("toy", "mainnet", "alice").checkpoint is None
