"""BEP-20 holder reports: Transfer logs over a block range, balances at its last block."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from eth_abi.abi import encode
from pydantic import SecretStr

from multichain_indexer import (
    AsyncIndexer,
    ConfigError,
    InvalidInput,
    MalformedResponse,
    NetworkError,
    UnsupportedChain,
)
from multichain_indexer.adapters.evm import balances as balances_module
from multichain_indexer.adapters.evm import holders as holders_module
from multichain_indexer.adapters.evm import logs as logs_module
from multichain_indexer.adapters.evm import nodereal as nodereal_module
from multichain_indexer.adapters.evm.adapter import EvmIndexer, indexer_factory
from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.logs import iter_logs, logs_rpc, nodereal_rpc
from multichain_indexer.adapters.evm.models import EvmTokenHolders
from multichain_indexer.adapters.evm.profiles import BNB, ETHEREUM, OPBNB
from multichain_indexer.adapters.evm.transactions import TRANSFER_TOPIC
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.http import ApiClient
from tests.conftest import FakeNode, Reply

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
SAVED = json.loads((FIXTURES / "holders_opbnb_mainnet.json").read_text("utf-8"))
COUNT = json.loads((FIXTURES / "nodereal_holder_count.json").read_text("utf-8"))
TOKEN = SAVED["token"]
LOW, HIGH = SAVED["from_block"], SAVED["to_block"]
KEY = "nodereal-test-key-0123456789abcd"


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "result": result})


def rpc_error(code: int, message: str) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


def saved_logs() -> list[dict[str, Any]]:
    (exchange,) = [e for e in SAVED["exchanges"] if e["method"] == "eth_getLogs"]
    logs: list[dict[str, Any]] = copy.deepcopy(exchange["reply"]["result"])
    return logs


def replay(node: FakeNode, exchanges: list[dict[str, Any]] = SAVED["exchanges"]) -> None:
    """Answer each method with the saved reply to the same params."""
    by_method: dict[str, dict[str, Any]] = {}
    for exchange in exchanges:
        by_method.setdefault(exchange["method"], {})[json.dumps(exchange["params"])] = exchange

    def handler(method: str) -> Callable[[list[Any]], Reply]:
        def answer(params: list[Any]) -> Reply:
            saved = by_method[method].get(json.dumps(params))
            if saved is None:
                return rpc_error(-32000, f"no saved reply for {method} {params}")
            return Reply(saved["reply"])

        return answer

    for method in by_method:
        node.handlers[f"rpc:{method}"] = handler(method)


def settings_for(node: FakeNode, tmp_path: Path, **env: str) -> Settings:
    values = {
        "OPBNB_MAINNET_RPC_SERVER": node.url,
        "OPBNB_MAINNET_LOGS_SERVER": node.url,
        "OPBNB_MAINNET_HISTORY_SERVER": node.url,
        "NODEREAL_API_KEY": KEY,
        **env,
    }
    return Settings(
        database=tmp_path / "holders.db",
        requests_per_second=1000,
        max_retries=0,
        env={name: SecretStr(value) for name, value in values.items() if value},
    )


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs_module, "LOGS_REQUESTS_PER_SECOND", 1000)
    monkeypatch.setattr(nodereal_module, "REQUESTS_PER_SECOND", 1000)


@pytest.fixture
async def opbnb(node: FakeNode, tmp_path: Path) -> AsyncIterator[EvmIndexer]:
    adapter = indexer_factory(OPBNB)("mainnet", settings_for(node, tmp_path), None)
    assert isinstance(adapter, EvmIndexer)
    yield adapter
    await adapter.aclose()


@pytest.fixture
async def rpc(node: FakeNode) -> AsyncIterator[EvmRpc]:
    client = EvmRpc(ApiClient(SecretStr(node.url), max_retries=0))
    yield client
    await client.aclose()


def test_saved_report_is_a_real_opbnb_range() -> None:
    hosts = {exchange["host"] for exchange in SAVED["exchanges"]}
    assert hosts == {"opbnb-mainnet-rpc.bnbchain.org", "opbnb-mainnet.nodereal.io"}
    assert SAVED["report"]["transfers"] == len(saved_logs()) == 57
    assert KEY not in json.dumps(SAVED)


async def test_report_matches_the_saved_run(node: FakeNode, opbnb: EvmIndexer) -> None:
    replay(node)
    report = await opbnb.fetch_token_holders(TOKEN, from_block=LOW, to_block=HIGH)
    expected = {**SAVED["report"], "observed_at": report.observed_at}
    assert report == EvmTokenHolders.model_validate(expected)
    assert report.holders_at_end + report.exited_holders <= report.active_addresses
    multicall = [p for p in node.rpc_params("eth_call") if p[0]["to"] == balances_module.MULTICALL3]
    assert len(multicall) == 1


async def test_starting_balances_follow_from_the_transfers(
    node: FakeNode, opbnb: EvmIndexer
) -> None:
    replay(node)
    report = await opbnb.fetch_token_holders(TOKEN, from_block=LOW, to_block=HIGH)
    moves = holders_module._moves(saved_logs())
    net: dict[str, int] = {}
    for move in moves:
        net[move.receiver] = net.get(move.receiver, 0) + move.value
        net[move.sender] = net.get(move.sender, 0) - move.value
    top = report.top_holders[0]
    assert top.share_of_supply == pytest.approx(top.balance / report.total_supply)
    assert sum(1 for value in net.values() if value) <= report.active_addresses + 1


async def test_nodereal_count_is_labelled_with_its_cost(node: FakeNode, opbnb: EvmIndexer) -> None:
    replay(node)
    node.routes["rpc:nr_getTokenHolderCount"] = Reply(COUNT["replies"]["bnb mainnet"])
    report = await opbnb.fetch_token_holders(
        TOKEN, from_block=LOW, to_block=HIGH, nodereal_count=True
    )
    assert report.nodereal_holder_count == int("0x1cecbe", 16)
    assert report.nodereal_compute_units == 50
    assert node.rpc_params("nr_getTokenHolderCount") == [[report.token]]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (Reply(COUNT["replies"]["opbnb testnet"]), None),
        (Reply(COUNT["replies"]["bnb testnet"]), ConfigError),
        (rpc_ok({"result": 5}), MalformedResponse),
    ],
)
async def test_nodereal_count_replies(
    node: FakeNode, tmp_path: Path, reply: Reply, expected: type[Exception] | None
) -> None:
    node.routes["rpc:nr_getTokenHolderCount"] = reply
    settings = settings_for(node, tmp_path)
    if expected is None:
        assert (
            await nodereal_module.nodereal_holder_count(settings, "opbnb", "mainnet", TOKEN) is None
        )
        return
    with pytest.raises(expected):
        await nodereal_module.nodereal_holder_count(settings, "opbnb", "mainnet", TOKEN)


async def test_nodereal_count_needs_a_served_network_and_a_key() -> None:
    with pytest.raises(ConfigError, match="holder count serves"):
        await nodereal_module.nodereal_holder_count(Settings(), "ethereum", "mainnet", TOKEN)
    with pytest.raises(ConfigError, match="set NODEREAL_API_KEY"):
        await nodereal_module.nodereal_holder_count(Settings(), "bnb", "mainnet", TOKEN)


async def test_range_checks(node: FakeNode, opbnb: EvmIndexer) -> None:
    replay(node)
    head = int(
        next(e for e in SAVED["exchanges"] if e["method"] == "eth_blockNumber")["reply"]["result"],
        16,
    )
    with pytest.raises(InvalidInput, match="is past the newest block"):
        await opbnb.fetch_token_holders(TOKEN, from_block=LOW, to_block=head + 1)
    with pytest.raises(InvalidInput, match="is after --to-block"):
        await opbnb.fetch_token_holders(TOKEN, from_block=HIGH, to_block=LOW)


async def test_too_many_addresses_stop_the_report_early(
    node: FakeNode, opbnb: EvmIndexer, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay(node)
    monkeypatch.setattr(holders_module, "MAX_ADDRESSES", 5)
    with pytest.raises(InvalidInput, match="more than 5 addresses moved"):
        await opbnb.fetch_token_holders(TOKEN, from_block=LOW, to_block=HIGH)
    assert node.rpc_params("eth_call") == []


async def test_pruned_state_names_the_fix(node: FakeNode, tmp_path: Path) -> None:
    replay(node)
    node.handlers.pop("rpc:eth_getCode")
    node.routes["rpc:eth_getCode"] = rpc_error(-32000, "missing trie node")
    adapter = indexer_factory(OPBNB)("mainnet", settings_for(node, tmp_path), None)
    try:
        with pytest.raises(InvalidInput) as refused:
            await adapter.fetch_token_holders(TOKEN, from_block=LOW, to_block=HIGH)
    finally:
        await adapter.aclose()
    assert f"no longer keeps the state of block {HIGH}; set NODEREAL_API_KEY" in str(refused.value)


async def test_other_state_errors_are_not_hidden(node: FakeNode, opbnb: EvmIndexer) -> None:
    replay(node)
    node.handlers.pop("rpc:eth_getCode")
    node.routes["rpc:eth_getCode"] = rpc_error(-32000, "busy")
    with pytest.raises(NetworkError, match="busy"):
        await opbnb.fetch_token_holders(TOKEN, from_block=LOW, to_block=HIGH)


async def test_without_a_key_state_is_read_at_the_newest_block(
    node: FakeNode, tmp_path: Path
) -> None:
    replay(node)
    head = next(e for e in SAVED["exchanges"] if e["method"] == "eth_blockNumber")["reply"][
        "result"
    ]
    settings = settings_for(node, tmp_path, NODEREAL_API_KEY="")
    adapter = indexer_factory(OPBNB)("mainnet", settings, None)
    try:
        with pytest.raises(NetworkError, match="no saved reply"):
            await adapter.fetch_token_holders(TOKEN, from_block=LOW)
    finally:
        await adapter.aclose()
    (log_filter,) = node.rpc_params("eth_getLogs")[0]
    assert log_filter["toBlock"] == head
    assert "end the range within" in adapter._state_hint()


def test_networks_nodereal_does_not_serve_have_their_own_hint() -> None:
    adapter = indexer_factory(ETHEREUM)("mainnet", Settings(), None)
    assert "archive endpoint" in adapter._state_hint()


async def test_bnb_smart_chain_logs_need_the_key(node: FakeNode, tmp_path: Path) -> None:
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(56))
    node.routes["rpc:eth_blockNumber"] = rpc_ok(hex(HIGH))
    node.routes["rpc:eth_getFinalizedHeader"] = rpc_ok({"number": hex(HIGH - 2)})
    settings = Settings(env={"BNB_MAINNET_RPC_SERVER": SecretStr(node.url)}, max_retries=0)
    adapter = indexer_factory(BNB)("mainnet", settings, None)
    try:
        with pytest.raises(ConfigError, match="Transfer logs on BNB Smart Chain mainnet"):
            await adapter.fetch_token_holders(TOKEN, from_block=LOW)
    finally:
        await adapter.aclose()


async def test_bnb_smart_chain_reads_logs_and_state_from_nodereal(
    node: FakeNode, tmp_path: Path
) -> None:
    exchanges = [e for e in SAVED["exchanges"] if e["method"] not in ("eth_chainId",)]
    replay(node, exchanges)
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(56))
    node.routes["rpc:eth_getFinalizedHeader"] = rpc_ok({"number": hex(HIGH)})
    values = {"BNB_MAINNET_RPC_SERVER": node.url, "BNB_MAINNET_LOGS_SERVER": node.url}
    settings = Settings(
        requests_per_second=1000,
        max_retries=0,
        env={**{k: SecretStr(v) for k, v in values.items()}, "NODEREAL_API_KEY": SecretStr(KEY)},
    )
    adapter = indexer_factory(BNB)("mainnet", settings, None)
    try:
        report = await adapter.fetch_token_holders(TOKEN, from_block=LOW)
        requests = adapter.requests_made
    finally:
        await adapter.aclose()
    assert (report.to_block, report.transfers) == (HIGH, 57)
    assert requests == len(node.rpc_calls)


async def test_balances_without_multicall_are_read_one_by_one(node: FakeNode, rpc: EvmRpc) -> None:
    node.routes["rpc:eth_getCode"] = rpc_ok("0x")
    node.routes["rpc:eth_call"] = rpc_ok("0x" + encode(["uint256"], [7]).hex())
    holders = ["0x" + "11" * 20, "0x" + "22" * 20]
    assert await balances_module.read_balances(rpc, TOKEN, holders, HIGH) == dict.fromkeys(
        holders, 7
    )
    assert await balances_module.read_balances(rpc, TOKEN, [], HIGH) == {}


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (rpc_ok("0x1234"), MalformedResponse),
        (rpc_ok("0x" + encode(["(bool,bytes)[]"], [[]]).hex()), MalformedResponse),
        (rpc_ok("0x" + encode(["(bool,bytes)[]"], [[(False, b"")]]).hex()), InvalidInput),
    ],
)
async def test_multicall_replies_are_checked(
    node: FakeNode, rpc: EvmRpc, reply: Reply, error: type[Exception]
) -> None:
    node.routes["rpc:eth_getCode"] = rpc_ok("0x60")
    node.routes["rpc:eth_call"] = reply
    with pytest.raises(error):
        await balances_module.read_balances(rpc, TOKEN, ["0x" + "11" * 20], HIGH)


async def test_multicall_batches_hold_at_most_its_limit(
    node: FakeNode, rpc: EvmRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(balances_module, "CALLS_PER_BATCH", 2)
    node.routes["rpc:eth_getCode"] = rpc_ok("0x60")
    word = encode(["uint256"], [3])
    node.routes["rpc:eth_call"] = [
        rpc_ok("0x" + encode(["(bool,bytes)[]"], [[(True, word), (True, word)]]).hex()),
        rpc_ok("0x" + encode(["(bool,bytes)[]"], [[(True, word)]]).hex()),
    ]
    holders = ["0x" + f"{n:02x}" * 20 for n in range(1, 4)]
    assert await balances_module.read_balances(rpc, TOKEN, holders, HIGH) == dict.fromkeys(
        holders, 3
    )


async def test_a_token_without_total_supply_is_refused(node: FakeNode, rpc: EvmRpc) -> None:
    node.routes["rpc:eth_call"] = rpc_ok("0x")
    with pytest.raises(InvalidInput, match=r"did not answer totalSupply\(\)"):
        await balances_module.read_total_supply(rpc, TOKEN, HIGH)


async def test_log_windows_halve_when_refused_as_too_large(
    node: FakeNode, rpc: EvmRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logs_module, "FIRST_WINDOW_BLOCKS", 8)

    def logs(params: list[dict[str, Any]]) -> Reply:
        (log_filter,) = params
        span = int(log_filter["toBlock"], 16) - int(log_filter["fromBlock"], 16) + 1
        if span > 2:
            return rpc_error(-32005, "logs count exceeds the limit 50000")
        return rpc_ok([{"blockNumber": log_filter["fromBlock"]}])

    node.handlers["rpc:eth_getLogs"] = logs
    windows = [window async for window in iter_logs(rpc, 0, 5, [TRANSFER_TOPIC], TOKEN)]
    assert len(windows) == 3
    spans = [
        int(p[0]["toBlock"], 16) - int(p[0]["fromBlock"], 16) + 1
        for p in node.rpc_params("eth_getLogs")
    ]
    assert spans == [6, 4, 2, 2, 2]
    assert node.rpc_params("eth_getLogs")[0][0]["address"] == TOKEN


@pytest.mark.parametrize(
    ("message", "calls"),
    [("limit exceeded", 4), ("method disabled", 1)],
    ids=["too large down to one block", "other refusal"],
)
async def test_log_refusals_that_never_ease_are_raised(
    node: FakeNode, rpc: EvmRpc, monkeypatch: pytest.MonkeyPatch, message: str, calls: int
) -> None:
    monkeypatch.setattr(logs_module, "FIRST_WINDOW_BLOCKS", 8)
    node.routes["rpc:eth_getLogs"] = rpc_error(-32005, message)
    with pytest.raises(NetworkError, match=message):
        _ = [window async for window in iter_logs(rpc, 0, 20, [TRANSFER_TOPIC])]
    assert len(node.rpc_params("eth_getLogs")) == calls
    assert "address" not in node.rpc_params("eth_getLogs")[0][0]


def test_nodereal_rpc_needs_a_served_network_and_a_key() -> None:
    keyed = Settings(env={"NODEREAL_API_KEY": SecretStr(KEY)})
    assert nodereal_rpc(ETHEREUM, "mainnet", keyed) is None
    assert nodereal_rpc(OPBNB, "mainnet", Settings()) is None
    own = EvmRpc(ApiClient(SecretStr("https://example.invalid")))
    assert logs_rpc(OPBNB, "mainnet", Settings(), own, "logs") is own


async def test_api_and_cli_report_holders(node: FakeNode, tmp_path: Path) -> None:
    replay(node)
    settings = settings_for(node, tmp_path)
    async with AsyncIndexer("opbnb", "mainnet", settings=settings) as indexer:
        report = await indexer.token_holders(TOKEN, from_block=LOW, to_block=HIGH)
    assert report.transfers == 57
    async with AsyncIndexer("algorand", settings=settings) as indexer:
        with pytest.raises(UnsupportedChain, match="has no token holder report"):
            await indexer.token_holders(TOKEN, from_block=LOW)


def invoke_holders(node: FakeNode, tmp_path: Path, *extra: str, **env: str) -> Any:
    values = {
        "OPBNB_MAINNET_RPC_SERVER": node.url,
        "OPBNB_MAINNET_LOGS_SERVER": node.url,
        "OPBNB_MAINNET_HISTORY_SERVER": node.url,
        "NODEREAL_API_KEY": KEY,
        "MCI_REQUESTS_PER_SECOND": "1000",
        "MCI_MAX_RETRIES": "0",
        **env,
    }
    arguments = [
        "--chain", "opbnb", "--network", "mainnet",
        "--db", str(tmp_path / "cli.db"), "--env-file", str(tmp_path / "none.env"),
        "index", "holders", TOKEN, "--from-block", str(LOW), "--to-block", str(HIGH), *extra,
    ]  # fmt: skip
    return CliRunner(env=values).invoke(cli, arguments)


def test_cli_names_the_range_and_its_time(node: FakeNode, tmp_path: Path) -> None:
    replay(node)
    node.routes["rpc:nr_getTokenHolderCount"] = Reply(COUNT["replies"]["bnb mainnet"])
    result = invoke_holders(node, tmp_path, "--nodereal-count")
    assert result.exit_code == 0, result.output
    lines = [" ".join(line.split()) for line in result.output.splitlines()]
    assert f"Blocks {LOW} to {HIGH}" in lines
    assert "Time covered 2026-11-19 17:10:46.500 UTC to 2026-11-19 17:11:01.500 UTC" in lines
    assert "Holders at the end 26 of the addresses active in the range" in lines
    assert "Holders, whole chain 1895614 according to NodeReal (50 compute units)" in lines
    assert "2026-11-19 57" in lines


def test_cli_prints_json(node: FakeNode, tmp_path: Path) -> None:
    replay(node)
    result = invoke_holders(node, tmp_path, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["transfers"] == 57


def test_cli_holders_is_for_evm_chains(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["--db", str(tmp_path / "x.db"), "index", "holders", TOKEN, "--from-block", "1"],
    )
    assert result.exit_code == 1
    assert "index holders is for ethereum" in result.output


def transfer_log(block: int, sender: int, receiver: int, value: int) -> dict[str, Any]:
    return {
        "address": TOKEN,
        "blockNumber": hex(block),
        "logIndex": "0x0",
        "topics": [TRANSFER_TOPIC, "0x" + f"{sender:064x}", "0x" + f"{receiver:064x}"],
        "data": "0x" + f"{value:064x}",
    }


async def test_days_are_split_at_utc_midnight_by_bisection(node: FakeNode, rpc: EvmRpc) -> None:
    # One block an hour from 2026-11-18 20:00 UTC: block 4 is the first of 19 November.
    start = 1_795_032_000

    def header(params: list[Any]) -> Reply:
        return rpc_ok({"number": params[0], "timestamp": hex(start + int(params[0], 16) * 3600)})

    nft = {
        **transfer_log(5, 1, 2, 0),
        "topics": [*transfer_log(5, 1, 2, 0)["topics"], "0x" + "0" * 63 + "9"],
        "data": "0x",
    }
    node.handlers["rpc:eth_getBlockByNumber"] = header
    node.routes["rpc:eth_getLogs"] = rpc_ok(
        [
            transfer_log(1, 0, 1, 5),
            transfer_log(3, 1, 2, 2),
            transfer_log(4, 2, 3, 1),
            nft,
            transfer_log(9, 1, 3, 1),
        ]
    )
    node.routes["rpc:eth_getCode"] = rpc_ok("0x")
    node.routes["rpc:eth_call"] = rpc_ok("0x" + encode(["uint256"], [0]).hex())
    query = holders_module.HolderQuery("opbnb", "mainnet", TOKEN, "BEP-20", 0, 10)
    sources = holders_module.HolderSources(rpc, rpc, rpc, "hint")
    report = await holders_module.token_holders(sources, query)
    assert report.transfers_per_day == {"2026-11-18": 2, "2026-11-19": 2}
    assert report.transfers == 4
    assert (report.total_supply, report.top_share_of_supply, report.top_holders) == (0, None, ())
    assert report.inconsistent_addresses == 3
