from __future__ import annotations

import copy
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from multichain_indexer import (
    ConfigError,
    Indexer,
    InvalidInput,
    MalformedResponse,
    NetworkError,
    NotFound,
    RateLimited,
    UnsupportedChain,
)
from multichain_indexer.adapters.evm import EvmIndexer
from multichain_indexer.adapters.evm import explorer as explorer_module
from multichain_indexer.adapters.evm import scan as scan_module
from multichain_indexer.adapters.evm.adapter import indexer_factory
from multichain_indexer.adapters.evm.explorer import BLOCKSCOUT, ETHERSCAN, ExplorerHistory
from multichain_indexer.adapters.evm.profiles import BASE, BNB, ETHEREUM, POLYGON, EvmChain
from multichain_indexer.adapters.evm.transactions import RawTransaction, parse_transaction
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.http import ApiClient
from multichain_indexer.core.models import Status
from tests.conftest import FakeNode, Reply

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
TXLIST = json.loads((FIXTURES / "history_sepolia_txlist.json").read_text("utf-8"))
ACCOUNT = TXLIST["address"]
ROWS: list[dict[str, Any]] = TXLIST["reply"]["result"]
NATIVE = json.loads((FIXTURES / "tx_sepolia_native.json").read_text("utf-8"))
NATIVE_HASH = NATIVE["transaction"]["hash"]
NATIVE_BLOCK = int(NATIVE["receipt"]["blockNumber"], 16)
SEPOLIA = 11155111
EXPLORER_PATH = "/v2/api"
BLOCKSCOUT_KEY = "proapi_test-key-value"
ETHERSCAN_KEY = "etherscan-test-key-value"
SMALL_PAGE = 5


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "result": result})


def txlist_reply(rows: list[dict[str, Any]]) -> Reply:
    if not rows:
        return Reply({"status": "0", "message": "No transactions found", "result": []})
    return Reply({"status": "1", "message": "OK", "result": rows})


def explorer_server(rows: list[dict[str, Any]]) -> Callable[[dict[str, str]], Reply]:
    """Answer txlist as an explorer does: newest first, within the block range, paged."""

    def handle(query: dict[str, str]) -> Reply:
        top = None if query["endblock"] == "latest" else int(query["endblock"])
        start = int(query["startblock"])
        size, page = int(query["offset"]), int(query["page"])
        matching = [
            row
            for row in rows
            if start <= int(row["blockNumber"]) and (top is None or int(row["blockNumber"]) <= top)
        ]
        return txlist_reply(matching[(page - 1) * size : page * size])

    return handle


def rpc_server(node: FakeNode, finalized: int) -> None:
    """Serve the saved Sepolia transfer and a finalized block."""

    def block(params: list[Any]) -> Reply:
        if params[0] == "finalized":
            return rpc_ok({"number": hex(finalized)})
        return rpc_ok(NATIVE["block"])

    node.routes["rpc:eth_chainId"] = rpc_ok(hex(SEPOLIA))
    node.routes["rpc:eth_blockNumber"] = rpc_ok(hex(NATIVE_BLOCK + 100))
    node.routes["rpc:eth_getBalance"] = rpc_ok("0x0")
    node.routes["rpc:eth_getTransactionCount"] = rpc_ok("0x13")
    node.routes["rpc:eth_getCode"] = rpc_ok("0x")
    node.routes["rpc:eth_getTransactionByHash"] = rpc_ok(NATIVE["transaction"])
    node.routes["rpc:eth_getTransactionReceipt"] = rpc_ok(NATIVE["receipt"])
    node.handlers["rpc:eth_getBlockByNumber"] = block


def explorer_settings(
    node: FakeNode, tmp_path: Path, chain: EvmChain = ETHEREUM, network: str = "testnet", **env: str
) -> Settings:
    prefix = f"{chain.name}_{network}".upper()
    values = {
        f"{prefix}_RPC_SERVER": node.url,
        f"{prefix}_HISTORY_SERVER": node.url,
        "BLOCKSCOUT_API_KEY": BLOCKSCOUT_KEY,
        "ETHERSCAN_API_KEY": ETHERSCAN_KEY,
        **env,
    }
    return Settings(
        database=tmp_path / "evm.db",
        requests_per_second=1000,
        request_timeout=5,
        max_retries=0,
        env={key: SecretStr(value) for key, value in values.items() if value},
    )


