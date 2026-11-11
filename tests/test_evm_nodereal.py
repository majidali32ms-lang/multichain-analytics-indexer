from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from multichain_indexer import (
    ConfigError,
    InvalidInput,
    MalformedResponse,
    NetworkError,
    RateLimited,
)
from multichain_indexer.adapters.evm import nodereal as nodereal_module
from multichain_indexer.adapters.evm.adapter import indexer_factory
from multichain_indexer.adapters.evm.nodereal import NodeRealHistory
from multichain_indexer.adapters.evm.profiles import BNB, ETHEREUM
from multichain_indexer.core.config import Settings
from tests.conftest import FakeNode, Reply

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "evm" / "history_bsc_nodereal.json").read_text("utf-8")
)
ADDRESS = FIXTURE["address"]
TOP, BOTTOM = FIXTURE["to_block"], FIXTURE["from_block"]
KEY = "nodereal-test-key-0123456789abcd"
METHOD = "rpc:nr_getAssetTransfers"


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "result": result})


def rpc_error(code: int, message: str) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


def saved_hashes() -> list[str]:
    return [
        transfer["hash"]
        for exchange in FIXTURE["exchanges"]
        for transfer in exchange["reply"]["result"].get("transfers") or []
    ]


def replay(exchanges: list[dict[str, Any]]) -> Callable[[list[dict[str, Any]]], Reply]:
    """Answer each call with the saved reply to the same direction and page key."""

    def handle(params: list[dict[str, Any]]) -> Reply:
        (asked,) = params
        for exchange in exchanges:
            saved = exchange["params"]
            same_direction = all(
                asked.get(name) == saved.get(name) for name in ("fromAddress", "toAddress")
            )
            if same_direction and asked.get("pageKey") == saved.get("pageKey"):
                return Reply(exchange["reply"])
        return rpc_ok({"transfers": [], "pageKey": ""})

    return handle


def settings_for(node: FakeNode, tmp_path: Path, network: str = "mainnet", **env: str) -> Settings:
    prefix = f"BNB_{network.upper()}"
    values = {
        f"{prefix}_RPC_SERVER": node.url,
        f"{prefix}_HISTORY_SERVER": node.url,
        "NODEREAL_API_KEY": KEY,
        **env,
    }
    return Settings(
        database=tmp_path / "bnb.db",
        requests_per_second=1000,
        max_retries=0,
        env={name: SecretStr(value) for name, value in values.items() if value},
    )


def source_for(settings: Settings, network: str = "mainnet") -> NodeRealHistory:
    adapter = indexer_factory(BNB)(network, settings, None)
    history = adapter._history_source()
    assert isinstance(history, NodeRealHistory)
    return history


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nodereal_module, "REQUESTS_PER_SECOND", 1000)


def test_saved_replies_are_one_bsc_address_paged_one_transfer_at_a_time() -> None:
    assert FIXTURE["network"] == "BNB Smart Chain mainnet"
    assert len(saved_hashes()) == 2
    assert TOP - BOTTOM + 1 == nodereal_module.WINDOW_BLOCKS
    assert KEY not in json.dumps(FIXTURE)


async def test_one_window_holds_both_directions_newest_first(
    node: FakeNode, tmp_path: Path
) -> None:
    node.handlers[METHOD] = replay(FIXTURE["exchanges"])
    history = source_for(settings_for(node, tmp_path))
    try:
        page = await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()
    assert list(page.hashes) == saved_hashes()
    assert page.next_cursor == str(BOTTOM - 1)
    asked = [params[0] for method, params in node.rpc_calls if method == "nr_getAssetTransfers"]
    assert [("fromAddress" in call, "toAddress" in call) for call in asked] == [
        (True, False),
        (True, False),
        (True, False),
        (False, True),
    ]
    assert all(call["category"] == ["external"] and call["order"] == "desc" for call in asked)
    assert all((call["fromBlock"], call["toBlock"]) == (hex(BOTTOM), hex(TOP)) for call in asked)
    assert asked[1]["pageKey"] == FIXTURE["exchanges"][0]["reply"]["result"]["pageKey"]


