from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from multichain_indexer import MalformedResponse
from multichain_indexer.adapters.evm.models import EvmTransaction
from multichain_indexer.adapters.evm.transactions import (
    TRANSFER_TOPIC,
    RawTransaction,
    parse_transaction,
)
from multichain_indexer.core.models import Status, TransactionRecord

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
ALL_FIXTURES = sorted(path.stem.removeprefix("tx_") for path in FIXTURES.glob("tx_*.json"))


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"tx_{name}.json").read_text("utf-8"))
    return loaded


def parse(raw: dict[str, Any], *, final: bool = True) -> TransactionRecord:
    return parse_transaction(
        RawTransaction(raw["transaction"], raw["receipt"], raw["block"]),
        chain=raw["chain"],
        network="testnet",
        final=final,
    )


def native(record: TransactionRecord) -> EvmTransaction:
    assert isinstance(record.native, EvmTransaction)
    return record.native


def wei(value: str) -> int:
    return int(value, 16)


def test_every_fixture_comes_from_its_chains_testnet_or_mainnet() -> None:
    assert len(ALL_FIXTURES) == 15
    for name in ALL_FIXTURES:
        raw = fixture(name)
        assert raw["transaction"]["hash"] == raw["receipt"]["transactionHash"]
        assert raw["receipt"]["blockNumber"] == raw["block"]["number"]


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_shared_fields_come_from_the_transaction_and_receipt(name: str) -> None:
    raw = fixture(name)
    record = parse(raw)
    details = native(record)
    receipt, transaction = raw["receipt"], raw["transaction"]
    assert record.id == transaction["hash"]
    assert record.sender.lower() == transaction["from"]
    assert record.amount == wei(transaction["value"])
    assert record.time.timestamp() == wei(raw["block"]["timestamp"])
    assert details.execution_fee == wei(receipt["gasUsed"]) * wei(receipt["effectiveGasPrice"])
    assert details.block_number == wei(receipt["blockNumber"])
    assert details.log_count == len(receipt["logs"])


def test_native_transfer() -> None:
    record = parse(fixture("bsc_testnet_native"))
    details = native(record)
    assert details.kind == "legacy"
    assert details.method_selector is None
    assert details.gas_used == 21_000
    assert record.fee == details.execution_fee
    assert record.status is Status.FINAL


def test_eip_1559_fees() -> None:
    raw = fixture("sepolia_eip1559")
    details = native(parse(raw))
    assert details.kind == "eip-1559"
    assert details.base_fee_per_gas == wei(raw["block"]["baseFeePerGas"])
    assert details.priority_fee_per_gas == details.effective_gas_price - wei("0x15")
    assert details.max_fee_per_gas == wei(raw["transaction"]["maxFeePerGas"])
    assert details.max_priority_fee_per_gas == wei(raw["transaction"]["maxPriorityFeePerGas"])
    assert details.method_selector == "0x01010002"


def test_erc20_transfer_is_decoded_from_its_log() -> None:
    raw = fixture("bsc_testnet_erc20")
    details = native(parse(raw))
    log = raw["receipt"]["logs"][0]
    (transfer,) = details.token_transfers
    assert details.method_selector == "0xa9059cbb"
    assert transfer.kind == "fungible"
    assert transfer.token.lower() == log["address"]
    assert transfer.sender.lower() == "0x" + log["topics"][1][-40:]
    assert transfer.receiver.lower() == "0x" + log["topics"][2][-40:]
    assert transfer.value == wei(log["data"]) == 6_101_000
    assert transfer.token_id is None


def test_several_transfers_in_one_contract_call() -> None:
    details = native(parse(fixture("arbitrum_sepolia_eip1559")))
    assert details.log_count == 4
    assert [transfer.kind for transfer in details.token_transfers] == ["fungible", "fungible"]


def test_erc721_transfer_carries_the_token_id() -> None:
    raw = fixture("bsc_testnet_erc20")
    log = raw["receipt"]["logs"][0]
    log["topics"].append("0x" + "0" * 63 + "7")
    log["data"] = "0x"
    (transfer,) = native(parse(raw)).token_transfers
    assert transfer.kind == "nft"
    assert transfer.token_id == 7
    assert transfer.value is None


@pytest.mark.parametrize(
    "change",
    [
        lambda log: log.update(topics=log["topics"][:2]),
        lambda log: log.update(data="0x" + "00" * 64),
        lambda log: log["topics"].__setitem__(1, "0x" + "f" * 64),
        lambda log: log["topics"].__setitem__(2, "0x1234"),
        lambda log: log["topics"].__setitem__(1, "not hex"),
        lambda log: log["topics"].__setitem__(0, "0x" + "a" * 64),
    ],
    ids=[
        "too-few-topics",
        "long-data",
        "not-an-address",
        "short-topic",
        "bad-topic",
        "other-event",
    ],
)
def test_logs_that_are_not_token_transfers_are_left_out(change: Any) -> None:
    raw = fixture("bsc_testnet_erc20")
    change(raw["receipt"]["logs"][0])
    details = native(parse(raw))
    assert details.token_transfers == ()
    assert details.log_count == 1


def test_failed_transaction_still_pays_its_fee() -> None:
    record = parse(fixture("sepolia_failed_erc20"))
    assert record.status is Status.FAILED
    assert record.fee == native(record).execution_fee > 0
    assert native(record).token_transfers == ()


