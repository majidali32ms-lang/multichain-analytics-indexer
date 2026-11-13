from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from multichain_indexer import MalformedResponse
from multichain_indexer.adapters.bitcoin.esplora import parse_esplora_transaction
from multichain_indexer.adapters.bitcoin.history import (
    BlockRef,
    SourceInput,
    SourceOutput,
    SourceTransaction,
)
from multichain_indexer.adapters.bitcoin.models import BitcoinTransaction
from multichain_indexer.adapters.bitcoin.parser import parse_transaction, virtual_size
from multichain_indexer.core.models import Status, TransactionRecord

FIXTURES = Path(__file__).parent / "fixtures" / "bitcoin"
TIP = 161_111
# Fee and fee rate as mempool.space showed them for the same transactions on 2026-11-13; it
# shows a rate to three significant digits.
MEMPOOL_SPACE = {
    "tx_legacy_p2pkh": (6690, "30.0"),
    "tx_p2sh_wrapped_segwit": (1660, "10.0"),
    "tx_native_segwit_p2wpkh": (80299, "572"),
    "tx_native_segwit_p2wsh": (20200, "101"),
    "tx_taproot": (310, "2.01"),
    "tx_many_inputs_outputs": (3940, "5.61"),
    "tx_op_return": (236, "1.01"),
    "tx_coinbase": (0, "0.00"),
    "testnet4_tx_mempool": (440, "2.01"),
    "signet_tx_mempool": (221, "1.00"),
}


