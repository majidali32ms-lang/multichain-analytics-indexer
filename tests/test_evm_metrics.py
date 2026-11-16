from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from multichain_indexer.adapters.evm.adapter import indexer_factory
from multichain_indexer.adapters.evm.metrics import evm_metrics
from multichain_indexer.adapters.evm.profiles import ARBITRUM, BASE, BNB, ETHEREUM, EvmChain
from multichain_indexer.adapters.evm.transactions import RawTransaction, parse_transaction
from multichain_indexer.core.config import Settings
from multichain_indexer.core.models import TransactionRecord

FIXTURES = Path(__file__).parent / "fixtures" / "evm"


def record(name: str) -> TransactionRecord:
    raw: dict[str, Any] = json.loads((FIXTURES / f"tx_{name}.json").read_text("utf-8"))
    return parse_transaction(
        RawTransaction(raw["transaction"], raw["receipt"], raw["block"]),
        chain=raw["chain"],
        network="testnet",
        final=True,
    )


def metrics_for(chain: EvmChain, names: list[str]) -> dict[str, Any]:
    adapter = indexer_factory(chain)(None, Settings(), None)
    return adapter.chain_metrics([record(name) for name in names])


def test_fee_split_on_an_op_stack_chain() -> None:
    names = ["base_sepolia_native", "base_sepolia_eip1559"]
    report = metrics_for(BASE, names)
    records = [record(name) for name in names]
    assert report["fees_wei"] == {
        "total": sum(r.fee for r in records),
        "execution": 126_000_000_000 + 134_400_000_000,
        "l1_data": 372 * 2,
        "blob": 0,
    }
    assert report["fees_eth"] == "0.000000260400000744"
    assert report["arbitrum_gas_used_for_l1"] is None


def test_blob_fees_on_ethereum() -> None:
    report = metrics_for(ETHEREUM, ["sepolia_blob", "sepolia_eip1559", "sepolia_failed_erc20"])
    assert report["fees_wei"]["blob"] == 786_432 * 6
    assert report["fees_wei"]["l1_data"] == 0
    assert report["transaction_types"] == {"eip-1559": 1, "eip-4844": 1, "legacy": 1}


def test_arbitrum_reports_the_l1_share_of_gas() -> None:
    report = metrics_for(ARBITRUM, ["arbitrum_one_l1_gas", "arbitrum_sepolia_native"])
    gas = record("arbitrum_one_l1_gas").native.gas_used + 21_000
    assert report["gas_used"] == gas
    assert report["arbitrum_gas_used_for_l1"] == {"gas": 233, "share_of_gas_used": 233 / gas}


def test_tokens_and_contracts_are_ranked_in_the_chains_own_terms() -> None:
    report = metrics_for(BNB, ["bsc_testnet_erc20", "bsc_testnet_erc20", "bsc_testnet_native"])
    token = record("bsc_testnet_erc20").native.token_transfers[0].token
    contract = record("bsc_testnet_erc20").receiver
    assert report["top_bep20_tokens_by_transfers"] == [{"token": token, "count": 2}]
    assert report["top_contracts_by_calls"] == [{"contract": contract, "count": 2}]
    assert report["fees_bnb"]
    assert report["post_quantum"] == {"transactions": 0, "share": 0.0, "schemes": {}}


def test_no_records() -> None:
    report = evm_metrics([], native_symbol="POL", token_standard="ERC-20")
    assert report["fees_pol"] == "0"
    assert report["top_erc20_tokens_by_transfers"] == []
    assert report["post_quantum"]["share"] is None
