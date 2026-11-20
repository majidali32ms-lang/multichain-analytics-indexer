"""Finality on EVM chains: BNB Smart Chain's eth_getFinalizedHeader, the finalized tag, the lag."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from multichain_indexer import AsyncIndexer, InvalidInput, MalformedResponse, UnsupportedChain
from multichain_indexer.adapters.evm.adapter import (
    ECONOMIC_FINALITY_SOURCE,
    FINALIZED_TAG_SOURCE,
    EvmIndexer,
    indexer_factory,
)
from multichain_indexer.adapters.evm.profiles import BASE, BNB, EvmChain
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from tests.conftest import FakeNode, Reply

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
BSC = json.loads((FIXTURES / "finality_bsc_mainnet.json").read_text("utf-8"))
NO_METHOD = json.loads((FIXTURES / "finality_sepolia_no_method.json").read_text("utf-8"))


def number(header: dict[str, Any]) -> int:
    return int(header["number"], 16)


def bsc_header(name: str) -> dict[str, Any]:
    header: dict[str, Any] = copy.deepcopy(BSC["replies"][name]["result"])
    return header


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "result": result})


def serve_blocks(node: FakeNode, finalized: dict[str, Any], latest: dict[str, Any]) -> None:
    def block(params: list[Any]) -> Reply:
        return rpc_ok({"finalized": finalized, "latest": latest}[params[0]])

    node.handlers["rpc:eth_getBlockByNumber"] = block


def serve_bsc(
    node: FakeNode,
    economic: Reply | None = None,
    latest: dict[str, Any] | None = None,
) -> None:
    node.routes["rpc:eth_chainId"] = Reply(BSC["replies"]["eth_chainId"])
    node.routes["rpc:eth_getFinalizedHeader"] = economic or Reply(
        BSC["replies"]["eth_getFinalizedHeader"]
    )
    head = latest or bsc_header("latest")
    node.routes["rpc:eth_blockNumber"] = rpc_ok(head["number"])
    serve_blocks(node, bsc_header("finalized"), head)


def settings_for(node: FakeNode, tmp_path: Path, chain: EvmChain, network: str) -> Settings:
    return Settings(
        database=tmp_path / "finality.db",
        requests_per_second=1000,
        max_retries=0,
        env={f"{chain.name}_{network}_RPC_SERVER".upper(): SecretStr(node.url)},
    )


@pytest.fixture
def make_adapter(node: FakeNode, tmp_path: Path) -> Callable[[EvmChain, str], EvmIndexer]:
    def build(chain: EvmChain, network: str = "mainnet") -> EvmIndexer:
        settings = settings_for(node, tmp_path, chain, network)
        adapter = indexer_factory(chain)(network, settings, None)
        assert isinstance(adapter, EvmIndexer)
        return adapter

    return build


@pytest.fixture
async def bsc(make_adapter: Callable[[EvmChain, str], EvmIndexer]) -> AsyncIterator[EvmIndexer]:
    adapter = make_adapter(BNB, "mainnet")
    yield adapter
    await adapter.aclose()


def test_fixtures_are_real_replies_read_in_order() -> None:
    assert BSC["network"] == "BNB Smart Chain mainnet"
    assert BSC["verified_validators"] == -2
    economic = bsc_header("eth_getFinalizedHeader")
    assert number(economic) <= number(bsc_header("finalized")) <= number(bsc_header("latest"))
    assert NO_METHOD["replies"]["eth_getFinalizedHeader"]["error"]["code"] == -32601


async def test_bnb_smart_chain_asks_two_thirds_of_the_validators(
    node: FakeNode, bsc: EvmIndexer
) -> None:
    serve_bsc(node)
    head = await bsc.chain_head()
    assert node.rpc_params("eth_getFinalizedHeader") == [[-2]]
    assert bsc.final_height(head) == number(bsc_header("eth_getFinalizedHeader"))


@pytest.mark.parametrize(
    "economic",
    [Reply(NO_METHOD["replies"]["eth_getFinalizedHeader"]), rpc_ok(None)],
    ids=["method not served", "no block"],
)
async def test_finalized_tag_is_used_when_the_method_gives_no_block(
    node: FakeNode, bsc: EvmIndexer, economic: Reply
) -> None:
    serve_bsc(node, economic)
    head = await bsc.chain_head()
    assert bsc.final_height(head) == number(bsc_header("finalized"))
    assert (await bsc.fetch_finality()).source == FINALIZED_TAG_SOURCE


async def test_a_block_above_the_head_never_moves_the_final_height_past_it(
    node: FakeNode, bsc: EvmIndexer
) -> None:
    latest = bsc_header("latest")
    ahead = {**bsc_header("eth_getFinalizedHeader"), "number": hex(number(latest) + 3)}
    serve_bsc(node, rpc_ok(ahead), latest)
    head = await bsc.chain_head()
    assert bsc.final_height(head) == head == number(latest)
    finality = await bsc.fetch_finality()
    assert finality.finalized_above_head
    assert finality.finalized_block == finality.head_block == number(latest)
    assert finality.lag_blocks == finality.lag_milliseconds == 0


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (rpc_ok("0x1"), MalformedResponse),
        (
            Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "uint64"}}),
            InvalidInput,
        ),
    ],
)
async def test_other_replies_are_errors(
    node: FakeNode, bsc: EvmIndexer, reply: Reply, error: type[Exception]
) -> None:
    serve_bsc(node, reply)
    with pytest.raises(error):
        await bsc.chain_head()


async def test_lag_is_read_from_millisecond_timestamps(node: FakeNode, bsc: EvmIndexer) -> None:
    serve_bsc(node)
    finality = await bsc.fetch_finality()
    economic, latest = bsc_header("eth_getFinalizedHeader"), bsc_header("latest")
    assert finality.source == ECONOMIC_FINALITY_SOURCE
    assert (finality.chain, finality.network) == ("bnb", "mainnet")
    assert finality.lag_blocks == number(latest) - number(economic)
    lag = int(latest["milliTimestamp"], 16) - int(economic["milliTimestamp"], 16)
    assert finality.lag_milliseconds == lag
    assert finality.head_time.timestamp() * 1000 == pytest.approx(int(latest["milliTimestamp"], 16))
    assert not finality.finalized_above_head


async def test_other_chains_use_the_tag_and_whole_seconds(
    node: FakeNode, make_adapter: Callable[[EvmChain, str], EvmIndexer]
) -> None:
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(BASE.networks["mainnet"].chain_id))
    finalized = {"number": "0x10", "timestamp": "0x64"}
    latest = {"number": "0x14", "timestamp": "0x6e"}
    serve_blocks(node, finalized, latest)
    adapter = make_adapter(BASE, "mainnet")
    try:
        finality = await adapter.fetch_finality()
    finally:
        await adapter.aclose()
    assert node.rpc_params("eth_getFinalizedHeader") == []
    assert finality.source == FINALIZED_TAG_SOURCE
    assert (finality.lag_blocks, finality.lag_milliseconds) == (4, 10_000)


async def test_api_reads_finality_for_evm_chains_only(node: FakeNode, tmp_path: Path) -> None:
    serve_bsc(node)
    settings = settings_for(node, tmp_path, BNB, "mainnet")
    async with AsyncIndexer("bnb", "mainnet", settings=settings) as indexer:
        assert (await indexer.finality()).source == ECONOMIC_FINALITY_SOURCE
    async with AsyncIndexer("algorand", settings=settings) as indexer:
        with pytest.raises(UnsupportedChain, match="algorand has no finality readout"):
            await indexer.finality()


def invoke_finality(node: FakeNode, tmp_path: Path) -> Any:
    env = {
        "BNB_MAINNET_RPC_SERVER": node.url,
        "MCI_REQUESTS_PER_SECOND": "1000",
        "MCI_MAX_RETRIES": "0",
    }
    return CliRunner(env=env).invoke(
        cli,
        [
            "--chain",
            "bnb",
            "--network",
            "mainnet",
            "--db",
            str(tmp_path / "cli.db"),
            "--env-file",
            str(tmp_path / "none.env"),
            "index",
            "finality",
        ],
    )


def test_cli_shows_the_lag_in_blocks_and_milliseconds(node: FakeNode, tmp_path: Path) -> None:
    latest = bsc_header("latest")
    one_behind = {**bsc_header("eth_getFinalizedHeader"), "number": hex(number(latest) - 1)}
    serve_bsc(node, rpc_ok(one_behind), latest)
    result = invoke_finality(node, tmp_path)
    assert result.exit_code == 0, result.output
    assert "Lag              1 block, " in result.output
    assert "Source           eth_getFinalizedHeader" in result.output
    assert "Note" not in result.output


def test_cli_says_when_the_endpoint_was_ahead(node: FakeNode, tmp_path: Path) -> None:
    latest = bsc_header("latest")
    ahead = {**bsc_header("eth_getFinalizedHeader"), "number": hex(number(latest) + 2)}
    serve_bsc(node, rpc_ok(ahead), latest)
    result = invoke_finality(node, tmp_path)
    assert result.exit_code == 0, result.output
    assert "Lag              0 blocks, 0 ms" in result.output
    assert "finalized block above its newest block" in result.output
