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


class EvmToken(NativeData):
    """A fungible token contract, read at ``block_number``. ``standard`` is the chain's name
    for it, ERC-20 or BEP-20; ``decimals_declared`` is false when the contract has no
    ``decimals()``, and its amounts are then whole units."""

    standard: str
    decimals_declared: bool
    block_number: int = Field(ge=0)


class EvmTokenTransfer(NativeData):
    """A ``Transfer`` event of a token contract, from the transaction's logs.

    ``kind`` is ``fungible`` (ERC-20, or BEP-20 on BNB Smart Chain), with ``value`` in the
    token's base units, or ``nft`` (ERC-721), with ``token_id``.
    """

    kind: str
    token: str
    sender: str
    receiver: str
    value: int | None = Field(default=None, ge=0)
    token_id: int | None = Field(default=None, ge=0)
    log_index: int = Field(ge=0)


class EvmBlobGas(NativeData):
    """EIP-4844 blob data paid for by a type 3 transaction, in wei per blob gas."""

    blob_count: int = Field(ge=0)
    blob_gas_used: int = Field(ge=0)
    blob_gas_price: int = Field(ge=0)
    max_fee_per_blob_gas: int | None = Field(default=None, ge=0)


class EvmL1DataFee(NativeData):
    """The L1 data fee an OP Stack style receipt reports (OP Mainnet, Base), in wei."""

    l1_fee: int = Field(ge=0)
    l1_gas_used: int | None = Field(default=None, ge=0)
    l1_gas_price: int | None = Field(default=None, ge=0)
    l1_blob_base_fee: int | None = Field(default=None, ge=0)
    l1_base_fee_scalar: int | None = Field(default=None, ge=0)
    l1_blob_base_fee_scalar: int | None = Field(default=None, ge=0)


class EvmArbitrumGas(NativeData):
    """Arbitrum's receipt fields. ``gas_used_for_l1`` is the part of ``gas_used`` that paid
    for parent chain data, in child chain gas."""

    gas_used_for_l1: int = Field(ge=0)
    l1_block_number: int = Field(ge=0)
    timeboosted: bool | None = None


class EvmTransaction(NativeData):
    """A transaction and its receipt. Gas prices are in wei per gas.

    ``kind`` names the transaction type (``legacy``, ``eip-2930``, ``eip-1559``,
    ``eip-4844``, ``eip-7702``, ``deposit``), or ``type N`` for one this version does not
    name. ``execution_fee`` is ``gas_used * effective_gas_price``; the record's ``fee`` adds
    the blob fee and the L1 data fee where the chain charges them. ``history_source`` names
    the service that listed the transaction for an account's history, such as ``etherscan``;
    it is None for a transaction read by hash.
    """

    kind: str
    block_number: int = Field(ge=0)
    block_hash: str
    transaction_index: int = Field(ge=0)
    nonce: int = Field(ge=0)
    gas_limit: int = Field(ge=0)
    gas_used: int = Field(ge=0)
    effective_gas_price: int = Field(ge=0)
    base_fee_per_gas: int | None = Field(default=None, ge=0)
    priority_fee_per_gas: int | None = Field(default=None, ge=0)
    max_fee_per_gas: int | None = Field(default=None, ge=0)
    max_priority_fee_per_gas: int | None = Field(default=None, ge=0)
    execution_fee: int = Field(ge=0)
    method_selector: str | None = None
    created_contract: str | None = None
    log_count: int = Field(ge=0)
    token_transfers: tuple[EvmTokenTransfer, ...] = ()
    blob: EvmBlobGas | None = None
    l1_data: EvmL1DataFee | None = None
    arbitrum: EvmArbitrumGas | None = None
    history_source: str | None = None
