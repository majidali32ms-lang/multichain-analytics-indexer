from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from algosdk import account as algosdk_account
from click.testing import CliRunner, Result

from multichain_indexer import AsyncIndexer, Indexer, InvalidInput, UnsupportedChain
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from tests.conftest import FakeNode, Reply, load_fixture

ADDRESS = algosdk_account.generate_account()[1]
TRANSACTIONS_PATH = f"/v2/accounts/{ADDRESS}/transactions"
LOOKUP_PATH = f"/v2/accounts/{ADDRESS}"
HEAD = 1000

Invoke = Callable[..., Result]


def script_history(node: FakeNode, *fixtures: str) -> None:
    node.routes["/health"] = Reply({"round": HEAD})
    node.routes[TRANSACTIONS_PATH] = Reply(
        {"transactions": [load_fixture(name) for name in fixtures]}
    )


@pytest.fixture
def invoke(node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Invoke:
    monkeypatch.setenv("ALGORAND_TESTNET_INDEXER_SERVER", node.url)
    monkeypatch.setenv("MCI_MAX_RETRIES", "0")
    monkeypatch.setenv("MCI_REQUESTS_PER_SECOND", "1000")
    database = tmp_path / "cli.db"

    def run(*arguments: str) -> Result:
        options = ["--db", str(database), "--env-file", str(tmp_path / "none.env")]
        return CliRunner().invoke(cli, [*options, *arguments])

    return run


def test_sync_twice_adds_no_duplicates_and_only_asks_for_new_rounds(
    node: FakeNode, settings: Settings
) -> None:
    script_history(node, "tx_pay", "tx_axfer")
    with Indexer("algorand", settings=settings) as indexer:
        first = indexer.sync(ADDRESS)
        node.routes["/health"] = Reply({"round": HEAD + 5})
        second = indexer.sync(ADDRESS)
        stored = indexer.stored_transactions(account=ADDRESS)

    assert first.records_fetched == 2
    assert first.from_height is None
    assert second.from_height == HEAD + 1
    assert len(stored) == 2
    queries = node.requests_to(TRANSACTIONS_PATH)
    assert "min-round" not in queries[0]
    assert (queries[1]["min-round"], queries[1]["max-round"]) == (str(HEAD + 1), str(HEAD + 5))


def test_sync_with_no_new_rounds_asks_the_node_for_nothing(
    node: FakeNode, settings: Settings
) -> None:
    script_history(node, "tx_pay")
    with Indexer("algorand", settings=settings) as indexer:
        indexer.sync(ADDRESS)
        repeat = indexer.sync(ADDRESS)
    assert repeat.records_fetched == 0
    assert len(node.requests_to(TRANSACTIONS_PATH)) == 1


def test_checkpoint_survives_a_restart(node: FakeNode, settings: Settings) -> None:
    script_history(node, "tx_pay")
    with Indexer("algorand", settings=settings) as indexer:
        indexer.sync(ADDRESS)
    node.routes["/health"] = Reply({"round": HEAD + 1})
    with Indexer("algorand", settings=settings) as indexer:
        assert indexer.sync(ADDRESS).from_height == HEAD + 1


def test_failed_sync_does_not_move_the_checkpoint(node: FakeNode, settings: Settings) -> None:
    node.routes["/health"] = Reply({"round": HEAD})
    node.routes[TRANSACTIONS_PATH] = Reply({"message": "broken"}, status=500)
    with Indexer("algorand", settings=settings) as indexer:
        with pytest.raises(Exception, match="broken"):
            indexer.sync(ADDRESS)
        script_history(node, "tx_pay")
        assert indexer.sync(ADDRESS).from_height is None


def test_sync_reports_progress_and_request_count(node: FakeNode, settings: Settings) -> None:
    script_history(node, "tx_pay", "tx_axfer")
    seen: list[int] = []
    with Indexer("algorand", settings=settings) as indexer:
        result = indexer.sync(ADDRESS, on_progress=seen.append)
    assert seen == [2]
    assert result.requests_made == 2  # the chain head and one page


def test_account_transactions_respects_the_limit(node: FakeNode, settings: Settings) -> None:
    script_history(node, "tx_pay", "tx_axfer", "tx_appl")
    with Indexer("algorand", settings=settings) as indexer:
        assert len(indexer.account_transactions(ADDRESS, limit=2)) == 2
        with pytest.raises(InvalidInput, match="limit"):
            indexer.account_transactions(ADDRESS, limit=0)


def test_metrics_cover_the_window_and_validate_days(node: FakeNode, settings: Settings) -> None:
    script_history(node, "tx_pay", "tx_group")
    with Indexer("algorand", settings=settings) as indexer:
        indexer.sync(ADDRESS)
        report = indexer.metrics(days=36500, account=ADDRESS)
        assert report.transactions == 2
        assert report.native["app_calls"] == 1
        assert indexer.metrics(days=1).transactions == 0
        with pytest.raises(InvalidInput, match="days"):
            indexer.metrics(days=0)
        with pytest.raises(InvalidInput, match="days"):
            indexer.stored_transactions(days=0)


def test_unsupported_chain_is_refused(settings: Settings) -> None:
    with pytest.raises(UnsupportedChain, match="later version"):
        Indexer("bitcoin", settings=settings)


def test_cache_is_used_for_pinned_history(
    node: FakeNode, settings: Settings, tmp_path: Path
) -> None:
    script_history(node, "tx_pay")
    cached = settings.model_copy(update={"cache_path": tmp_path / "cache.db"})
    with Indexer("algorand", settings=cached) as indexer:
        indexer.sync(ADDRESS)
    fresh_database = cached.model_copy(update={"database": tmp_path / "other.db"})
    with Indexer("algorand", settings=fresh_database) as indexer:
        assert indexer.sync(ADDRESS).records_fetched == 1
    assert len(node.requests_to(TRANSACTIONS_PATH)) == 1


async def test_blocking_api_works_inside_a_running_event_loop(
    node: FakeNode, settings: Settings
) -> None:
    script_history(node, "tx_pay")
    with Indexer("algorand", settings=settings) as indexer:
        assert indexer.sync(ADDRESS).records_fetched == 1
        assert len(indexer.stored_transactions()) == 1


async def test_async_api_matches_the_blocking_one(node: FakeNode, settings: Settings) -> None:
    script_history(node, "tx_pay")
    node.routes[LOOKUP_PATH] = Reply(load_fixture("account"))
    node.routes["/v2/assets/7"] = Reply(load_fixture("asset"))
    node.routes["/v2/applications/8"] = Reply(load_fixture("application"))
    async with AsyncIndexer("algorand", settings=settings) as indexer:
        assert indexer.network == "testnet"
        assert indexer.profile.native_symbol == "ALGO"
        account = await indexer.account(ADDRESS)
        asset = await indexer.asset(7)
        contract = await indexer.contract(8)
        result = await indexer.sync(ADDRESS)
    assert account.chain == asset.chain == contract.chain == "algorand"
    assert result.records_fetched == 1


def test_cli_and_api_give_the_same_results(
    node: FakeNode, settings: Settings, invoke: Invoke, tmp_path: Path
) -> None:
    script_history(node, "tx_pay", "tx_axfer")
    assert invoke("sync", ADDRESS).exit_code == 0
    from_cli = json.loads(invoke("metrics", "--days", "36500").stdout)

    with Indexer("algorand", settings=settings) as indexer:
        indexer.sync(ADDRESS)
        from_api = indexer.metrics(days=36500).model_dump(mode="json")
    assert from_cli == from_api


def test_cli_index_account_shows_algo_and_transactions(node: FakeNode, invoke: Invoke) -> None:
    script_history(node, "tx_pay", "tx_axfer")
    node.routes[LOOKUP_PATH] = Reply(load_fixture("account"))
    result = invoke("index", "account", ADDRESS, "--limit", "1")
    assert result.exit_code == 0, result.output
    assert "ALGO" in result.stdout
    assert "Min balance" in result.stdout
    assert load_fixture("tx_pay")["id"] in result.stdout
    assert load_fixture("tx_axfer")["id"] not in result.stdout


def test_cli_index_asset_and_app(node: FakeNode, invoke: Invoke) -> None:
    node.routes["/v2/assets/7"] = Reply(load_fixture("asset"))
    node.routes["/v2/applications/8"] = Reply(load_fixture("application"))
    assert "ASA ID" in invoke("index", "asset", "7").stdout
    assert "App ID" in invoke("index", "app", "8").stdout


def test_cli_sync_prints_a_summary_and_progress(node: FakeNode, invoke: Invoke) -> None:
    script_history(node, "tx_pay")
    result = invoke("sync", ADDRESS)
    assert result.exit_code == 0
    assert "1 transactions" in result.stdout
    assert "requests" in result.stdout
    assert "Stored 1" in result.stderr


def test_cli_quiet_hides_progress(node: FakeNode, invoke: Invoke) -> None:
    script_history(node, "tx_pay")
    assert "Stored" not in invoke("-q", "sync", ADDRESS).stderr


def test_cli_export_to_stdout_and_file(node: FakeNode, invoke: Invoke, tmp_path: Path) -> None:
    script_history(node, "tx_pay")
    invoke("sync", ADDRESS)
    assert load_fixture("tx_pay")["id"] in invoke("export", "--format", "csv").stdout
    assert load_fixture("tx_pay")["id"] in invoke("export", "--format", "json").stdout

    output = tmp_path / "records.parquet"
    assert invoke("export", "--format", "parquet", "--output", str(output)).exit_code == 0
    assert len(pd.read_parquet(output)) == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("index", "account", "BAD"), "not a valid Algorand address"),
        (("index", "asset", "abc"), "ASA ID"),
        (("index", "app", "0"), "app ID"),
        (("sync", "BAD"), "not a valid Algorand address"),
        (("--chain", "bitcoin", "metrics"), "later version"),
        (("export", "--format", "parquet"), "--output"),
    ],
)
def test_cli_reports_bad_input_in_one_clean_line(
    invoke: Invoke, arguments: tuple[str, ...], message: str
) -> None:
    result = invoke(*arguments)
    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output


def test_cli_reports_an_unknown_asa_cleanly(node: FakeNode, invoke: Invoke) -> None:
    node.routes["/v2/assets/7"] = Reply({"message": "no assets found"}, status=404)
    result = invoke("index", "asset", "7")
    assert result.exit_code == 1
    assert "no assets found" in result.output


def test_cli_reports_a_timeout_cleanly(
    node: FakeNode, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCI_REQUEST_TIMEOUT", "0.05")
    node.routes["/v2/assets/7"] = Reply(delay=0.5)
    result = invoke("index", "asset", "7")
    assert result.exit_code == 1
    assert "TimeoutError" in result.output
    assert "Traceback" not in result.output


def test_cli_invalid_configuration_is_named(
    invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCI_MAX_RETRIES", "many")
    result = invoke("metrics")
    assert result.exit_code == 1
    assert "MCI_MAX_RETRIES" in result.output
