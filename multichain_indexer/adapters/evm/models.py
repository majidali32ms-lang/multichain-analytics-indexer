"""EVM concepts, carried in the ``native`` block of each record."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from multichain_indexer.core.models import NativeData


class EvmAccount(NativeData):
    """An account's state at ``block_number``.

    ``nonce`` counts the transactions an externally owned account has sent, or the contracts
    a contract has created. ``delegated_to`` is set when an externally owned account has
    delegated its code to a contract under EIP-7702. ``key_exposure`` says whether the
    account's public key is on chain as of that block (``exposure.py`` has the rule).
    """

    nonce: int = Field(ge=0)
    is_contract: bool
    delegated_to: str | None = None
    block_number: int = Field(ge=0)
    # None in snapshots stored before version 0.8.0.
    key_exposure: str | None = None


class EvmContract(NativeData):
    """Deployed bytecode at ``block_number``; ``code_hash`` is its Keccak-256 hash."""

    code_size: int = Field(gt=0)
    code_hash: str
    block_number: int = Field(ge=0)


class EvmToken(NativeData):
    """A fungible token contract, read at ``block_number``. ``standard`` is the chain's name
    for it, ERC-20 or BEP-20; ``decimals_declared`` is false when the contract has no
    ``decimals()``, and its amounts are then whole units. ``cas20_variant`` is set for a BNB
    Smart Chain CAS20 token (BEP-702), a token the chain runs natively: ``asset``,
    ``stablecoin`` or ``variant N``."""

    standard: str
    decimals_declared: bool
    block_number: int = Field(ge=0)
    cas20_variant: str | None = None


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


class EvmBnbLabels(NativeData):
    """BNB Smart Chain's labels for a transaction: ``validator`` produced its block (the
    coinbase); ``system_transaction`` is true for the block's own system calls, which Parlia
    adds at no gas price; ``system_contract`` names the system contract it calls, if any."""

    validator: str
    system_transaction: bool
    system_contract: str | None = None


class EvmTransaction(NativeData):
    """A transaction and its receipt. Gas prices are in wei per gas.

    ``kind`` names the transaction type (``legacy``, ``eip-2930``, ``eip-1559``,
    ``eip-4844``, ``eip-7702``, ``deposit``), or ``type N`` for one this version does not
    name. ``execution_fee`` is ``gas_used * effective_gas_price``; the record's ``fee`` adds
    the blob fee and the L1 data fee where the chain charges them. ``history_source`` names
    the service that listed the transaction for an account's history, such as ``etherscan``;
    it is None for a transaction read by hash. ``bnb`` carries BNB Smart Chain's own labels.
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
    bnb: EvmBnbLabels | None = None
    history_source: str | None = None


class EvmFinality(BaseModel):
    """How far an EVM network's finalized block trails its newest block, as one endpoint saw it.

    ``source`` names where the finalized block came from: ``eth_getFinalizedHeader`` (BNB Smart
    Chain's Economic Finality API) or the ``finalized`` block tag. The finalized block is read
    first and the newest block after it, so the lag is an upper bound. An endpoint behind a load
    balancer can report a finalized block above the newest one; it is then shown at the newest
    block and ``finalized_above_head`` says so. Times come from the headers' ``milliTimestamp``
    where the chain has one, else from whole seconds.
    """

    model_config = ConfigDict(frozen=True)

    chain: str
    network: str
    source: str
    head_block: int = Field(ge=0)
    head_time: datetime
    finalized_block: int = Field(ge=0)
    finalized_time: datetime
    lag_blocks: int = Field(ge=0)
    lag_milliseconds: int = Field(ge=0)
    finalized_above_head: bool
    observed_at: datetime


class EvmHolder(BaseModel):
    """One address's token balance at the end of a holder report's range, in base units."""

    model_config = ConfigDict(frozen=True)

    address: str
    balance: int = Field(ge=0)
    share_of_supply: float | None = Field(default=None, ge=0)


class EvmTokenHolders(BaseModel):
    """Holder figures for one token over a block range, from its ``Transfer`` logs.

    Only addresses that sent or received the token in the range are counted; a holder that
    moved nothing in the range is not seen. Balances are read with ``balanceOf`` at
    ``to_block``; the balance before the range is that minus the range's net change, so
    ``new_holders`` (zero before, positive after) and ``exited_holders`` need no older state.
    ``inconsistent_addresses`` counts addresses whose balance does not follow from their
    transfers (fee-on-transfer or rebasing tokens). ``top_holders`` are the ten largest of
    these balances, and ``top_share_of_supply`` their part of ``total_supply``: a lower bound
    of the token's true top-ten share. ``transfers_per_day`` uses UTC days of block time.
    ``nodereal_holder_count`` is NodeReal's own full-chain count, set only when asked for,
    with the compute units it cost.
    """

    model_config = ConfigDict(frozen=True)

    chain: str
    network: str
    token: str
    standard: str
    from_block: int = Field(ge=0)
    to_block: int = Field(ge=0)
    from_time: datetime
    to_time: datetime
    transfers: int = Field(ge=0)
    transfers_per_day: dict[str, int]
    active_addresses: int = Field(ge=0)
    holders_at_end: int = Field(ge=0)
    new_holders: int = Field(ge=0)
    exited_holders: int = Field(ge=0)
    inconsistent_addresses: int = Field(ge=0)
    total_supply: int = Field(ge=0)
    top_holders: tuple[EvmHolder, ...]
    top_share_of_supply: float | None = Field(default=None, ge=0)
    nodereal_holder_count: int | None = Field(default=None, ge=0)
    nodereal_compute_units: int | None = Field(default=None, ge=0)
    observed_at: datetime


class EvmValidator(BaseModel):
    """A BNB Smart Chain validator: the consensus address that signs its blocks (a block's
    coinbase), its operator address in StakeHub, and the name and website it registered."""

    model_config = ConfigDict(frozen=True)

    consensus_address: str
    operator_address: str | None = None
    moniker: str | None = None
    website: str | None = None


class EvmValidatorSet(BaseModel):
    """BNB Smart Chain's current validators, read at ``block_number`` from BSCValidatorSet's
    ``getValidators()`` and StakeHub's ``consensusToOperator`` and ``getValidatorDescription``."""

    model_config = ConfigDict(frozen=True)

    chain: str
    network: str
    block_number: int = Field(ge=0)
    observed_at: datetime
    validators: tuple[EvmValidator, ...]


class EvmExposure(BaseModel):
    """One account's public key status, read at ``block_number``."""

    model_config = ConfigDict(frozen=True)

    address: str
    status: str
    block_number: int = Field(ge=0)


class EvmExposureReport(BaseModel):
    """Public key status of a list of accounts, read at query time (``from_block`` to
    ``to_block``). ``revealed_share`` is the part of the accounts that can sign (all but
    contracts) whose key is revealed; ``note`` says why it is a lower bound."""

    model_config = ConfigDict(frozen=True)

    chain: str
    network: str
    from_block: int = Field(ge=0)
    to_block: int = Field(ge=0)
    observed_at: datetime
    accounts: tuple[EvmExposure, ...]
    counts: dict[str, int]
    revealed_share: float | None = Field(default=None, ge=0, le=1)
    note: str