def adapter_for(settings: Settings, chain: EvmChain = ETHEREUM, network: str = "testnet") -> Any:
    return indexer_factory(chain)(network, settings, None)


def explorer_history(node: FakeNode, service: Any = BLOCKSCOUT) -> ExplorerHistory:
    client = ApiClient(SecretStr(node.url), max_retries=0)
    key = SecretStr(BLOCKSCOUT_KEY if service is BLOCKSCOUT else ETHERSCAN_KEY)
    return ExplorerHistory(service, SEPOLIA, client, key)


async def walk(history: Any, **bounds: int | None) -> list[str]:
    hashes: list[str] = []
    cursor = None
    while True:
        page = await history.account_page(
            ACCOUNT,
            from_height=bounds.get("from_height"),
            to_height=bounds.get("to_height"),
            cursor=cursor,
        )
        hashes.extend(page.hashes)
        if page.next_cursor is None:
            return hashes
        cursor = page.next_cursor


@pytest.fixture
def small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explorer_module, "PAGE_SIZE", SMALL_PAGE)


@pytest.fixture
async def history(node: FakeNode) -> AsyncIterator[ExplorerHistory]:
    source = explorer_history(node)
    yield source
    await source.aclose()


def test_saved_history_is_one_sepolia_account_newest_first() -> None:
    blocks = [int(row["blockNumber"]) for row in ROWS]
    assert len(ROWS) == 20
    assert blocks == sorted(blocks, reverse=True)
    assert ROWS[0]["hash"] == NATIVE_HASH
    assert all(ACCOUNT in (row["from"], row["to"]) for row in ROWS)


@pytest.mark.usefixtures("small_pages")
async def test_pages_walk_every_transaction_once(node: FakeNode, history: ExplorerHistory) -> None:
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS)
    assert await walk(history) == [row["hash"] for row in ROWS]


@pytest.mark.usefixtures("small_pages")
async def test_many_transactions_in_one_block_are_not_repeated_or_lost(
    node: FakeNode, history: ExplorerHistory
) -> None:
    rows = copy.deepcopy(ROWS)
    for row in rows[3:15]:
        row["blockNumber"] = rows[3]["blockNumber"]
    node.handlers[EXPLORER_PATH] = explorer_server(rows)
    assert await walk(history) == [row["hash"] for row in rows]


@pytest.mark.usefixtures("small_pages")
async def test_block_range_is_passed_to_the_explorer(
    node: FakeNode, history: ExplorerHistory
) -> None:
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS)
    low, high = int(ROWS[12]["blockNumber"]), int(ROWS[2]["blockNumber"])
    hashes = await walk(history, from_height=low, to_height=high)
    assert hashes == [row["hash"] for row in ROWS[2:13]]
    first = node.requests_to(EXPLORER_PATH)[0]
    assert (first["startblock"], first["endblock"]) == (str(low), str(high))


async def test_blockscout_request_uses_its_universal_api_and_a_bearer_key(
    node: FakeNode, history: ExplorerHistory
) -> None:
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS)
    await history.account_page(ACCOUNT, from_height=None, to_height=None, cursor=None)
    query = node.requests_to(EXPLORER_PATH)[0]
    assert query == {
        "chain_id": str(SEPOLIA),
        "module": "account",
        "action": "txlist",
        "address": ACCOUNT,
        "startblock": "0",
        "endblock": "latest",
        "sort": "desc",
        "page": "1",
        "offset": "100",
    }


async def test_etherscan_request_carries_its_key_and_chainid(node: FakeNode) -> None:
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS)
    source = explorer_history(node, ETHERSCAN)
    try:
        page = await source.account_page(ACCOUNT, from_height=None, to_height=None, cursor=None)
    finally:
        await source.aclose()
    query = node.requests_to(EXPLORER_PATH)[0]
    assert query["chainid"] == str(SEPOLIA)
    assert query["apikey"] == ETHERSCAN_KEY
    assert "chain_id" not in query
    assert len(page.hashes) == 20


async def test_no_transactions_is_an_empty_last_page(
    node: FakeNode, history: ExplorerHistory
) -> None:
    node.routes[EXPLORER_PATH] = txlist_reply([])
    page = await history.account_page(ACCOUNT, from_height=None, to_height=None, cursor=None)
    assert (page.hashes, page.next_cursor) == ((), None)


