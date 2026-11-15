"""Alembic brings every database to the current schema, and a 0.6.0 database loses nothing.

``fixtures/db/indexer-0.6.0.sql`` is the complete SQL dump of a database written by the
released 0.6.0 package from PyPI, in a fresh virtualenv, by syncing small accounts on
Algorand testnet, Bitcoin testnet4, NEAR testnet, Solana devnet and Ethereum Sepolia, then
looking up one Algorand account. The repository keeps no database files, so each test
rebuilds that database from the dump.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from multichain_indexer import CrossChain, Indexer, db
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError
from multichain_indexer.core.storage import SQLiteStorage
from multichain_indexer.db import chosen_database, database_secrets, open_storage
from multichain_indexer.db.migrate import BASELINE, current_revision, upgrade
from multichain_indexer.db.snapshots import stored_account

RELEASED_DUMP = Path(__file__).parent / "fixtures" / "db" / "indexer-0.6.0.sql"
RELEASED_DUMP_SHA256 = "5d907ba97ccefb83bc86eb0c79dd59806ec1507a65c79f17b82232483b89f2ee"
TABLES = ("transactions", "account_transactions", "snapshots", "sync_state")
RELEASED_COUNTS = {
    ("algorand", "testnet"): 108,
    ("bitcoin", "testnet4"): 4,
    ("ethereum", "testnet"): 20,
    ("near", "testnet"): 20,
    ("solana", "devnet"): 6,
}
PASSWORD = "not-a-real-password"


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def dump(path: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall())
            for table in TABLES
        }
    finally:
        connection.close()


def schema(path: Path) -> list[tuple[str, str]]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND name != 'alembic_version' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    return [(name, " ".join(sql.replace("IF NOT EXISTS ", "").split())) for name, sql in rows]


@pytest.fixture
def released(tmp_path: Path) -> Path:
    path = tmp_path / "indexer-0.6.0.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(RELEASED_DUMP.read_text(encoding="utf-8"))
    finally:
        connection.close()
    return path


def test_the_fixture_is_the_dump_of_the_database_0_6_0_wrote(released: Path) -> None:
    dump_text = RELEASED_DUMP.read_text(encoding="utf-8")
    assert hashlib.sha256(dump_text.encode()).hexdigest() == RELEASED_DUMP_SHA256
    assert current_revision(sqlite_url(released)) is None


def test_a_0_6_0_database_is_stamped_and_keeps_every_row(released: Path) -> None:
    before = dump(released)
    assert sum(len(rows) for rows in before.values()) == 158 + 158 + 1 + 5

    open_storage(released).close()

    assert current_revision(sqlite_url(released)) == BASELINE
    assert dump(released) == before


def test_a_stamped_0_6_0_database_still_reads_and_syncs_from_its_checkpoint(
    released: Path, tmp_path: Path
) -> None:
    settings = Settings(database=tmp_path / "unused.db")
    with CrossChain(settings=settings, database=released) as everything:
        rows = everything.metrics(days=36500).rows
    assert {(row.chain, row.network): row.transactions for row in rows} == RELEASED_COUNTS
    assert all(row.error is None for row in rows)
    with Indexer("bitcoin", settings=settings, database=released) as indexer:
        stored = indexer.stored_transactions(account="mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu")
    assert len(stored) == 4
    storage = SQLiteStorage(released)
    try:
        state = storage.load_sync_state("bitcoin", "testnet4", "mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu")
    finally:
        storage.close()
    assert (state.checkpoint, state.backfill) == (161489, None)


def test_migrating_twice_changes_nothing(released: Path) -> None:
    open_storage(released).close()
    after_first = dump(released)
    open_storage(released).close()
    assert dump(released) == after_first
    assert current_revision(sqlite_url(released)) == BASELINE


def test_a_new_database_gets_exactly_the_schema_earlier_releases_made(
    tmp_path: Path, released: Path
) -> None:
    migrated, plain = tmp_path / "migrated.db", tmp_path / "plain.db"
    open_storage(migrated).close()
    SQLiteStorage(plain).close()
    assert current_revision(sqlite_url(migrated)) == BASELINE
    assert schema(migrated) == schema(plain)
    assert schema(released) == schema(plain)


def test_a_database_url_comes_from_the_argument_then_the_setting_then_the_file(
    tmp_path: Path,
) -> None:
    url = f"postgresql://indexer:{PASSWORD}@db.example/indexer"
    with_url = Settings(database=tmp_path / "f.db", env={"MCI_DATABASE_URL": SecretStr(url)})
    assert chosen_database(with_url) == url
    assert chosen_database(with_url, tmp_path / "x.db") == tmp_path / "x.db"
    assert chosen_database(Settings(database=tmp_path / "f.db")) == tmp_path / "f.db"
    assert database_secrets(url) == (PASSWORD,)
    assert database_secrets("postgresql://indexer@db.example/indexer") == ()
    assert database_secrets(tmp_path / "f.db") == ()


def test_postgresql_without_the_extra_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "multichain_indexer.db.postgres", None)
    with pytest.raises(
        ConfigError, match=r"pip install 'multichain-analytics-indexer\[postgres\]'"
    ):
        open_storage("postgresql://indexer@127.0.0.1:1/indexer")


def test_an_unreachable_database_is_reported_without_its_password() -> None:
    url = f"postgresql+psycopg://indexer:{PASSWORD}@127.0.0.1:1/indexer"
    with pytest.raises(ConfigError, match="cannot migrate the database") as failed:
        upgrade(url, redact=(PASSWORD,), connect_args={"connect_timeout": 1})
    assert PASSWORD not in str(failed.value)


def test_the_command_line_hides_the_password_of_an_unreachable_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "CONNECT_TIMEOUT_SECONDS", 1)
    url = f"postgresql://indexer:{PASSWORD}@127.0.0.1:1/indexer"
    result = CliRunner().invoke(
        cli,
        ["--chain", "bitcoin", "--db", url, "--env-file", str(tmp_path / "no.env"), "metrics"],
    )
    assert result.exit_code == 1
    assert "cannot migrate the database" in result.output
    assert PASSWORD not in result.output
    assert "Traceback" not in result.output


def test_reading_a_snapshot_never_creates_a_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(ConfigError, match="cannot read the database"):
        stored_account(missing, "bitcoin", "testnet4", "tb1q")
    assert not missing.exists()


def test_reading_a_snapshot_hides_the_database_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "CONNECT_TIMEOUT_SECONDS", 1)
    url = f"postgresql://indexer:{PASSWORD}@127.0.0.1:1/indexer"
    with pytest.raises(ConfigError, match="cannot read the database") as failed:
        stored_account(url, "bitcoin", "testnet4", "tb1q")
    assert PASSWORD not in str(failed.value)
