"""Every chain at once: side-by-side metrics, one export table, parallel syncs, the report."""

from __future__ import annotations

import io
import json
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from multichain_indexer import ChainMetrics, CrossChain, InvalidInput, UnsupportedChain
from multichain_indexer.adapters.algorand.parser import parse_transaction as parse_algorand
from multichain_indexer.adapters.bitcoin.esplora import parse_esplora_transaction
from multichain_indexer.adapters.bitcoin.parser import parse_transaction as parse_bitcoin
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.config_file import Price
from multichain_indexer.core.models import TransactionRecord
from multichain_indexer.core.storage import SQLiteStorage
from multichain_indexer.report import metrics_table, render_html, write_report
from tests.bitcoin_support import SMALL, fixture, serve_small
from tests.conftest import FakeNode, Reply, load_fixture
from tests.test_storage import record as toy_record

ALL_TIME = 36500
BTC_PRICE = Price(usd=Decimal("98000.5"), date=date(2026, 11, 15))
ALGO_ACCOUNT = "ALGORANDACCOUNT"


def algorand_records() -> list[TransactionRecord]:
    return [parse_algorand(load_fixture(name), "testnet") for name in ("tx_pay", "tx_axfer")]


def bitcoin_mainnet_record() -> TransactionRecord:
    source = parse_esplora_transaction(fixture("tx_mainnet_p2pk"), "/tx/p2pk")
    return parse_bitcoin(source, "mainnet", tip_height=930_000, history_source="esplora")


@pytest.fixture
def working_database(tmp_path: Path) -> Path:
    path = tmp_path / "all.db"
    storage = SQLiteStorage(path)
    storage.save_transactions("algorand", "testnet", ALGO_ACCOUNT, algorand_records())
    storage.save_transactions("bitcoin", "mainnet", "key", [bitcoin_mainnet_record()])
    storage.close()
    return path


@pytest.fixture
def database(working_database: Path) -> Path:
    """Also holds records of a chain whose plugin is no longer installed."""
    storage = SQLiteStorage(working_database)
    storage.save_transactions("gone", "testnet", "alice", [toy_record("x", chain="gone")])
    storage.close()
    return working_database


def priced(tmp_path: Path, **changes: object) -> Settings:
    prices = {"bitcoin": BTC_PRICE, "algorand": Price(usd=Decimal("0.2"), date=date(2026, 11, 15))}
    return Settings(database=tmp_path / "unused.db", prices=prices, **changes)  # type: ignore[arg-type]


def rows_by_chain(settings: Settings, database: Path) -> dict[str, ChainMetrics]:
    with CrossChain(settings=settings, database=database) as everything:
        report = everything.metrics(ALL_TIME)
    return {row.chain: row for row in report.rows}


def test_every_stored_chain_is_reported_side_by_side(tmp_path: Path, database: Path) -> None:
    rows = rows_by_chain(priced(tmp_path), database)
    assert sorted(rows) == ["algorand", "bitcoin", "gone"]

    algorand = rows["algorand"]
    expected_fees = sum(item.fee for item in algorand_records())
    assert (algorand.network, algorand.transactions, algorand.total_fees) == (
        "testnet",
        2,
        expected_fees,
    )
    assert algorand.total_fees_display.endswith(" ALGO")
    assert algorand.transactions_per_day == round(2 / ALL_TIME, 2)
    assert sum(algorand.daily_transactions.values()) == 2


def test_us_dollars_come_only_from_a_given_price_and_only_on_mainnet(
    tmp_path: Path, database: Path
) -> None:
    rows = rows_by_chain(priced(tmp_path), database)
    bitcoin = rows["bitcoin"]
    expected = Decimal(bitcoin.total_fees).scaleb(-8) * BTC_PRICE.usd
    assert (bitcoin.fees_usd, bitcoin.price_usd, bitcoin.price_date) == (
        expected,
        BTC_PRICE.usd,
        BTC_PRICE.date,
    )
    assert rows["algorand"].fees_usd is None
    assert rows_by_chain(Settings(database=tmp_path / "u.db"), database)["bitcoin"].fees_usd is None


def test_a_chain_that_fails_shows_its_error_and_the_others_still_report(
    tmp_path: Path, database: Path
) -> None:
    rows = rows_by_chain(priced(tmp_path), database)
    assert rows["gone"].error is not None
    assert "unknown chain 'gone'" in rows["gone"].error
    assert rows["algorand"].error is None and rows["bitcoin"].error is None


def test_every_chain_exports_into_one_table(tmp_path: Path, working_database: Path) -> None:
    with CrossChain(settings=priced(tmp_path), database=working_database) as everything:
        text = everything.export("json")
        everything.export("parquet", tmp_path / "all.parquet")
    assert text is not None
    frame = pd.read_json(io.StringIO(text))
    parquet = pd.read_parquet(tmp_path / "all.parquet")
    assert sorted(set(frame["chain"])) == ["algorand", "bitcoin"]
    assert list(frame.columns) == list(parquet.columns)
    assert len(frame) == len(parquet) == 3


