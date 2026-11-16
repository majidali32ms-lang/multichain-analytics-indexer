"""The read-only HTTP API gives what the command line gives, and only to polite clients."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import SecretStr

from multichain_indexer import CrossChain, Indexer, db, server
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError
from multichain_indexer.db import open_storage
from multichain_indexer.server import ClientRateLimit, create_app
from tests.bitcoin_support import SMALL, serve_small
from tests.conftest import FakeNode
from tests.test_migrations import RELEASED_DUMP

ALL_TIME = 36500


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def client(tmp_path: Path, working_database: Path) -> TestClient:
    settings = Settings(database=tmp_path / "unused.db")
    return TestClient(create_app(settings=settings, database=working_database))


def cli_json(database: Path, tmp_path: Path, *arguments: str) -> Any:
    options = ["--db", str(database), "--env-file", str(tmp_path / "no.env")]
    options += ["--config", str(tmp_path / "no.toml")]
    return json.loads(CliRunner().invoke(cli, [*options, *arguments]).stdout)


def test_health_and_the_chains_it_can_read(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    chains = client.get("/chains").json()
    assert "bitcoin" in chains["available"]
    assert chains["stored"] == [
        {"chain": "algorand", "network": "testnet"},
        {"chain": "bitcoin", "network": "mainnet"},
    ]


def test_metrics_are_the_ones_the_command_line_prints(
    client: TestClient,
    tmp_path: Path,
    working_database: Path,
) -> None:
    served = client.get("/chains/algorand/testnet/metrics", params={"days": ALL_TIME}).json()
    printed = cli_json(working_database, tmp_path, "metrics", "--days", str(ALL_TIME))
    assert served == printed
    everything = client.get("/metrics", params={"days": ALL_TIME}).json()
    printed_all = cli_json(
        working_database, tmp_path, "--chain", "all", "metrics", "--days", str(ALL_TIME), "--json"
    )
    assert everything["rows"] == printed_all["rows"]


def test_transactions_come_in_pages_with_amounts_as_text(
    client: TestClient,
    tmp_path: Path,
    working_database: Path,
) -> None:
    first = client.get("/chains/algorand/testnet/transactions", params={"limit": 1}).json()
    assert (first["total"], first["offset"], first["next_offset"]) == (2, 0, 1)
    second = client.get(
        "/chains/algorand/testnet/transactions", params={"limit": 1, "offset": 1}
    ).json()
    assert second["next_offset"] is None
    items = first["items"] + second["items"]
    with Indexer(
        "algorand", settings=Settings(database=tmp_path / "u.db"), database=working_database
    ) as indexer:
        stored = indexer.stored_transactions()
    assert [item["id"] for item in items] == [record.id for record in stored]
    assert [item["fee"] for item in items] == [str(record.fee) for record in stored]
    assert all(isinstance(item["amount"], str) for item in items)


def test_by_default_an_account_is_the_state_index_account_stored(
    node: FakeNode, btc_settings: Settings, tmp_path: Path
) -> None:
    serve_small(node)
    database = tmp_path / "a.db"
    with Indexer("bitcoin", settings=btc_settings, database=database) as indexer:
        stored = indexer.account(SMALL).model_dump(mode="json")
    asked_before = len(node.requests)
    client = TestClient(create_app(settings=btc_settings, database=database))
    assert client.get(f"/chains/bitcoin/testnet4/accounts/{SMALL}").json() == stored
    assert len(node.requests) == asked_before


def test_an_account_never_looked_up_is_not_found_with_a_hint(client: TestClient) -> None:
    reply = client.get(f"/chains/bitcoin/testnet4/accounts/{SMALL}")
    assert reply.status_code == 404
    detail = reply.json()["detail"]
    assert f"no stored state for {SMALL} on bitcoin testnet4" in detail
    assert f"multichain-indexer --chain bitcoin --network testnet4 index account {SMALL}" in detail
    assert "--live-accounts" in detail
    unknown = client.get(f"/chains/dogecoin/mainnet/accounts/{SMALL}")
    assert (unknown.status_code, unknown.json()["detail"]) == (404, "unknown chain 'dogecoin'")


def test_by_default_no_route_reaches_the_network_or_writes_to_the_database(
    node: FakeNode, tmp_path: Path, working_database: Path
) -> None:
    env = {
        "ALGORAND_TESTNET_INDEXER_SERVER": SecretStr(node.url),
        "BITCOIN_MAINNET_ESPLORA_SERVER": SecretStr(node.url),
    }
    settings = Settings(database=tmp_path / "unused.db", env=env)
    client = TestClient(create_app(settings=settings, database=working_database))
    before = hashlib.sha256(working_database.read_bytes()).hexdigest()
    routes = [
        "/health",
        "/chains",
        "/metrics",
        "/chains/algorand/testnet/metrics",
        "/chains/algorand/testnet/transactions",
        "/chains/bitcoin/mainnet/transactions",
        f"/chains/bitcoin/mainnet/accounts/{SMALL}",
        "/chains/algorand/testnet/accounts/ALGORANDACCOUNT",
    ]
    assert [client.get(route).status_code for route in routes] == [200] * 6 + [404, 404]
    assert node.requests == []
    assert hashlib.sha256(working_database.read_bytes()).hexdigest() == before


def test_live_accounts_read_the_chain_and_store_the_state_as_index_account_does(
    node: FakeNode, btc_settings: Settings, tmp_path: Path
) -> None:
    serve_small(node)
    database = tmp_path / "a.db"
    open_storage(database).close()
    live = TestClient(create_app(settings=btc_settings, database=database, live_accounts=True))
    served = live.get(f"/chains/bitcoin/testnet4/accounts/{SMALL}").json()
    with Indexer("bitcoin", settings=btc_settings, database=tmp_path / "b.db") as indexer:
        looked_up = indexer.account(SMALL).model_dump(mode="json")
    stored = TestClient(create_app(settings=btc_settings, database=database)).get(
        f"/chains/bitcoin/testnet4/accounts/{SMALL}"
    )
    assert stored.json() == served
    for reading in (served, looked_up):
        reading["native"]["quantum_exposure"].pop("observed_at")
    assert served == looked_up
    refused = live.get("/chains/bitcoin/testnet4/accounts/not-an-address")
    assert refused.status_code == 400


@pytest.mark.parametrize(
    ("path", "status", "detail"),
    [
        ("/chains/dogecoin/mainnet/metrics", 404, "unknown chain 'dogecoin'"),
        ("/chains/bitcoin/regtest/metrics", 400, "unknown Bitcoin network"),
    ],
)
def test_errors_carry_a_status_and_the_message(
    client: TestClient, path: str, status: int, detail: str
) -> None:
    reply = client.get(path)
    assert reply.status_code == status
    assert detail in reply.json()["detail"]


def test_nothing_can_be_written_and_bad_queries_are_refused(client: TestClient) -> None:
    assert client.post("/chains").status_code == 405
    assert client.delete("/chains/algorand/testnet/transactions").status_code == 405
    assert client.get("/metrics", params={"days": 0}).status_code == 422
    too_many = {"limit": 1001}
    assert client.get("/chains/algorand/testnet/transactions", params=too_many).status_code == 422


def test_a_network_error_is_a_bad_gateway(tmp_path: Path) -> None:
    settings = Settings(
        database=tmp_path / "d.db",
        max_retries=0,
        request_timeout=1,
        env={"BITCOIN_TESTNET4_ESPLORA_SERVER": SecretStr("http://127.0.0.1:9")},
    )
    open_storage(settings.database).close()
    reply = TestClient(create_app(settings=settings, live_accounts=True)).get(
        f"/chains/bitcoin/testnet4/accounts/{SMALL}"
    )
    assert reply.status_code == 502


def test_each_client_is_held_to_its_rate(tmp_path: Path, working_database: Path) -> None:
    clock = Clock()
    app = create_app(
        settings=Settings(database=tmp_path / "u.db"),
        database=working_database,
        requests_per_minute=2,
        clock=clock,
    )
    client = TestClient(app)
    assert [client.get("/health").status_code for _ in range(2)] == [200, 200]
    refused = client.get("/health")
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "30"
    clock.now += 30
    assert client.get("/health").status_code == 200


def test_the_allowance_refills_steadily_and_is_kept_per_client() -> None:
    clock = Clock()
    limit = ClientRateLimit(60, clock)
    assert all(limit.wait_seconds("a") == 0 for _ in range(60))
    assert limit.wait_seconds("a") == pytest.approx(1)
    assert limit.wait_seconds("b") == 0
    clock.now += 1
    assert limit.wait_seconds("a") == 0
    with pytest.raises(ValueError, match="at least 1"):
        ClientRateLimit(0)


def test_openapi_lists_every_route(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/health",
        "/chains",
        "/metrics",
        "/chains/{chain}/{network}/metrics",
        "/chains/{chain}/{network}/transactions",
        "/chains/{chain}/{network}/accounts/{address}",
    }
    assert all(set(methods) == {"get"} for methods in paths.values())
    assert client.get("/docs").status_code == 200


def test_serve_listens_on_localhost_unless_told_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append(options))
    open_storage(tmp_path / "s.db").close()
    options = ["--db", str(tmp_path / "s.db"), "--env-file", str(tmp_path / "no.env")]
    local = CliRunner().invoke(cli, [*options, "serve"])
    assert local.exit_code == 0, local.output
    assert (calls[0]["host"], calls[0]["port"]) == ("127.0.0.1", 8000)
    assert local.stderr == ""
    shared = CliRunner().invoke(
        cli, [*options, "-q", "serve", "--host", "0.0.0.0", "--port", "9100"]
    )
    assert (calls[1]["host"], calls[1]["port"], calls[1]["log_level"]) == (
        "0.0.0.0",
        9100,
        "warning",
    )
    assert "anyone who can reach it can read this database" in shared.stderr


def test_serve_without_the_extra_says_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    result = CliRunner().invoke(cli, ["--env-file", str(tmp_path / "no.env"), "serve"])
    assert result.exit_code == 1
    assert "pip install 'multichain-analytics-indexer[serve]'" in result.output


def test_cross_chain_metrics_match_the_library(
    client: TestClient,
    tmp_path: Path,
    working_database: Path,
) -> None:
    with CrossChain(
        settings=Settings(database=tmp_path / "u.db"), database=working_database
    ) as everything:
        rows = everything.metrics(ALL_TIME).model_dump(mode="json")["rows"]
    assert client.get("/metrics", params={"days": ALL_TIME}).json()["rows"] == rows


def test_serve_passes_live_accounts_and_reports_a_bad_database_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []
    real_create_app = server.create_app

    def recording_create_app(**options: Any) -> Any:
        built.append(options)
        return real_create_app(**options)

    monkeypatch.setattr(server, "create_app", recording_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: None)
    open_storage(tmp_path / "s.db").close()
    options = ["--db", str(tmp_path / "s.db"), "--env-file", str(tmp_path / "no.env")]
    assert CliRunner().invoke(cli, [*options, "serve"]).exit_code == 0
    assert CliRunner().invoke(cli, [*options, "serve", "--live-accounts"]).exit_code == 0
    assert [call["live_accounts"] for call in built] == [False, True]

    monkeypatch.setattr(db, "CONNECT_TIMEOUT_SECONDS", 1)
    url = "postgresql://indexer:not-a-real-password@127.0.0.1:1/indexer"
    failed = CliRunner().invoke(cli, ["--db", url, "--env-file", str(tmp_path / "no.env"), "serve"])
    assert failed.exit_code == 1
    assert "cannot read the database" in failed.output
    assert "not-a-real-password" not in failed.output
    assert "Traceback" not in failed.output


def test_serve_never_migrates_and_refuses_an_older_schema(tmp_path: Path) -> None:
    older = tmp_path / "older.db"
    connection = sqlite3.connect(older)
    connection.executescript(RELEASED_DUMP.read_text(encoding="utf-8"))
    connection.close()
    before = hashlib.sha256(older.read_bytes()).hexdigest()
    with pytest.raises(ConfigError) as refused:
        create_app(settings=Settings(database=tmp_path / "unused.db"), database=older)
    message = str(refused.value)
    assert "schema revision none and this version needs 0001" in message
    assert "multichain-indexer --db <the same database> --chain all metrics" in message
    assert hashlib.sha256(older.read_bytes()).hexdigest() == before

    upgraded = CliRunner().invoke(
        cli, ["--db", str(older), "--env-file", str(tmp_path / "e"), "--chain", "all", "metrics"]
    )
    assert upgraded.exit_code == 0
    create_app(settings=Settings(database=tmp_path / "unused.db"), database=older)


def test_serve_refuses_a_database_that_does_not_exist_and_creates_none(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(ConfigError, match="no database at"):
        create_app(settings=Settings(database=tmp_path / "unused.db"), database=missing)
    assert not missing.exists()
    result = CliRunner().invoke(
        cli, ["--db", str(missing), "--env-file", str(tmp_path / "e"), "serve"]
    )
    assert result.exit_code == 1
    assert "no database at" in result.output
    assert "Traceback" not in result.output


def test_no_request_migrates_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*arguments: object, **options: object) -> None:
        raise AssertionError("serve tried to migrate the database")

    monkeypatch.setattr(db, "upgrade", refuse)
    routes = ["/chains", "/metrics", "/chains/algorand/testnet/metrics"]
    routes.append("/chains/algorand/testnet/transactions")
    assert [client.get(route).status_code for route in routes] == [200, 200, 200, 200]
