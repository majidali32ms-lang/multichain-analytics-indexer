"""Turns Solana JSON-RPC results into records.

The ``_Raw*`` models mirror the documented JSON, so a result that breaks the format
fails here instead of further down as a wrong number. Transactions are requested with
``jsonParsed`` encoding, where the node decodes system and SPL token instructions itself.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

import base58
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel
from solders.pubkey import Pubkey

from multichain_indexer.adapters.solana.models import (
    SolanaAccount,
    SolanaInstruction,
    SolanaProgram,
    SolanaTransaction,
    SolTransfer,
    SplMint,
    SplTokenAccount,
    SplTokenTransfer,
)
from multichain_indexer.core.errors import InvalidInput, MalformedResponse
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    Status,
    TransactionRecord,
)

CHAIN = "solana"
FINALIZED = "finalized"

LAMPORTS_PER_SIGNATURE = 5000
MICRO_LAMPORTS_PER_LAMPORT = 1_000_000
# A transaction may ask for more, but is charged for at most this many compute units.
MAX_COMPUTE_UNIT_LIMIT = 1_400_000

SYSTEM_PROGRAM = "11111111111111111111111111111111"
VOTE_PROGRAM = "Vote111111111111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
TOKEN_PROGRAMS = frozenset(
    {
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    }
)
# Each signature these programs verify adds to the base fee, though it is not a signer.
PRECOMPILE_PROGRAMS = frozenset(
    {
        "Ed25519SigVerify111111111111111111111111111",
        "KeccakSecp256k11111111111111111111111111111",
        "Secp256r1SigVerify1111111111111111111111111",
    }
)

SET_COMPUTE_UNIT_LIMIT = 2
SET_COMPUTE_UNIT_PRICE = 3
COMPUTE_UNIT_LIMIT_BYTES = 4
COMPUTE_UNIT_PRICE_BYTES = 8

SOL_TRANSFER_TYPES = frozenset({"transfer", "transferWithSeed"})
TOKEN_TRANSFER_TYPES = frozenset({"transfer", "transferChecked", "transferCheckedWithFee"})

KIND_SOL_TRANSFER = "sol-transfer"
KIND_SPL_TRANSFER = "spl-transfer"
KIND_VOTE = "vote"
KIND_NO_INSTRUCTIONS = "no-instructions"

# UpgradeableLoaderState::ProgramData: a 4-byte tag, the slot as a u64, then an optional
# authority as a 1-byte flag and a 32-byte key.
PROGRAM_DATA_SLOT_OFFSET = 4
PROGRAM_DATA_AUTHORITY_FLAG_OFFSET = 12
PROGRAM_DATA_AUTHORITY_OFFSET = 13
PROGRAM_DATA_HEADER_BYTES = 45
PUBLIC_KEY_BYTES = 32

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _Raw(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _RawInstruction(_Raw):
    program_id: str
    program: str | None = None
    parsed: object = None
    data: str | None = None


class _RawInnerInstructions(_Raw):
    instructions: list[_RawInstruction] = []


class _RawAccountKey(_Raw):
    pubkey: str


class _RawTransactionConfig(_Raw):
    compute_unit_limit: int | None = None
    priority_fee: int | None = None
    heap_size: int | None = None
    loaded_accounts_data_size_limit: int | None = None


class _RawLookup(_Raw):
    account_key: str


class _RawMessage(_Raw):
    account_keys: list[_RawAccountKey]
    instructions: list[_RawInstruction] = []
    transaction_config: _RawTransactionConfig | None = None
    address_table_lookups: list[_RawLookup] | None = None


class _RawEnvelope(_Raw):
    signatures: list[str]
    message: _RawMessage


class _RawTokenBalance(_Raw):
    account_index: int
    mint: str


class _RawMeta(_Raw):
    err: object = None
    fee: int
    compute_units_consumed: int | None = None
    inner_instructions: list[_RawInnerInstructions] | None = None
    pre_token_balances: list[_RawTokenBalance] | None = None
    post_token_balances: list[_RawTokenBalance] | None = None


class _RawTransaction(_Raw):
    slot: int | None = None
    block_time: int | None = None
    version: int | str = "legacy"
    meta: _RawMeta
    transaction: _RawEnvelope


class _RawBlock(_Raw):
    block_time: int | None = None
    transactions: list[_RawTransaction] = []


class _RawSignatureRow(_Raw):
    signature: str
    slot: int
    err: object = None
    confirmation_status: str | None = None


class _RawSolTransferInfo(_Raw):
    source: str
    destination: str
    lamports: int


class _RawParsed(_Raw):
    type: str
    info: dict[str, object]


class _RawTokenAmount(_Raw):
    amount: str
    decimals: int | None = None


class _RawTokenTransferInfo(_Raw):
    source: str
    destination: str
    mint: str | None = None
    authority: str | None = None
    multisig_authority: str | None = None
    amount: str | None = None
    token_amount: _RawTokenAmount | None = None


class _RawAccountValue(_Raw):
    lamports: int
    owner: str
    executable: bool = False
    space: int | None = None
    data: object = None


class _RawContext(_Raw):
    slot: int


class _RawAccountInfo(_Raw):
    context: _RawContext
    value: _RawAccountValue | None = None


class _RawParsedAccountData(_Raw):
    program: str
    parsed: _RawParsed


class _RawExtension(_Raw):
    extension: str
    state: dict[str, object] | None = None


class _RawMintInfo(_Raw):
    decimals: int
    supply: str
    mint_authority: str | None = None
    freeze_authority: str | None = None
    is_initialized: bool = True
    extensions: list[_RawExtension] = []


class _RawProgramInfo(_Raw):
    program_data: str


class _RawTokenAccountInfo(_Raw):
    mint: str
    state: str | None = None
    token_amount: _RawTokenAmount


class _RawKeyedAccountValue(_Raw):
    pubkey: str
    account: _RawAccountValue


class _RawKeyedAccounts(_Raw):
    value: list[_RawKeyedAccountValue] = []


@dataclass(frozen=True)
class SignatureRow:
    """One row of an account's history, before its transaction has been fetched."""

    signature: str
    slot: int
    confirmation: str | None


