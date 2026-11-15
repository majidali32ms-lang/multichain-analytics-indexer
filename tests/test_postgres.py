"""PostgreSQL end to end: migrated schema, a real sync, and the same answers as SQLite.

Skipped unless ``MCI_TEST_DATABASE_URL`` names a test database, as it does in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from multichain_indexer import Indexer
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.db import SQLALCHEMY_POSTGRES, open_storage
from multichain_indexer.db.migrate import BASELINE, current_revision
from tests.bitcoin_support import SMALL, serve_small
from tests.conftest import FakeNode
from tests.postgres_support import postgres_url

pytestmark = pytest.mark.filterwarnings("error")


def sqlalchemy_url(url: str) -> str:
    return SQLALCHEMY_POSTGRES + url.split("://", 1)[1]


def test_a_new_database_is_migrated_with_postgresql_types() -> None:
    import psycopg

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