def test_an_export_never_leaves_out_a_chain_it_cannot_read(tmp_path: Path, database: Path) -> None:
    with (
        CrossChain(settings=priced(tmp_path), database=database) as everything,
        pytest.raises(UnsupportedChain, match="unknown chain 'gone'"),
    ):
        everything.export("csv")


def test_sync_runs_every_listed_account_and_one_failure_stops_nothing(
    node: FakeNode, tmp_path: Path
) -> None:
    serve_small(node)
    settings = Settings(
        database=tmp_path / "sync.db",
        max_retries=0,
        requests_per_second=1000,
        request_timeout=2,
        env={
            "BITCOIN_TESTNET4_ESPLORA_SERVER": SecretStr(node.url),
            "ALGORAND_TESTNET_INDEXER_SERVER": SecretStr("http://127.0.0.1:9"),
        },
        accounts={
            "bitcoin": {"testnet4": (SMALL,), "mainnet": ("bc1qnotsyncedwithoutthenetworkflag",)},
            "algorand": {"testnet": (ALGO_ACCOUNT,)},
        },
    )
    with CrossChain(settings=settings) as everything:
        outcomes = {outcome.chain: outcome for outcome in everything.sync()}
    assert set(outcomes) == {"bitcoin", "algorand"}
    bitcoin = outcomes["bitcoin"].result
    assert bitcoin is not None and bitcoin.records_fetched > 0
    assert (outcomes["bitcoin"].network, outcomes["algorand"].result) == ("testnet4", None)
    assert outcomes["algorand"].error


def test_mainnet_accounts_are_synced_only_when_mainnet_is_named(tmp_path: Path) -> None:
    settings = Settings(database=tmp_path / "db", accounts={"bitcoin": {"mainnet": ("bc1q",)}})
    with (
        CrossChain(settings=settings) as everything,
        pytest.raises(InvalidInput, match="no accounts"),
    ):
        everything.sync()


