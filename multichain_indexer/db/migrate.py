"""Brings a database to the current schema with Alembic before anything reads it.

A database made before migrations existed, by 0.6.0 or earlier, has the baseline tables
but no ``alembic_version``; it is stamped at the baseline and then upgraded, so not a row
is touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError

from multichain_indexer.core.errors import ConfigError

MIGRATIONS = Path(__file__).parent / "migrations"
BASELINE = "0001"
BASELINE_TABLE = "transactions"
VERSION_TABLE = "alembic_version"


def upgrade(
    sqlalchemy_url: str,
    *,
    redact: tuple[str, ...] = (),
    connect_args: Mapping[str, object] | None = None,
) -> None:
    """Migrate the database at ``sqlalchemy_url`` to the newest revision.

    Args:
        sqlalchemy_url: The database, in SQLAlchemy's URL form.
        redact: Values, such as a password, that must not appear in an error message.
        connect_args: Passed to the database driver, such as a connect timeout.

    Raises:
        ConfigError: The database cannot be reached or migrated.
    """
    engine = create_engine(sqlalchemy_url, connect_args=dict(connect_args or {}))
    try:
        with engine.begin() as connection:
            config = _config(connection)
            if _made_before_migrations(connection):
                command.stamp(config, BASELINE)
            command.upgrade(config, "head")
    except SQLAlchemyError as exc:
        raise ConfigError(f"cannot migrate the database: {_first_line(exc, redact)}") from None
    finally:
        engine.dispose()


def head_revision() -> str:
    """Return the newest revision this version of the package migrates to."""
    head = ScriptDirectory(str(MIGRATIONS)).get_current_head()
    if head is None:
        raise ConfigError("the package ships no database migrations")
    return head


def current_revision(
    sqlalchemy_url: str,
    *,
    redact: tuple[str, ...] = (),
    connect_args: Mapping[str, object] | None = None,
) -> str | None:
    """Return the revision the database is at, or None if it has never been migrated.

    Only reads, so a read-only database user or connection is enough.

    Raises:
        ConfigError: The database cannot be reached.
    """
    engine = create_engine(sqlalchemy_url, connect_args=dict(connect_args or {}))
    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table(VERSION_TABLE):
                return None
            revision = connection.exec_driver_sql(f"SELECT version_num FROM {VERSION_TABLE}")
            return revision.scalar()
    except SQLAlchemyError as exc:
        raise ConfigError(f"cannot read the database: {_first_line(exc, redact)}") from None
    finally:
        engine.dispose()


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    return config


def _made_before_migrations(connection: Connection) -> bool:
    tables = inspect(connection)
    return tables.has_table(BASELINE_TABLE) and not tables.has_table(VERSION_TABLE)


def _first_line(error: Exception, redact: tuple[str, ...]) -> str:
    text = str(getattr(error, "orig", None) or error).strip().splitlines()[0]
    for secret in redact:
        text = text.replace(secret, "***")
    return text
