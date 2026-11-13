"""Turn a source's transaction into a record.

A Bitcoin transaction has many inputs and many outputs; ``native`` keeps them all. The
shared fields follow one rule:

- ``sender``: the address of the input spending the most sats (the first on a tie), the
  public key in hex for a P2PK input, or ``coinbase`` for a coinbase transaction. When no
  input has either, as for bare multisig, the first spent output as ``txid:vout``.
- ``receiver`` and ``amount``: the largest output paying an address that no input spends
  from, so change going back to a sender is skipped; when every output pays a sender, the
  largest output with an address. No output with an address leaves ``receiver`` None and
  ``amount`` 0.
- ``fee``: inputs minus outputs, in sats; 0 for a coinbase transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from multichain_indexer.adapters.bitcoin.history import SourceInput, SourceOutput, SourceTransaction
from multichain_indexer.adapters.bitcoin.models import (
    BitcoinInput,
    BitcoinOutput,
    BitcoinTransaction,
)
from multichain_indexer.adapters.bitcoin.scripts import P2TR
from multichain_indexer.core.errors import MalformedResponse
from multichain_indexer.core.models import Status, TransactionRecord

CHAIN = "bitcoin"
COINBASE_SENDER = "coinbase"
WITNESS_SCALE_FACTOR = 4
# BIP-125: an input whose sequence is below this signals that the transaction may be replaced.
MAX_RBF_SEQUENCE = 0xFFFFFFFE
# A transaction is final at this many confirmations; until then it is pending, because a
# reorg can still replace its block.
CONFIRMATION_DEPTH = 6


def parse_transaction(
    source: SourceTransaction, network: str, *, tip_height: int, history_source: str
) -> TransactionRecord:
    """Build the record for ``source`` as seen with the chain at ``tip_height``.

    A transaction in the mempool has no block time, so its time is when it was read.

    Raises:
        MalformedResponse: The fee the source reports differs from inputs minus outputs.
    """
    fee = _fee(source)
    vsize = virtual_size(source.weight)
    confirmations = _confirmations(source, tip_height)
    receiver = _receiver(source)
    return TransactionRecord(
        id=source.txid,
        time=_time(source),
        sender=_sender(source),
        receiver=receiver.address if receiver else None,
        amount=receiver.value if receiver else 0,
        fee=fee,
        status=Status.FINAL if confirmations >= CONFIRMATION_DEPTH else Status.PENDING,
        chain=CHAIN,
        network=network,
        native=BitcoinTransaction(
            version=source.version,
            locktime=source.locktime,
            size=source.size,
            weight=source.weight,
            vsize=vsize,
            fee_rate=fee_rate(fee, source.weight),
            rbf=any(entry.sequence < MAX_RBF_SEQUENCE for entry in source.inputs),
            coinbase=_is_coinbase(source),
            segwit=any(entry.witness_items and not entry.coinbase for entry in source.inputs),
            taproot=any(entry.script_type == P2TR for entry in source.inputs),
            confirmed=source.block is not None,
            block_height=source.block.height if source.block else None,
            block_hash=source.block.hash if source.block else None,
            confirmations=confirmations,
            inputs=tuple(_input(entry) for entry in source.inputs),
            outputs=tuple(_output(entry) for entry in source.outputs),
            history_source=history_source,
        ),
    )


def virtual_size(weight: int) -> int:
    """Return the virtual size in vbytes: the weight over four, rounded up (BIP-141)."""
    return -(-weight // WITNESS_SCALE_FACTOR)


def fee_rate(fee: int, weight: int) -> float:
    """Return sats per virtual byte over the unrounded virtual size, weight over four.

    This is the rate mempool.space shows; dividing by the rounded ``vsize`` gives a
    slightly lower rate whenever the weight is not a multiple of four.
    """
    return fee * WITNESS_SCALE_FACTOR / weight if weight else 0.0


def _fee(source: SourceTransaction) -> int:
    if _is_coinbase(source):
        return 0
    fee = sum(entry.value for entry in source.inputs) - sum(out.value for out in source.outputs)
    if fee < 0 or (source.reported_fee is not None and source.reported_fee != fee):
        raise MalformedResponse(
            f"transaction {source.txid}: inputs minus outputs is {fee} sats, "
            f"but the source reports a fee of {source.reported_fee}"
        )
    return fee


def _is_coinbase(source: SourceTransaction) -> bool:
    return any(entry.coinbase for entry in source.inputs)


def _confirmations(source: SourceTransaction, tip_height: int) -> int:
    if source.block is None:
        return 0
    return max(tip_height, source.block.height) - source.block.height + 1


def _time(source: SourceTransaction) -> datetime:
    if source.block is None:
        return datetime.now(UTC)
    return datetime.fromtimestamp(source.block.time, UTC)


def _sender(source: SourceTransaction) -> str:
    if _is_coinbase(source):
        return COINBASE_SENDER
    addressed = [entry for entry in source.inputs if entry.address]
    if not addressed:
        first = source.inputs[0]
        return f"{first.txid}:{first.vout}"
    largest = max(addressed, key=lambda entry: entry.value)
    return str(largest.address)


def _receiver(source: SourceTransaction) -> SourceOutput | None:
    addressed = [out for out in source.outputs if out.address]
    senders = {entry.address for entry in source.inputs}
    payments = [out for out in addressed if out.address not in senders] or addressed
    return max(payments, key=lambda out: out.value, default=None)


def _input(entry: SourceInput) -> BitcoinInput:
    return BitcoinInput(
        txid=entry.txid,
        vout=entry.vout,
        address=entry.address,
        script_type=entry.script_type,
        value=entry.value,
        sequence=entry.sequence,
        witness=entry.witness_items > 0,
        coinbase=entry.coinbase,
    )


def _output(entry: SourceOutput) -> BitcoinOutput:
    return BitcoinOutput(
        index=entry.index,
        address=entry.address,
        script_type=entry.script_type,
        script_pubkey=entry.script_pubkey,
        value=entry.value,
    )
