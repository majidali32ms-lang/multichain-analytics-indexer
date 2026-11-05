"""Solana's own concepts, carried in the ``native`` block of each record."""

from __future__ import annotations

from pydantic import Field

from multichain_indexer.core.models import NativeData


class SolanaInstruction(NativeData):
    """One program invocation. ``kind`` is set when the node could decode it."""

    program_id: str
    program: str | None = None
    kind: str | None = None
    inner: bool = False


class SolTransfer(NativeData):
    source: str
    destination: str
    lamports: int = Field(ge=0)


class SplTokenTransfer(NativeData):
    """An SPL Token or Token-2022 transfer; ``amount`` is in the token's base units."""

    token_program: str
    mint: str | None = None
    source: str
    destination: str
    authority: str | None = None
    amount: int = Field(ge=0)
    kind: str


class SolanaTransaction(NativeData):
    """A transaction of any version.

    ``priority_fee_lamports`` is in lamports for every version. A v1 transaction states
    the total in ``transactionConfig``; legacy and v0 state a price per compute unit in
    micro-lamports, kept in ``compute_unit_price_micro_lamports``.

    The shared ``amount`` is the first transfer found: in lamports for a SOL transfer,
    and in the token's base units when ``amount_mint`` is set.
    """

    slot: int = Field(ge=0)
    version: str
    kind: str
    confirmation: str
    error: str | None = None
    signature_count: int = Field(ge=1)
    precompile_signature_count: int = Field(default=0, ge=0)
    compute_units_consumed: int | None = None
    compute_unit_limit: int | None = None
    compute_unit_price_micro_lamports: int | None = None
    priority_fee_lamports: int = Field(ge=0)
    heap_size: int | None = None
    loaded_accounts_data_size_limit: int | None = None
    address_lookup_tables: tuple[str, ...] = ()
    amount_mint: str | None = None
    instructions: tuple[SolanaInstruction, ...] = ()
    sol_transfers: tuple[SolTransfer, ...] = ()
    token_transfers: tuple[SplTokenTransfer, ...] = ()


class SplTokenAccount(NativeData):
    address: str
    mint: str
    token_program: str
    amount: int = Field(ge=0)
    decimals: int = Field(ge=0)
    state: str | None = None


class SolanaAccount(NativeData):
    """Account state. ``exists`` is false for an address that has never held lamports."""

    slot: int = Field(ge=0)
    exists: bool
    owner: str | None = None
    executable: bool = False
    space: int | None = None
    token_accounts: tuple[SplTokenAccount, ...] = ()


class SplMint(NativeData):
    """An SPL mint. A mint has no creator on-chain; these authorities are what it records."""

    token_program: str
    mint_authority: str | None = None
    freeze_authority: str | None = None
    is_initialized: bool = True
    extensions: tuple[str, ...] = ()


class SolanaProgram(NativeData):
    """A program. A program has no creator on-chain; the upgrade authority can change it."""

    loader: str
    program_data: str | None = None
    upgrade_authority: str | None = None
    last_deployed_slot: int | None = None
