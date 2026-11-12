from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from click.testing import CliRunner, Result
from pydantic import SecretStr

from multichain_indexer import (
    ConfigError,
    Indexer,
    InvalidAddress,
    InvalidInput,
    MalformedResponse,
    NetworkError,
    NotFound,
    RateLimited,
    WrongNetwork,
)
from multichain_indexer.adapters.near import NearIndexer, create_indexer
from multichain_indexer.adapters.near import adapter as adapter_module
from multichain_indexer.adapters.near import fastnear as fastnear_module
from multichain_indexer.adapters.near.fastnear import _executed_transaction, _RawTransaction
from multichain_indexer.adapters.near.history import ExecutedTransaction, HistoryPage
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from tests.conftest import FakeNode, Reply
from tests.conftest import load_near_fixture as fixture

ACCOUNT = "guest-book.testnet"
HISTORY_ACCOUNT = fixture("history_pages")["account"]
TRANSFER_SIGNER = fixture("tx_transfer")["transaction"]["transaction"]["signer_id"]
FUNCTION_CALL_SIGNER = fixture("tx_function_call")["transaction"]["transaction"]["signer_id"]
# The saved history holds 22 transactions; HISTORY_ACCOUNT signed or received 20 of them.
ALL_IN_HISTORY = 22
DIRECT_IN_HISTORY = 20
FINAL_HEAD = 276_700_000
INDEXED_HEAD = FINAL_HEAD - 7
ACCOUNT_PATH = "/v0/account"
TRANSACTIONS_PATH = "/v0/transactions"
BLOCKS_PATH = "/v0/blocks"
API_KEY = "history-key-value"


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": "1", "result": result})


def rpc_error(name: str, cause: str | None = None, data: str = "refused") -> Reply:
    error: dict[str, Any] = {"name": name, "code": -32000, "message": "Server error", "data": data}
    if cause:
        error["cause"] = {"name": cause, "info": {}}
    return Reply({"jsonrpc": "2.0", "id": "1", "error": error})


def history_pages() -> list[dict[str, Any]]:
    return fixture("history_pages")["pages"]


def rows_reply(page: dict[str, Any]) -> Reply:
    return Reply(page["rows"])


def transactions_reply(page: dict[str, Any]) -> Reply:
    return Reply({"transactions": page["transactions"]})


def script_two_pages(node: FakeNode) -> list[dict[str, Any]]:
    pages = history_pages()
    node.routes[ACCOUNT_PATH] = [rows_reply(page) for page in pages]
    node.routes[TRANSACTIONS_PATH] = [transactions_reply(page) for page in pages]
    return pages


def script_one_transaction(node: FakeNode, name: str = "tx_transfer") -> dict[str, Any]:
    raw = fixture(name)["transaction"]
    row = {
        "account_id": raw["transaction"]["signer_id"],
        "transaction_hash": raw["transaction"]["hash"],
        "tx_block_height": raw["execution_outcome"]["block_height"],
        "is_success": fixture(name)["is_success"],
    }
    node.routes[ACCOUNT_PATH] = Reply({"account_txs": [row], "txs_count": 1})
    node.routes[TRANSACTIONS_PATH] = Reply({"transactions": [raw]})
    return raw


def hashes_of(page: dict[str, Any]) -> list[str]:
    return [row["transaction_hash"] for row in page["rows"]["account_txs"]]


def direct_hashes(page: dict[str, Any]) -> list[str]:
    """The page's transactions that HISTORY_ACCOUNT signed or received, in row order."""
    direct = {
        tx["transaction"]["hash"]
        for tx in page["transactions"]
        if HISTORY_ACCOUNT in (tx["transaction"]["signer_id"], tx["transaction"]["receiver_id"])
    }
    return [tx_hash for tx_hash in hashes_of(page) if tx_hash in direct]


def with_scope(settings: Settings, scope: str) -> Settings:
    env = {**settings.env, "NEAR_TESTNET_HISTORY_SCOPE": SecretStr(scope)}
    return settings.model_copy(update={"env": env})


def test_saved_history_has_both_kinds_of_transaction() -> None:
    pages = history_pages()
    assert sum(len(hashes_of(page)) for page in pages) == ALL_IN_HISTORY
    assert sum(len(direct_hashes(page)) for page in pages) == DIRECT_IN_HISTORY


@pytest.fixture
def near_settings(node: FakeNode, tmp_path: Path) -> Settings:
    node.routes["rpc:status"] = rpc_ok({"chain_id": "testnet"})
    node.routes["rpc:block"] = rpc_ok({"header": {"height": FINAL_HEAD}})
    node.routes[BLOCKS_PATH] = Reply({"blocks": [{"block_height": INDEXED_HEAD}]})
    node.routes["rpc:EXPERIMENTAL_protocol_config"] = rpc_ok(
        {"shard_layout": fixture("rpc_shard_layout")["result"]["shard_layout"]}
    )
    return Settings(
        database=tmp_path / "near.db",
        requests_per_second=1000,
        request_timeout=5,
        max_retries=0,
        env={
            "NEAR_TESTNET_RPC_SERVER": SecretStr(node.url),
            "NEAR_TESTNET_HISTORY_SERVER": SecretStr(node.url),
        },
    )


@pytest.fixture
async def indexer(near_settings: Settings) -> AsyncIterator[NearIndexer]:
    adapter = create_indexer(None, near_settings)
    yield adapter
    await adapter.aclose()


