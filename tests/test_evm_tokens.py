from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from eth_abi.abi import encode
from eth_utils.abi import function_signature_to_4byte_selector
from pydantic import SecretStr

from multichain_indexer import InvalidAddress, InvalidInput, NetworkError, NotFound
from multichain_indexer.adapters.evm.adapter import indexer_factory
from multichain_indexer.adapters.evm.models import EvmToken
from multichain_indexer.adapters.evm.profiles import EVM_CHAINS, EvmChain
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from tests.conftest import FakeNode, Reply

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
FUNCTIONS = ("totalSupply()", "decimals()", "name()", "symbol()")


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"token_{name}.json").read_text("utf-8"))
    return loaded


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "result": result})


def chain_of(saved: dict[str, Any]) -> EvmChain:
    return next(chain for chain in EVM_CHAINS if chain.name == saved["chain"])


def by_selector(calls: dict[str, Any]) -> Callable[[list[Any]], Reply]:
    replies = {
        "0x" + function_signature_to_4byte_selector(function).hex(): reply
        for function, reply in calls.items()
    }

    def handle(params: list[Any]) -> Reply:
        return Reply(replies[params[0]["data"]])

    return handle


def serve(node: FakeNode, saved: dict[str, Any]) -> None:
    chain = chain_of(saved)
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(chain.networks[saved["network"]].chain_id))
    node.routes["rpc:eth_blockNumber"] = rpc_ok(hex(saved["block"]))
    node.routes["rpc:eth_getCode"] = Reply(saved["code"])
    node.handlers["rpc:eth_call"] = by_selector(saved["calls"])


def settings_for(node: FakeNode, saved: dict[str, Any]) -> Settings:
    setting = f"{saved['chain']}_{saved['network']}_RPC_SERVER".upper()
    return Settings(max_retries=0, requests_per_second=1000, env={setting: SecretStr(node.url)})


async def lookup(node: FakeNode, saved: dict[str, Any], address: str | None = None) -> Any:
    serve(node, saved)
    adapter = indexer_factory(chain_of(saved))(saved["network"], settings_for(node, saved), None)
    try:
        record = await adapter.fetch_asset(address or saved["address"])
        return record, adapter.describe(record)
    finally:
        await adapter.aclose()


def test_fixtures_are_real_calls_at_one_block() -> None:
    for name in (
        "sepolia_weth",
        "bsc_testnet_bep20",
        "ethereum_mkr_bytes32",
        "base_sepolia_not_a_token",
    ):
        saved = fixture(name)
        assert set(saved["calls"]) == set(FUNCTIONS)
        assert saved["code"]["result"] != "0x"


async def test_erc20_token(node: FakeNode) -> None:
    saved = fixture("sepolia_weth")
    record, fields = await lookup(node, saved, saved["address"].lower())
    assert record.id == saved["address"]
    assert (record.name, record.unit, record.decimals) == ("Wrapped Ether", "WETH", 18)
    assert record.total == int(saved["calls"]["totalSupply()"]["result"], 16)
    assert isinstance(record.native, EvmToken)
    assert record.native.block_number == saved["block"]
    assert fields["Standard"] == "ERC-20"
    called = {params[1] for params in node.rpc_params("eth_call")}
    assert called == {hex(saved["block"])}


async def test_bnb_smart_chain_calls_it_bep20(node: FakeNode) -> None:
    record, fields = await lookup(node, fixture("bsc_testnet_bep20"))
    assert fields["Standard"] == "BEP-20"
    assert record.unit


async def test_bytes32_name_and_symbol_are_read(node: FakeNode) -> None:
    record, _ = await lookup(node, fixture("ethereum_mkr_bytes32"))
    assert (record.name, record.unit) == ("Maker", "MKR")


async def test_contract_without_total_supply_is_not_a_token(node: FakeNode) -> None:
    saved = fixture("base_sepolia_not_a_token")
    with pytest.raises(InvalidInput, match="is not an ERC-20 token: it has no totalSupply"):
        await lookup(node, saved)


async def test_address_without_code_is_not_found(node: FakeNode) -> None:
    saved = {**fixture("sepolia_weth"), "code": {"jsonrpc": "2.0", "id": 1, "result": "0x"}}
    with pytest.raises(NotFound, match="no contract is deployed"):
        await lookup(node, saved)


def call_reply(value: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": value}


def revert_reply(code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}


@pytest.mark.parametrize(
    ("missing", "field", "shown"),
    [
        (revert_reply(3, "execution reverted"), "Decimals", "not declared, so 0"),
        (revert_reply(-32000, "execution reverted"), "Name", "not declared"),
        (call_reply("0x"), "Symbol", "not declared"),
    ],
)
async def test_optional_functions_may_be_missing(
    node: FakeNode, missing: dict[str, Any], field: str, shown: str
) -> None:
    saved = fixture("sepolia_weth")
    function = {"Decimals": "decimals()", "Name": "name()", "Symbol": "symbol()"}[field]
    saved["calls"][function] = missing
    record, fields = await lookup(node, saved)
    assert fields[field] == shown
    if field == "Decimals":
        assert record.decimals == 0
        assert not record.native.decimals_declared


async def test_out_of_range_decimals_and_bad_strings_count_as_missing(node: FakeNode) -> None:
    saved = fixture("sepolia_weth")
    saved["calls"]["decimals()"] = call_reply("0x" + encode(["uint256"], [256]).hex())
    saved["calls"]["name()"] = call_reply("0x" + "00" * 31 + "20" + "ff" * 40)
    saved["calls"]["symbol()"] = call_reply("0x" + encode(["bytes"], [bytes([0xFF, 0xFE])]).hex())
    record, fields = await lookup(node, saved)
    assert fields["Decimals"] == "not declared, so 0"
    assert (record.name, record.unit) == (None, None)


async def test_other_call_errors_are_not_hidden(node: FakeNode) -> None:
    saved = fixture("sepolia_weth")
    saved["calls"]["totalSupply()"] = revert_reply(-32000, "header not found")
    with pytest.raises(NetworkError, match="header not found"):
        await lookup(node, saved)


async def test_bad_address_never_reaches_the_network(node: FakeNode) -> None:
    saved = fixture("sepolia_weth")
    with pytest.raises(InvalidAddress):
        await lookup(node, saved, saved["address"][:-1])
    assert node.rpc_calls == []


def test_cli_index_token_on_an_evm_chain(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = fixture("sepolia_weth")
    serve(node, saved)
    monkeypatch.setenv("ETHEREUM_TESTNET_RPC_SERVER", node.url)
    monkeypatch.setenv("MCI_MAX_RETRIES", "0")
    options = ["--db", str(tmp_path / "cli.db"), "--env-file", str(tmp_path / "none.env")]
    result = CliRunner().invoke(
        cli, [*options, "--chain", "ethereum", "index", "token", saved["address"]]
    )
    assert result.exit_code == 0, result.output
    assert "Wrapped Ether" in result.stdout
    assert "ERC-20" in result.stdout