@pytest.mark.parametrize(
    ("result", "error", "message"),
    [
        ("Missing/Invalid API Key", ConfigError, "Check BLOCKSCOUT_API_KEY"),
        ("Max calls per sec rate limit reached (3/sec)", RateLimited, "rate limit"),
        ("Query Timeout occured", NetworkError, "Query Timeout"),
    ],
)
async def test_explorer_errors_map_to_library_errors(
    node: FakeNode, history: ExplorerHistory, result: str, error: type[Exception], message: str
) -> None:
    node.routes[EXPLORER_PATH] = Reply({"status": "0", "message": "NOTOK", "result": result})
    with pytest.raises(error, match=message):
        await history.account_page(ACCOUNT, from_height=None, to_height=None, cursor=None)


@pytest.mark.parametrize(
    "body",
    [
        {"status": "1", "message": "OK", "result": "rows"},
        {"status": "1", "message": "OK", "result": [{"hash": 7, "blockNumber": "1"}]},
        {"status": "1", "message": "OK", "result": [{"hash": "0x1", "blockNumber": "0x1"}]},
    ],
)
async def test_malformed_explorer_replies_are_reported(
    node: FakeNode, history: ExplorerHistory, body: dict[str, Any]
) -> None:
    node.routes[EXPLORER_PATH] = Reply(body)
    with pytest.raises(MalformedResponse, match="Blockscout txlist"):
        await history.account_page(ACCOUNT, from_height=None, to_height=None, cursor=None)


@pytest.mark.parametrize("cursor", ["12", "a:1", "12:x", ""])
async def test_cursor_from_elsewhere_is_refused(history: ExplorerHistory, cursor: str) -> None:
    with pytest.raises(InvalidInput, match="not a history cursor"):
        await history.account_page(ACCOUNT, from_height=None, to_height=None, cursor=cursor or ":")


@pytest.mark.parametrize(
    ("service", "setting", "site"),
    [
        ("blockscout", "BLOCKSCOUT_API_KEY", "https://dev.blockscout.com"),
        ("etherscan", "ETHERSCAN_API_KEY", "https://etherscan.io/apidashboard"),
    ],
)
async def test_missing_key_names_the_setting_and_where_to_get_one(
    node: FakeNode, tmp_path: Path, service: str, setting: str, site: str
) -> None:
    rpc_server(node, NATIVE_BLOCK)
    settings = explorer_settings(
        node, tmp_path, **{setting: "", "ETHEREUM_TESTNET_HISTORY_SOURCE": service}
    )
    adapter = adapter_for(settings)
    try:
        assert (await adapter.fetch_account(ACCOUNT)).address.lower() == ACCOUNT
        with pytest.raises(ConfigError, match=f"create one at {site} and set {setting}"):
            await anext(adapter.iter_account_transactions(ACCOUNT))
    finally:
        await adapter.aclose()
    assert node.requests_to(EXPLORER_PATH) == []


def test_lookups_need_no_history_key() -> None:
    adapter = indexer_factory(ETHEREUM)(None, Settings(), None)
    assert adapter._history is None


async def test_history_records_come_from_the_rpc(node: FakeNode, tmp_path: Path) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.routes[EXPLORER_PATH] = txlist_reply(ROWS[:1])
    adapter = adapter_for(explorer_settings(node, tmp_path))
    try:
        (page,) = [page async for page in adapter.iter_account_transactions(ACCOUNT)]
    finally:
        await adapter.aclose()
    (record,) = page.records
    assert record.id == NATIVE_HASH
    assert record.status is Status.FINAL
    assert record.native.history_source == "blockscout"
    assert record.native.token_transfers
    assert node.rpc_params("eth_getTransactionReceipt") == [[NATIVE_HASH]]
    assert node.rpc_params("eth_getBlockByNumber") == [
        ["finalized", False],
        [hex(NATIVE_BLOCK), False],
    ]


async def test_records_after_the_finalized_block_are_pending(
    node: FakeNode, tmp_path: Path
) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK - 1)
    node.routes[EXPLORER_PATH] = txlist_reply(ROWS[:1])
    adapter = adapter_for(explorer_settings(node, tmp_path))
    try:
        page = await anext(adapter.iter_account_transactions(ACCOUNT))
    finally:
        await adapter.aclose()
    assert page.records[0].status is Status.PENDING


