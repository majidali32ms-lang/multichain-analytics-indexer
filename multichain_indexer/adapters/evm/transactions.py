"""Turns a transaction, its receipt and its block header into a ``TransactionRecord``.

All three come from the chain's own RPC, so the chain-specific receipt fields are read as the
chain reports them: ``l1Fee`` and its parts on OP Mainnet and Base, ``gasUsedForL1`` on
Arbitrum, ``blobGasUsed`` and ``blobGasPrice`` for EIP-4844 blob transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak
from pydantic import JsonValue

from multichain_indexer.adapters.evm.encoding import parse_data, parse_quantity
from multichain_indexer.adapters.evm.models import (
    EvmArbitrumGas,
    EvmBlobGas,
    EvmL1DataFee,
    EvmTokenTransfer,
    EvmTransaction,
)
from multichain_indexer.core.errors import MalformedResponse
from multichain_indexer.core.models import Status, TransactionRecord

TRANSACTION_KINDS = {
    0: "legacy",
    1: "eip-2930",
    2: "eip-1559",
    3: "eip-4844",
    4: "eip-7702",
    0x7E: "deposit",
}
RECEIPT_SUCCESS = 1
SELECTOR_BYTES = 4
WORD_BYTES = 32
ADDRESS_BYTES = 20

TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
# ERC-20 indexes sender and receiver, so the value is the data; ERC-721 indexes the token ID
# as well and has no data. Anything else with the same signature is not a token transfer.
FUNGIBLE_TOPICS = 3
NFT_TOPICS = 4

JsonObject = dict[str, JsonValue]


@dataclass(frozen=True)
class RawTransaction:
    """What the RPC returns for one transaction: the transaction, receipt and block header."""

    transaction: JsonObject
    receipt: JsonObject
    block: JsonObject


def parse_transaction(
    raw: RawTransaction, *, chain: str, network: str, final: bool
) -> TransactionRecord:
    """Build the record; ``final`` says whether the block is past the chain's finality rule.

    Raises:
        MalformedResponse: A field is missing or does not have its documented format.
    """
    transaction, receipt, block = raw.transaction, raw.receipt, raw.block
    native = _native(transaction, receipt, block)
    return TransactionRecord(
        id=_text(transaction, "hash"),
        time=datetime.fromtimestamp(_quantity(block, "timestamp"), tz=UTC),
        sender=_address(transaction, "from"),
        receiver=_optional_address(transaction, "to") or native.created_contract,
        amount=_quantity(transaction, "value"),
        fee=_total_fee(native),
        status=_status(receipt, final),
        chain=chain,
        network=network,
        native=native,
    )


def _native(transaction: JsonObject, receipt: JsonObject, block: JsonObject) -> EvmTransaction:
    type_number = _quantity(transaction, "type")
    gas_used = _quantity(receipt, "gasUsed")
    effective_gas_price = _quantity(receipt, "effectiveGasPrice")
    base_fee = _optional_quantity(block, "baseFeePerGas")
    logs = _logs(receipt)
    return EvmTransaction(
        kind=TRANSACTION_KINDS.get(type_number, f"type {type_number}"),
        block_number=_quantity(receipt, "blockNumber"),
        block_hash=_text(receipt, "blockHash"),
        transaction_index=_quantity(receipt, "transactionIndex"),
        nonce=_quantity(transaction, "nonce"),
        gas_limit=_quantity(transaction, "gas"),
        gas_used=gas_used,
        effective_gas_price=effective_gas_price,
        base_fee_per_gas=base_fee,
        priority_fee_per_gas=_priority_fee(effective_gas_price, base_fee),
        max_fee_per_gas=_optional_quantity(transaction, "maxFeePerGas"),
        max_priority_fee_per_gas=_optional_quantity(transaction, "maxPriorityFeePerGas"),
        execution_fee=gas_used * effective_gas_price,
        method_selector=_method_selector(transaction),
        created_contract=_optional_address(receipt, "contractAddress"),
        log_count=len(logs),
        token_transfers=tuple(
            transfer for transfer in (_token_transfer(log) for log in logs) if transfer
        ),
        blob=_blob_gas(transaction, receipt),
        l1_data=_l1_data_fee(receipt),
        arbitrum=_arbitrum_gas(receipt),
    )


def _total_fee(native: EvmTransaction) -> int:
    blob_fee = native.blob.blob_gas_used * native.blob.blob_gas_price if native.blob else 0
    l1_fee = native.l1_data.l1_fee if native.l1_data else 0
    return native.execution_fee + blob_fee + l1_fee


def _status(receipt: JsonObject, final: bool) -> Status:
    if _quantity(receipt, "status") != RECEIPT_SUCCESS:
        return Status.FAILED
    return Status.FINAL if final else Status.PENDING


def _priority_fee(effective_gas_price: int, base_fee: int | None) -> int | None:
    if base_fee is None:
        return None
    return max(effective_gas_price - base_fee, 0)


def _method_selector(transaction: JsonObject) -> str | None:
    if transaction.get("to") is None:
        return None
    data = parse_data(transaction.get("input"), "transaction input")
    if len(data) < SELECTOR_BYTES:
        return None
    return "0x" + data[:SELECTOR_BYTES].hex()


def _blob_gas(transaction: JsonObject, receipt: JsonObject) -> EvmBlobGas | None:
    # OP Stack receipts report a blobGasUsed for every transaction, the data-availability
    # footprint, with no blob gas price; only an EIP-4844 blob fee has both.
    blob_gas_price = _optional_quantity(receipt, "blobGasPrice")
    if blob_gas_price is None:
        return None
    hashes = transaction.get("blobVersionedHashes")
    return EvmBlobGas(
        blob_count=len(hashes) if isinstance(hashes, list) else 0,
        blob_gas_used=_quantity(receipt, "blobGasUsed"),
        blob_gas_price=blob_gas_price,
        max_fee_per_blob_gas=_optional_quantity(transaction, "maxFeePerBlobGas"),
    )


def _l1_data_fee(receipt: JsonObject) -> EvmL1DataFee | None:
    l1_fee = _optional_quantity(receipt, "l1Fee")
    if l1_fee is None:
        return None
    return EvmL1DataFee(
        l1_fee=l1_fee,
        l1_gas_used=_optional_quantity(receipt, "l1GasUsed"),
        l1_gas_price=_optional_quantity(receipt, "l1GasPrice"),
        l1_blob_base_fee=_optional_quantity(receipt, "l1BlobBaseFee"),
        l1_base_fee_scalar=_optional_quantity(receipt, "l1BaseFeeScalar"),
        l1_blob_base_fee_scalar=_optional_quantity(receipt, "l1BlobBaseFeeScalar"),
    )


def _arbitrum_gas(receipt: JsonObject) -> EvmArbitrumGas | None:
    gas_used_for_l1 = _optional_quantity(receipt, "gasUsedForL1")
    if gas_used_for_l1 is None:
        return None
    timeboosted = receipt.get("timeboosted")
    return EvmArbitrumGas(
        gas_used_for_l1=gas_used_for_l1,
        l1_block_number=_quantity(receipt, "l1BlockNumber"),
        timeboosted=timeboosted if isinstance(timeboosted, bool) else None,
    )


def _logs(receipt: JsonObject) -> list[JsonObject]:
    logs = receipt.get("logs")
    if not isinstance(logs, list) or not all(isinstance(log, dict) for log in logs):
        raise MalformedResponse("receipt: expected a list of logs")
    return [log for log in logs if isinstance(log, dict)]


def _token_transfer(log: JsonObject) -> EvmTokenTransfer | None:
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) not in (FUNGIBLE_TOPICS, NFT_TOPICS):
        return None
    if topics[0] != TRANSFER_TOPIC:
        return None
    words = [_topic_bytes(topic) for topic in topics[1:]]
    sender, receiver = _topic_address(words[0]), _topic_address(words[1])
    if sender is None or receiver is None:
        return None
    data = parse_data(log.get("data"), "log data")
    parties = {
        "token": _address(log, "address"),
        "sender": sender,
        "receiver": receiver,
        "log_index": _quantity(log, "logIndex"),
    }
    if len(topics) == FUNGIBLE_TOPICS and len(data) == WORD_BYTES:
        return EvmTokenTransfer(kind="fungible", value=int.from_bytes(data), **parties)
    token_id = words[-1]
    if len(topics) == NFT_TOPICS and not data and token_id is not None:
        return EvmTokenTransfer(kind="nft", token_id=int.from_bytes(token_id), **parties)
    return None


def _topic_bytes(topic: JsonValue) -> bytes | None:
    try:
        word = parse_data(topic, "log topic")
    except MalformedResponse:
        return None
    return word if len(word) == WORD_BYTES else None


def _topic_address(word: bytes | None) -> str | None:
    padding = WORD_BYTES - ADDRESS_BYTES
    if word is None or any(word[:padding]):
        return None
    return to_checksum_address(word[padding:])


def _text(source: JsonObject, field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value:
        raise MalformedResponse(f"expected {field} as text")
    return value


def _quantity(source: JsonObject, field: str) -> int:
    return parse_quantity(source.get(field), field)


def _optional_quantity(source: JsonObject, field: str) -> int | None:
    value = source.get(field)
    return None if value is None else parse_quantity(value, field)


def _address(source: JsonObject, field: str) -> str:
    address = _optional_address(source, field)
    if address is None:
        raise MalformedResponse(f"expected {field} as an address")
    return address


def _optional_address(source: JsonObject, field: str) -> str | None:
    value = source.get(field)
    if value is None:
        return None
    raw = parse_data(value, field)
    if len(raw) != ADDRESS_BYTES:
        raise MalformedResponse(f"expected {field} as a 20-byte address")
    return to_checksum_address(raw)