def test_recent_block_is_pending() -> None:
    assert parse(fixture("sepolia_native"), final=False).status is Status.PENDING
    assert parse(fixture("sepolia_failed_erc20"), final=False).status is Status.FAILED


@pytest.mark.parametrize("name", ["sepolia_blob", "bsc_testnet_blob"])
def test_blob_transaction_pays_for_its_blob_gas(name: str) -> None:
    raw = fixture(name)
    record = parse(raw)
    blob = native(record).blob
    assert blob is not None
    assert native(record).kind == "eip-4844"
    assert blob.blob_count == len(raw["transaction"]["blobVersionedHashes"])
    assert blob.blob_gas_used == wei(raw["receipt"]["blobGasUsed"])
    assert blob.blob_gas_price == wei(raw["receipt"]["blobGasPrice"])
    assert blob.max_fee_per_blob_gas == wei(raw["transaction"]["maxFeePerBlobGas"])
    assert record.fee == native(record).execution_fee + blob.blob_gas_used * blob.blob_gas_price


def test_sepolia_blob_fee_matches_the_explorer() -> None:
    # Blockscout lists 32625000 wei as the execution fee and 786432 blob gas at 6 wei.
    record = parse(fixture("sepolia_blob"))
    assert native(record).execution_fee == 32_625_000
    assert record.fee == 32_625_000 + 786_432 * 6


@pytest.mark.parametrize(
    ("name", "explorer_fee"),
    [
        ("base_sepolia_native", 126_000_000_372),
        ("base_sepolia_eip1559", 134_400_000_372),
        ("op_sepolia_eip1559", 87_918_831_446),
    ],
)
def test_op_stack_fee_adds_the_l1_data_fee_as_the_explorer_does(
    name: str, explorer_fee: int
) -> None:
    raw = fixture(name)
    record = parse(raw)
    l1_data = native(record).l1_data
    assert l1_data is not None
    assert l1_data.l1_fee == wei(raw["receipt"]["l1Fee"])
    assert l1_data.l1_gas_used == wei(raw["receipt"]["l1GasUsed"])
    assert l1_data.l1_gas_price == wei(raw["receipt"]["l1GasPrice"])
    assert l1_data.l1_blob_base_fee == wei(raw["receipt"]["l1BlobBaseFee"])
    assert record.fee == explorer_fee


@pytest.mark.parametrize("name", ["base_sepolia_native", "op_sepolia_eip1559"])
def test_op_stack_blob_gas_used_without_a_price_is_not_a_blob_fee(name: str) -> None:
    raw = fixture(name)
    assert "blobGasUsed" in raw["receipt"]
    assert "blobGasPrice" not in raw["receipt"]
    assert native(parse(raw)).blob is None


def test_op_stack_deposit_pays_nothing_on_l2() -> None:
    record = parse(fixture("op_sepolia_deposit"))
    assert native(record).kind == "deposit"
    assert record.fee == 0


def test_arbitrum_l1_component_is_part_of_gas_used() -> None:
    raw = fixture("arbitrum_one_l1_gas")
    record = parse(raw)
    arbitrum = native(record).arbitrum
    assert arbitrum is not None
    assert arbitrum.gas_used_for_l1 == 233
    assert arbitrum.gas_used_for_l1 < native(record).gas_used
    assert arbitrum.l1_block_number == wei(raw["receipt"]["l1BlockNumber"])
    assert arbitrum.timeboosted is False
    assert record.fee == native(record).execution_fee
    assert native(record).l1_data is None


def test_unnamed_transaction_types_keep_their_number() -> None:
    raw = fixture("arbitrum_sepolia_native")
    raw["transaction"]["type"] = "0x6a"
    assert native(parse(raw)).kind == "type 106"


def test_contract_creation_names_the_new_contract() -> None:
    raw = fixture("sepolia_native")
    raw["transaction"]["to"] = None
    raw["receipt"]["contractAddress"] = "0x" + "ab" * 20
    record = parse(raw)
    assert native(record).created_contract == record.receiver
    assert native(record).method_selector is None


def test_chains_without_a_base_fee_have_no_priority_fee() -> None:
    raw = fixture("sepolia_native")
    del raw["block"]["baseFeePerGas"]
    assert native(parse(raw)).priority_fee_per_gas is None


@pytest.mark.parametrize(
    ("part", "field", "value", "message"),
    [
        ("receipt", "gasUsed", None, "gasUsed"),
        ("receipt", "logs", "none", "list of logs"),
        ("transaction", "hash", "", "hash"),
        ("transaction", "from", None, "from"),
        ("transaction", "to", "0x1234", "20-byte address"),
        ("transaction", "input", "0x123", "whole bytes"),
        ("block", "timestamp", "soon", "timestamp"),
    ],
)
def test_malformed_fields_are_reported(part: str, field: str, value: Any, message: str) -> None:
    raw = copy.deepcopy(fixture("bsc_testnet_erc20"))
    raw[part][field] = value
    with pytest.raises(MalformedResponse, match=message):
        parse(raw)


def test_transfer_topic_is_the_event_signature_hash() -> None:
    assert fixture("bsc_testnet_erc20")["receipt"]["logs"][0]["topics"][0] == TRANSFER_TOPIC