@dataclass(frozen=True)
class ComputeBudget:
    limit: int | None
    price_micro_lamports: int | None


def parse_signature_rows(result: object) -> list[SignatureRow]:
    """Normalise a ``getSignaturesForAddress`` result.

    Raises:
        MalformedResponse: The result does not match the documented format.
    """
    if not isinstance(result, list):
        raise MalformedResponse("getSignaturesForAddress: expected a list")
    rows = [_validate(_RawSignatureRow, row, "signature row") for row in result]
    return [SignatureRow(row.signature, row.slot, row.confirmation_status) for row in rows]


def parse_transaction(result: object, network: str, confirmation: str) -> TransactionRecord:
    """Normalise a ``getTransaction`` result of any transaction version.

    Raises:
        MalformedResponse: The result does not match the documented format.
    """
    raw = _validate(_RawTransaction, result, "transaction")
    if raw.slot is None:
        raise MalformedResponse("transaction: the result has no slot")
    return _record(raw, raw.slot, raw.block_time, network, confirmation)


def parse_block(
    result: object, slot: int, network: str, confirmation: str
) -> list[TransactionRecord]:
    """Normalise a ``getBlock`` result, which may hold every transaction version.

    Raises:
        MalformedResponse: The result does not match the documented format.
    """
    raw = _validate(_RawBlock, result, "block")
    return [_record(tx, slot, raw.block_time, network, confirmation) for tx in raw.transactions]


def parse_account(
    info: object, token_accounts: Sequence[object], address: str, network: str
) -> AccountRecord:
    """Normalise ``getAccountInfo`` and ``getTokenAccountsByOwner`` results."""
    raw = _validate(_RawAccountInfo, info, "account")
    value = raw.value
    return AccountRecord(
        address=address,
        balance=value.lamports if value else 0,
        chain=CHAIN,
        network=network,
        native=SolanaAccount(
            slot=raw.context.slot,
            exists=value is not None,
            owner=value.owner if value else None,
            executable=value.executable if value else False,
            space=value.space if value else None,
            token_accounts=tuple(
                account for result in token_accounts for account in _token_accounts(result)
            ),
        ),
    )


def parse_mint(info: object, mint: str, network: str) -> AssetRecord:
    """Normalise the ``getAccountInfo`` result of an SPL Token or Token-2022 mint.

    Raises:
        InvalidInput: The account exists but is not a mint.
    """
    value = _validate(_RawAccountInfo, info, "mint").value
    if value is None:
        raise InvalidInput(f"no account found at {mint}")
    parsed = _parsed_data(value)
    if value.owner not in TOKEN_PROGRAMS or parsed is None or parsed.parsed.type != "mint":
        raise InvalidInput(f"{mint} is not an SPL token mint")
    details = _validate(_RawMintInfo, parsed.parsed.info, "mint")
    metadata = _token_metadata(details)
    return AssetRecord(
        id=mint,
        name=metadata.get("name"),
        unit=metadata.get("symbol"),
        decimals=details.decimals,
        total=int(details.supply),
        chain=CHAIN,
        network=network,
        native=SplMint(
            token_program=value.owner,
            mint_authority=details.mint_authority,
            freeze_authority=details.freeze_authority,
            is_initialized=details.is_initialized,
            extensions=tuple(extension.extension for extension in details.extensions),
        ),
    )


