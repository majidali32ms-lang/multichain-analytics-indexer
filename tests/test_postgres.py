"""PostgreSQL end to end: migrated schema, a real sync, and the same answers as SQLite.

Skipped unless ``MCI_TEST_DATABASE_URL`` names a test database, as it does in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from multichain_indexer import Indexer
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError
from multichain_indexer.db import SQLALCHEMY_POSTGRES, open_storage
from multichain_indexer.db.migrate import BASELINE, current_revision
from multichain_indexer.db.postgres import PostgresStorage
from multichain_indexer.db.snapshots import stored_account
from multichain_indexer.server import create_app
from tests.bitcoin_support import SMALL, serve_small
from tests.conftest import FakeNode
from tests.postgres_support import postgres_url


def sqlalchemy_url(url: str) -> str:
    return SQLALCHEMY_POSTGRES + url.split("://", 1)[1]


def test_a_new_database_is_migrated_with_postgresql_types() -> None:
    url = postgres_url()
    open_storage(url).close()
    assert current_revision(sqlalchemy_url(url)) == BASELINE
    with psycopg.connect(url) as connection:
        types = dict(
            connection.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'transactions'"
            ).fetchall()
        )
    assert (types["amount"], types["time"], types["native"]) == (
        "numeric",
        "timestamp with time zone",
        "jsonb",
    )
    open_storage(url).close()
    assert current_revision(sqlalchemy_url(url)) == BASELINE


def test_a_sync_gives_the_same_records_and_metrics_as_sqlite(
    node: FakeNode, btc_settings: Settings, tmp_path: Path
) -> None:
    url = postgres_url()
    serve_small(node)
    results = {}
    for database in (url, tmp_path / "same.db"):
        with Indexer("bitcoin", settings=btc_settings, database=database) as indexer:
            synced = indexer.sync(SMALL)
            results[str(database)[:10]] = (
                synced.records_fetched,
                indexer.stored_transactions(account=SMALL),
                indexer.metrics(days=36500),
            )
    on_postgres, on_sqlite = results.values()
    assert on_postgres == on_sqlite
    assert on_postgres[0] > 0


def test_the_command_line_takes_a_postgresql_url(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = postgres_url()
    serve_small(node)
    monkeypatch.setenv("BITCOIN_TESTNET4_ESPLORA_SERVER", node.url)
    monkeypatch.setenv("MCI_MAX_RETRIES", "0")
    options = ["--chain", "bitcoin", "--db", url, "--env-file", str(tmp_path / "no.env")]
    assert CliRunner().invoke(cli, [*options, "sync", SMALL]).exit_code == 0
    report = json.loads(CliRunner().invoke(cli, [*options, "metrics", "--days", "36500"]).stdout)
    assert report["transactions"] > 0


def test_an_unreachable_server_is_reported_without_its_password() -> None:
    password = "not-a-real-password"
    with pytest.raises(ConfigError, match="cannot open the PostgreSQL database") as failed:
        PostgresStorage(
            f"postgresql://indexer:{password}@127.0.0.1:1/indexer",
            connect_timeout=1,
            redact=(password,),
        )
    assert password not in str(failed.value)


def test_a_snapshot_keeps_the_latest_state_as_jsonb(node: FakeNode, btc_settings: Settings) -> None:
    url = postgres_url()
    serve_small(node)
    with Indexer("bitcoin", settings=btc_settings, database=url) as indexer:
        indexer.account(SMALL)
        latest = indexer.account(SMALL)
    with psycopg.connect(url) as connection:
        rows = connection.execute(
            "SELECT kind, item_id, record->>'balance' FROM snapshots"
        ).fetchall()
    assert rows == [("AccountRecord", SMALL, str(latest.balance))]
    assert stored_account(url, "bitcoin", "testnet4", SMALL) == latest.model_dump(mode="json")
    served = TestClient(create_app(settings=btc_settings, database=url)).get(
        f"/chains/bitcoin/testnet4/accounts/{SMALL}"
    )
    assert served.json() == latest.model_dump(mode="json")


def test_serve_refuses_a_postgresql_database_that_was_never_migrated() -> None:
    url = postgres_url()
    with pytest.raises(ConfigError, match="schema revision none and this version needs 0001"):
        create_app(settings=Settings(), database=url)
    assert current_revision(sqlalchemy_url(url)) is None
