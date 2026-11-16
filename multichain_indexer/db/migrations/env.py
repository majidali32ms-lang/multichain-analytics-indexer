"""Runs migrations on the connection ``multichain_indexer.db.migrate`` hands over."""

from __future__ import annotations

from alembic import context

context.configure(connection=context.config.attributes["connection"])
with context.begin_transaction():
    context.run_migrations()