async def test_key_goes_in_the_path_and_never_in_a_message(node: FakeNode, tmp_path: Path) -> None:
    node.routes[METHOD] = Reply({"message": "down"}, status=500)
    history = source_for(settings_for(node, tmp_path))
    try:
        with pytest.raises(NetworkError) as caught:
            await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()
    assert node.requests[0][0] == f"/v1/{KEY}/"
    assert KEY not in str(caught.value)


async def test_windows_walk_down_to_the_lowest_height(node: FakeNode, tmp_path: Path) -> None:
    node.handlers[METHOD] = replay([])
    node.routes["rpc:eth_blockNumber"] = rpc_ok(hex(2_500_000))
    history = source_for(settings_for(node, tmp_path))
    cursors = []
    try:
        cursor = None
        while True:
            page = await history.account_page(
                ADDRESS, from_height=200_000, to_height=None, cursor=cursor
            )
            cursors.append(page.next_cursor)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    finally:
        await history.aclose()
        await history._rpc.aclose()
    assert cursors == ["1500000", "500000", None]
    assert history.requests_made == 6
    windows = {
        (params[0]["fromBlock"], params[0]["toBlock"])
        for method, params in node.rpc_calls
        if method == "nr_getAssetTransfers"
    }
    assert windows == {
        (hex(1_500_001), hex(2_500_000)),
        (hex(500_001), hex(1_500_000)),
        (hex(200_000), hex(500_000)),
    }


async def test_a_transaction_to_oneself_is_listed_once(node: FakeNode, tmp_path: Path) -> None:
    exchanges = copy.deepcopy(FIXTURE["exchanges"][:1])
    received = copy.deepcopy(exchanges[0])
    received["params"] = {**received["params"], "toAddress": ADDRESS}
    del received["params"]["fromAddress"]
    received["reply"]["result"]["pageKey"] = ""
    exchanges[0]["reply"]["result"]["pageKey"] = ""
    node.handlers[METHOD] = replay([*exchanges, received])
    history = source_for(settings_for(node, tmp_path))
    try:
        page = await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()
    assert list(page.hashes) == saved_hashes()[:1]


@pytest.mark.parametrize(
    ("reply", "error", "message"),
    [
        (rpc_error(-32005, "ran out of cu"), RateLimited, "compute units"),
        (rpc_error(-32601, "method not available"), ConfigError, "BSC mainnet only"),
        (rpc_error(-32000, "range must be less than 2000000"), NetworkError, "2000000"),
        (Reply({"jsonrpc": "2.0", "id": 1, "error": "busy"}), NetworkError, "busy"),
        (rpc_ok("none"), MalformedResponse, "result object"),
        (rpc_ok({"transfers": "none"}), MalformedResponse, "list of transfers"),
        (rpc_ok({"transfers": [{"blockNum": "0x1"}]}), MalformedResponse, "transaction hash"),
        (rpc_ok({"transfers": [{"hash": "0x1", "blockNum": 1}]}), MalformedResponse, "hex"),
        (rpc_ok({"transfers": [], "pageKey": 5}), MalformedResponse, "page key"),
    ],
)
async def test_errors_map_to_library_errors(
    node: FakeNode, tmp_path: Path, reply: Reply, error: type[Exception], message: str
) -> None:
    node.routes[METHOD] = reply
    history = source_for(settings_for(node, tmp_path))
    try:
        with pytest.raises(error, match=message):
            await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()


async def test_a_reply_without_transfers_is_an_empty_window(node: FakeNode, tmp_path: Path) -> None:
    node.routes[METHOD] = rpc_ok({})
    history = source_for(settings_for(node, tmp_path))
    try:
        page = await history.account_page(ADDRESS, from_height=TOP - 10, to_height=TOP, cursor=None)
    finally:
        await history.aclose()
    assert (page.hashes, page.next_cursor) == ((), None)


