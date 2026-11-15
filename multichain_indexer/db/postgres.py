"""PostgreSQL behind the same ``Storage`` interface as SQLite, through psycopg 3.

Needs the ``postgres`` extra: ``pip install multichain-analytics-indexer[postgres]``.
Amounts are NUMERIC, times TIMESTAMPTZ and ``native`` JSONB, so the tables can be queried
directly with SQL.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from multichain_indexer.core.errors import ConfigError
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    NativeData,
    Status,
    TransactionRecord,
)
from multichain_indexer.core.storage import Backfill, Storage, SyncState

_UPSERT_TRANSACTION = """
INSERT INTO transactions
    (chain, network, tx_id, time, sender, receiver, amount, fee, status, native)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (chain, network, tx_id) DO UPDATE SET
    time = excluded.time,
    sender = excluded.sender,
    receiver = excluded.receiver,
    amount = excluded.amount,
    fee = excluded.fee,
    status = excluded.status,
    native = excluded.native
"""

_LINK_ACCOUNT = """
INSERT INTO account_transactions VALUES (%s, %s, %s, %s)
ON CONFLICT (chain, network, account, tx_id) DO NOTHING
"""

_SELECT_TRANSACTIONS = """
SELECT t.tx_id, t.time, t.sender, t.receiver, t.amount, t.fee, t.status, t.chain, t.network,
       t.native::text
FROM transactions t
"""

_JOIN_ACCOUNT = """
JOIN account_transactions a
  ON a.chain = t.chain AND a.network = t.network AND a.tx_id = t.tx_id AND a.account = %s
"""

_SAVE_SNAPSHOT = """
INSERT INTO snapshots VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (chain, network, kind, item_id) DO UPDATE SET record = excluded.record
"""

_SAVE_BACKFILL = """
INSERT INTO sync_state (chain, network, account, backfill_from, backfill_to, backfill_cursor)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (chain, network, account) DO UPDATE SET
    backfill_from = excluded.backfill_from,
    backfill_to = excluded.backfill_to,
    backfill_cursor = excluded.backfill_cursor
"""

_FINISH_SYNC = """
INSERT INTO sync_state (chain, network, account, checkpoint)
VALUES (%s, %s, %s, %s)
ON CONFLICT (chain, network, account) DO UPDATE SET
    checkpoint = excluded.checkpoint,
    backfill_from = NULL,
    backfill_to = NULL,
    backfill_cursor = NULL
"""

_LOAD_SYNC_STATE = """
SELECT checkpoint, backfill_from, backfill_to, backfill_cursor
FROM sync_state
WHERE chain = %s AND network = %s AND account = %s
"""

_STORED_NETWORKS = "SELECT DISTINCT chain, network FROM transactions ORDER BY chain, network"


class PostgresStorage(Storage):
    """Stores records in a PostgreSQL database already migrated to the current schema.

    Raises:
        ConfigError: The database cannot be reached. The message never holds the password.
    """

    def __init__(self, url: str, *, connect_timeout: int, redact: tuple[str, ...] = ()) -> None:
        try:
            self._connection = psycopg.connect(
                url, autocommit=True, connect_timeout=connect_timeout
            )
        except psycopg.Error as exc:
            message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            for secret in redact:
                message = message.replace(secret, "***")
            raise ConfigError(f"cannot open the PostgreSQL database: {message}") from None
        self._connection.execute("SET TIME ZONE 'UTC'")

    def save_transactions(
        self,
        chain: str,
        network: str,
        account: str,
        records: Iterable[TransactionRecord],
        *,
        backfill: Backfill | None = None,
    ) -> int:
        saved = 0
        with self._connection.transaction():
            for record in records:
                self._connection.execute(_UPSERT_TRANSACTION, _transaction_row(record))
                self._connection.execute(_LINK_ACCOUNT, (chain, network, account, record.id))
                saved += 1
            if backfill is not None:
                self._connection.execute(
                    _SAVE_BACKFILL,
                    (
                        chain,
                        network,
                        account,
                        backfill.from_height,
                        backfill.to_height,
                        backfill.cursor,
                    ),
                )
        return saved

    def load_transactions(
        self,
        chain: str,
        network: str,
        native_model: type[NativeData],
        *,
        account: str | None = None,
        since: datetime | None = None,
        status: Status | None = None,
    ) -> list[TransactionRecord]:
        query = _SELECT_TRANSACTIONS
        arguments: list[object] = []
        if account is not None:
            query += _JOIN_ACCOUNT
            arguments.append(account)
        query += " WHERE t.chain = %s AND t.network = %s"
        arguments += [chain, network]
        if since is not None:
            query += " AND t.time >= %s"
            arguments.append(since)
        if status is not None:
            query += " AND t.status = %s"
            arguments.append(status.value)
        query += " ORDER BY t.time DESC, t.tx_id"
        rows = self._connection.execute(query, arguments).fetchall()
        return [_transaction_from_row(row, native_model) for row in rows]

    def stored_networks(self) -> list[tuple[str, str]]:
        rows = self._connection.execute(_STORED_NETWORKS).fetchall()
        return [(chain, network) for chain, network in rows]

    def save_snapshot(self, record: AccountRecord | AssetRecord | ContractRecord) -> None:
        item_id = record.address if isinstance(record, AccountRecord) else record.id
        self._connection.execute(
            _SAVE_SNAPSHOT,
            (
                record.chain,
                record.network,
                type(record).__name__,
                item_id,
                Jsonb(record.model_dump(mode="json")),
            ),
        )

    def load_sync_state(self, chain: str, network: str, account: str) -> SyncState:
        row = self._connection.execute(_LOAD_SYNC_STATE, (chain, network, account)).fetchone()
        if row is None:
            return SyncState()
        checkpoint, backfill_from, backfill_to, cursor = row
        backfill = None if backfill_to is None else Backfill(backfill_from, backfill_to, cursor)
        return SyncState(checkpoint=checkpoint, backfill=backfill)

    def finish_sync(self, chain: str, network: str, account: str, height: int) -> None:
        self._connection.execute(_FINISH_SYNC, (chain, network, account, height))

    def close(self) -> None:
        self._connection.close()


def _transaction_row(record: TransactionRecord) -> tuple[object, ...]:
    return (
        record.chain,
        record.network,
        record.id,
        record.time,
        record.sender,
        record.receiver,
        Decimal(record.amount),
        Decimal(record.fee),
        record.status.value,
        Jsonb(record.native.model_dump(mode="json")),
    )


def _transaction_from_row(
    row: tuple[Any, ...], native_model: type[NativeData]
) -> TransactionRecord:
    tx_id, time, sender, receiver, amount, fee, status, chain, network, native = row
    return TransactionRecord(
        id=tx_id,
        time=time,
        sender=sender,
        receiver=receiver,
        amount=int(amount),
        fee=int(fee),
        status=Status(status),
        chain=chain,
        network=network,
        native=native_model.model_validate_json(native),
    )