async def test_transaction_the_rpc_does_not_have_yet_is_left_for_the_next_sync(
    node: FakeNode, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.routes["rpc:eth_getTransactionReceipt"] = [rpc_ok(None), rpc_ok(NATIVE["receipt"])]
    node.routes[EXPLORER_PATH] = txlist_reply(ROWS[:2])
    adapter = adapter_for(explorer_settings(node, tmp_path))
    try:
        with caplog.at_level(logging.WARNING):
            pages = [page async for page in adapter.iter_account_transactions(ACCOUNT)]
    finally:
        await adapter.aclose()
    assert [len(page.records) for page in pages] == [1]
    assert "does not have yet" in caplog.text


async def test_requests_are_counted_across_rpc_and_explorer(node: FakeNode, tmp_path: Path) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.routes[EXPLORER_PATH] = txlist_reply(ROWS[:1])
    adapter = adapter_for(explorer_settings(node, tmp_path))
    try:
        [page async for page in adapter.iter_account_transactions(ACCOUNT)]
        # Chain ID, finalized block, one explorer page, then receipt, transaction and block.
        assert adapter.requests_made == 6
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("chain", "network", "source"),
    [
        (ETHEREUM, "mainnet", "blockscout"),
        (ETHEREUM, "testnet", "blockscout"),
        (POLYGON, "mainnet", "etherscan"),
        (POLYGON, "testnet", "etherscan"),
        (ETHEREUM, "localnet", "rpc-scan"),
    ],
)
def test_default_history_source_per_network(chain: EvmChain, network: str, source: str) -> None:
    settings = Settings(
        env={"BLOCKSCOUT_API_KEY": SecretStr("k"), "ETHERSCAN_API_KEY": SecretStr("k")}
    )
    adapter = indexer_factory(chain)(network, settings, None)
    history = adapter._history_source()
    expected = {
        "blockscout": "Blockscout",
        "etherscan": "Etherscan",
        "rpc-scan": "RpcScanHistory",
    }[source]
    name = (
        history._service.title if isinstance(history, ExplorerHistory) else type(history).__name__
    )
    assert name == expected


async def test_history_source_is_switched_by_configuration(node: FakeNode, tmp_path: Path) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS[:1])
    settings = explorer_settings(node, tmp_path, ETHEREUM_TESTNET_HISTORY_SOURCE="etherscan")
    adapter = adapter_for(settings)
    try:
        await anext(adapter.iter_account_transactions(ACCOUNT))
    finally:
        await adapter.aclose()
    assert node.requests_to(EXPLORER_PATH)[0]["apikey"] == ETHERSCAN_KEY


def test_unknown_history_source_is_refused() -> None:
    env = {"ETHEREUM_TESTNET_HISTORY_SOURCE": SecretStr("nosuch")}
    adapter = indexer_factory(ETHEREUM)(None, Settings(env=env), None)
    with pytest.raises(
        ConfigError, match="ETHEREUM_TESTNET_HISTORY_SOURCE must be one of: blockscout, etherscan"
    ):
        adapter._history_source()


def test_explorers_do_not_serve_local_networks() -> None:
    env = {
        "ETHEREUM_LOCALNET_HISTORY_SOURCE": SecretStr("blockscout"),
        "BLOCKSCOUT_API_KEY": SecretStr("k"),
    }
    adapter = indexer_factory(ETHEREUM)("localnet", Settings(env=env), None)
    with pytest.raises(ConfigError, match="set ETHEREUM_LOCALNET_HISTORY_SOURCE to rpc-scan"):
        adapter._history_source()


@pytest.mark.parametrize(
    ("chain", "network", "title"),
    [
        (BNB, "mainnet", "BNB Smart Chain mainnet"),
        (BNB, "testnet", "BNB Smart Chain testnet"),
        (BASE, "mainnet", "Base Mainnet"),
        (BASE, "testnet", "Base Sepolia"),
    ],
)
async def test_networks_without_a_free_history_service_say_so(
    node: FakeNode, tmp_path: Path, chain: EvmChain, network: str, title: str
) -> None:
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(chain.networks[network].chain_id))
    adapter = adapter_for(explorer_settings(node, tmp_path, chain, network), chain, network)
    try:
        with pytest.raises(UnsupportedChain, match=f"{title} has no free account history"):
            await anext(adapter.iter_account_transactions(ACCOUNT))
    finally:
        await adapter.aclose()