def settings_for(settings: Settings, network: str, node: FakeNode) -> Settings:
    env = {
        f"NEAR_{network.upper()}_RPC_SERVER": SecretStr(node.url),
        f"NEAR_{network.upper()}_HISTORY_SERVER": SecretStr(node.url),
    }
    return settings.model_copy(update={"env": env})


def test_defaults_to_testnet_with_public_endpoints() -> None:
    adapter = create_indexer(None, Settings())
    assert adapter.network == "testnet"
    assert adapter._rpc._client._base_url.get_secret_value() == "https://near-testnet.drpc.org"
    history = adapter._history
    assert history._client._base_url.get_secret_value() == "https://tx.test.fastnear.com"

    mainnet = create_indexer("mainnet", Settings())
    assert mainnet._rpc._client._base_url.get_secret_value() == "https://near.drpc.org"
    assert mainnet._history._client._base_url.get_secret_value() == "https://tx.main.fastnear.com"


def test_rpc_default_is_not_the_history_service() -> None:
    for network in ("mainnet", "testnet"):
        assert "fastnear" not in adapter_module.DEFAULT_RPC_SERVERS[network]


def test_default_rates_are_measured_and_configurable() -> None:
    adapter = create_indexer(None, Settings())
    assert adapter._rpc._client._limiter._interval == pytest.approx(1)
    assert adapter._history._client._limiter._interval == pytest.approx(2)

    configured = create_indexer(
        None, Settings.from_env(None, environ={"MCI_REQUESTS_PER_SECOND": "50"})
    )
    assert configured._rpc._client._limiter._interval == pytest.approx(0.02)
    assert configured._history._client._limiter._interval == pytest.approx(0.02)


def test_unknown_network_is_refused() -> None:
    with pytest.raises(InvalidInput, match="unknown NEAR network"):
        create_indexer("devnet", Settings())


def test_unencrypted_remote_endpoints_are_refused() -> None:
    for setting in ("RPC_SERVER", "HISTORY_SERVER"):
        env = {f"NEAR_TESTNET_{setting}": SecretStr("http://node.example")}
        with pytest.raises(ConfigError, match=setting):
            create_indexer(None, Settings(env=env))


async def test_account_comes_from_the_standard_rpc_only(
    node: FakeNode, indexer: NearIndexer
) -> None:
    node.routes["rpc:query"] = [
        Reply(fixture("account_named")),
        Reply(fixture("access_keys_function_call")),
    ]
    record = await indexer.fetch_account("calmhare3618.testnet")

    assert record.native.shard_id == 12
    assert len(record.native.access_keys) > 1
    requests = [params["request_type"] for params in node.rpc_params("query")]
    assert requests == ["view_account", "view_access_key_list"]
    assert all(params["finality"] == "final" for params in node.rpc_params("query"))
    assert node.post_bodies == []


async def test_account_nobody_created_is_not_an_error(node: FakeNode, indexer: NearIndexer) -> None:
    node.routes["rpc:query"] = Reply(fixture("account_unknown"))
    record = await indexer.fetch_account("no-such-account-xyz.testnet")
    assert not record.native.exists
    assert record.balance == 0
    assert len(node.rpc_params("query")) == 1


async def test_contract_lookup(node: FakeNode, indexer: NearIndexer) -> None:
    node.routes["rpc:query"] = Reply(fixture("account_contract"))
    record = await indexer.fetch_contract(ACCOUNT)
    assert record.native.code_hash == fixture("account_contract")["result"]["code_hash"]
    assert indexer.describe(record)["Contract"] == ACCOUNT


async def test_contract_on_an_account_without_code_or_missing(
    node: FakeNode, indexer: NearIndexer
) -> None:
    node.routes["rpc:query"] = Reply(fixture("account_named"))
    with pytest.raises(NotFound, match="no contract"):
        await indexer.fetch_contract("calmhare3618.testnet")
    node.routes["rpc:query"] = Reply(fixture("account_unknown"))
    with pytest.raises(NotFound, match="does not exist"):
        await indexer.fetch_contract("no-such-account-xyz.testnet")


async def test_asset_lookup_points_at_contracts(node: FakeNode, indexer: NearIndexer) -> None:
    with pytest.raises(InvalidInput, match="index contract"):
        await indexer.fetch_asset("wrap.testnet")
    assert node.rpc_calls == []


async def test_bad_account_ids_never_reach_the_network(
    node: FakeNode, indexer: NearIndexer
) -> None:
    with pytest.raises(InvalidAddress):
        await indexer.fetch_account("BAD")
    with pytest.raises(InvalidAddress):
        await indexer.fetch_contract("a..b")
    with pytest.raises(InvalidAddress):
        [page async for page in indexer.iter_account_transactions("x")]
    assert node.rpc_calls == []
    assert node.post_bodies == []


