"""Solana behind the ``BaseIndexer`` contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import cast

from pydantic import JsonValue
from solders.pubkey import Pubkey

from multichain_indexer.adapters.solana.client import SolanaRpc
from multichain_indexer.adapters.solana.metrics import solana_metrics
from multichain_indexer.adapters.solana.models import (
    SolanaAccount,
    SolanaProgram,
    SolanaTransaction,
    SplMint,
)
from multichain_indexer.adapters.solana.parser import (
    CHAIN,
    FINALIZED,
    TOKEN_PROGRAMS,
    SignatureRow,
    parse_account,
    parse_block,
    parse_mint,
    parse_program,
    parse_signature_rows,
    parse_transaction,
    program_data_address,
)
from multichain_indexer.core.base_indexer import BaseIndexer, ChainProfile
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError, InvalidAddress, InvalidInput
from multichain_indexer.core.http import ApiClient, ResponseStore
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    TransactionPage,
    TransactionRecord,
)
from multichain_indexer.core.ratelimit import RateLimiter

LAMPORT_DECIMALS = 9
CONFIRMED = "confirmed"
DEFAULT_COMMITMENT = CONFIRMED
# getSignaturesForAddress and getTransaction both refuse anything below confirmed.
COMMITMENTS = (CONFIRMED, FINALIZED)

# From solana.com/docs/references/clusters.
DEFAULT_RPC_SERVERS = {
    "mainnet": "https://api.mainnet.solana.com",
    "devnet": "https://api.devnet.solana.com",
    "testnet": "https://api.testnet.solana.com",
    "localnet": "http://localhost:8899",
}
# The cluster docs state 40 requests per 10 seconds for one RPC method, but the public
# endpoints report a limit of 10 for getTransaction and enforce it: measured on devnet,
# 1 request per second still drew HTTP 429, and 1 every 2 seconds drew none, on mainnet as
# well. MCI_REQUESTS_PER_SECOND overrides this.
DEFAULT_REQUESTS_PER_SECOND = 0.5

# Each row costs one getTransaction, and a page is stored atomically, so small pages mean
# little work is lost to an interruption and a short lookup stays short.
SIGNATURES_PER_PAGE = 10

# Finalization normally trails confirmation by about 32 slots. A wider margin costs a few
# repeated lookups; too narrow a one would leave records marked pending forever.
FINALITY_MARGIN_SLOTS = 150

PROFILE = ChainProfile(
    name=CHAIN,
    native_symbol="SOL",
    base_unit="lamports",
    native_decimals=LAMPORT_DECIMALS,
    networks=tuple(DEFAULT_RPC_SERVERS),
    default_network="devnet",
    transaction_model=SolanaTransaction,
)


class SolanaIndexer(BaseIndexer):
    profile = PROFILE

    def __init__(self, network: str, rpc: SolanaRpc, commitment: str = DEFAULT_COMMITMENT) -> None:
        super().__init__(network)
        self._rpc = rpc
        self._commitment = commitment

    def validate_address(self, address: str) -> str:
        return _validated_public_key(address, "Solana address")

    async def fetch_account(self, address: str) -> AccountRecord:
        owner = self.validate_address(address)
        info = await self._rpc.account_info(owner)
        token_accounts = [
            await self._rpc.token_accounts_by_owner(owner, program)
            for program in sorted(TOKEN_PROGRAMS)
        ]
        return parse_account(info, token_accounts, owner, self.network)

    async def fetch_asset(self, asset_id: str) -> AssetRecord:
        mint = _validated_public_key(asset_id, "SPL token mint")
        return parse_mint(await self._rpc.account_info(mint), mint, self.network)

    async def fetch_contract(self, contract_id: str) -> ContractRecord:
        program_id = _validated_public_key(contract_id, "program ID")
        info = await self._rpc.account_info(program_id)
        program_data = program_data_address(info, program_id)
        header = await self._rpc.program_data_header(program_data) if program_data else None
        return parse_program(info, header, program_id, self.network)

    async def fetch_block(self, slot: int) -> list[TransactionRecord]:
        """Return every transaction in the block at ``slot``, of any transaction version.

        Raises:
            InvalidInput: ``slot`` is negative.
            NotFound: The slot was skipped, or the endpoint does not hold its block.
        """
        if slot < 0:
            raise InvalidInput("a slot cannot be negative")
        result = await self._rpc.block(slot)
        return parse_block(result, slot, self.network, self._commitment)

    async def chain_head(self) -> int:
        return await self._rpc.slot()

    def final_height(self, head: int) -> int:
        if self._commitment == FINALIZED:
            return head
        return max(head - FINALITY_MARGIN_SLOTS, 0)

    async def iter_account_transactions(
        self,
        address: str,
        *,
        from_height: int | None = None,
        to_height: int | None = None,
        cursor: str | None = None,
    ) -> AsyncIterator[TransactionPage]:
        account = self.validate_address(address)
        before = cursor
        while True:
            rows = await self._signature_rows(account, before)
            if not rows:
                return
            next_cursor = _next_cursor(rows, from_height)
            records = await self._transactions(_rows_in_range(rows, from_height, to_height))
            if records:
                yield TransactionPage(records=records, next_cursor=next_cursor)
            if next_cursor is None:
                return
            before = next_cursor

    def chain_metrics(self, records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
        return solana_metrics(records)

    def describe(
        self, record: TransactionRecord | AccountRecord | AssetRecord | ContractRecord
    ) -> dict[str, str]:
        if isinstance(record, TransactionRecord):
            return _describe_transaction(record)
        if isinstance(record, AccountRecord):
            return _describe_account(record)
        if isinstance(record, AssetRecord):
            return _describe_mint(record)
        return _describe_program(record)

    @property
    def requests_made(self) -> int:
        return self._rpc.requests_made

    async def aclose(self) -> None:
        await self._rpc.aclose()

    async def _signature_rows(self, account: str, before: str | None) -> list[SignatureRow]:
        result = await self._rpc.signatures_for_address(
            account, limit=SIGNATURES_PER_PAGE, before=before
        )
        return parse_signature_rows(result)

    async def _transactions(self, rows: Sequence[SignatureRow]) -> tuple[TransactionRecord, ...]:
        records = []
        for row in rows:
            confirmation = row.confirmation or self._commitment
            result = await self._rpc.transaction(row.signature, immutable=confirmation == FINALIZED)
            records.append(parse_transaction(result, self.network, confirmation))
        return tuple(records)


def create_indexer(
    network: str | None, settings: Settings, cache: ResponseStore | None = None
) -> SolanaIndexer:
    """Build the adapter for ``network``, devnet when None."""
    chosen = network or PROFILE.default_network
    if chosen not in PROFILE.networks:
        raise InvalidInput(f"unknown Solana cluster {chosen!r}: {', '.join(PROFILE.networks)}")

    commitment = _commitment(settings, chosen)
    client = ApiClient(
        settings.endpoint(CHAIN, chosen, "RPC_SERVER", DEFAULT_RPC_SERVERS[chosen]),
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        limiter=RateLimiter(_requests_per_second(settings)),
        cache=cache,
    )
    return SolanaIndexer(chosen, SolanaRpc(client, commitment), commitment)


def _commitment(settings: Settings, network: str) -> str:
    configured = settings.chain_setting(CHAIN, network, "COMMITMENT")
    commitment = configured.get_secret_value() if configured else DEFAULT_COMMITMENT
    if commitment not in COMMITMENTS:
        setting = f"{CHAIN}_{network}_COMMITMENT".upper()
        raise ConfigError(f"{setting} must be one of: {', '.join(COMMITMENTS)}")
    return commitment


def _requests_per_second(settings: Settings) -> float:
    if "requests_per_second" in settings.model_fields_set:
        return settings.requests_per_second
    return DEFAULT_REQUESTS_PER_SECOND


def _validated_public_key(value: str, label: str) -> str:
    try:
        return str(Pubkey.from_string(value))
    except ValueError:
        raise InvalidAddress(f"not a valid {label}: {value!r}") from None


def _next_cursor(rows: Sequence[SignatureRow], from_height: int | None) -> str | None:
    if len(rows) < SIGNATURES_PER_PAGE:
        return None
    if from_height is not None and rows[-1].slot < from_height:
        return None
    return rows[-1].signature


def _rows_in_range(
    rows: Sequence[SignatureRow], from_height: int | None, to_height: int | None
) -> list[SignatureRow]:
    # getSignaturesForAddress has no slot bounds, so the range is applied to its rows.
    return [
        row
        for row in rows
        if (from_height is None or row.slot >= from_height)
        and (to_height is None or row.slot <= to_height)
    ]


def _describe_transaction(record: TransactionRecord) -> dict[str, str]:
    native = cast(SolanaTransaction, record.native)
    amount = (
        f"{record.amount} of mint {native.amount_mint}"
        if native.amount_mint
        else PROFILE.format_native(record.amount)
    )
    return {
        "Type": native.kind,
        "Status": record.status.value,
        "Amount": amount,
        "Fee": f"{record.fee} lamports",
        "Priority fee": f"{native.priority_fee_lamports} lamports",
    }


def _describe_account(record: AccountRecord) -> dict[str, str]:
    native = cast(SolanaAccount, record.native)
    return {
        "Address": record.address,
        "Balance": PROFILE.format_native(record.balance),
        "Exists": "yes" if native.exists else "no, it has never held lamports",
        "Owner program": native.owner or "none",
        "Executable": "yes" if native.executable else "no",
        "SPL token accounts": str(len(native.token_accounts)),
        "As of slot": str(native.slot),
    }


def _describe_mint(record: AssetRecord) -> dict[str, str]:
    native = cast(SplMint, record.native)
    return {
        "Mint": record.id,
        "Token program": native.token_program,
        "Name": record.name or "not recorded on the mint",
        "Symbol": record.unit or "not recorded on the mint",
        "Decimals": str(record.decimals),
        "Supply": f"{record.total} base units",
        "Mint authority": native.mint_authority or "none",
        "Freeze authority": native.freeze_authority or "none",
    }


def _describe_program(record: ContractRecord) -> dict[str, str]:
    native = cast(SolanaProgram, record.native)
    return {
        "Program ID": record.id,
        "Loader": native.loader,
        "Program data": native.program_data or "none",
        "Upgrade authority": native.upgrade_authority or "none",
        "Last deployed slot": str(native.last_deployed_slot or "not recorded"),
    }