async def test_transaction_by_hash(node: FakeNode, tmp_path: Path) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    adapter = adapter_for(explorer_settings(node, tmp_path))
    try:
        record = await adapter.fetch_transaction(NATIVE_HASH)
        node.routes["rpc:eth_getTransactionReceipt"] = rpc_ok(None)
        with pytest.raises(NotFound, match="is not in a block on Sepolia"):
            await adapter.fetch_transaction(NATIVE_HASH)
    finally:
        await adapter.aclose()
    assert record.id == NATIVE_HASH
    assert record.native.history_source is None


@pytest.mark.parametrize("tx_hash", ["", "0x1234", NATIVE_HASH[2:], NATIVE_HASH[:-1] + "g"])
async def test_bad_transaction_hash_never_reaches_the_network(
    node: FakeNode, tmp_path: Path, tx_hash: str
) -> None:
    adapter: EvmIndexer = adapter_for(explorer_settings(node, tmp_path))
    try:
        with pytest.raises(InvalidInput, match="not a transaction hash"):
            await adapter.fetch_transaction(tx_hash)
    finally:
        await adapter.aclose()
    assert node.rpc_calls == []


def local_chain(node: FakeNode, blocks: dict[int, list[dict[str, Any]]], head: int) -> None:
    def block(params: list[Any]) -> Reply:
        tag, full = params
        if tag == "finalized":
            return rpc_ok({"number": hex(head)})
        if full:
            return rpc_ok({"transactions": blocks.get(int(tag, 16), [])})
        return rpc_ok(NATIVE["block"])

    node.routes["rpc:eth_chainId"] = rpc_ok(hex(31337))
    node.routes["rpc:eth_blockNumber"] = rpc_ok(hex(head))
    node.routes["rpc:eth_getTransactionByHash"] = rpc_ok(NATIVE["transaction"])
    node.routes["rpc:eth_getTransactionReceipt"] = rpc_ok(NATIVE["receipt"])
    node.handlers["rpc:eth_getBlockByNumber"] = block


def local_settings(node: FakeNode, tmp_path: Path) -> Settings:
    return explorer_settings(node, tmp_path, ETHEREUM, "localnet")


def scanned(number: int, sender: str, receiver: str | None) -> dict[str, Any]:
    return {"hash": f"0x{number:064x}", "from": sender, "to": receiver}


async def test_local_network_history_reads_every_block(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_module, "BLOCKS_PER_PAGE", 4)
    other = "0x" + "22" * 20
    blocks = {
        9: [scanned(9, ACCOUNT.upper().replace("X", "x"), other)],
        6: [scanned(61, other, other), scanned(62, other, ACCOUNT)],
        2: [scanned(2, other, None)],
        1: [scanned(1, ACCOUNT, None)],
    }
    local_chain(node, blocks, head=10)
    adapter = adapter_for(local_settings(node, tmp_path), ETHEREUM, "localnet")
    source = adapter._history_source()
    try:
        assert await walk(source) == [f"0x{9:064x}", f"0x{62:064x}", f"0x{1:064x}"]
        assert await walk(source, from_height=5) == [f"0x{9:064x}", f"0x{62:064x}"]
        assert source.requests_made == 0
    finally:
        await adapter.aclose()
    full_reads = [params for params in node.rpc_params("eth_getBlockByNumber") if params[1]]
    assert [int(params[0], 16) for params in full_reads[:11]] == list(range(10, -1, -1))


async def test_local_network_history_through_the_adapter(node: FakeNode, tmp_path: Path) -> None:
    local_chain(node, {7: [scanned(7, ACCOUNT, None)]}, head=8)
    adapter = adapter_for(local_settings(node, tmp_path), ETHEREUM, "localnet")
    try:
        pages = [page async for page in adapter.iter_account_transactions(ACCOUNT)]
    finally:
        await adapter.aclose()
    assert [record.id for page in pages for record in page.records] == [NATIVE_HASH]


