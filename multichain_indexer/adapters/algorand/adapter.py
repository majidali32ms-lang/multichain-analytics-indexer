"""Algorand behind the ``BaseIndexer`` contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import cast

from algosdk import constants as algosdk_constants
from algosdk import encoding as algosdk_encoding
from pydantic import JsonValue, SecretStr

from multichain_indexer.adapters.algorand.client import IndexerApi
from multichain_indexer.adapters.algorand.metrics import algorand_metrics
from multichain_indexer.adapters.algorand.models import (
    AlgorandAccount,
    AlgorandApplication,
    AlgorandAsset,
    AlgorandTransaction,
)
from multichain_indexer.adapters.algorand.parser import (
    CHAIN,
    parse_account,
    parse_application,
    parse_asset,
    parse_transaction,
)
from multichain_indexer.core.base_indexer import BaseIndexer, ChainProfile
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import InvalidAddress, InvalidInput
from multichain_indexer.core.http import ApiClient, ResponseStore
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    TransactionRecord,
)
from multichain_indexer.core.ratelimit import RateLimiter

MICROALGO_DECIMALS = 6
MAX_UINT64 = 2**64 - 1

# Nodely's free tier, per nodely.io/docs/free/start; no token needed.
DEFAULT_INDEXER_SERVERS = {
    "mainnet": "https://mainnet-idx.4160.nodely.dev",
    "testnet": "https://testnet-idx.4160.nodely.dev",
    "localnet": "http://localhost:8980",
}
# AlgoKit LocalNet's fixed development token; it protects nothing outside this machine.
LOCALNET_INDEXER_TOKEN = "a" * 64

PROFILE = ChainProfile(
    name=CHAIN,
    native_symbol="ALGO",
    base_unit="microAlgos",
    native_decimals=MICROALGO_DECIMALS,
    networks=tuple(DEFAULT_INDEXER_SERVERS),
    default_network="testnet",
    transaction_model=AlgorandTransaction,
)


class AlgorandIndexer(BaseIndexer):
    profile = PROFILE

    def __init__(self, network: str, api: IndexerApi) -> None:
        super().__init__(network)
        self._api = api

    def validate_address(self, address: str) -> str:
        if not algosdk_encoding.is_valid_address(address):  # type: ignore[no-untyped-call]
            raise InvalidAddress(f"not a valid Algorand address: {address!r}")
        return address

    async def fetch_account(self, address: str) -> AccountRecord:
        return parse_account(await self._api.account(self.validate_address(address)))

    async def fetch_asset(self, asset_id: str) -> AssetRecord:
        return parse_asset(await self._api.asset(_parse_id(asset_id, "ASA ID")))

    async def fetch_contract(self, contract_id: str) -> ContractRecord:
        return parse_application(await self._api.application(_parse_id(contract_id, "app ID")))

    async def chain_head(self) -> int:
        return await self._api.latest_round()

    async def iter_account_transactions(
        self, address: str, *, from_height: int | None = None, to_height: int | None = None
    ) -> AsyncIterator[list[TransactionRecord]]:
        pages = self._api.account_transaction_pages(
            self.validate_address(address), min_round=from_height, max_round=to_height
        )
        async for page in pages:
            yield [parse_transaction(item, self.network) for item in page]

    def chain_metrics(self, records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
        return algorand_metrics(records)

    def describe(
        self, record: TransactionRecord | AccountRecord | AssetRecord | ContractRecord
    ) -> dict[str, str]:
        if isinstance(record, TransactionRecord):
            return _describe_transaction(record)
        if isinstance(record, AccountRecord):
            return _describe_account(record)
        if isinstance(record, AssetRecord):
            return _describe_asset(record)
        return _describe_application(record)

    @property
    def requests_made(self) -> int:
        return self._api.requests_made

    async def aclose(self) -> None:
        await self._api.aclose()


def create_indexer(
    network: str | None, settings: Settings, cache: ResponseStore | None = None
) -> AlgorandIndexer:
    """Build the adapter for ``network``, the test network when None."""
    chosen = network or PROFILE.default_network
    if chosen not in PROFILE.networks:
        raise InvalidInput(f"unknown Algorand network {chosen!r}: {', '.join(PROFILE.networks)}")

    server = settings.endpoint(CHAIN, chosen, "INDEXER_SERVER", DEFAULT_INDEXER_SERVERS[chosen])
    client = ApiClient(
        server,
        headers=_auth_headers(settings, chosen),
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        limiter=RateLimiter(settings.requests_per_second),
        cache=cache,
    )
    return AlgorandIndexer(chosen, IndexerApi(client))


def _auth_headers(settings: Settings, network: str) -> dict[str, SecretStr]:
    token = settings.chain_setting(CHAIN, network, "INDEXER_TOKEN")
    if token is None and network == "localnet":
        token = SecretStr(LOCALNET_INDEXER_TOKEN)
    return {algosdk_constants.indexer_auth_header: token} if token else {}


def _parse_id(value: str, label: str) -> int:
    if not value.isdecimal() or not 0 < int(value) <= MAX_UINT64:
        raise InvalidInput(f"{label} must be a positive 64-bit integer, got {value!r}")
    return int(value)


def _describe_transaction(record: TransactionRecord) -> dict[str, str]:
    native = cast(AlgorandTransaction, record.native)
    is_asa = native.asa_id is not None
    return {
        "Type": native.asa_action.value if native.asa_action else native.tx_type,
        "Amount": f"{record.amount} of ASA {native.asa_id}"
        if is_asa
        else PROFILE.format_native(record.amount),
        "Fee": PROFILE.format_native(record.fee),
    }


def _describe_account(record: AccountRecord) -> dict[str, str]:
    native = cast(AlgorandAccount, record.native)
    return {
        "Address": record.address,
        "Balance": PROFILE.format_native(record.balance),
        "Min balance": PROFILE.format_native(native.min_balance),
        "Participation": native.participation_status,
        "Rekeyed to": native.auth_addr or "not rekeyed",
        "ASAs held": str(len(native.holdings)),
        "Apps opted in": str(native.apps_opted_in),
        "ASAs created": str(native.created_asas),
        "Apps created": str(native.created_apps),
        "As of round": str(native.round),
    }


def _describe_asset(record: AssetRecord) -> dict[str, str]:
    native = cast(AlgorandAsset, record.native)
    return {
        "ASA ID": record.id,
        "Name": record.name or "unnamed",
        "Unit": record.unit or "none",
        "Decimals": str(record.decimals),
        "Total": f"{record.total} base units",
        "Creator": record.creator,
        "Manager": native.manager or "none",
        "Clawback": native.clawback or "none",
        "Freeze": native.freeze or "none",
        "Deleted": "yes" if native.deleted else "no",
    }


def _describe_application(record: ContractRecord) -> dict[str, str]:
    native = cast(AlgorandApplication, record.native)
    return {
        "App ID": record.id,
        "Creator": record.creator,
        "Created at round": str(native.created_at_round),
        "Global state": f"{native.global_uints} uints, {native.global_byte_slices} byte slices",
        "Local state": f"{native.local_uints} uints, {native.local_byte_slices} byte slices",
        "Deleted": "yes" if native.deleted else "no",
    }