@pytest.mark.parametrize(
    ("network", "chain_id"),
    [
        ("testnet", "testnet"),
        ("mainnet", "mainnet"),
        ("localnet", "localnet"),
        ("localnet", "test-chain-6Xn3B"),
    ],
)
async def test_endpoint_serving_the_requested_network_is_accepted(
    node: FakeNode, near_settings: Settings, network: str, chain_id: str
) -> None:
    node.routes["rpc:status"] = rpc_ok({"chain_id": chain_id})
    adapter = create_indexer(network, settings_for(near_settings, network, node))
    try:
        assert await adapter.chain_head() == INDEXED_HEAD
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("network", "chain_id", "served"),
    [
        ("testnet", "mainnet", "NEAR mainnet"),
        ("mainnet", "testnet", "NEAR testnet"),
        ("localnet", "mainnet", "NEAR mainnet"),
        ("testnet", "test-chain-1", "another network, 'test-chain-1'"),
    ],
)
async def test_endpoint_serving_another_network_is_refused_before_any_other_call(
    node: FakeNode, near_settings: Settings, network: str, chain_id: str, served: str
) -> None:
    node.routes["rpc:status"] = rpc_ok({"chain_id": chain_id})
    adapter = create_indexer(network, settings_for(near_settings, network, node))
    try:
        with pytest.raises(WrongNetwork) as caught:
            await adapter.chain_head()
    finally:
        await adapter.aclose()
    setting = f"NEAR_{network.upper()}_RPC_SERVER"
    assert str(caught.value) == (
        f"the RPC endpoint serves {served}, not NEAR {network}. Check {setting}."
    )
    assert [method for method, _ in node.rpc_calls] == ["status"]
    assert node.post_bodies == []


@pytest.mark.parametrize(
    "lookup",
    [
        lambda adapter: adapter.fetch_account(ACCOUNT),
        lambda adapter: adapter.fetch_contract(ACCOUNT),
        lambda adapter: adapter.chain_head(),
        lambda adapter: anext(adapter.iter_account_transactions(ACCOUNT)),
    ],
    ids=["account", "contract", "head", "history"],
)
async def test_every_lookup_checks_the_network_first(
    node: FakeNode, indexer: NearIndexer, lookup: Callable[[NearIndexer], Awaitable[object]]
) -> None:
    node.routes["rpc:status"] = rpc_ok({"chain_id": "mainnet"})
    with pytest.raises(WrongNetwork, match="serves NEAR mainnet, not NEAR testnet"):
        await lookup(indexer)
    assert [method for method, _ in node.rpc_calls] == ["status"]
    assert node.post_bodies == []


async def test_network_is_confirmed_once(node: FakeNode, indexer: NearIndexer) -> None:
    await indexer.chain_head()
    await indexer.chain_head()
    assert len(node.rpc_params("status")) == 1


async def test_network_check_that_could_not_run_is_tried_again(
    node: FakeNode, indexer: NearIndexer
) -> None:
    node.routes["rpc:status"] = [
        Reply({"message": "restarting"}, status=503),
        rpc_ok({"chain_id": "testnet"}),
    ]
    with pytest.raises(NetworkError):
        await indexer.chain_head()
    assert await indexer.chain_head() == INDEXED_HEAD


@pytest.mark.parametrize(
    ("final", "indexed", "head"),
    [(1000, 990, 990), (990, 1000, 990), (1000, 1000, 1000)],
)
async def test_head_never_passes_what_the_history_service_has_indexed(
    node: FakeNode, indexer: NearIndexer, final: int, indexed: int, head: int
) -> None:
    node.routes["rpc:block"] = rpc_ok({"header": {"height": final}})
    node.routes[BLOCKS_PATH] = Reply({"blocks": [{"block_height": indexed}]})
    assert await indexer.chain_head() == head
    assert node.rpc_params("block") == [{"finality": "final"}]
    assert node.posted_to(BLOCKS_PATH) == [{"limit": 1, "desc": True}]


def test_final_height_keeps_a_margin(indexer_sync: NearIndexer) -> None:
    margin = adapter_module.FINALITY_MARGIN_BLOCKS
    assert indexer_sync.final_height(FINAL_HEAD) == FINAL_HEAD - margin
    assert indexer_sync.final_height(5) == 0


@pytest.fixture
def indexer_sync(near_settings: Settings) -> NearIndexer:
    return create_indexer(None, near_settings)


async def test_history_walks_every_page(node: FakeNode, indexer: NearIndexer) -> None:
    pages = script_two_pages(node)
    found = [page async for page in indexer.iter_account_transactions(HISTORY_ACCOUNT)]

    assert [len(page.records) for page in found] == [18, 2]
    assert found[0].next_cursor == pages[0]["rows"]["resume_token"]
    assert found[1].next_cursor is None
    assert [r.id for r in found[0].records] == direct_hashes(pages[0])

    first, second = node.posted_to(ACCOUNT_PATH)
    assert first == {"account_id": HISTORY_ACCOUNT, "limit": 20, "desc": True}
    assert second == {**first, "resume_token": pages[0]["rows"]["resume_token"]}
    assert [body["tx_hashes"] for body in node.posted_to(TRANSACTIONS_PATH)] == [
        hashes_of(page) for page in pages
    ]


async def test_requests_are_counted_across_rpc_and_history(
    node: FakeNode, indexer: NearIndexer
) -> None:
    script_two_pages(node)
    [page async for page in indexer.iter_account_transactions(HISTORY_ACCOUNT)]
    assert indexer.requests_made == 1 + 4


async def test_history_passes_the_height_range(node: FakeNode, indexer: NearIndexer) -> None:
    script_one_transaction(node)
    pages = indexer.iter_account_transactions(
        TRANSFER_SIGNER, from_height=5, to_height=9, cursor="abc"
    )
    [page async for page in pages]
    assert node.posted_to(ACCOUNT_PATH)[0] == {
        "account_id": TRANSFER_SIGNER,
        "limit": 20,
        "desc": True,
        "from_tx_block_height": 5,
        "to_tx_block_height": 9,
        "resume_token": "abc",
    }


async def test_empty_history(node: FakeNode, indexer: NearIndexer) -> None:
    node.routes[ACCOUNT_PATH] = Reply({"account_txs": [], "txs_count": 0})
    assert [page async for page in indexer.iter_account_transactions(ACCOUNT)] == []
    assert node.posted_to(TRANSACTIONS_PATH) == []