@pytest.mark.parametrize("cursor", ["x", "-1", "1.5"])
async def test_block_scan_cursor_from_elsewhere_is_refused(
    node: FakeNode, tmp_path: Path, cursor: str
) -> None:
    adapter = adapter_for(local_settings(node, tmp_path), ETHEREUM, "localnet")
    try:
        with pytest.raises(InvalidInput, match="not a block scan cursor"):
            await adapter._history_source().account_page(
                ACCOUNT, from_height=None, to_height=None, cursor=cursor
            )
    finally:
        await adapter.aclose()


async def test_block_scan_reports_malformed_blocks(node: FakeNode, tmp_path: Path) -> None:
    local_chain(node, {3: [{"from": ACCOUNT}]}, head=3)
    adapter = adapter_for(local_settings(node, tmp_path), ETHEREUM, "localnet")
    try:
        with pytest.raises(MalformedResponse, match="transaction hashes"):
            await walk(adapter._history_source())
        node.handlers["rpc:eth_getBlockByNumber"] = lambda params: rpc_ok({"transactions": 5})
        with pytest.raises(MalformedResponse, match="list of transactions"):
            await walk(adapter._history_source())
        node.handlers["rpc:eth_getBlockByNumber"] = lambda params: rpc_ok(None)
        with pytest.raises(NotFound, match="is not available"):
            await walk(adapter._history_source())
    finally:
        await adapter.aclose()


def test_sync_stores_history_and_resumes_from_the_finalized_block(
    node: FakeNode, tmp_path: Path
) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS[:1])
    settings = explorer_settings(node, tmp_path)
    with Indexer("ethereum", settings=settings) as indexer:
        first = indexer.sync(ACCOUNT)
        second = indexer.sync(ACCOUNT)
        stored = indexer.stored_transactions(account=first.account)
    assert first.records_fetched == 1
    assert first.account == "0xAA37651031A4b641962550415dB2C43E8d962232"
    assert first.to_height == NATIVE_BLOCK + 100
    assert [record.id for record in stored] == [NATIVE_HASH]
    assert node.requests_to(EXPLORER_PATH)[-1]["startblock"] == str(NATIVE_BLOCK + 1)
    assert second.records_fetched == 0


def test_cli_index_account_lists_recent_transactions(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS[:1])
    for name, value in {
        "ETHEREUM_TESTNET_RPC_SERVER": node.url,
        "ETHEREUM_TESTNET_HISTORY_SERVER": node.url,
        "BLOCKSCOUT_API_KEY": BLOCKSCOUT_KEY,
        "MCI_MAX_RETRIES": "0",
        "MCI_REQUESTS_PER_SECOND": "1000",
    }.items():
        monkeypatch.setenv(name, value)
    options = ["--db", str(tmp_path / "cli.db"), "--env-file", str(tmp_path / "none.env")]
    result = CliRunner().invoke(cli, [*options, "--chain", "ethereum", "index", "account", ACCOUNT])
    assert result.exit_code == 0, result.output
    line = next(line for line in result.stdout.splitlines() if NATIVE_HASH in line)
    assert "legacy  final  0.038050303691589778 ETH  0.00000021 ETH  21000" in line
    assert line.endswith("1 ERC-20")
    assert BLOCKSCOUT_KEY not in result.output


def test_history_api_keys_are_masked_in_logs() -> None:
    settings = Settings(
        env={
            "BLOCKSCOUT_API_KEY": SecretStr(BLOCKSCOUT_KEY),
            "ETHERSCAN_API_KEY": SecretStr(ETHERSCAN_KEY),
        }
    )
    assert {BLOCKSCOUT_KEY, ETHERSCAN_KEY} <= set(settings.secret_values())


async def test_adapter_walks_every_page_of_the_explorer(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(explorer_module, "PAGE_SIZE", 1)
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS[:2])
    adapter = adapter_for(explorer_settings(node, tmp_path))
    try:
        pages = [page async for page in adapter.iter_account_transactions(ACCOUNT)]
    finally:
        await adapter.aclose()
    assert [len(page.records) for page in pages] == [1, 1]
    assert pages[0].next_cursor == f"{ROWS[0]['blockNumber']}:1"


