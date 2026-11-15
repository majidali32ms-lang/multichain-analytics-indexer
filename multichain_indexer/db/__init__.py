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
from multichain_indexer.db.migrate import current_revision, head_revision, upgrade

DATABASE_URL_SETTING = "MCI_DATABASE_URL"
POSTGRES_SCHEMES = ("postgresql://", "postgres://")
SQLALCHEMY_POSTGRES = "postgresql+psycopg://"
_UPGRADE_HINT = (
    "serve never writes, so upgrade it first with any other command on the same database, "
    "such as: multichain-indexer --db <the same database> --chain all metrics"
)
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


def sqlalchemy_target(
    database: Database, *, read_only: bool = False
) -> tuple[str, dict[str, object]]:
    """Return the SQLAlchemy URL and driver arguments for ``database``.

    Args:
        read_only: Open a SQLite file so that nothing can be written to it.
    """
    if is_postgres(database):
        url = str(database)
        scheme = next(prefix for prefix in POSTGRES_SCHEMES if url.startswith(prefix))
        return SQLALCHEMY_POSTGRES + url.removeprefix(scheme), {
            "connect_timeout": CONNECT_TIMEOUT_SECONDS
        }
    path = Path(database).resolve().as_posix()
    if read_only:
        return f"sqlite:///file:{path}?mode=ro&uri=true", {}
    return f"sqlite:///{path}", {}


def open_storage(database: Database, *, migrate: bool = True) -> Storage:
    """Migrate ``database`` to the current schema, then open it.

    Args:
        migrate: Bring the schema up to date first. Turned off only where nothing may be
            written, and only after ``require_current_schema`` has passed.

    Raises:
        ConfigError: The database cannot be reached or migrated, or a PostgreSQL URL was
            given without the ``postgres`` extra installed.
    """
    if is_postgres(database):
        return _open_postgres(str(database), migrate=migrate)
    path = Path(database)
    if migrate:
        path.parent.mkdir(parents=True, exist_ok=True)
        upgrade(sqlalchemy_target(path)[0])
    return SQLiteStorage(path)


def require_current_schema(database: Database) -> None:
    """Check, without writing anything, that ``database`` is at the current schema.

    Raises:
        ConfigError: It is missing, older or newer than this version expects; the message
            says how to upgrade it. Or it cannot be reached.
    """
    head = head_revision()
    if not is_postgres(database) and not Path(database).is_file():
        raise ConfigError(f"no database at {database}; {_UPGRADE_HINT}")
    url, connect_args = sqlalchemy_target(database, read_only=True)
    found = current_revision(url, redact=database_secrets(database), connect_args=connect_args)
    if found != head:
        raise ConfigError(
            f"the database is at schema revision {found or 'none'} and this version needs "
            f"{head}; {_UPGRADE_HINT}"
        )


def _open_postgres(url: str, *, migrate: bool) -> Storage:
    try:
        from multichain_indexer.db.postgres import PostgresStorage
    except ImportError:
        raise ConfigError(
            "PostgreSQL needs the postgres extra: "
            "pip install 'multichain-analytics-indexer[postgres]'"
        ) from None
    redact = database_secrets(url)
    if migrate:
        sqlalchemy_url, connect_args = sqlalchemy_target(url)
        upgrade(sqlalchemy_url, redact=redact, connect_args=connect_args)
    return PostgresStorage(url, connect_timeout=CONNECT_TIMEOUT_SECONDS, redact=redact)