def test_the_html_report_is_offline_and_matches_the_metrics(tmp_path: Path, database: Path) -> None:
    with CrossChain(settings=priced(tmp_path), database=database) as everything:
        report = everything.metrics(ALL_TIME)
    page = render_html(report)

    class Links(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.external: list[str] = []
            self.scripts = 0

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.scripts += tag == "script"
            self.external += [value or "" for name, value in attrs if name in ("src", "href")]

    links = Links()
    links.feed(page)
    assert (links.scripts, links.external) == (0, [])
    for row in report.rows:
        if row.error is None:
            assert f"<td>{row.transactions}</td>" in page
            assert f"<td>{row.total_fees_display}</td>" in page
    assert "unknown chain &#x27;gone&#x27;" in page
    assert "$98000.5 on 2026-11-15" in page


def test_csv_and_json_reports_carry_the_same_rows(tmp_path: Path, database: Path) -> None:
    with CrossChain(settings=priced(tmp_path), database=database) as everything:
        report = everything.metrics(ALL_TIME)
    csv_text = write_report(report, "csv")
    json_text = write_report(report, "json")
    assert csv_text is not None and json_text is not None
    from_csv = pd.read_csv(io.StringIO(csv_text))
    from_json = json.loads(json_text)["rows"]
    assert list(from_csv["transactions"]) == [row["transactions"] for row in from_json]
    write_report(report, "html", tmp_path / "out" / "report.html")
    assert (tmp_path / "out" / "report.html").read_text(encoding="utf-8").startswith("<!doctype")


def test_the_table_lines_up_and_shows_errors(tmp_path: Path, database: Path) -> None:
    with CrossChain(settings=priced(tmp_path), database=database) as everything:
        lines = metrics_table(everything.metrics(ALL_TIME))
    assert lines[0].startswith("Chain")
    assert any(line.startswith("gone testnet") and "error:" in line for line in lines)
    assert len({len(line) for line in lines[1:3]}) == 1


def test_the_command_line_works_on_all_chains(tmp_path: Path, database: Path) -> None:
    def run(*arguments: str) -> object:
        options = ["--chain", "all", "--db", str(database), "--env-file", str(tmp_path / "no.env")]
        options += ["--config", str(tmp_path / "no.toml")]
        return CliRunner().invoke(cli, [*options, *arguments])

    table = run("metrics", "--days", str(ALL_TIME))
    assert table.exit_code == 0, table.output  # type: ignore[attr-defined]
    assert "algorand testnet" in table.stdout  # type: ignore[attr-defined]
    as_json = json.loads(run("metrics", "--days", str(ALL_TIME), "--json").stdout)  # type: ignore[attr-defined]
    assert {row["chain"] for row in as_json["rows"]} == {"algorand", "bitcoin", "gone"}
    exported = run("export", "--format", "csv")
    assert exported.exit_code == 1  # type: ignore[attr-defined]
    assert "unknown chain 'gone'" in exported.output  # type: ignore[attr-defined]
    html = run("report", "--days", str(ALL_TIME))
    assert html.stdout.startswith("<!doctype html>")  # type: ignore[attr-defined]
    one_chain_only = run("index", "account", "x")
    assert one_chain_only.exit_code == 1  # type: ignore[attr-defined]
    assert "works on one chain" in one_chain_only.output  # type: ignore[attr-defined]
    assert run("metrics", "--account", "x").exit_code == 2  # type: ignore[attr-defined]
    assert run("export", "--format", "csv", "--account", "x").exit_code == 2  # type: ignore[attr-defined]
    assert run("sync", "addr").exit_code == 2  # type: ignore[attr-defined]


def test_cross_chain_sync_from_the_command_line_reports_each_account(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_small(node)
    node.routes["/health"] = Reply({"round": 10})
    config = tmp_path / "config.toml"
    config.write_text(
        f'[accounts.bitcoin]\ntestnet4 = ["{SMALL}"]\n'
        f'[accounts.algorand]\ntestnet = ["{ALGO_ACCOUNT}"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BITCOIN_TESTNET4_ESPLORA_SERVER", node.url)
    monkeypatch.setenv("ALGORAND_TESTNET_INDEXER_SERVER", "http://127.0.0.1:9")
    monkeypatch.setenv("MCI_MAX_RETRIES", "0")
    monkeypatch.setenv("MCI_REQUEST_TIMEOUT", "2")
    options = ["--chain", "all", "--db", str(tmp_path / "c.db"), "--config", str(config)]
    options += ["--env-file", str(tmp_path / "no.env")]
    result = CliRunner().invoke(cli, [*options, "sync"])
    assert result.exit_code == 1
    assert f"Synced {SMALL} on bitcoin testnet4" in result.stdout
    assert f"Failed {ALGO_ACCOUNT} on algorand testnet" in result.stderr
    assert CliRunner().invoke(cli, [*options[:2], "sync"]).exit_code == 1


def test_more_cross_chain_edges(tmp_path: Path, working_database: Path) -> None:
    with CrossChain(settings=priced(tmp_path), database=working_database) as everything:
        assert everything.networks() == [("algorand", "testnet"), ("bitcoin", "mainnet")]
        with pytest.raises(InvalidInput, match="days must be at least 1"):
            everything.metrics(0)
    unknown = Settings(database=tmp_path / "db", accounts={"gone": {"testnet": ("a", "b")}})
    with CrossChain(settings=unknown) as everything:
        outcomes = everything.sync()
    assert [(outcome.account, outcome.result) for outcome in outcomes] == [("a", None), ("b", None)]
    assert all("unknown chain 'gone'" in (outcome.error or "") for outcome in outcomes)


def test_report_edges(tmp_path: Path) -> None:
    with CrossChain(settings=priced(tmp_path), database=tmp_path / "empty.db") as everything:
        empty = everything.metrics(7)
    assert empty.rows == ()
    assert "No data." in render_html(empty)
    with pytest.raises(InvalidInput, match="unknown report format 'pdf'"):
        write_report(empty, "pdf")
    tiny = ChainMetrics(
        chain="bitcoin",
        network="mainnet",
        days=7,
        transactions=1,
        total_fees=1,
        total_fees_display="0.00000001 BTC",
        fees_usd=Decimal("0.00098"),
        price_usd=BTC_PRICE.usd,
        price_date=BTC_PRICE.date,
    )
    assert "$0.000980" in metrics_table(empty.model_copy(update={"rows": (tiny,)}))[1]


def test_the_command_line_names_an_unknown_chain_and_needs_an_address(tmp_path: Path) -> None:
    unknown = CliRunner().invoke(cli, ["--chain", "dogecoin", "pq-status"])
    assert unknown.exit_code == 2
    assert "'dogecoin' is not a known chain; choose from algorand" in unknown.output
    options = ["--db", str(tmp_path / "db"), "--env-file", str(tmp_path / "no.env")]
    assert CliRunner().invoke(cli, [*options, "sync"]).exit_code == 2


def test_a_cross_chain_sync_with_no_failure_exits_cleanly_and_reports_go_to_files(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, working_database: Path
) -> None:
    serve_small(node)
    config = tmp_path / "config.toml"
    config.write_text(f'[accounts.bitcoin]\ntestnet4 = ["{SMALL}"]\n', encoding="utf-8")
    monkeypatch.setenv("BITCOIN_TESTNET4_ESPLORA_SERVER", node.url)
    monkeypatch.setenv("MCI_MAX_RETRIES", "0")
    options = ["--chain", "all", "--config", str(config), "--env-file", str(tmp_path / "no.env")]
    synced = CliRunner().invoke(cli, [*options, "--db", str(tmp_path / "s.db"), "sync"])
    assert synced.exit_code == 0, synced.output
    output = tmp_path / "report.csv"
    written = CliRunner().invoke(
        cli,
        [
            *options,
            "--db",
            str(working_database),
            "report",
            "--format",
            "csv",
            "--output",
            str(output),
        ],
    )
    assert (written.exit_code, written.stdout) == (0, "")
    assert "algorand,testnet" in output.read_text(encoding="utf-8")
