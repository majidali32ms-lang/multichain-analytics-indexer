"""Whether EVM accounts have revealed their public keys: the rule, the report, the CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from multichain_indexer import AsyncIndexer, InvalidInput, UnsupportedChain
from multichain_indexer.adapters.evm import adapter as adapter_module
from multichain_indexer.adapters.evm.adapter import EvmIndexer, indexer_factory
from multichain_indexer.adapters.evm.exposure import (
    CONTRACT,
    DELEGATED,
    LOWER_BOUND_NOTE,
    NOT_SEEN,
    REVEALED,
    exposure_report,
    key_exposure,
)
from multichain_indexer.adapters.evm.models import EvmAccount
from multichain_indexer.adapters.evm.profiles import BNB
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.models import AccountRecord
from tests.conftest import FakeNode, Reply

# Real replies from BNB Smart Chain mainnet: an EIP-7702 delegated account (found through a
# type 4 transaction), a validator that signs every turn, the USDT contract (its code cut to
# its first bytes) and the dead address.
SAVED = json.loads(
    (Path(__file__).parent / "fixtures" / "evm" / "exposure_bsc_mainnet.json").read_text("utf-8")
)
ACCOUNTS = SAVED["accounts"]
EXPECTED = {
    "delegated": DELEGATED,
    "signer": REVEALED,
    "contract": CONTRACT,
    "unused": NOT_SEEN,
}


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "result": result})


def serve(node: FakeNode) -> None:
    by_address = {entry["address"]: entry for entry in ACCOUNTS.values()}
    node.routes["rpc:eth_chainId"] = rpc_ok(SAVED["chain_id"])
    node.routes["rpc:eth_blockNumber"] = rpc_ok(SAVED["block"])
    node.handlers["rpc:eth_getTransactionCount"] = lambda params: rpc_ok(
        by_address[params[0].lower()]["nonce"]
    )
    node.handlers["rpc:eth_getCode"] = lambda params: rpc_ok(by_address[params[0].lower()]["code"])
    node.routes["rpc:eth_getBalance"] = rpc_ok("0x0")


def settings_for(node: FakeNode, tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "exposure.db",
        requests_per_second=1000,
        max_retries=0,
        env={"BNB_MAINNET_RPC_SERVER": SecretStr(node.url)},
    )


@pytest.mark.parametrize(
    ("nonce", "is_contract", "delegated_to", "status"),
    [
        (5, False, None, REVEALED),
        (0, False, None, NOT_SEEN),
        (1, True, None, CONTRACT),
        (3, False, "0x" + "11" * 20, DELEGATED),
    ],
)
def test_the_rule(nonce: int, is_contract: bool, delegated_to: str | None, status: str) -> None:
    assert key_exposure(nonce=nonce, is_contract=is_contract, delegated_to=delegated_to) == status


def test_wording_never_says_unsafe() -> None:
    for status in (REVEALED, DELEGATED, CONTRACT, NOT_SEEN, LOWER_BOUND_NOTE):
        assert "unsafe" not in status.lower()
    assert "EIP-712" in LOWER_BOUND_NOTE


@pytest.mark.parametrize("kind", list(EXPECTED))
async def test_each_real_account_gets_its_status(node: FakeNode, tmp_path: Path, kind: str) -> None:
    serve(node)
    adapter = indexer_factory(BNB)("mainnet", settings_for(node, tmp_path), None)
    try:
        record = await adapter.fetch_account(ACCOUNTS[kind]["address"])
    finally:
        await adapter.aclose()
    assert isinstance(record.native, EvmAccount)
    assert record.native.key_exposure == EXPECTED[kind]
    block = int(SAVED["block"], 16)
    line = adapter.describe(record)["Public key"]
    assert line == f"{EXPECTED[kind]}, as of block {block} (query time)"


async def test_report_counts_and_share(node: FakeNode, tmp_path: Path) -> None:
    serve(node)
    addresses = [entry["address"] for entry in ACCOUNTS.values()]
    async with AsyncIndexer("bnb", "mainnet", settings=settings_for(node, tmp_path)) as indexer:
        report = await indexer.key_exposure(addresses)
    assert [row.status for row in report.accounts] == list(EXPECTED.values())
    assert report.counts == {REVEALED: 1, DELEGATED: 1, CONTRACT: 1, NOT_SEEN: 1}
    assert report.revealed_share == pytest.approx(2 / 3)
    assert report.from_block == report.to_block == int(SAVED["block"], 16)
    assert report.note == LOWER_BOUND_NOTE


async def test_the_newest_block_is_read_again_every_few_accounts(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(node)
    monkeypatch.setattr(adapter_module, "EXPOSURE_ADDRESSES_PER_BLOCK", 2)
    adapter = indexer_factory(BNB)("mainnet", settings_for(node, tmp_path), None)
    assert isinstance(adapter, EvmIndexer)
    try:
        await adapter.fetch_exposure([entry["address"] for entry in ACCOUNTS.values()])
    finally:
        await adapter.aclose()
    assert len(node.rpc_params("eth_blockNumber")) == 2


@pytest.mark.parametrize("count", [0, adapter_module.MAX_EXPOSURE_ADDRESSES + 1])
async def test_list_size_is_bounded(count: int) -> None:
    adapter = indexer_factory(BNB)("mainnet", Settings(), None)
    assert isinstance(adapter, EvmIndexer)
    with pytest.raises(InvalidInput, match="give between 1 and 500 addresses"):
        await adapter.fetch_exposure(["0x" + "11" * 20] * count)


def test_an_empty_list_has_no_share() -> None:
    report = exposure_report(chain="bnb", network="mainnet", accounts=[], addresses=[])
    assert (report.revealed_share, report.from_block, report.to_block) == (None, 0, 0)


async def test_other_chains_have_no_report(tmp_path: Path) -> None:
    async with AsyncIndexer("bitcoin", settings=Settings(database=tmp_path / "x.db")) as indexer:
        with pytest.raises(UnsupportedChain, match="has no EVM public key report"):
            await indexer.key_exposure(["0x" + "11" * 20])


def invoke(node: FakeNode, tmp_path: Path, *arguments: str) -> Any:
    env = {
        "BNB_MAINNET_RPC_SERVER": node.url,
        "MCI_REQUESTS_PER_SECOND": "1000",
        "MCI_MAX_RETRIES": "0",
    }
    options = ["--chain", "bnb", "--network", "mainnet", "--db", str(tmp_path / "cli.db")]
    options += ["--env-file", str(tmp_path / "none.env")]
    return CliRunner(env=env).invoke(cli, [*options, "index", "exposure", *arguments])


def test_cli_reads_addresses_from_arguments_and_a_file(node: FakeNode, tmp_path: Path) -> None:
    serve(node)
    listed = tmp_path / "accounts.txt"
    listed.write_text(f"{ACCOUNTS['contract']['address']}\n\n{ACCOUNTS['unused']['address']}\n")
    result = invoke(node, tmp_path, ACCOUNTS["signer"]["address"], "--file", str(listed))
    assert result.exit_code == 0, result.output
    lines = [" ".join(line.split()) for line in result.output.splitlines()]
    assert "Accounts 3" in lines
    assert "Revealed share 50.00% of the accounts that can sign" in lines
    assert LOWER_BOUND_NOTE in lines


def test_cli_prints_json(node: FakeNode, tmp_path: Path) -> None:
    serve(node)
    result = invoke(node, tmp_path, ACCOUNTS["delegated"]["address"], "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["accounts"][0]["status"] == DELEGATED


def test_a_snapshot_stored_before_this_version_has_no_public_key_line() -> None:
    adapter = indexer_factory(BNB)("mainnet", Settings(), None)
    stored = AccountRecord(
        address=ACCOUNTS["signer"]["address"],
        balance=0,
        chain="bnb",
        network="mainnet",
        native=EvmAccount(nonce=3, is_contract=False, block_number=1),
    )
    assert "Public key" not in adapter.describe(stored)