async def test_cursor_from_elsewhere_is_refused(node: FakeNode, tmp_path: Path) -> None:
    history = source_for(settings_for(node, tmp_path))
    try:
        with pytest.raises(InvalidInput, match="not a NodeReal history cursor"):
            await history.account_page(ADDRESS, from_height=None, to_height=None, cursor="12:3")
    finally:
        await history.aclose()


def test_bnb_mainnet_defaults_to_nodereal(node: FakeNode, tmp_path: Path) -> None:
    assert isinstance(source_for(settings_for(node, tmp_path)), NodeRealHistory)


def test_missing_key_names_the_setting(node: FakeNode, tmp_path: Path) -> None:
    adapter = indexer_factory(BNB)(
        "mainnet", settings_for(node, tmp_path, NODEREAL_API_KEY=""), None
    )
    with pytest.raises(ConfigError, match=r"dashboard\.nodereal\.io and set NODEREAL_API_KEY"):
        adapter._history_source()


@pytest.mark.parametrize(("chain", "network"), [(BNB, "testnet"), (ETHEREUM, "mainnet")])
def test_other_networks_are_refused(chain: Any, network: str) -> None:
    env = {
        f"{chain.name}_{network}_HISTORY_SOURCE".upper(): SecretStr("nodereal"),
        "NODEREAL_API_KEY": SecretStr(KEY),
    }
    adapter = indexer_factory(chain)(network, Settings(env=env), None)
    with pytest.raises(ConfigError, match="BNB Smart Chain mainnet only"):
        adapter._history_source()


def test_key_is_masked_in_logs() -> None:
    assert KEY in Settings(env={"NODEREAL_API_KEY": SecretStr(KEY)}).secret_values()


def lagging(indexed: int) -> Callable[[list[dict[str, Any]]], Reply]:
    def handle(params: list[dict[str, Any]]) -> Reply:
        if int(params[0]["toBlock"], 16) > indexed:
            return rpc_error(-32000, "blockNum not reached")
        return rpc_ok({"transfers": [], "pageKey": ""})

    return handle


async def test_top_is_lowered_until_nodereal_has_indexed_it(node: FakeNode, tmp_path: Path) -> None:
    node.handlers[METHOD] = lagging(TOP - 250)
    history = source_for(settings_for(node, tmp_path))
    try:
        assert history.indexed_up_to is None
        page = await history.account_page(
            ADDRESS, from_height=TOP - 1000, to_height=TOP, cursor=None
        )
    finally:
        await history.aclose()
    assert page.next_cursor is None
    assert history.indexed_up_to == TOP - 300


async def test_a_service_that_never_catches_up_is_reported(node: FakeNode, tmp_path: Path) -> None:
    node.handlers[METHOD] = lagging(0)
    history = source_for(settings_for(node, tmp_path))
    try:
        with pytest.raises(NetworkError, match="has not indexed the newest blocks yet"):
            await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()


async def test_checkpoint_never_passes_what_nodereal_indexed(
    node: FakeNode, tmp_path: Path
) -> None:
    node.handlers[METHOD] = lagging(TOP - 250)
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(56))
    node.routes["rpc:eth_blockNumber"] = rpc_ok(hex(TOP))
    node.routes["rpc:eth_getBlockByNumber"] = rpc_ok({"number": hex(TOP - 2)})
    adapter = indexer_factory(BNB)("mainnet", settings_for(node, tmp_path), None)
    try:
        head = await adapter.chain_head()
        pages = [
            page
            async for page in adapter.iter_account_transactions(
                ADDRESS, from_height=TOP - 1000, to_height=head
            )
        ]
        assert pages == []
        assert adapter.final_height(head) == TOP - 300
    finally:
        await adapter.aclose()


async def test_all_scope_adds_bep20_transfers_to_the_same_calls(
    node: FakeNode, tmp_path: Path
) -> None:
    node.handlers[METHOD] = replay(FIXTURE["exchanges"])
    history = source_for(settings_for(node, tmp_path, BNB_MAINNET_HISTORY_SCOPE="all"))
    try:
        await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()
    categories = {
        tuple(params[0]["category"])
        for method, params in node.rpc_calls
        if method == "nr_getAssetTransfers"
    }
    assert categories == {("external", "20")}
