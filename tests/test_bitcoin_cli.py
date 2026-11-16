from __future__ import annotations

import json

import pytest
from click.testing import CliRunner, Result

from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from tests.bitcoin_support import (
    SERVER_SETTING,
    SMALL,
    fixture,
    serve_chain,
    serve_small,
)
from tests.conftest import FakeNode, Reply


def run_cli(btc_settings: Settings, *args: str) -> Result:
    runner = CliRunner()
    env = {SERVER_SETTING: btc_settings.env[SERVER_SETTING].get_secret_value()}
    database = str(btc_settings.database)
    return runner.invoke(
        cli, ["--chain", "bitcoin", "--db", database, "--env-file", "", *args], env=env
    )


def test_cli_index_address_shows_btc_utxos_and_recent_txids(
    node: FakeNode, btc_settings: Settings
) -> None:
    serve_small(node)
    result = run_cli(btc_settings, "index", "address", SMALL, "--limit", "2")
    assert result.exit_code == 0, result.output
    assert "Balance           0.03571478 BTC" in result.output
    assert "UTXOs             2" in result.output
    assert (
        "Quantum exposure  exposed: 2 confirmed UTXOs, 0.03571478 BTC; a confirmed spend "
        "revealed the public key (as of query time, from Esplora, height 161111)"
    ) in result.output
    lines = result.output.strip().splitlines()
    assert lines[-1].split("  ")[1] == fixture("testnet4_scripthash_small_chain")[1]["txid"]
    assert len(lines) == 7 + 1 + 2


def test_cli_index_tx_shows_fee_rate_and_parties(node: FakeNode, btc_settings: Settings) -> None:
    serve_chain(node)
    tx = fixture("tx_p2sh_wrapped_segwit")
    node.routes[f"/tx/{tx['txid']}"] = Reply(tx)
    result = run_cli(btc_settings, "index", "tx", tx["txid"])
    assert result.exit_code == 0, result.output
    assert "Fee                 1660 sats" in result.output
    assert "Fee rate            10.03 sat/vB" in result.output
    assert f"Sender              {tx['vin'][0]['prevout']['scriptpubkey_address']}" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("index", "tx", "abc"), "not a txid"),
        (("index", "address", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"), "mainnet, not testnet4"),
        (("index", "asset", "1"), "index asset is for algorand"),
    ],
)
def test_cli_errors_are_one_line(
    btc_settings: Settings, args: tuple[str, ...], message: str
) -> None:
    result = run_cli(btc_settings, *args)
    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("command", [("index", "address", SMALL), ("index", "tx", "0" * 64)])
def test_cli_bitcoin_commands_are_for_bitcoin_only(
    settings: Settings, command: tuple[str, ...]
) -> None:
    result = CliRunner().invoke(cli, ["--env-file", "", *command])
    assert result.exit_code == 1
    assert f"{' '.join(command[:2])} is for bitcoin, not algorand" in result.output


def test_cli_metrics_and_export_after_a_sync(node: FakeNode, btc_settings: Settings) -> None:
    serve_small(node)
    assert run_cli(btc_settings, "sync", SMALL).exit_code == 0
    metrics = run_cli(btc_settings, "metrics", "--days", "100000")
    assert json.loads(metrics.output)["native"]["fees_sats"] == 1472
    export = run_cli(btc_settings, "export", "--format", "json")
    rows = json.loads(export.output)
    assert {row["chain"] for row in rows} == {"bitcoin"}
    assert all(json.loads(row["native"])["history_source"] == "esplora" for row in rows)
