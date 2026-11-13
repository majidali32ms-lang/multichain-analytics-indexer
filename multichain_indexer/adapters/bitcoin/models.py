"""Bitcoin's own concepts, carried in the ``native`` block of each record. Amounts in sats."""

from __future__ import annotations

from pydantic import Field

from multichain_indexer.core.models import NativeData


class BitcoinInput(NativeData):
    """An input and the output it spends; a coinbase input spends none.

    ``address`` is the public key in hex when the spent output is P2PK.
    """

    txid: str | None
    vout: int | None = Field(ge=0)
    address: str | None
    script_type: str | None
    value: int = Field(ge=0)
    sequence: int = Field(ge=0)
    witness: bool
    coinbase: bool


class BitcoinOutput(NativeData):
    """An output. ``address`` is the public key in hex for P2PK, None for OP_RETURN."""

    index: int = Field(ge=0)
    address: str | None
    script_type: str
    script_pubkey: str
    value: int = Field(ge=0)


class BitcoinTransaction(NativeData):
    """A transaction in the UTXO model.

    ``vsize`` is the weight divided by four, rounded up (BIP-141). ``fee_rate`` is the fee
    in sats per virtual byte over the weight divided by four, not rounded, as mempool.space
    shows it. ``confirmations`` counts the block holding it as the first, and
    is 0 in the mempool; it is the count when the record was read. ``rbf`` means an input
    signals replaceability under BIP-125. ``segwit`` means an input other than a coinbase
    carries witness data, and ``taproot`` that an input spends a P2TR output.
    """

    version: int
    locktime: int = Field(ge=0)
    size: int = Field(ge=0)
    weight: int = Field(ge=0)
    vsize: int = Field(ge=0)
    fee_rate: float = Field(ge=0)
    rbf: bool
    coinbase: bool
    segwit: bool
    taproot: bool
    confirmed: bool
    block_height: int | None = Field(default=None, ge=0)
    block_hash: str | None = None
    confirmations: int = Field(ge=0)
    inputs: tuple[BitcoinInput, ...]
    outputs: tuple[BitcoinOutput, ...]
    history_source: str


class BitcoinAddress(NativeData):
    """An address or P2PK public key: what it holds and whether its key is on chain.

    ``utxo_count`` and the record's balance cover confirmed outputs; the mempool's effect
    is in ``unconfirmed_change`` and ``mempool_transactions``. ``spent_outputs`` above zero
    means a spend has revealed the key or script behind the address.
    """

    address_type: str
    script_hash: str
    transactions: int = Field(ge=0)
    utxo_count: int = Field(ge=0)
    received_outputs: int = Field(ge=0)
    spent_outputs: int = Field(ge=0)
    unconfirmed_change: int
    mempool_transactions: int = Field(ge=0)
    history_source: str