def program_data_address(info: object, program_id: str) -> str | None:
    """Return where an upgradeable program keeps its data; None for any other program.

    Raises:
        InvalidInput: The account does not exist or is not executable.
    """
    value = _program_account(info, program_id)
    parsed = _parsed_data(value)
    if parsed is None or parsed.parsed.type != "program":
        return None
    return _validate(_RawProgramInfo, parsed.parsed.info, "program").program_data


def parse_program(
    info: object, program_data_info: object | None, program_id: str, network: str
) -> ContractRecord:
    """Normalise a program account and, if upgradeable, its program data header."""
    value = _program_account(info, program_id)
    native = SolanaProgram(loader=value.owner)
    if program_data_info is not None:
        native = _with_program_data(
            value.owner, program_data_address(info, program_id), program_data_info
        )
    return ContractRecord(id=program_id, chain=CHAIN, network=network, native=native)


def compute_budget_of(instructions: Sequence[_RawInstruction]) -> ComputeBudget:
    """Read the limit and price from Compute Budget instructions of a legacy or v0 message."""
    limit = price = None
    for instruction in instructions:
        if instruction.program_id != COMPUTE_BUDGET_PROGRAM:
            continue
        data = _decoded_data(instruction)
        if data[:1] == bytes([SET_COMPUTE_UNIT_LIMIT]):
            limit = int.from_bytes(data[1 : 1 + COMPUTE_UNIT_LIMIT_BYTES], "little")
        elif data[:1] == bytes([SET_COMPUTE_UNIT_PRICE]):
            price = int.from_bytes(data[1 : 1 + COMPUTE_UNIT_PRICE_BYTES], "little")
    return ComputeBudget(limit, price)


def precompile_signature_count(instructions: Sequence[_RawInstruction]) -> int:
    """Count the signatures verified by precompile programs; the first data byte states it."""
    count = 0
    for instruction in instructions:
        if instruction.program_id in PRECOMPILE_PROGRAMS:
            count += _decoded_data(instruction)[:1][0]
    return count


def _record(
    raw: _RawTransaction,
    slot: int,
    block_time: int | None,
    network: str,
    confirmation: str,
) -> TransactionRecord:
    if block_time is None:
        raise MalformedResponse(f"slot {slot}: the node did not report a block time")
    envelope = raw.transaction
    if not envelope.signatures or not envelope.message.account_keys:
        raise MalformedResponse(f"slot {slot}: transaction without signatures or account keys")
    native, receiver, amount = _native(raw, slot, confirmation)
    return TransactionRecord(
        id=envelope.signatures[0],
        time=datetime.fromtimestamp(block_time, tz=UTC),
        sender=envelope.message.account_keys[0].pubkey,
        receiver=receiver,
        amount=amount,
        fee=raw.meta.fee,
        status=_status(raw.meta.err, confirmation),
        chain=CHAIN,
        network=network,
        native=native,
    )


def _status(error: object, confirmation: str) -> Status:
    if confirmation != FINALIZED:
        return Status.PENDING
    return Status.FAILED if error is not None else Status.FINAL


def _native(
    raw: _RawTransaction, slot: int, confirmation: str
) -> tuple[SolanaTransaction, str | None, int]:
    message = raw.transaction.message
    balances = _mints_by_account(raw)
    top_level = message.instructions
    invoked = [*top_level, *_inner_instructions(raw.meta)]
    sol_transfers = tuple(_sol_transfers(invoked))
    token_transfers = tuple(_token_transfers(invoked, balances))
    receiver, amount, amount_mint = _headline_transfer(sol_transfers, token_transfers)
    config = message.transaction_config or _RawTransactionConfig()
    budget = compute_budget_of(top_level)
    signature_count = len(raw.transaction.signatures)
    precompile_count = precompile_signature_count(top_level)
    native = SolanaTransaction(
        slot=slot,
        version=str(raw.version),
        kind=_kind(top_level, sol_transfers, token_transfers),
        confirmation=confirmation,
        error=None if raw.meta.err is None else json.dumps(raw.meta.err, sort_keys=True),
        signature_count=signature_count,
        precompile_signature_count=precompile_count,
        compute_units_consumed=raw.meta.compute_units_consumed,
        compute_unit_limit=_compute_unit_limit(raw, budget),
        compute_unit_price_micro_lamports=budget.price_micro_lamports,
        priority_fee_lamports=_priority_fee(raw, budget, signature_count + precompile_count),
        heap_size=config.heap_size,
        loaded_accounts_data_size_limit=config.loaded_accounts_data_size_limit,
        address_lookup_tables=tuple(
            lookup.account_key for lookup in message.address_table_lookups or []
        ),
        amount_mint=amount_mint,
        instructions=tuple(_instructions(top_level, raw.meta)),
        sol_transfers=sol_transfers,
        token_transfers=token_transfers,
    )
    return native, receiver, amount


