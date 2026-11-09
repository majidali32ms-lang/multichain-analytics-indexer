"""EVM concepts, carried in the ``native`` block of each record."""

from __future__ import annotations

from pydantic import Field

from multichain_indexer.core.models import NativeData


class EvmAccount(NativeData):
    """An account's state at ``block_number``.

    ``nonce`` counts the transactions an externally owned account has sent, or the contracts
    a contract has created. ``delegated_to`` is set when an externally owned account has
    delegated its code to a contract under EIP-7702.
    """

    nonce: int = Field(ge=0)
    is_contract: bool
    delegated_to: str | None = None
    block_number: int = Field(ge=0)


class EvmContract(NativeData):
    """Deployed bytecode at ``block_number``; ``code_hash`` is its Keccak-256 hash."""

    code_size: int = Field(gt=0)
    code_hash: str
    block_number: int = Field(ge=0)


class EvmTransaction(NativeData):
    """A transaction and its receipt. ``kind`` is the transaction type, such as ``eip-1559``."""

    kind: str
    block_number: int = Field(ge=0)
    nonce: int = Field(ge=0)
    gas_used: int = Field(ge=0)
    effective_gas_price: int = Field(ge=0)
