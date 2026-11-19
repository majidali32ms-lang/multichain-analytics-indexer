"""Block scan history on a public network: BNB Smart Chain testnet, its range guard and its
token transfers from NodeReal's eth_getLogs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from multichain_indexer import ConfigError, MalformedResponse
from multichain_indexer.adapters.evm import scan as scan_module
from multichain_indexer.adapters.evm.adapter import EvmIndexer, indexer_factory
from multichain_indexer.adapters.evm.profiles import BNB, ETHEREUM
from multichain_indexer.adapters.evm.scan import MAX_SCAN_BLOCKS, RpcScanHistory
from multichain_indexer.core.config import Settings
from tests.conftest import FakeNode, Reply

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
# A real BNB Smart Chain testnet BEP-20 transfer: the receiver gets tokens inside the sender's
# call to the token contract, so only the token's Transfer log names it.
TOKEN_TX = json.loads((FIXTURES / "tx_bsc_testnet_erc20.json").read_text("utf-8"))
(TRANSFER_LOG,) = TOKEN_TX["receipt"]["logs"]
TOKEN_BLOCK = int(TOKEN_TX["receipt"]["blockNumber"], 16)
RECEIVER = "0x" + TRANSFER_LOG["topics"][2][-40:]
KEY = "nodereal-test-key-0123456789abcd"


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "result": result})


def scanned(number: int, index: int, sender: str, receiver: str) -> dict[str, Any]:
    return {"hash": f"0x{number:060x}{index:04x}", "from": sender, "to": receiver}


def serve_testnet(node: FakeNode, blocks: dict[int, list[dict[str, Any]]], head: int) -> None:
    def block(params: list[Any]) -> Reply:
        tag, full = params
        if not full:
            return rpc_ok({"number": hex(head)})
        return rpc_ok({"transactions": blocks.get(int(tag, 16), [])})

    node.routes["rpc:eth_chainId"] = rpc_ok(hex(97))
    node.routes["rpc:eth_blockNumber"] = rpc_ok(hex(head))
    node.handlers["rpc:eth_getBlockByNumber"] = block


def settings_for(node: FakeNode, tmp_path: Path, **env: str) -> Settings:
    values = {"BNB_TESTNET_RPC_SERVER": node.url, **env}
    return Settings(
        database=tmp_path / "scan.db",
        requests_per_second=1000,
        max_retries=0,
        env={name: SecretStr(value) for name, value in values.items()},
    )


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan_module, "LOGS_REQUESTS_PER_SECOND", 1000)


@pytest.fixture
async def make_source(node: FakeNode, tmp_path: Path) -> AsyncIterator[Any]:
    adapters: list[EvmIndexer] = []

    def build(**env: str) -> RpcScanHistory:
        adapter = indexer_factory(BNB)("testnet", settings_for(node, tmp_path, **env), None)
        assert isinstance(adapter, EvmIndexer)
        adapters.append(adapter)
        source = adapter._history_source()
        assert isinstance(source, RpcScanHistory)
        return source

    yield build
    for adapter in adapters:
        await adapter.aclose()


async def walk(source: RpcScanHistory, address: str, **bounds: int | None) -> list[str]:
    hashes: list[str] = []
    cursor = None
    while True:
        page = await source.account_page(
            address,
            from_height=bounds.get("from_height"),
            to_height=bounds.get("to_height"),
            cursor=cursor,
        )
        hashes.extend(page.hashes)
        if page.next_cursor is None:
            return hashes
        cursor = page.next_cursor


def test_fixture_is_a_token_transfer_inside_another_call() -> None:
    assert TOKEN_TX["network"] == "BNB Smart Chain testnet"
    assert RECEIVER not in (TOKEN_TX["transaction"]["from"], TOKEN_TX["transaction"]["to"])


def test_bnb_smart_chain_testnet_defaults_to_the_block_scan() -> None:
    adapter = indexer_factory(BNB)("testnet", Settings(), None)
    assert isinstance(adapter._history_source(), RpcScanHistory)


async def test_a_long_range_without_a_start_block_is_refused(
    node: FakeNode, make_source: Any
) -> None:
    head = MAX_SCAN_BLOCKS + 500
    serve_testnet(node, {}, head)
    source = make_source()
    with pytest.raises(ConfigError) as refused:
        await walk(source, RECEIVER)
    message = str(refused.value)
    assert f"this range is {head + 1} blocks (at most {MAX_SCAN_BLOCKS}" in message
    assert "Set BNB_TESTNET_SCAN_FROM_BLOCK to the first block to read" in message
    assert node.rpc_params("eth_getBlockByNumber") == []


async def test_a_short_range_is_read_block_by_block(
    node: FakeNode, make_source: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_module, "BLOCKS_PER_PAGE", 4)
    other = "0x" + "22" * 20
    head = MAX_SCAN_BLOCKS * 3
    blocks = {
        head: [scanned(head, 0, RECEIVER, other)],
        head - 5: [scanned(head - 5, 0, other, other), scanned(head - 5, 1, other, RECEIVER)],
    }
    serve_testnet(node, blocks, head)
    found = await walk(make_source(), RECEIVER, from_height=head - 9)
    assert found == [blocks[head][0]["hash"], blocks[head - 5][1]["hash"]]


async def test_a_start_block_lifts_the_guard_and_bounds_the_scan(
    node: FakeNode, make_source: Any
) -> None:
    head = MAX_SCAN_BLOCKS * 3
    serve_testnet(node, {head - 2: [scanned(head - 2, 0, RECEIVER, RECEIVER)]}, head)
    source = make_source(BNB_TESTNET_SCAN_FROM_BLOCK=str(head - 2))
    assert len(await walk(source, RECEIVER)) == 1
    read = [int(params[0], 16) for params in node.rpc_params("eth_getBlockByNumber")]
    assert read == [head, head - 1, head - 2]


@pytest.mark.parametrize("value", ["-1", "1e5", "next"])
def test_a_start_block_must_be_a_block_number(value: str) -> None:
    env = {"BNB_TESTNET_SCAN_FROM_BLOCK": SecretStr(value)}
    adapter = indexer_factory(BNB)("testnet", Settings(env=env), None)
    with pytest.raises(ConfigError, match="BNB_TESTNET_SCAN_FROM_BLOCK must be a block number"):
        adapter._history_source()


def test_all_scope_on_bnb_testnet_needs_the_nodereal_key() -> None:
    env = {"BNB_TESTNET_HISTORY_SCOPE": SecretStr("all")}
    adapter = indexer_factory(BNB)("testnet", Settings(env=env), None)
    with pytest.raises(ConfigError) as refused:
        adapter._history_source()
    assert "set NODEREAL_API_KEY, or set BNB_TESTNET_HISTORY_SCOPE to direct" in str(refused.value)


async def test_all_scope_adds_token_transfers_from_nodereal_logs(
    node: FakeNode, make_source: Any
) -> None:
    serve_testnet(node, {}, TOKEN_BLOCK)

    def logs(params: list[dict[str, Any]]) -> Reply:
        (log_filter,) = params
        received = len(log_filter["topics"]) == 3
        return rpc_ok([TRANSFER_LOG, TRANSFER_LOG] if received else [])

    node.handlers["rpc:eth_getLogs"] = logs
    source = make_source(
        BNB_TESTNET_HISTORY_SCOPE="all",
        BNB_TESTNET_LOGS_SERVER=node.url,
        NODEREAL_API_KEY=KEY,
    )
    page = await source.account_page(
        RECEIVER, from_height=TOKEN_BLOCK, to_height=TOKEN_BLOCK, cursor=None
    )
    assert page.hashes == (TOKEN_TX["transaction"]["hash"],)
    party = "0x" + "0" * 24 + RECEIVER[2:]
    filters = [params[0] for params in node.rpc_params("eth_getLogs")]
    assert [log_filter["topics"][1:] for log_filter in filters] == [[party], [None, party]]
    assert ("/v1/" + KEY + "/") in [path for path, _ in node.requests]
    assert source.requests_made == 2


async def test_local_networks_read_logs_from_their_own_node(node: FakeNode, tmp_path: Path) -> None:
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(31337))
    node.routes["rpc:eth_blockNumber"] = rpc_ok(hex(3))
    node.handlers["rpc:eth_getBlockByNumber"] = lambda params: rpc_ok({"transactions": []})
    node.routes["rpc:eth_getLogs"] = rpc_ok([])
    env = {
        "ETHEREUM_LOCALNET_RPC_SERVER": SecretStr(node.url),
        "ETHEREUM_LOCALNET_HISTORY_SCOPE": SecretStr("all"),
    }
    settings = Settings(requests_per_second=1000, max_retries=0, env=env)
    adapter = indexer_factory(ETHEREUM)("localnet", settings, None)
    try:
        source = adapter._history_source()
        assert await walk(source, RECEIVER) == []
        assert source.requests_made == 0
    finally:
        await adapter.aclose()
    assert len(node.rpc_params("eth_getLogs")) == 2


@pytest.mark.parametrize(
    "reply",
    [rpc_ok("none"), rpc_ok([5]), rpc_ok([{"transactionHash": "0x1"}])],
)
async def test_malformed_logs_are_reported(node: FakeNode, make_source: Any, reply: Reply) -> None:
    serve_testnet(node, {}, TOKEN_BLOCK)
    node.routes["rpc:eth_getLogs"] = reply
    source = make_source(
        BNB_TESTNET_HISTORY_SCOPE="all", BNB_TESTNET_LOGS_SERVER=node.url, NODEREAL_API_KEY=KEY
    )
    with pytest.raises(MalformedResponse, match="eth_getLogs"):
        await source.account_page(
            RECEIVER, from_height=TOKEN_BLOCK, to_height=TOKEN_BLOCK, cursor=None
        )