def _compute_unit_limit(raw: _RawTransaction, budget: ComputeBudget) -> int | None:
    config = raw.transaction.message.transaction_config
    return config.compute_unit_limit if config else budget.limit


def _priority_fee(raw: _RawTransaction, budget: ComputeBudget, signatures_paid_for: int) -> int:
    """Return the priority fee in lamports, whichever way the transaction states it.

    A v1 transaction states the total. Legacy and v0 state a price per compute unit, and
    the total is the price times the capped limit, rounded up. A price with no explicit limit
    is charged against a default the node works out from the instructions, so there the
    total is read back from what was charged.
    """
    config = raw.transaction.message.transaction_config
    if config is not None:
        return config.priority_fee or 0
    if budget.price_micro_lamports is None:
        return 0
    if budget.limit is None:
        return max(raw.meta.fee - LAMPORTS_PER_SIGNATURE * signatures_paid_for, 0)
    charged_units = min(budget.limit, MAX_COMPUTE_UNIT_LIMIT)
    return math.ceil(budget.price_micro_lamports * charged_units / MICRO_LAMPORTS_PER_LAMPORT)


def _inner_instructions(meta: _RawMeta) -> Iterator[_RawInstruction]:
    for group in meta.inner_instructions or []:
        yield from group.instructions


def _instructions(
    top_level: Sequence[_RawInstruction], meta: _RawMeta
) -> Iterator[SolanaInstruction]:
    for instruction in top_level:
        yield _instruction(instruction, inner=False)
    for instruction in _inner_instructions(meta):
        yield _instruction(instruction, inner=True)


def _instruction(instruction: _RawInstruction, *, inner: bool) -> SolanaInstruction:
    parsed = _parsed_instruction(instruction)
    return SolanaInstruction(
        program_id=instruction.program_id,
        program=instruction.program,
        kind=parsed.type if parsed else None,
        inner=inner,
    )


def _parsed_instruction(instruction: _RawInstruction) -> _RawParsed | None:
    # The memo program's parsed form is a plain string, and undecoded instructions have none.
    if not isinstance(instruction.parsed, dict):
        return None
    return _validate(_RawParsed, instruction.parsed, "instruction")


def _sol_transfers(instructions: Sequence[_RawInstruction]) -> Iterator[SolTransfer]:
    for instruction in instructions:
        if instruction.program_id != SYSTEM_PROGRAM:
            continue
        parsed = _parsed_instruction(instruction)
        if parsed is None or parsed.type not in SOL_TRANSFER_TYPES:
            continue
        info = _validate(_RawSolTransferInfo, parsed.info, "SOL transfer")
        yield SolTransfer(source=info.source, destination=info.destination, lamports=info.lamports)


def _token_transfers(
    instructions: Sequence[_RawInstruction], mints: Mapping[str, str]
) -> Iterator[SplTokenTransfer]:
    for instruction in instructions:
        if instruction.program_id not in TOKEN_PROGRAMS:
            continue
        parsed = _parsed_instruction(instruction)
        if parsed is None or parsed.type not in TOKEN_TRANSFER_TYPES:
            continue
        yield _token_transfer(instruction.program_id, parsed, mints)


def _token_transfer(
    token_program: str, parsed: _RawParsed, mints: Mapping[str, str]
) -> SplTokenTransfer:
    info = _validate(_RawTokenTransferInfo, parsed.info, "SPL transfer")
    amount = info.token_amount.amount if info.token_amount else info.amount
    if amount is None:
        raise MalformedResponse("SPL transfer: the instruction states no amount")
    return SplTokenTransfer(
        token_program=token_program,
        # A plain transfer names only token accounts; the balance lists say which mint.
        mint=info.mint or mints.get(info.source) or mints.get(info.destination),
        source=info.source,
        destination=info.destination,
        authority=info.authority or info.multisig_authority,
        amount=int(amount),
        kind=parsed.type,
    )


