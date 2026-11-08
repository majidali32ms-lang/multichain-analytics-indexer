"""NEAR behind the ``BaseIndexer`` contract.

Account state and access keys come from NEAR's standard JSON-RPC. Transaction history comes
from a ``HistorySource``, chosen by configuration, because the RPC cannot list it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import cast

from pydantic import JsonValue

from multichain_indexer.adapters.near.client import NearRpc
from multichain_indexer.adapters.near.fastnear import create_fastnear_history
from multichain_indexer.adapters.near.history import HistorySource, HistorySourceFactory
from multichain_indexer.adapters.near.metrics import GAS_PER_TGAS, YOCTO_DECIMALS, near_metrics
from multichain_indexer.adapters.near.models import NearAccount, NearContract, NearTransaction
from multichain_indexer.adapters.near.parser import (
    CHAIN,
    parse_account,
    parse_contract,
    parse_transaction,
    validated_account_id,
)
from multichain_indexer.core.base_indexer import BaseIndexer, ChainProfile
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError, InvalidInput, NotFound, WrongNetwork
from multichain_indexer.core.http import ApiClient, ResponseStore
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    TransactionPage,
    TransactionRecord,
)
from multichain_indexer.core.ratelimit import RateLimiter

LOCALNET = "localnet"
PUBLIC_NETWORKS = frozenset({"mainnet", "testnet"})

# From docs.near.org/api/rpc/providers. A different company from the default history service,
# so account state does not depend on it. It was the only public endpoint measured to draw
# no HTTP 429 and no failed request on both networks; near.org's drew long runs of 429.
DEFAULT_RPC_SERVERS = {
    "mainnet": "https://near.drpc.org",
    "testnet": "https://near-testnet.drpc.org",
    LOCALNET: "http://localhost:3030",
}
# The fastest rate measured. MCI_REQUESTS_PER_SECOND overrides this.
DEFAULT_RPC_REQUESTS_PER_SECOND = 1.0

DEFAULT_HISTORY_SOURCE = "fastnear"
HISTORY_SOURCES: dict[str, HistorySourceFactory] = {"fastnear": create_fastnear_history}

# Receipts run in the blocks after their transaction. Records this close to the head are
# fetched again on the next sync, so one still executing is not left unfinished.
FINALITY_MARGIN_BLOCKS = 300

PROFILE = ChainProfile(
    name=CHAIN,
    native_symbol="NEAR",
    base_unit="yoctoNEAR",
    native_decimals=YOCTO_DECIMALS,
    networks=tuple(DEFAULT_RPC_SERVERS),
    default_network="testnet",
    transaction_model=NearTransaction,
)


class NearIndexer(BaseIndexer):
    profile = PROFILE

    def __init__(self, network: str, rpc: NearRpc, history: HistorySource) -> None:
        super().__init__(network)
        self._rpc = rpc
        self._history = history
        self._network_confirmed = False

    def validate_address(self, address: str) -> str:
        return validated_account_id(address)

    async def fetch_account(self, address: str) -> AccountRecord:
        account = self.validate_address(address)
        await self._require_network()
        state = await self._account_state(account)
        keys = None if state is None else await self._rpc.view_access_key_list(account)
        layout = await self._rpc.shard_layout()
        return parse_account(state, keys, layout, account, self.network)

    async def fetch_asset(self, asset_id: str) -> AssetRecord:
        raise InvalidInput(
            "NEAR has no asset records: a fungible token is a contract, "
            "so look it up with index contract"
        )

    async def fetch_contract(self, contract_id: str) -> ContractRecord:
        account = self.validate_address(contract_id)
        await self._require_network()
        state = await self._account_state(account)
        if state is None:
            raise NotFound(f"account {account} does not exist")
        return parse_contract(state, account, self.network)

    async def chain_head(self) -> int:
        # A sync's checkpoint moves up to this height, so it must not pass what the history
        # service has indexed: transactions in between would never be fetched.
        await self._require_network()
        return min(await self._rpc.final_height(), await self._history.indexed_height())

    def final_height(self, head: int) -> int:
        return max(head - FINALITY_MARGIN_BLOCKS, 0)

    async def iter_account_transactions(
        self,
        address: str,
        *,
        from_height: int | None = None,
        to_height: int | None = None,
        cursor: str | None = None,
    ) -> AsyncIterator[TransactionPage]:
        account = self.validate_address(address)
        await self._require_network()
        immutable_up_to = None if to_height is None else self.final_height(to_height)
        next_cursor = cursor
        while True:
            page = await self._history.account_page(
                account,
                from_height=from_height,
                to_height=to_height,
                cursor=next_cursor,
                immutable_up_to=immutable_up_to,
            )
            if page.transactions:
                records = tuple(parse_transaction(tx, self.network) for tx in page.transactions)
                yield TransactionPage(records=records, next_cursor=page.next_cursor)
            if page.next_cursor is None:
                return
            next_cursor = page.next_cursor

    def chain_metrics(self, records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
        return near_metrics(records)

    def describe(
        self, record: TransactionRecord | AccountRecord | AssetRecord | ContractRecord
    ) -> dict[str, str]:
        if isinstance(record, TransactionRecord):
            return _describe_transaction(record)
        if isinstance(record, AccountRecord):
            return _describe_account(record)
        return _describe_contract(cast(ContractRecord, record))

    @property
    def requests_made(self) -> int:
        return self._rpc.requests_made + self._history.requests_made

    async def aclose(self) -> None:
        await self._rpc.aclose()
        await self._history.aclose()

    async def _account_state(self, account: str) -> JsonValue | None:
        try:
            return await self._rpc.view_account(account)
        except NotFound:
            return None

    async def _require_network(self) -> None:
        """Confirm once that the RPC endpoint serves the requested network.

        Raises:
            WrongNetwork: Its chain ID belongs to another network.
        """
        if self._network_confirmed:
            return
        chain_id = await self._rpc.chain_id()
        served = chain_id if chain_id in PUBLIC_NETWORKS else LOCALNET
        if served != self.network:
            raise WrongNetwork(_wrong_network_message(self.network, chain_id))
        self._network_confirmed = True


def create_indexer(
    network: str | None, settings: Settings, cache: ResponseStore | None = None
) -> NearIndexer:
    """Build the adapter for ``network``, testnet when None."""
    chosen = network or PROFILE.default_network
    if chosen not in PROFILE.networks:
        raise InvalidInput(f"unknown NEAR network {chosen!r}: {', '.join(PROFILE.networks)}")
    client = ApiClient(
        settings.endpoint(CHAIN, chosen, "RPC_SERVER", DEFAULT_RPC_SERVERS[chosen]),
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        limiter=RateLimiter(_rpc_requests_per_second(settings)),
    )
    return NearIndexer(chosen, NearRpc(client), _history_source(chosen, settings, cache))


def _history_source(network: str, settings: Settings, cache: ResponseStore | None) -> HistorySource:
    configured = settings.chain_setting(CHAIN, network, "HISTORY_SOURCE")
    name = configured.get_secret_value() if configured else DEFAULT_HISTORY_SOURCE
    factory = HISTORY_SOURCES.get(name)
    if factory is None:
        setting = f"{CHAIN}_{network}_HISTORY_SOURCE".upper()
        raise ConfigError(f"{setting} must be one of: {', '.join(sorted(HISTORY_SOURCES))}")
    return factory(network, settings, cache)


def _rpc_requests_per_second(settings: Settings) -> float:
    if "requests_per_second" in settings.model_fields_set:
        return settings.requests_per_second
    return DEFAULT_RPC_REQUESTS_PER_SECOND


def _wrong_network_message(network: str, chain_id: str) -> str:
    served = f"NEAR {chain_id}" if chain_id in PUBLIC_NETWORKS else f"another network, {chain_id!r}"
    setting = f"{CHAIN}_{network}_RPC_SERVER".upper()
    return f"the RPC endpoint serves {served}, not NEAR {network}. Check {setting}."


def _tgas(gas: int) -> str:
    return f"{Decimal(gas) / GAS_PER_TGAS:.3f} Tgas"


def _describe_transaction(record: TransactionRecord) -> dict[str, str]:
    native = cast(NearTransaction, record.native)
    return {
        "Type": native.kind,
        "Status": record.status.value,
        "Amount": PROFILE.format_native(record.amount),
        "Fee": PROFILE.format_native(record.fee),
        "Gas burnt": _tgas(native.gas_burnt),
        "Receipts": str(len(native.receipts)),
    }


def _describe_account(record: AccountRecord) -> dict[str, str]:
    native = cast(NearAccount, record.native)
    function_call_keys = sum(1 for key in native.access_keys if key.permission == "FunctionCall")
    return {
        "Account ID": record.address,
        "Kind": native.kind,
        "Exists": "yes" if native.exists else "no, nobody has created it yet",
        "Balance": PROFILE.format_native(record.balance),
        "Staked": PROFILE.format_native(native.locked),
        "Storage used": f"{native.storage_usage or 0} bytes",
        "Contract": "yes" if native.has_contract else "no",
        "Access keys": f"{len(native.access_keys)} ({function_call_keys} function-call)",
        "Shard": "not reported" if native.shard_id is None else str(native.shard_id),
        "As of block": str(native.block_height or "not recorded"),
    }


def _describe_contract(record: ContractRecord) -> dict[str, str]:
    native = cast(NearContract, record.native)
    return {
        "Contract": record.id,
        "Code hash": native.code_hash,
        "Global contract": native.global_contract_hash
        or native.global_contract_account_id
        or "none",
        "Storage used": f"{native.storage_usage} bytes",
        "As of block": str(native.block_height),
    }