async def test_transaction_the_service_left_out_stops_the_page(
    node: FakeNode, indexer: NearIndexer
) -> None:
    pages = script_two_pages(node)
    left_out, *returned = pages[0]["transactions"]
    node.routes[TRANSACTIONS_PATH] = Reply({"transactions": returned})
    with pytest.raises(MalformedResponse, match=left_out["transaction"]["hash"]):
        [page async for page in indexer.iter_account_transactions(HISTORY_ACCOUNT)]


async def test_same_hash_listed_twice_is_fetched_once(node: FakeNode, indexer: NearIndexer) -> None:
    raw = script_one_transaction(node)
    row = node.routes[ACCOUNT_PATH].body["account_txs"][0]  # type: ignore[union-attr]
    node.routes[ACCOUNT_PATH] = Reply({"account_txs": [row, row]})
    page = await anext(indexer.iter_account_transactions(TRANSFER_SIGNER))
    assert [record.id for record in page.records] == [raw["transaction"]["hash"]]
    assert node.posted_to(TRANSACTIONS_PATH)[0]["tx_hashes"] == [raw["transaction"]["hash"]]


async def test_receipt_with_no_outcome_yet_leaves_the_transaction_pending(
    node: FakeNode, indexer: NearIndexer
) -> None:
    raw = script_one_transaction(node)
    for receipt in raw["receipts"]:
        receipt["execution_outcome"] = None
    page = await anext(indexer.iter_account_transactions(TRANSFER_SIGNER))
    assert page.records[0].status.value == "pending"
    assert page.records[0].native.receipts == ()


@pytest.mark.parametrize(
    ("path", "body", "message"),
    [
        (ACCOUNT_PATH, {"account_txs": "many"}, "/v0/account"),
        (ACCOUNT_PATH, {"account_txs": [{"tx_block_height": 1}]}, "/v0/account"),
        (TRANSACTIONS_PATH, {"transactions": [{"transaction": {}}]}, "/v0/transactions"),
    ],
)
async def test_malformed_history_is_reported(
    node: FakeNode, indexer: NearIndexer, path: str, body: object, message: str
) -> None:
    script_one_transaction(node)
    node.routes[path] = Reply(body)
    with pytest.raises(MalformedResponse, match=message):
        await anext(indexer.iter_account_transactions(TRANSFER_SIGNER))


@pytest.mark.parametrize("body", [{"blocks": []}, {"blocks": [{}]}, {"nope": 1}])
async def test_malformed_indexed_height_is_reported(
    node: FakeNode, indexer: NearIndexer, body: object
) -> None:
    node.routes[BLOCKS_PATH] = Reply(body)
    with pytest.raises(MalformedResponse, match="/v0/blocks"):
        await indexer.chain_head()


@pytest.mark.parametrize(
    ("route", "reply", "message"),
    [
        ("rpc:status", rpc_ok({"nope": 1}), "chain_id"),
        ("rpc:status", rpc_ok("up"), "chain_id"),
        ("rpc:block", rpc_ok({"header": {}}), "block height"),
        ("rpc:block", rpc_ok("nope"), "block height"),
        ("rpc:block", Reply({"jsonrpc": "2.0"}), "neither a result nor an error"),
    ],
)
async def test_malformed_rpc_replies_are_reported(
    node: FakeNode, indexer: NearIndexer, route: str, reply: Reply, message: str
) -> None:
    node.routes[route] = reply
    with pytest.raises(MalformedResponse, match=message):
        await indexer.chain_head()


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (rpc_error("REQUEST_VALIDATION_ERROR", "PARSE_ERROR"), InvalidInput),
        (rpc_error("HANDLER_ERROR", "UNAVAILABLE_SHARD"), NetworkError),
        (rpc_error("INTERNAL_ERROR"), NetworkError),
        (Reply({"jsonrpc": "2.0", "error": "plain text"}), NetworkError),
    ],
)
async def test_rpc_errors_map_to_library_errors(
    node: FakeNode, indexer: NearIndexer, reply: Reply, error: type[Exception]
) -> None:
    node.routes["rpc:query"] = reply
    with pytest.raises(error, match="query"):
        await indexer.fetch_account(ACCOUNT)


async def test_layout_not_reported_gives_no_shard(node: FakeNode, indexer: NearIndexer) -> None:
    node.routes["rpc:query"] = [
        Reply(fixture("account_named")),
        Reply(fixture("access_keys_implicit")),
    ]
    node.routes["rpc:EXPERIMENTAL_protocol_config"] = rpc_ok("not an object")
    record = await indexer.fetch_account("calmhare3618.testnet")
    assert record.native.shard_id is None
    assert indexer.describe(record)["Shard"] == "not reported"


async def test_rate_limit_from_the_history_service_is_clear(
    node: FakeNode, indexer: NearIndexer
) -> None:
    node.routes[ACCOUNT_PATH] = Reply({"message": "slow down"}, status=429)
    with pytest.raises(RateLimited, match="MCI_REQUESTS_PER_SECOND"):
        await anext(indexer.iter_account_transactions(ACCOUNT))


