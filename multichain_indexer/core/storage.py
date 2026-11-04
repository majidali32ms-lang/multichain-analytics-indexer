"""Where records are kept. SQLite by default, behind an interface callers depend on.

Amounts and fees are stored as text because SQLite integers stop at 64 bits, and
several chains go past that. Every table is keyed by network as well as chain, so a
testnet sync can never overwrite the checkpoint of a mainnet one.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    NativeData,
    Status,
    TransactionRecord,
)

_SCHEMA = """
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
    checkpoint INTEGER NOT NULL,
    PRIMARY KEY (chain, network, account)
);
"""

_UPSERT_TRANSACTION = """
INSERT INTO transactions
    (chain, network, tx_id, time, sender, receiver, amount, fee, status, native)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (chain, network, tx_id) DO UPDATE SET
    time = excluded.time,
    sender = excluded.sender,
    receiver = excluded.receiver,
    amount = excluded.amount,
    fee = excluded.fee,
    status = excluded.status,
    native = excluded.native
"""

_SELECT_TRANSACTIONS = """
SELECT t.tx_id, t.time, t.sender, t.receiver, t.amount, t.fee, t.status, t.chain, t.native
FROM transactions t
"""

_JOIN_ACCOUNT = """
JOIN account_transactions a
  ON a.chain = t.chain AND a.network = t.network AND a.tx_id = t.tx_id AND a.account = ?
"""


class Storage(ABC):
    """Everything the pipeline needs from a database."""

    @abstractmethod
    def save_transactions(
        self, network: str, account: str, records: Iterable[TransactionRecord]
    ) -> int:
        """Upsert records and link them to ``account``; return how many were given."""

    @abstractmethod
    def load_transactions(
        self,
        chain: str,
        network: str,
        native_model: type[NativeData],
        *,
        account: str | None = None,
        since: datetime | None = None,
    ) -> list[TransactionRecord]:
        """Return stored transactions, newest first."""

    @abstractmethod
    def save_snapshot(
        self, network: str, record: AccountRecord | AssetRecord | ContractRecord
    ) -> None:
        """Keep the latest state of an account, asset or contract."""

    @abstractmethod
    def load_checkpoint(self, chain: str, network: str, account: str) -> int | None:
        """Return the last fully synced height for ``account``, if any."""

    @abstractmethod
    def save_checkpoint(self, chain: str, network: str, account: str, height: int) -> None:
        """Record that ``account`` is fully synced up to ``height``."""

    @abstractmethod
    def close(self) -> None:
        """Release the database connection."""


class SQLiteStorage(Storage):
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.executescript(_SCHEMA)

    def save_transactions(
        self, network: str, account: str, records: Iterable[TransactionRecord]
    ) -> int:
        saved = 0
        with self._connection:
            for record in records:
                self._connection.execute(_UPSERT_TRANSACTION, _transaction_row(network, record))
                self._connection.execute(
                    "INSERT OR IGNORE INTO account_transactions VALUES (?, ?, ?, ?)",
                    (record.chain, network, account, record.id),
                )
                saved += 1
        return saved

    def load_transactions(
        self,
        chain: str,
        network: str,
        native_model: type[NativeData],
        *,
        account: str | None = None,
        since: datetime | None = None,
    ) -> list[TransactionRecord]:
        query = _SELECT_TRANSACTIONS
        arguments: list[str] = []
        if account is not None:
            query += _JOIN_ACCOUNT
            arguments.append(account)
        query += " WHERE t.chain = ? AND t.network = ?"
        arguments += [chain, network]
        if since is not None:
            query += " AND t.time >= ?"
            arguments.append(since.isoformat())
        query += " ORDER BY t.time DESC, t.tx_id"
        rows = self._connection.execute(query, arguments).fetchall()
        return [_transaction_from_row(row, native_model) for row in rows]

    def save_snapshot(
        self, network: str, record: AccountRecord | AssetRecord | ContractRecord
    ) -> None:
        item_id = record.address if isinstance(record, AccountRecord) else record.id
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)",
                (record.chain, network, type(record).__name__, item_id, record.model_dump_json()),
            )

    def load_checkpoint(self, chain: str, network: str, account: str) -> int | None:
        row = self._connection.execute(
            "SELECT checkpoint FROM sync_state WHERE chain = ? AND network = ? AND account = ?",
            (chain, network, account),
        ).fetchone()
        return None if row is None else int(row[0])

    def save_checkpoint(self, chain: str, network: str, account: str, height: int) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO sync_state VALUES (?, ?, ?, ?)",
                (chain, network, account, height),
            )

    def close(self) -> None:
        self._connection.close()


def _transaction_row(network: str, record: TransactionRecord) -> tuple[str | None, ...]:
    return (
        record.chain,
        network,
        record.id,
        record.time.isoformat(),
        record.sender,
        record.receiver,
        str(record.amount),
        str(record.fee),
        record.status.value,
        record.native.model_dump_json(),
    )


def _transaction_from_row(
    row: tuple[str, ...], native_model: type[NativeData]
) -> TransactionRecord:
    tx_id, time, sender, receiver, amount, fee, status, chain, native = row
    return TransactionRecord(
        id=tx_id,
        time=datetime.fromisoformat(time),
        sender=sender,
        receiver=receiver,
        amount=int(amount),
        fee=int(fee),
        status=Status(status),
        chain=chain,
        native=native_model.model_validate_json(native),
    )
