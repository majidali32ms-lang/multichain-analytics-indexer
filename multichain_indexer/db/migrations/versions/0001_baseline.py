"""The schema every release since 0.4.0 has used.

A database written before migrations existed already has these tables; it is stamped at
this revision, never created again. The SQLite statements are exactly the ones those
releases ran. PostgreSQL keeps the same tables in its own types: exact NUMERIC amounts,
TIMESTAMPTZ times and JSONB for the ``native`` block.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None

SQLITE = (
    """CREATE TABLE IF NOT EXISTS transactions (
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
)""",
    "CREATE INDEX IF NOT EXISTS transactions_by_time ON transactions (chain, network, time)",
    "CREATE INDEX IF NOT EXISTS transactions_by_sender "
    "ON transactions (chain, network, sender, time)",
    """CREATE TABLE IF NOT EXISTS account_transactions (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    account TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    PRIMARY KEY (chain, network, account, tx_id)
)""",
    """CREATE TABLE IF NOT EXISTS snapshots (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (chain, network, kind, item_id)
)""",
    """CREATE TABLE IF NOT EXISTS sync_state (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    account TEXT NOT NULL,
    checkpoint INTEGER,
    backfill_from INTEGER,
    backfill_to INTEGER,
    backfill_cursor TEXT,
    PRIMARY KEY (chain, network, account)
)""",
)

# NUMERIC(78, 0) holds any unsigned 256-bit integer, the largest amount any chain uses.
POSTGRESQL = (
    """CREATE TABLE transactions (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    time TIMESTAMPTZ NOT NULL,
    sender TEXT NOT NULL,
    receiver TEXT,
    amount NUMERIC(78, 0) NOT NULL,
    fee NUMERIC(78, 0) NOT NULL,
    status TEXT NOT NULL,
    native JSONB NOT NULL,
    PRIMARY KEY (chain, network, tx_id)
)""",
    "CREATE INDEX transactions_by_time ON transactions (chain, network, time)",
    "CREATE INDEX transactions_by_sender ON transactions (chain, network, sender, time)",
    """CREATE TABLE account_transactions (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    account TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    PRIMARY KEY (chain, network, account, tx_id)
)""",
    """CREATE TABLE snapshots (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    record JSONB NOT NULL,
    PRIMARY KEY (chain, network, kind, item_id)
)""",
    """CREATE TABLE sync_state (
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    account TEXT NOT NULL,
    checkpoint BIGINT,
    backfill_from BIGINT,
    backfill_to BIGINT,
    backfill_cursor TEXT,
    PRIMARY KEY (chain, network, account)
)""",
)


def upgrade() -> None:
    statements = SQLITE if op.get_bind().dialect.name == "sqlite" else POSTGRESQL
    for statement in statements:
        op.execute(statement)