def _mints_by_account(raw: _RawTransaction) -> dict[str, str]:
    keys = raw.transaction.message.account_keys
    balances = [*(raw.meta.pre_token_balances or []), *(raw.meta.post_token_balances or [])]
    return {
        keys[balance.account_index].pubkey: balance.mint
        for balance in balances
        if balance.account_index < len(keys)
    }


def _headline_transfer(
    sol_transfers: Sequence[SolTransfer], token_transfers: Sequence[SplTokenTransfer]
) -> tuple[str | None, int, str | None]:
    if sol_transfers:
        return sol_transfers[0].destination, sol_transfers[0].lamports, None
    if token_transfers:
        transfer = token_transfers[0]
        return transfer.destination, transfer.amount, transfer.mint
    return None, 0, None


def _kind(
    top_level: Sequence[_RawInstruction],
    sol_transfers: Sequence[SolTransfer],
    token_transfers: Sequence[SplTokenTransfer],
) -> str:
    programs = [i.program_id for i in top_level if i.program_id != COMPUTE_BUDGET_PROGRAM]
    if not programs:
        return KIND_NO_INSTRUCTIONS
    if VOTE_PROGRAM in programs:
        return KIND_VOTE
    if programs[0] == SYSTEM_PROGRAM and sol_transfers:
        return KIND_SOL_TRANSFER
    if programs[0] in TOKEN_PROGRAMS and token_transfers:
        return KIND_SPL_TRANSFER
    return programs[0]


def _token_accounts(result: object) -> Iterator[SplTokenAccount]:
    for keyed in _validate(_RawKeyedAccounts, result, "token accounts").value:
        parsed = _parsed_data(keyed.account)
        if parsed is None:
            raise MalformedResponse("token accounts: the node did not decode the account")
        info = _validate(_RawTokenAccountInfo, parsed.parsed.info, "token account")
        yield SplTokenAccount(
            address=keyed.pubkey,
            mint=info.mint,
            token_program=keyed.account.owner,
            amount=int(info.token_amount.amount),
            decimals=info.token_amount.decimals or 0,
            state=info.state,
        )


def _parsed_data(value: _RawAccountValue) -> _RawParsedAccountData | None:
    # An account the node cannot decode has its data as a list of encoded text instead.
    if not isinstance(value.data, dict):
        return None
    return _validate(_RawParsedAccountData, value.data, "account data")


def _token_metadata(mint: _RawMintInfo) -> dict[str, str]:
    for extension in mint.extensions:
        if extension.extension == "tokenMetadata" and extension.state:
            return {k: v for k, v in extension.state.items() if isinstance(v, str)}
    return {}


def _program_account(info: object, program_id: str) -> _RawAccountValue:
    value = _validate(_RawAccountInfo, info, "program").value
    if value is None:
        raise InvalidInput(f"no account found at {program_id}")
    if not value.executable:
        raise InvalidInput(f"{program_id} is not an executable program")
    return value


def _with_program_data(
    loader: str, program_data: str | None, program_data_info: object
) -> SolanaProgram:
    value = _validate(_RawAccountInfo, program_data_info, "program data").value
    if value is None or not isinstance(value.data, list) or not value.data:
        raise MalformedResponse("program data: the account has no data")
    header = _program_data_header(value.data[0])
    last_deployed_slot = int.from_bytes(
        header[PROGRAM_DATA_SLOT_OFFSET:PROGRAM_DATA_AUTHORITY_FLAG_OFFSET], "little"
    )
    authority = None
    if header[PROGRAM_DATA_AUTHORITY_FLAG_OFFSET]:
        key = header[PROGRAM_DATA_AUTHORITY_OFFSET:PROGRAM_DATA_HEADER_BYTES]
        authority = str(Pubkey.from_bytes(key))
    return SolanaProgram(
        loader=loader,
        program_data=program_data,
        upgrade_authority=authority,
        last_deployed_slot=last_deployed_slot,
    )


def _program_data_header(encoded: object) -> bytes:
    try:
        header = base64.b64decode(str(encoded), validate=True)
    except (binascii.Error, ValueError):
        raise MalformedResponse("program data: the data is not base64") from None
    if len(header) < PROGRAM_DATA_HEADER_BYTES:
        raise MalformedResponse("program data: the header is too short")
    return header


def _decoded_data(instruction: _RawInstruction) -> bytes:
    try:
        data = base58.b58decode(instruction.data or "")
    except ValueError:
        raise MalformedResponse("instruction: the data is not base58") from None
    if not data:
        raise MalformedResponse(f"instruction of {instruction.program_id} has no data")
    return data


def _validate(model: type[_ModelT], data: object, what: str) -> _ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise MalformedResponse(f"{what}: unexpected fields: {', '.join(fields)}") from None