def raw(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


def parsed(name: str, tip: int = TIP, network: str = "testnet4") -> TransactionRecord:
    source = parse_esplora_transaction(raw(name), f"/tx/{name}")
    return parse_transaction(source, network, tip_height=tip, history_source="esplora")


def native(record: TransactionRecord) -> BitcoinTransaction:
    assert isinstance(record.native, BitcoinTransaction)
    return record.native


@pytest.mark.parametrize(("name", "shown"), MEMPOOL_SPACE.items())
def test_fee_and_fee_rate_match_mempool_space(name: str, shown: tuple[int, str]) -> None:
    fee, rate = shown
    record = parsed(name)
    assert record.fee == fee == raw(name)["fee"]
    assert float(f"{native(record).fee_rate:.3g}") == float(rate)


@pytest.mark.parametrize("name", [*MEMPOOL_SPACE, "tx_mainnet_p2pk"])
def test_fee_is_inputs_minus_outputs_and_vsize_is_weight_over_four_rounded_up(name: str) -> None:
    record = parsed(name, network="mainnet" if "mainnet" in name else "testnet4")
    details = native(record)
    spent = sum(entry.value for entry in details.inputs)
    paid = sum(out.value for out in details.outputs)
    assert record.fee == (0 if details.coinbase else spent - paid)
    assert details.vsize == -(-details.weight // 4)
    assert len(details.inputs) == len(raw(name)["vin"])
    assert len(details.outputs) == len(raw(name)["vout"])


def test_a_weight_off_a_multiple_of_four_gives_the_unrounded_rate() -> None:
    assert virtual_size(585) == 147
    record = parse_transaction(
        _source(weight=585, fee_inputs=(294 + 1000,), outputs=(("tb1qa", 1000),)),
        "testnet4",
        tip_height=TIP,
        history_source="esplora",
    )
    assert native(record).fee_rate == pytest.approx(294 / 146.25)


def test_legacy_p2pkh_has_no_witness() -> None:
    details = native(parsed("tx_legacy_p2pkh"))
    assert [entry.script_type for entry in details.inputs] == ["p2pkh"]
    assert not details.segwit
    assert not details.taproot


def test_p2sh_wrapped_segwit_spends_p2sh_with_a_witness() -> None:
    details = native(parsed("tx_p2sh_wrapped_segwit"))
    assert details.inputs[0].script_type == "p2sh"
    assert details.inputs[0].witness
    assert details.segwit
    assert details.size > details.vsize


@pytest.mark.parametrize(
    ("name", "kind"), [("tx_native_segwit_p2wpkh", "p2wpkh"), ("tx_native_segwit_p2wsh", "p2wsh")]
)
def test_native_segwit_inputs_are_named_without_the_witness_version(name: str, kind: str) -> None:
    details = native(parsed(name))
    assert details.inputs[0].script_type == kind
    assert details.segwit
    assert not details.taproot


def test_taproot_spends_a_p2tr_output() -> None:
    details = native(parsed("tx_taproot"))
    assert details.inputs[0].script_type == "p2tr"
    assert details.taproot
    assert details.segwit


def test_many_inputs_and_outputs_are_all_kept() -> None:
    record = parsed("tx_many_inputs_outputs")
    details = native(record)
    assert (len(details.inputs), len(details.outputs)) == (7, 7)
    largest = max(details.inputs, key=lambda entry: entry.value)
    assert record.sender == largest.address
    assert [out.index for out in details.outputs] == list(range(7))


def test_coinbase_has_no_fee_and_is_sent_by_coinbase() -> None:
    record = parsed("tx_coinbase")
    details = native(record)
    assert record.sender == "coinbase"
    assert record.fee == 0
    assert details.coinbase
    assert details.inputs[0].txid is None
    assert details.inputs[0].coinbase
    assert not details.segwit


def test_op_return_output_has_no_address_and_is_never_the_receiver() -> None:
    record = parsed("tx_op_return")
    details = native(record)
    op_return = [out for out in details.outputs if out.script_type == "op_return"]
    assert op_return
    assert all(out.address is None for out in op_return)
    assert record.receiver is not None


@pytest.mark.parametrize("name", ["testnet4_tx_mempool", "signet_tx_mempool"])
def test_a_mempool_transaction_is_pending_with_no_block(name: str) -> None:
    before = datetime.now(UTC)
    record = parsed(name, network="signet" if "signet" in name else "testnet4")
    details = native(record)
    assert record.status is Status.PENDING
    assert not details.confirmed
    assert details.confirmations == 0
    assert details.block_height is None
    assert details.block_hash is None
    assert record.time >= before


def test_rbf_follows_the_input_sequences() -> None:
    signalling = raw("tx_taproot")
    sequences = {entry["sequence"] for entry in signalling["vin"]}
    assert native(parsed("tx_taproot")).rbf is any(seq < 0xFFFFFFFE for seq in sequences)
    assert not native(parsed("tx_legacy_p2pkh")).rbf


def test_status_is_final_from_six_confirmations() -> None:
    height = raw("tx_taproot")["status"]["block_height"]
    assert parsed("tx_taproot", tip=height + 4).status is Status.PENDING
    record = parsed("tx_taproot", tip=height + 5)
    assert record.status is Status.FINAL
    assert native(record).confirmations == 6


def test_a_tip_behind_the_block_counts_one_confirmation() -> None:
    height = raw("tx_taproot")["status"]["block_height"]
    assert native(parsed("tx_taproot", tip=height - 3)).confirmations == 1


def test_mainnet_p2pk_shows_the_public_key_as_the_address() -> None:
    record = parsed("tx_mainnet_p2pk", network="mainnet")
    details = native(record)
    key_of = raw("tx_mainnet_p2pk")["vin"][0]["prevout"]["scriptpubkey"][2:-2]
    assert details.inputs[0].script_type == "p2pk"
    assert record.sender == key_of
    assert [out.script_type for out in details.outputs] == ["p2pk", "p2pk"]
    assert record.receiver == details.outputs[0].address
    assert record.amount == 1_000_000_000
    assert record.time == datetime(2009, 1, 12, 3, 30, 25, tzinfo=UTC)


def test_change_back_to_the_sender_is_not_the_receiver() -> None:
    source = _source(fee_inputs=(10_000,), outputs=(("tb1qsender", 8_000), ("tb1qpaid", 1_000)))
    record = parse_transaction(source, "testnet4", tip_height=TIP, history_source="esplora")
    assert (record.receiver, record.amount) == ("tb1qpaid", 1_000)


def test_a_transfer_to_oneself_names_the_largest_output() -> None:
    source = _source(fee_inputs=(10_000,), outputs=(("tb1qsender", 8_000), ("tb1qsender", 1_000)))
    record = parse_transaction(source, "testnet4", tip_height=TIP, history_source="esplora")
    assert (record.receiver, record.amount) == ("tb1qsender", 8_000)


def test_no_output_with_an_address_leaves_no_receiver() -> None:
    source = _source(fee_inputs=(10_000,), outputs=((None, 0),))
    record = parse_transaction(source, "testnet4", tip_height=TIP, history_source="esplora")
    assert (record.receiver, record.amount) == (None, 0)


def test_inputs_without_an_address_name_the_first_spent_output() -> None:
    source = _source(fee_inputs=(10_000,), outputs=(("tb1qpaid", 9_000),), input_address=None)
    record = parse_transaction(source, "testnet4", tip_height=TIP, history_source="esplora")
    assert record.sender == f"{'a' * 64}:0"


def test_a_reported_fee_that_disagrees_with_the_amounts_is_malformed() -> None:
    item = raw("tx_taproot")
    item["fee"] += 1
    source = parse_esplora_transaction(item, "/tx/x")
    with pytest.raises(MalformedResponse, match="inputs minus outputs"):
        parse_transaction(source, "testnet4", tip_height=TIP, history_source="esplora")


def test_outputs_worth_more_than_the_inputs_are_malformed() -> None:
    source = _source(fee_inputs=(1_000,), outputs=(("tb1qpaid", 2_000),), reported_fee=None)
    with pytest.raises(MalformedResponse, match="inputs minus outputs"):
        parse_transaction(source, "testnet4", tip_height=TIP, history_source="esplora")


def _breaks(item: dict[str, Any]) -> list[dict[str, Any]]:
    variants = []
    for change in (
        lambda tx: tx.pop("txid"),
        lambda tx: tx.update(txid="xyz"),
        lambda tx: tx.update(vin="none"),
        lambda tx: tx.update(vout=[1]),
        lambda tx: tx.update(vin=[1]),
        lambda tx: tx["vin"][0].update(witness="none"),
        lambda tx: tx["vin"][0].update(prevout=None),
        lambda tx: tx["vout"][0].update(scriptpubkey_address=7),
        lambda tx: tx["vout"][0].update(value=-1),
        lambda tx: tx["vout"][0].update(value=True),
        lambda tx: tx.update(status=None),
        lambda tx: tx["status"].pop("block_hash"),
        lambda tx: tx.update(weight="616"),
    ):
        broken = json.loads(json.dumps(item))
        change(broken)
        variants.append(broken)
    return variants


@pytest.mark.parametrize("broken", _breaks(raw("tx_taproot")))
def test_a_transaction_that_breaks_the_format_is_malformed(broken: dict[str, Any]) -> None:
    with pytest.raises(MalformedResponse):
        parse_esplora_transaction(broken, "/tx/x")


def test_a_transaction_that_is_not_an_object_is_malformed() -> None:
    with pytest.raises(MalformedResponse, match="transaction object"):
        parse_esplora_transaction([], "/tx/x")


def _source(
    *,
    fee_inputs: tuple[int, ...],
    outputs: tuple[tuple[str | None, int], ...],
    weight: int = 600,
    input_address: str | None = "tb1qsender",
    reported_fee: int | None = None,
) -> SourceTransaction:
    return SourceTransaction(
        txid="b" * 64,
        version=2,
        locktime=0,
        size=200,
        weight=weight,
        inputs=tuple(
            SourceInput("a" * 64, index, input_address, "p2wpkh", value, 0xFFFFFFFD, 2, False)
            for index, value in enumerate(fee_inputs)
        ),
        outputs=tuple(
            SourceOutput(index, address, "p2wpkh" if address else "op_return", "00", value)
            for index, (address, value) in enumerate(outputs)
        ),
        block=BlockRef(TIP, "c" * 64, 1_794_589_005),
        reported_fee=reported_fee,
    )
