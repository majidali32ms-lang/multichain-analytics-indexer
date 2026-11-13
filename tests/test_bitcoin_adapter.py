from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from multichain_indexer import (
    ConfigError,
    Indexer,
    InvalidAddress,
    InvalidInput,
    MalformedResponse,
    NotFound,
    UnsupportedChain,
    WrongNetwork,
)
from multichain_indexer.adapters.bitcoin import BitcoinIndexer, create_indexer
from multichain_indexer.adapters.bitcoin import adapter as adapter_module
from multichain_indexer.adapters.bitcoin.esplora import create_esplora_source
from multichain_indexer.adapters.bitcoin.models import BitcoinAddress, BitcoinTransaction
from multichain_indexer.adapters.bitcoin.networks import NETWORKS
from multichain_indexer.core.config import Settings
from multichain_indexer.core.models import Status
from tests.bitcoin_support import (
    BUSY,
    SERVER_SETTING,
    SMALL,
    TIP,
    fixture,
    script_path,
    serve_chain,
    serve_small,
)
from tests.conftest import FakeNode, Reply


async def test_the_default_network_is_testnet4(adapter: BitcoinIndexer) -> None:
    assert adapter.network == "testnet4"
    assert adapter.profile.base_unit == "sats"


async def test_the_genesis_block_is_checked_once_before_the_first_lookup(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_small(node)
    await adapter.fetch_account(SMALL)
    await adapter.chain_head()
    assert node.requests[0][0] == "/blocks/0"
    assert len(node.requests_to("/blocks/0")) == 1


async def test_a_server_on_another_network_is_refused_by_name_of_the_setting(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_chain(node, genesis="signet_blocks_0")
    with pytest.raises(WrongNetwork) as caught:
        await adapter.chain_head()
    message = str(caught.value)
    assert "serves Bitcoin signet, not Bitcoin testnet4" in message
    assert SERVER_SETTING in message
    assert node.url not in message
    assert node.requests_to("/blocks/tip/height") == []


async def test_an_unknown_genesis_block_is_another_network(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    blocks = fixture("testnet4_blocks_0")
    blocks[0]["id"] = "f" * 64
    node.routes["/blocks/0"] = Reply(blocks)
    with pytest.raises(WrongNetwork, match="serves another network"):
        await adapter.chain_head()


@pytest.mark.parametrize(
    ("genesis", "tip", "message"),
    [
        (Reply([]), Reply(raw="1"), "genesis block"),
        (Reply([{"id": "x", "height": 5}]), Reply(raw="1"), "genesis block"),
        (Reply({"height": 0}), Reply(raw="1"), "genesis block"),
        (Reply([{"id": 7, "height": 0}]), Reply(raw="1"), "id to be text"),
        (None, Reply(raw='"161111"'), "block height"),
        (None, Reply(raw="-1"), "block height"),
        (None, Reply(raw="true"), "block height"),
    ],
)
async def test_malformed_network_replies_are_reported(
    node: FakeNode, adapter: BitcoinIndexer, genesis: Reply | None, tip: Reply, message: str
) -> None:
    node.routes["/blocks/0"] = genesis or Reply(fixture("testnet4_blocks_0"))
    node.routes["/blocks/tip/height"] = tip
    with pytest.raises(MalformedResponse, match=message):
        await adapter.chain_head()


async def test_an_address_holds_its_confirmed_outputs(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_small(node)
    record = await adapter.fetch_account(SMALL)
    stats = fixture("testnet4_scripthash_small")["chain_stats"]
    native = record.native
    assert isinstance(native, BitcoinAddress)
    assert record.balance == stats["funded_txo_sum"] - stats["spent_txo_sum"]
    assert native.utxo_count == stats["funded_txo_count"] - stats["spent_txo_count"]
    assert native.address_type == "p2pkh"
    assert native.transactions == stats["tx_count"]
    assert native.unconfirmed_change == 0
    assert adapter.describe(record)["Balance"] == "0.03571478 BTC"


async def test_the_mempool_changes_the_unconfirmed_figures_only(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_chain(node)
    stats = fixture("testnet4_scripthash_small")
    stats["mempool_stats"] = {
        "funded_txo_count": 1,
        "funded_txo_sum": 500,
        "spent_txo_count": 1,
        "spent_txo_sum": 2_000,
        "tx_count": 2,
    }
    node.routes[script_path(SMALL)] = Reply(stats)
    record = await adapter.fetch_account(SMALL)
    native = record.native
    assert isinstance(native, BitcoinAddress)
    assert native.unconfirmed_change == -1_500
    assert native.mempool_transactions == 2
    assert native.spent_outputs == stats["chain_stats"]["spent_txo_count"] + 1
    assert adapter.describe(record)["Unconfirmed"] == "-1500 sats"


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ({"chain_stats": {}, "mempool_stats": None}, "funded_txo_count"),
        ({"mempool_stats": {}}, "chain_stats and mempool_stats"),
    ],
)
async def test_malformed_address_totals_are_reported(
    node: FakeNode, adapter: BitcoinIndexer, reply: dict[str, Any], message: str
) -> None:
    serve_chain(node)
    node.routes[script_path(SMALL)] = Reply(reply)
    with pytest.raises(MalformedResponse, match=message):
        await adapter.fetch_account(SMALL)


async def test_a_mainnet_address_is_refused_before_any_request(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    with pytest.raises(InvalidAddress, match="mainnet"):
        await adapter.fetch_account("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    assert node.requests == []


async def test_bitcoin_has_no_tokens_or_contracts(adapter: BitcoinIndexer) -> None:
    with pytest.raises(InvalidInput, match="no tokens"):
        await adapter.fetch_asset("1")
    with pytest.raises(InvalidInput, match="no contracts"):
        await adapter.fetch_contract("1")


async def test_history_lists_mempool_first_then_confirmed_newest_first(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_small(node)
    node.routes[script_path(SMALL, "/txs/mempool")] = Reply([fixture("testnet4_tx_mempool")])
    pages = [page async for page in adapter.iter_account_transactions(SMALL)]
    assert len(pages) == 1
    records = pages[0].records
    assert records[0].status is Status.PENDING
    assert [r.id for r in records[1:]] == [
        tx["txid"] for tx in fixture("testnet4_scripthash_small_chain")
    ]
    assert pages[0].next_cursor is None


async def test_a_full_page_continues_after_the_last_txid_seen(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    first, second = (
        fixture("testnet4_scripthash_busy_chain_1"),
        fixture("testnet4_scripthash_busy_chain_2"),
    )
    serve_chain(node)
    node.routes[script_path(BUSY, "/txs/mempool")] = Reply([])
    node.routes[script_path(BUSY, "/txs/chain")] = Reply(first)
    node.routes[script_path(BUSY, f"/txs/chain/{first[-1]['txid']}")] = Reply(second)
    node.routes[script_path(BUSY, f"/txs/chain/{second[-1]['txid']}")] = Reply([])
    pages = [page async for page in adapter.iter_account_transactions(BUSY)]
    assert [len(page.records) for page in pages] == [25, 25]
    assert pages[0].next_cursor == first[-1]["txid"]
    assert pages[1].next_cursor == second[-1]["txid"]


async def test_history_stops_at_the_oldest_height_asked_for(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    first = fixture("testnet4_scripthash_busy_chain_1")
    serve_chain(node)
    node.routes[script_path(BUSY, "/txs/chain")] = Reply(first)
    floor = first[10]["status"]["block_height"]
    pages = [
        page
        async for page in adapter.iter_account_transactions(
            BUSY, from_height=floor, to_height=TIP - 1
        )
    ]
    heights = [r.native.block_height for page in pages for r in page.records]  # type: ignore[attr-defined]
    assert heights and min(heights) >= floor
    assert pages[-1].next_cursor is None
    assert len(node.requests_to(script_path(BUSY, "/txs/chain"))) == 1
    assert node.requests_to(script_path(BUSY, "/txs/mempool")) == []


async def test_history_below_the_tip_skips_newer_and_unconfirmed_transactions(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_small(node)
    chain = fixture("testnet4_scripthash_small_chain")
    ceiling = min(tx["status"]["block_height"] for tx in chain)
    pages = [page async for page in adapter.iter_account_transactions(SMALL, to_height=ceiling)]
    records = [r for page in pages for r in page.records]
    assert records
    assert all(r.native.block_height == ceiling for r in records)  # type: ignore[attr-defined]
    assert node.requests_to(script_path(SMALL, "/txs/mempool")) == []


async def test_an_empty_history_yields_no_page(node: FakeNode, adapter: BitcoinIndexer) -> None:
    serve_chain(node)
    node.routes[script_path(SMALL, "/txs/mempool")] = Reply([])
    node.routes[script_path(SMALL, "/txs/chain")] = Reply([])
    assert [page async for page in adapter.iter_account_transactions(SMALL)] == []


async def test_a_history_cursor_that_is_not_a_txid_is_refused(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_chain(node)
    with pytest.raises(InvalidInput, match="history cursor"):
        await anext(adapter.iter_account_transactions(SMALL, cursor="12:3"))


async def test_a_history_reply_that_is_not_a_list_is_malformed(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_chain(node)
    node.routes[script_path(SMALL, "/txs/mempool")] = Reply({"txs": []})
    with pytest.raises(MalformedResponse, match="list of transactions"):
        await anext(adapter.iter_account_transactions(SMALL))


async def test_one_transaction_is_read_by_txid(node: FakeNode, adapter: BitcoinIndexer) -> None:
    serve_chain(node)
    tx = fixture("tx_taproot")
    node.routes[f"/tx/{tx['txid']}"] = Reply(tx)
    record = await adapter.fetch_transaction(tx["txid"].upper())
    assert record.id == tx["txid"]
    assert record.fee == tx["fee"]
    details = adapter.describe(record)
    assert details["Fee rate"] == "2.01 sat/vB"
    assert details["Status"].endswith(f"block {tx['status']['block_height']}")


async def test_a_txid_must_be_64_hex_digits(node: FakeNode, adapter: BitcoinIndexer) -> None:
    with pytest.raises(InvalidInput, match="not a txid"):
        await adapter.fetch_transaction("abc")
    assert node.requests == []


async def test_an_unknown_txid_is_not_found(node: FakeNode, adapter: BitcoinIndexer) -> None:
    serve_chain(node)
    node.routes[f"/tx/{'0' * 64}"] = Reply(raw="Transaction not found", status=404)
    with pytest.raises(NotFound):
        await adapter.fetch_transaction("0" * 64)


async def test_a_mempool_transaction_is_described_as_unconfirmed(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_chain(node)
    tx = fixture("testnet4_tx_mempool")
    node.routes[f"/tx/{tx['txid']}"] = Reply(tx)
    record = await adapter.fetch_transaction(tx["txid"])
    assert adapter.describe(record)["Status"] == "unconfirmed (mempool)"


def test_final_height_leaves_the_last_five_blocks_pending(adapter: BitcoinIndexer) -> None:
    assert adapter.final_height(TIP) == TIP - 5
    assert adapter.final_height(3) == 0


def test_a_sync_stores_the_history_and_a_resync_adds_nothing(
    node: FakeNode, btc_settings: Settings
) -> None:
    serve_small(node)
    with Indexer("bitcoin", settings=btc_settings) as indexer:
        first = indexer.sync(SMALL)
        again = indexer.sync(SMALL)
        stored = indexer.stored_transactions(account=SMALL)
        report = indexer.metrics(days=100_000, account=SMALL)
    assert first.records_fetched == 4
    assert first.to_height == TIP
    assert again.records_fetched == 0
    assert len(stored) == 4
    assert report.native["fees_sats"] == sum(record.fee for record in stored)


def test_a_replaced_block_orphans_its_transaction_and_the_new_one_is_stored(
    node: FakeNode, btc_settings: Settings
) -> None:
    taproot = fixture("tx_taproot")
    height = taproot["status"]["block_height"]
    serve_chain(node, tip=height + 2)
    node.routes[script_path(SMALL, "/txs/mempool")] = Reply([])
    node.routes[script_path(SMALL, "/txs/chain")] = Reply([taproot])
    with Indexer("bitcoin", settings=btc_settings) as indexer:
        indexer.sync(SMALL)
        assert indexer.stored_transactions()[0].status is Status.PENDING

        replacement = fixture("tx_op_return")
        replacement["status"] = {**taproot["status"], "block_hash": "e" * 64}
        serve_chain(node, tip=height + 3)
        node.routes[script_path(SMALL, "/txs/chain")] = Reply([replacement])
        node.routes[f"/tx/{taproot['txid']}"] = Reply(raw="Transaction not found", status=404)
        node.routes[f"/block/{taproot['status']['block_hash']}/status"] = Reply(
            {"in_best_chain": False, "height": height}
        )
        indexer.sync(SMALL)
        stored = {record.id: record for record in indexer.stored_transactions()}
    assert stored[taproot["txid"]].status is Status.ORPHANED
    assert stored[replacement["txid"]].status is Status.PENDING


def test_a_transaction_dropped_from_the_mempool_is_orphaned(
    node: FakeNode, btc_settings: Settings
) -> None:
    waiting = fixture("testnet4_tx_mempool")
    serve_small(node)
    node.routes[script_path(SMALL, "/txs/mempool")] = Reply([waiting])
    with Indexer("bitcoin", settings=btc_settings) as indexer:
        indexer.sync(SMALL)
        node.routes[script_path(SMALL, "/txs/mempool")] = Reply([])
        node.routes[f"/tx/{waiting['txid']}"] = Reply(raw="Transaction not found", status=404)
        indexer.sync(SMALL)
        stored = {record.id: record for record in indexer.stored_transactions()}
    assert stored[waiting["txid"]].status is Status.ORPHANED


async def test_recheck_updates_what_confirmed_and_leaves_what_it_cannot_prove(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_chain(node)
    taproot, waiting = fixture("tx_taproot"), fixture("testnet4_tx_mempool")
    node.routes[f"/tx/{taproot['txid']}"] = Reply(taproot)
    node.routes[f"/tx/{waiting['txid']}"] = Reply(waiting)
    old_tip = taproot["status"]["block_height"] + 1
    pending = await _records_at(adapter, node, [taproot, waiting], tip=old_tip)

    node.routes["/blocks/tip/height"] = Reply(raw=str(old_tip + 10))
    confirmed = {**waiting, "status": taproot["status"]}
    node.routes[f"/tx/{waiting['txid']}"] = Reply(confirmed)
    changed = await adapter.recheck_pending(pending)
    assert {record.id: record.status for record in changed} == {
        taproot["txid"]: Status.FINAL,
        waiting["txid"]: Status.FINAL,
    }

    node.routes[f"/tx/{taproot['txid']}"] = Reply(raw="not found", status=404)
    node.routes[f"/block/{taproot['status']['block_hash']}/status"] = Reply({"in_best_chain": True})
    assert await adapter.recheck_pending(pending[:1]) == []


async def test_an_unknown_block_counts_as_replaced(node: FakeNode, adapter: BitcoinIndexer) -> None:
    serve_chain(node)
    taproot = fixture("tx_taproot")
    node.routes[f"/tx/{taproot['txid']}"] = Reply(taproot)
    pending = await _records_at(adapter, node, [taproot], tip=taproot["status"]["block_height"])
    node.routes[f"/tx/{taproot['txid']}"] = Reply(raw="not found", status=404)
    changed = await adapter.recheck_pending(pending)
    assert [record.status for record in changed] == [Status.ORPHANED]


async def test_a_malformed_block_status_is_reported(
    node: FakeNode, adapter: BitcoinIndexer
) -> None:
    serve_chain(node)
    taproot = fixture("tx_taproot")
    node.routes[f"/tx/{taproot['txid']}"] = Reply(taproot)
    pending = await _records_at(adapter, node, [taproot], tip=taproot["status"]["block_height"])
    node.routes[f"/tx/{taproot['txid']}"] = Reply(raw="not found", status=404)
    node.routes[f"/block/{taproot['status']['block_hash']}/status"] = Reply({"height": 1})
    with pytest.raises(MalformedResponse, match="in_best_chain"):
        await adapter.recheck_pending(pending)


async def test_unchanged_records_are_not_returned(node: FakeNode, adapter: BitcoinIndexer) -> None:
    serve_chain(node)
    taproot = fixture("tx_taproot")
    node.routes[f"/tx/{taproot['txid']}"] = Reply(taproot)
    pending = await _records_at(adapter, node, [taproot], tip=taproot["status"]["block_height"])
    assert await adapter.recheck_pending(pending) == []


async def _records_at(
    adapter: BitcoinIndexer, node: FakeNode, txs: list[dict[str, Any]], *, tip: int
) -> list[Any]:
    node.routes["/blocks/tip/height"] = Reply(raw=str(tip))
    records = [await adapter.fetch_transaction(tx["txid"]) for tx in txs]
    assert all(record.status is Status.PENDING for record in records)
    return records


def test_metrics_cover_fees_rates_types_and_adoption(
    node: FakeNode, btc_settings: Settings
) -> None:
    names = ["tx_coinbase", "tx_taproot", "tx_legacy_p2pkh", "tx_op_return"]
    txs = [fixture(name) for name in names]
    serve_chain(node)
    node.routes[script_path(SMALL, "/txs/mempool")] = Reply([])
    node.routes[script_path(SMALL, "/txs/chain")] = Reply(txs)
    with Indexer("bitcoin", settings=btc_settings) as indexer:
        indexer.sync(SMALL)
        native = indexer.metrics(days=100_000).native
    natives = [
        r.native for r in [_parse(tx) for tx in txs] if isinstance(r.native, BitcoinTransaction)
    ]
    rates = sorted(n.fee_rate for n in natives if not n.coinbase)
    assert native["fees_sats"] == sum(tx["fee"] for tx in txs)
    assert native["coinbase_transactions"] == 1
    assert native["op_return_outputs"] == sum(
        1 for tx in txs for out in tx["vout"] if out["scriptpubkey_type"] == "op_return"
    )
    assert native["taproot_share"] == pytest.approx(1 / 3)
    assert native["fee_rate_sat_per_vb"] == {
        "average": pytest.approx(sum(rates) / 3),
        "median": pytest.approx(rates[1]),
    }
    shares = native["address_types"]
    assert isinstance(shares, dict)
    assert sum(entry["outputs"] for entry in shares.values()) == sum(  # type: ignore[index]
        len(tx["vout"]) for tx in txs
    )


def test_metrics_over_nothing_have_no_rates(node: FakeNode, btc_settings: Settings) -> None:
    with Indexer("bitcoin", settings=btc_settings) as indexer:
        native = indexer.metrics(days=1).native
    assert native["fee_rate_sat_per_vb"] == {"average": None, "median": None}
    assert native["segwit_share"] is None
    assert native["fees_btc"] == "0"


def _parse(tx: dict[str, Any]) -> Any:
    from multichain_indexer.adapters.bitcoin.esplora import parse_esplora_transaction
    from multichain_indexer.adapters.bitcoin.parser import parse_transaction

    return parse_transaction(
        parse_esplora_transaction(tx, "/tx"), "testnet4", tip_height=TIP, history_source="esplora"
    )


def test_an_unknown_history_source_is_a_config_error(btc_settings: Settings) -> None:
    settings = btc_settings.model_copy(
        update={"env": {**btc_settings.env, "BITCOIN_TESTNET4_HISTORY_SOURCE": SecretStr("core")}}
    )
    with pytest.raises(ConfigError, match="BITCOIN_TESTNET4_HISTORY_SOURCE must be one of"):
        create_indexer(None, settings)


async def test_the_source_is_chosen_by_configuration(
    node: FakeNode, btc_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen: list[str] = []

    def other_source(network: Any, settings: Settings, cache: Any) -> Any:
        chosen.append(network.name)
        return create_esplora_source(network, settings, cache)

    monkeypatch.setitem(adapter_module.HISTORY_SOURCES, "other", other_source)
    settings = btc_settings.model_copy(
        update={"env": {**btc_settings.env, "BITCOIN_TESTNET4_HISTORY_SOURCE": SecretStr("other")}}
    )
    serve_chain(node)
    built = create_indexer(None, settings)
    try:
        assert await built.chain_head() == TIP
    finally:
        await built.aclose()
    assert chosen == ["testnet4"]


def test_an_unknown_network_is_refused(btc_settings: Settings) -> None:
    with pytest.raises(InvalidInput, match="unknown Bitcoin network 'testnet'"):
        create_indexer("testnet", btc_settings)


def test_a_remote_server_must_use_https(btc_settings: Settings) -> None:
    settings = btc_settings.model_copy(
        update={"env": {SERVER_SETTING: SecretStr("http://esplora.example")}}
    )
    with pytest.raises(ConfigError, match=f"{SERVER_SETTING} must use https"):
        create_indexer(None, settings)


def test_the_default_servers_are_mempool_space() -> None:
    assert {network.esplora_server for network in NETWORKS.values()} >= {
        "https://mempool.space/testnet4/api",
        "https://mempool.space/signet/api",
        "https://mempool.space/api",
    }


def test_a_chain_that_cannot_read_one_transaction_says_so(settings: Settings) -> None:
    with (
        Indexer("algorand", settings=settings) as indexer,
        pytest.raises(UnsupportedChain, match="cannot look up a single transaction"),
    ):
        indexer.transaction("x")