async def test_timeout_from_the_history_service_is_clear(
    node: FakeNode, near_settings: Settings
) -> None:
    node.routes[ACCOUNT_PATH] = Reply({"account_txs": []}, delay=0.5)
    adapter = create_indexer(None, near_settings.model_copy(update={"request_timeout": 0.05}))
    node.routes["rpc:status"] = rpc_ok({"chain_id": "testnet"})
    adapter._network_confirmed = True
    try:
        with pytest.raises(NetworkError, match="/v0/account"):
            await anext(adapter.iter_account_transactions(ACCOUNT))
    finally:
        await adapter.aclose()


async def test_api_key_goes_to_the_history_service_only_when_set(
    node: FakeNode, near_settings: Settings
) -> None:
    script_one_transaction(node)
    without_key = create_indexer(None, near_settings)
    await anext(without_key.iter_account_transactions(TRANSFER_SIGNER))
    await without_key.aclose()
    assert not any("Authorization" in headers for headers in node.received_headers)

    node.received_headers.clear()
    node.rpc_calls.clear()
    env = {**near_settings.env, "NEAR_TESTNET_HISTORY_API_KEY": SecretStr(API_KEY)}
    with_key = create_indexer(None, near_settings.model_copy(update={"env": env}))
    script_one_transaction(node)
    await anext(with_key.iter_account_transactions(TRANSFER_SIGNER))
    await with_key.aclose()
    authorised = [h["Authorization"] for h in node.received_headers if "Authorization" in h]
    assert authorised == [f"Bearer {API_KEY}", f"Bearer {API_KEY}"]
    assert len(node.received_headers) == 3


def test_api_key_is_masked_in_logs() -> None:
    settings = Settings(env={"NEAR_TESTNET_HISTORY_API_KEY": SecretStr(API_KEY)})
    assert API_KEY in settings.secret_values()
    assert API_KEY not in repr(create_indexer(None, settings)._history._client._headers)


class _FixedHistory:
    """A second service, in the fewest lines the protocol allows."""

    def __init__(self, transactions: tuple[ExecutedTransaction, ...], height: int) -> None:
        self._transactions = transactions
        self._height = height
        self.closed = False
        self.requests_made = 3

    async def indexed_height(self) -> int:
        return self._height

    async def account_page(
        self,
        account: str,
        *,
        from_height: int | None,
        to_height: int | None,
        cursor: str | None,
        immutable_up_to: int | None,
    ) -> HistoryPage:
        return HistoryPage(self._transactions, None)

    async def aclose(self) -> None:
        self.closed = True


