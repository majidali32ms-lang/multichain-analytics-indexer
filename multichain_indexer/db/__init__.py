"""Opens the database a user chose, migrated to the current schema first.

A database is a SQLite file path, the default, or a PostgreSQL URL such as
``postgresql://user@host/indexer``. The URL comes from ``--db``, the ``database`` argument
or ``MCI_DATABASE_URL``; a URL with a password belongs in ``.env``, never in config.toml.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError
from multichain_indexer.core.storage import SQLiteStorage, Storage
from multichain_indexer.db.migrate import upgrade

DATABASE_URL_SETTING = "MCI_DATABASE_URL"
POSTGRES_SCHEMES = ("postgresql://", "postgres://")
SQLALCHEMY_POSTGRES = "postgresql+psycopg://"
# A server that drops packets instead of refusing them would otherwise hang a connect.
CONNECT_TIMEOUT_SECONDS = 10

Database = Path | str


def chosen_database(settings: Settings, database: Database | None = None) -> Database:
    """Return ``database`` if given, else ``MCI_DATABASE_URL``, else the configured file."""
    if database is not None:
        return database
    url = settings.env.get(DATABASE_URL_SETTING)
    return url.get_secret_value() if url is not None else settings.database


def is_postgres(database: Database) -> bool:
    return isinstance(database, str) and database.startswith(POSTGRES_SCHEMES)


def database_secrets(database: Database) -> tuple[str, ...]:
    """Return the parts of a database URL that logs and errors must never show."""
    if not is_postgres(database):
        return ()
    password = urlsplit(str(database)).password
    return (password,) if password else ()


def open_storage(database: Database) -> Storage:
    """Migrate ``database`` to the current schema, then open it.

    Raises:
        ConfigError: The database cannot be reached or migrated, or a PostgreSQL URL was
            given without the ``postgres`` extra installed.
    """
    if is_postgres(database):
        return _open_postgres(str(database))
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    upgrade(f"sqlite:///{path.resolve().as_posix()}")
    return SQLiteStorage(path)


def _open_postgres(url: str) -> Storage:
    try:
        from multichain_indexer.db.postgres import PostgresStorage
    except ImportError:
        raise ConfigError(
            "PostgreSQL needs the postgres extra: "
            "pip install 'multichain-analytics-indexer[postgres]'"
        ) from None
    redact = database_secrets(url)
    scheme = next(prefix for prefix in POSTGRES_SCHEMES if url.startswith(prefix))
    timeout = {"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    upgrade(SQLALCHEMY_POSTGRES + url.removeprefix(scheme), redact=redact, connect_args=timeout)
    return PostgresStorage(url, connect_timeout=CONNECT_TIMEOUT_SECONDS, redact=redact)
