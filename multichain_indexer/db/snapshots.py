"""Reads an account's stored state, as ``index account`` last saved it, and writes nothing.

A SQLite file is opened read-only, so not even an accidental write could reach it.
"""

from __future__ import annotations

import json

from pydantic import JsonValue
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from multichain_indexer.core.errors import ConfigError
from multichain_indexer.db import Database, database_secrets, sqlalchemy_target

ACCOUNT_KIND = "AccountRecord"

_SELECT_SNAPSHOT = text(
    "SELECT record FROM snapshots "
    "WHERE chain = :chain AND network = :network AND kind = :kind AND item_id = :item_id"
)


def stored_account(
    database: Database, chain: str, network: str, address: str
) -> dict[str, JsonValue] | None:
    """Return the stored account record as JSON, or None if it was never looked up.

    Raises:
        ConfigError: The database cannot be read. The message never holds a password.
    """
    url, connect_args = sqlalchemy_target(database, read_only=True)
    engine = create_engine(url, connect_args=connect_args)
    parameters = {"chain": chain, "network": network, "kind": ACCOUNT_KIND, "item_id": address}
    try:
        with engine.connect() as connection:
            record = connection.execute(_SELECT_SNAPSHOT, parameters).scalar()
    except SQLAlchemyError as exc:
        message = str(getattr(exc, "orig", None) or exc).strip().splitlines()[0]
        for secret in database_secrets(database):
            message = message.replace(secret, "***")
        raise ConfigError(f"cannot read the database: {message}") from None
    finally:
        engine.dispose()
    if record is None:
        return None
    loaded: dict[str, JsonValue] = json.loads(record) if isinstance(record, str) else record
    return loaded