async def test_malformed_transaction_object_is_reported(node: FakeNode, tmp_path: Path) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.routes["rpc:eth_getTransactionByHash"] = rpc_ok("0xabc")
    adapter = adapter_for(explorer_settings(node, tmp_path))
    try:
        with pytest.raises(MalformedResponse, match="eth_getTransactionByHash: expected an object"):
            await adapter.fetch_transaction(NATIVE_HASH)
    finally:
        await adapter.aclose()


def transaction_fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"tx_{name}.json").read_text("utf-8"))
    return loaded


def describe(chain: EvmChain, raw: dict[str, Any]) -> dict[str, str]:
    adapter = indexer_factory(chain)(None, Settings(), None)
    record = parse_transaction(
        RawTransaction(raw["transaction"], raw["receipt"], raw["block"]),
        chain=chain.name,
        network="testnet",
        final=True,
    )
    return adapter.describe(record)


def test_description_shows_the_l1_data_fee_on_op_stack_chains() -> None:
    fields = describe(BASE, transaction_fixture("base_sepolia_native"))
    assert fields["Fee"] == "0.000000126000000372 ETH"
    assert fields["L1 data fee"] == "0.000000000000000372 ETH"
    assert "Blobs" not in fields


def test_description_counts_blobs() -> None:
    assert describe(ETHEREUM, transaction_fixture("sepolia_blob"))["Blobs"] == "6"


def test_description_names_bep20_and_nft_transfers() -> None:
    raw = transaction_fixture("bsc_testnet_erc20")
    nft = copy.deepcopy(raw["receipt"]["logs"][0])
    nft["topics"].append("0x" + "0" * 63 + "9")
    nft["data"] = "0x"
    nft["logIndex"] = "0x9"
    raw["receipt"]["logs"].append(nft)
    fields = describe(BNB, raw)
    assert fields["Token transfers"] == "1 BEP-20, 1 NFT"
    assert fields["Fee"].endswith(" BNB")
    del raw["receipt"]["logs"][0]
    assert describe(BNB, raw)["Token transfers"] == "1 NFT"


def cli_environment(monkeypatch: pytest.MonkeyPatch, node: FakeNode, **extra: str) -> None:
    for name, value in {
        "ETHEREUM_TESTNET_RPC_SERVER": node.url,
        "ETHEREUM_TESTNET_HISTORY_SERVER": node.url,
        "BLOCKSCOUT_API_KEY": BLOCKSCOUT_KEY,
        "ETHERSCAN_API_KEY": ETHERSCAN_KEY,
        "MCI_MAX_RETRIES": "0",
        "MCI_REQUESTS_PER_SECOND": "1000",
        **extra,
    }.items():
        monkeypatch.setenv(name, value)


def run_cli(tmp_path: Path, *arguments: str) -> Any:
    options = ["--db", str(tmp_path / "cli.db"), "--env-file", str(tmp_path / "none.env")]
    return CliRunner().invoke(cli, [*options, "--chain", "ethereum", *arguments])


ATTRIBUTION = "Powered by Etherscan.io APIs"


def test_etherscan_data_is_shown_with_its_attribution_line(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS[:1])
    cli_environment(monkeypatch, node, ETHEREUM_TESTNET_HISTORY_SOURCE="etherscan")
    shown = run_cli(tmp_path, "index", "account", ACCOUNT)
    assert shown.exit_code == 0, shown.output
    assert shown.stdout.splitlines()[-1] == ATTRIBUTION
    assert shown.stdout.count(ATTRIBUTION) == 1
    assert ATTRIBUTION not in shown.stderr

    assert run_cli(tmp_path, "sync", ACCOUNT).exit_code == 0
    for export_format in ("json", "csv"):
        exported = run_cli(tmp_path, "export", "--format", export_format)
        assert exported.exit_code == 0, exported.output
        assert ATTRIBUTION not in exported.stdout
    (row,) = json.loads(run_cli(tmp_path, "export", "--format", "json").stdout)
    assert json.loads(row["native"])["history_source"] == "etherscan"


def test_other_sources_print_no_attribution(
    node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpc_server(node, finalized=NATIVE_BLOCK)
    node.handlers[EXPLORER_PATH] = explorer_server(ROWS[:1])
    cli_environment(monkeypatch, node)
    shown = run_cli(tmp_path, "index", "account", ACCOUNT)
    assert shown.exit_code == 0, shown.output
    assert NATIVE_HASH in shown.stdout
    assert ATTRIBUTION not in shown.output