def test_a_history_source_can_be_added_without_touching_parser_or_core(
    node: FakeNode, near_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = fixture("tx_transfer")["transaction"]
    executed = _executed_transaction(_RawTransaction.model_validate(raw))
    source = _FixedHistory((executed,), INDEXED_HEAD - 1)
    seen: list[str] = []

    def factory(network: str, settings: Settings, cache: object) -> _FixedHistory:
        seen.append(network)
        return source

    monkeypatch.setitem(adapter_module.HISTORY_SOURCES, "other", factory)
    env = {**near_settings.env, "NEAR_TESTNET_HISTORY_SOURCE": SecretStr("other")}
    with Indexer("near", settings=near_settings.model_copy(update={"env": env})) as indexer:
        result = indexer.sync(TRANSFER_SIGNER)
        stored = indexer.stored_transactions()

    assert seen == ["testnet"]
    assert source.closed
    assert result.to_height == INDEXED_HEAD - 1
    assert [record.id for record in stored] == [raw["transaction"]["hash"]]
    assert node.post_bodies == []


def test_history_source_is_fastnear_unless_configured(near_settings: Settings) -> None:
    default = create_indexer(None, near_settings)
    assert isinstance(default._history, fastnear_module.FastNearHistory)
    env = {**near_settings.env, "NEAR_TESTNET_HISTORY_SOURCE": SecretStr("fastnear")}
    named = create_indexer(None, near_settings.model_copy(update={"env": env}))
    assert isinstance(named._history, fastnear_module.FastNearHistory)


def test_unknown_history_source_is_refused(near_settings: Settings) -> None:
    env = {**near_settings.env, "NEAR_TESTNET_HISTORY_SOURCE": SecretStr("nosuch")}
    with pytest.raises(ConfigError, match="NEAR_TESTNET_HISTORY_SOURCE must be one of: fastnear"):
        create_indexer(None, near_settings.model_copy(update={"env": env}))


async def test_localnet_reads_accounts_but_has_no_history_without_a_server(
    node: FakeNode, near_settings: Settings
) -> None:
    node.routes["rpc:status"] = rpc_ok({"chain_id": "localnet"})
    node.routes["rpc:query"] = [
        Reply(fixture("account_named")),
        Reply(fixture("access_keys_implicit")),
    ]
    env = {"NEAR_LOCALNET_RPC_SERVER": SecretStr(node.url)}
    adapter = create_indexer("localnet", near_settings.model_copy(update={"env": env}))
    try:
        assert (await adapter.fetch_account("ab.test.near")).native.exists
        with pytest.raises(ConfigError, match="NEAR_LOCALNET_HISTORY_SERVER"):
            await anext(adapter.iter_account_transactions("ab.test.near"))
        with pytest.raises(ConfigError, match="NEAR_LOCALNET_HISTORY_SERVER"):
            await adapter.chain_head()
    finally:
        await adapter.aclose()
    assert adapter._history.requests_made == 0


def test_localnet_default_is_the_sandbox() -> None:
    client = create_indexer("localnet", Settings())._rpc._client
    assert client._base_url.get_secret_value() == "http://localhost:3030"


def test_sync_stores_history_once_and_only_asks_for_new_blocks_after(
    node: FakeNode, near_settings: Settings
) -> None:
    pages = script_two_pages(node)
    with Indexer("near", settings=near_settings) as indexer:
        first = indexer.sync(HISTORY_ACCOUNT)
        assert first.records_fetched == DIRECT_IN_HISTORY
        assert first.to_height == INDEXED_HEAD
        assert indexer.network == "testnet"

        node.post_bodies.clear()
        node.routes[ACCOUNT_PATH] = Reply({"account_txs": []})
        node.routes[BLOCKS_PATH] = Reply({"blocks": [{"block_height": INDEXED_HEAD + 500}]})
        node.routes["rpc:block"] = rpc_ok({"header": {"height": INDEXED_HEAD + 600}})
        second = indexer.sync(HISTORY_ACCOUNT)
        stored = indexer.stored_transactions(account=HISTORY_ACCOUNT)

    margin = adapter_module.FINALITY_MARGIN_BLOCKS
    assert second.from_height == INDEXED_HEAD - margin + 1
    assert second.records_fetched == 0
    body = node.posted_to(ACCOUNT_PATH)[0]
    assert body["from_tx_block_height"] == INDEXED_HEAD - margin + 1
    assert body["to_tx_block_height"] == INDEXED_HEAD + 500
    assert len(stored) == DIRECT_IN_HISTORY
    assert {record.id for record in stored} == {h for page in pages for h in direct_hashes(page)}


def test_indexer_behind_the_chain_does_not_leave_a_gap(
    node: FakeNode, near_settings: Settings
) -> None:
    node.routes[ACCOUNT_PATH] = Reply({"account_txs": []})
    node.routes[BLOCKS_PATH] = Reply({"blocks": [{"block_height": 1000}]})
    node.routes["rpc:block"] = rpc_ok({"header": {"height": 1_000_000}})
    with Indexer("near", settings=near_settings) as indexer:
        indexer.sync(ACCOUNT)
        node.post_bodies.clear()
        node.routes[BLOCKS_PATH] = Reply({"blocks": [{"block_height": 2000}]})
        indexer.sync(ACCOUNT)
    margin = adapter_module.FINALITY_MARGIN_BLOCKS
    assert node.posted_to(ACCOUNT_PATH)[0]["from_tx_block_height"] == 1000 - margin + 1


def test_interrupted_sync_resumes_from_the_stored_cursor(
    node: FakeNode, near_settings: Settings
) -> None:
    pages = history_pages()
    node.routes[ACCOUNT_PATH] = [
        rows_reply(pages[0]),
        Reply({"message": "restarting"}, status=503),
    ]
    node.routes[TRANSACTIONS_PATH] = transactions_reply(pages[0])
    with Indexer("near", settings=near_settings) as indexer, pytest.raises(NetworkError):
        indexer.sync(HISTORY_ACCOUNT)

    node.post_bodies.clear()
    node.rpc_calls.clear()
    node.routes[ACCOUNT_PATH] = rows_reply(pages[1])
    node.routes[TRANSACTIONS_PATH] = transactions_reply(pages[1])
    with Indexer("near", settings=near_settings) as indexer:
        resumed = indexer.sync(HISTORY_ACCOUNT)

    assert resumed.resumed
    assert resumed.records_fetched == 2
    assert node.posted_to(ACCOUNT_PATH)[0]["resume_token"] == pages[0]["rows"]["resume_token"]
    assert node.rpc_params("block") == []

    connection = sqlite3.connect(near_settings.database)
    ids = sorted(row[0] for row in connection.execute("SELECT tx_id FROM transactions"))
    connection.close()
    assert ids == sorted(h for page in pages for h in direct_hashes(page))


def test_rejected_cursor_restarts_the_range(node: FakeNode, near_settings: Settings) -> None:
    pages = history_pages()
    node.routes[ACCOUNT_PATH] = [
        rows_reply(pages[0]),
        Reply({"message": "restarting"}, status=503),
    ]
    node.routes[TRANSACTIONS_PATH] = transactions_reply(pages[0])
    with Indexer("near", settings=near_settings) as indexer, pytest.raises(NetworkError):
        indexer.sync(HISTORY_ACCOUNT)

    node.routes[ACCOUNT_PATH] = [
        Reply({"error": "bad resume_token"}, status=400),
        rows_reply(pages[0]),
        rows_reply(pages[1]),
    ]
    node.routes[TRANSACTIONS_PATH] = [transactions_reply(pages[0]), transactions_reply(pages[1])]
    with Indexer("near", settings=near_settings) as indexer:
        assert indexer.sync(HISTORY_ACCOUNT).records_fetched == DIRECT_IN_HISTORY
        assert len(indexer.stored_transactions()) == DIRECT_IN_HISTORY


def test_sync_against_another_network_stores_nothing(
    node: FakeNode, near_settings: Settings
) -> None:
    node.routes["rpc:status"] = rpc_ok({"chain_id": "mainnet"})
    with Indexer("near", settings=near_settings) as indexer:
        with pytest.raises(WrongNetwork):
            indexer.sync(ACCOUNT)
        assert indexer.stored_transactions() == []


def test_finished_transactions_are_cached_and_recent_ones_are_not(
    node: FakeNode, near_settings: Settings, tmp_path: Path
) -> None:
    pages = script_two_pages(node)
    cached = near_settings.model_copy(update={"cache_path": tmp_path / "cache.db"})
    with Indexer("near", settings=cached) as indexer:
        indexer.sync(HISTORY_ACCOUNT)

    node.post_bodies.clear()
    node.routes[ACCOUNT_PATH] = [rows_reply(page) for page in pages]
    node.routes[TRANSACTIONS_PATH] = [transactions_reply(page) for page in pages]
    fresh = cached.model_copy(update={"database": tmp_path / "other.db"})
    with Indexer("near", settings=fresh) as indexer:
        assert indexer.sync(HISTORY_ACCOUNT).records_fetched == DIRECT_IN_HISTORY
    assert node.posted_to(TRANSACTIONS_PATH) == []

    newest = pages[0]["rows"]["account_txs"][0]["tx_block_height"]
    node.routes[BLOCKS_PATH] = Reply({"blocks": [{"block_height": newest + 1}]})
    node.routes[ACCOUNT_PATH] = [rows_reply(page) for page in pages]
    node.routes[TRANSACTIONS_PATH] = [transactions_reply(page) for page in pages]
    newer = cached.model_copy(update={"database": tmp_path / "third.db"})
    with Indexer("near", settings=newer) as indexer:
        indexer.sync(HISTORY_ACCOUNT)
    assert len(node.posted_to(TRANSACTIONS_PATH)) >= 1


def test_transaction_with_crafted_arguments_cannot_stop_a_sync(
    node: FakeNode, near_settings: Settings, tmp_path: Path
) -> None:
    raw = script_one_transaction(node, "tx_function_call")
    crafted = base64.b64encode(b"[" * 150 + b"]" * 150).decode()
    raw["transaction"]["actions"][0]["FunctionCall"]["args"] = crafted
    with Indexer("near", settings=near_settings) as indexer:
        assert indexer.sync(FUNCTION_CALL_SIGNER).records_fetched == 1
        action = indexer.stored_transactions()[0].native.actions[0]
        assert action.args_base64 == crafted
        assert indexer.metrics(days=36500).transactions == 1
        assert crafted in indexer.export("json")
        indexer.export("parquet", tmp_path / "crafted.parquet")


def test_amounts_past_64_bits_stay_exact_in_every_format(
    node: FakeNode, near_settings: Settings, tmp_path: Path
) -> None:
    raw = script_one_transaction(node)
    amount = int(raw["transaction"]["actions"][0]["Transfer"]["deposit"])
    assert amount > 2**64
    with Indexer("near", settings=near_settings) as indexer:
        indexer.sync(TRANSFER_SIGNER)
        record = indexer.stored_transactions()[0]
        assert record.amount == amount
        assert record.fee > 2**64
        as_json = json.loads(indexer.export("json"))
        assert as_json[0]["amount"] == str(amount)
        assert str(amount) in indexer.export("csv")
        output = tmp_path / "records.parquet"
        indexer.export("parquet", output)
    assert pd.read_parquet(output)["amount"].tolist() == [str(amount)]


def test_metrics_through_the_api(node: FakeNode, near_settings: Settings) -> None:
    script_two_pages(node)
    with Indexer("near", settings=near_settings) as indexer:
        indexer.sync(HISTORY_ACCOUNT)
        report = indexer.metrics(days=36500, account=HISTORY_ACCOUNT)
    assert report.chain == "near"
    assert report.transactions == DIRECT_IN_HISTORY
    assert report.total_fees == report.native["fees_yoctonear"]
    assert "function_calls_by_method" in report.native
    assert sum(report.transactions_per_day.values()) == DIRECT_IN_HISTORY


@pytest.fixture
def invoke(node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    node.routes["rpc:status"] = rpc_ok({"chain_id": "testnet"})
    node.routes["rpc:block"] = rpc_ok({"header": {"height": FINAL_HEAD}})
    node.routes[BLOCKS_PATH] = Reply({"blocks": [{"block_height": INDEXED_HEAD}]})
    node.routes["rpc:EXPERIMENTAL_protocol_config"] = rpc_ok(
        {"shard_layout": fixture("rpc_shard_layout")["result"]["shard_layout"]}
    )
    monkeypatch.setenv("NEAR_TESTNET_RPC_SERVER", node.url)
    monkeypatch.setenv("NEAR_TESTNET_HISTORY_SERVER", node.url)
    monkeypatch.setenv("ALGORAND_TESTNET_INDEXER_SERVER", node.url)
    monkeypatch.setenv("MCI_MAX_RETRIES", "0")
    monkeypatch.setenv("MCI_REQUESTS_PER_SECOND", "1000")

    def run(*arguments: str) -> Result:
        options = ["--db", str(tmp_path / "cli.db"), "--env-file", str(tmp_path / "none.env")]
        return CliRunner().invoke(cli, [*options, *arguments])

    return run


def test_cli_index_account_uses_near_terms(node: FakeNode, invoke: Any) -> None:
    raw = script_one_transaction(node)
    node.routes["rpc:query"] = [
        Reply(fixture("account_named")),
        Reply(fixture("access_keys_function_call")),
    ]
    result = invoke("--chain", "near", "index", "account", TRANSFER_SIGNER)
    assert result.exit_code == 0, result.output
    assert "NEAR" in result.stdout
    assert "Tgas" in result.stdout
    assert "function-call" in result.stdout
    assert "Shard" in result.stdout
    assert raw["transaction"]["hash"] in result.stdout


def test_cli_index_contract(node: FakeNode, invoke: Any) -> None:
    node.routes["rpc:query"] = Reply(fixture("account_contract"))
    result = invoke("--chain", "near", "index", "contract", ACCOUNT)
    assert result.exit_code == 0, result.output
    assert "Code hash" in result.stdout
    assert "Global contract" in result.stdout


def test_cli_index_contract_is_for_near_and_evm_chains_only(invoke: Any) -> None:
    result = invoke("index", "contract", ACCOUNT)
    assert result.exit_code == 1
    assert (
        "index contract is for near, ethereum, arbitrum, optimism, base, polygon, bnb, not algorand"
    ) in result.output


def test_cli_other_chains_lookups_are_refused_on_near(invoke: Any) -> None:
    assert "for solana" in invoke("--chain", "near", "index", "token", "wrap.testnet").output
    assert "for algorand" in invoke("--chain", "near", "index", "app", "5").output


def test_cli_sync_metrics_and_export(node: FakeNode, invoke: Any, tmp_path: Path) -> None:
    script_two_pages(node)
    synced = invoke("--chain", "near", "sync", HISTORY_ACCOUNT)
    assert synced.exit_code == 0, synced.output
    assert f"{DIRECT_IN_HISTORY} transactions" in synced.stdout

    report = json.loads(invoke("--chain", "near", "metrics", "--days", "36500").stdout)
    assert report["chain"] == "near"
    assert report["native"]["fees_near"]

    output = tmp_path / "records.parquet"
    exported = invoke("--chain", "near", "export", "--format", "parquet", "--output", str(output))
    assert exported.exit_code == 0, exported.output
    assert len(pd.read_parquet(output)) == DIRECT_IN_HISTORY


def test_cli_reports_the_wrong_network_without_the_endpoint_or_a_traceback(
    node: FakeNode, invoke: Any
) -> None:
    node.routes["rpc:status"] = rpc_ok({"chain_id": "mainnet"})
    result = invoke("--chain", "near", "index", "account", ACCOUNT)
    assert result.exit_code == 1
    assert "serves NEAR mainnet, not NEAR testnet" in result.output
    assert "NEAR_TESTNET_RPC_SERVER" in result.output
    assert node.url not in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("account", ["BAD", "a", "x..y"])
def test_cli_reports_a_bad_account_id_in_one_clean_line(invoke: Any, account: str) -> None:
    result = invoke("--chain", "near", "index", "account", account)
    assert result.exit_code == 1
    assert "not a valid NEAR account ID" in result.output
    assert isinstance(result.exception, SystemExit)


def test_default_scope_keeps_what_nearblocks_lists(node: FakeNode, near_settings: Settings) -> None:
    pages = script_two_pages(node)
    with Indexer("near", settings=near_settings) as indexer:
        assert indexer.sync(HISTORY_ACCOUNT).records_fetched == DIRECT_IN_HISTORY
        stored = indexer.stored_transactions(account=HISTORY_ACCOUNT)

    assert all(HISTORY_ACCOUNT in (record.sender, record.receiver) for record in stored)
    left_out = [
        tx["transaction"]
        for page in pages
        for tx in page["transactions"]
        if tx["transaction"]["hash"] not in {record.id for record in stored}
    ]
    assert len(left_out) == ALL_IN_HISTORY - DIRECT_IN_HISTORY
    assert all(HISTORY_ACCOUNT not in (tx["signer_id"], tx["receiver_id"]) for tx in left_out)


def test_scope_all_keeps_the_full_count(node: FakeNode, near_settings: Settings) -> None:
    pages = script_two_pages(node)
    with Indexer("near", settings=with_scope(near_settings, "all")) as indexer:
        assert indexer.sync(HISTORY_ACCOUNT).records_fetched == ALL_IN_HISTORY
        report = indexer.metrics(days=36500, account=HISTORY_ACCOUNT)
        stored = indexer.stored_transactions(account=HISTORY_ACCOUNT)
    assert report.transactions == ALL_IN_HISTORY
    assert {record.id for record in stored} == {h for page in pages for h in hashes_of(page)}


def test_scope_direct_can_also_be_named(near_settings: Settings) -> None:
    adapter = create_indexer(None, with_scope(near_settings, "direct"))
    assert adapter._scope == "direct"
    assert create_indexer(None, near_settings)._scope == "direct"


def test_unknown_scope_is_refused(near_settings: Settings) -> None:
    with pytest.raises(ConfigError, match="NEAR_TESTNET_HISTORY_SCOPE must be one of: direct, all"):
        create_indexer(None, with_scope(near_settings, "everything"))


def test_page_with_nothing_in_scope_still_moves_to_the_next(
    node: FakeNode, near_settings: Settings
) -> None:
    script_two_pages(node)
    with Indexer("near", settings=near_settings) as indexer:
        result = indexer.sync("someone-else.testnet")
    assert result.records_fetched == 0
    assert len(node.posted_to(ACCOUNT_PATH)) == 2
    assert len(node.posted_to(TRANSACTIONS_PATH)) == 2


def test_recent_transactions_follow_the_same_scope(node: FakeNode, near_settings: Settings) -> None:
    script_two_pages(node)
    with Indexer("near", settings=near_settings) as indexer:
        recent = indexer.account_transactions(HISTORY_ACCOUNT, limit=50)
    assert len(recent) == DIRECT_IN_HISTORY


def test_cli_scope_all_gives_the_full_count(
    node: FakeNode, invoke: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_two_pages(node)
    monkeypatch.setenv("NEAR_TESTNET_HISTORY_SCOPE", "all")
    synced = invoke("--chain", "near", "sync", HISTORY_ACCOUNT)
    assert synced.exit_code == 0, synced.output
    assert f"{ALL_IN_HISTORY} transactions" in synced.stdout
