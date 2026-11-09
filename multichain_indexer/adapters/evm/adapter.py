"""The EVM chains behind the ``BaseIndexer`` contract: one adapter, one profile per chain.

Every lookup first confirms, once, that the RPC endpoint serves the chain ID of the requested
network, so pointing ``--chain base`` at a BNB Smart Chain endpoint fails before any data is
read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import ClassVar, cast

from pydantic import JsonValue

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.models import EvmAccount, EvmContract, EvmTransaction
from multichain_indexer.adapters.evm.parser import (
    is_contract_code,
    parse_account,
    parse_contract,
    validated_address,
)
from multichain_indexer.adapters.evm.profiles import (
    LOCALNET,
    LOCALNET_RPC_SERVER,
    LOCALNET_TITLE,
    PUBLIC_NETWORKS_BY_CHAIN_ID,
    TESTNET,
    EvmChain,
)
from multichain_indexer.core.base_indexer import BaseIndexer, ChainProfile
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import (
    InvalidInput,
    NotFound,
    UnsupportedChain,
    WrongNetwork,
)
from multichain_indexer.core.http import ApiClient, ResponseStore
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    TransactionPage,
    TransactionRecord,
)
from multichain_indexer.core.ratelimit import RateLimiter
from multichain_indexer.core.registry import IndexerFactory

WEI_DECIMALS = 18


def chain_profile(chain: EvmChain) -> ChainProfile:
    return ChainProfile(
        name=chain.name,
        native_symbol=chain.native_symbol,
        base_unit="wei",
        native_decimals=WEI_DECIMALS,
        networks=chain.network_names,
        default_network=TESTNET,
        transaction_model=EvmTransaction,
    )


class EvmIndexer(BaseIndexer):
    """One network of one EVM chain. Each chain has a subclass, from ``indexer_class``."""

    chain: ClassVar[EvmChain]

    def __init__(self, network: str, rpc: EvmRpc) -> None:
        super().__init__(network)
        self._rpc = rpc
        self._chain_confirmed = False

    def validate_address(self, address: str) -> str:
        return validated_address(address)

    async def fetch_account(self, address: str) -> AccountRecord:
        account = self.validate_address(address)
        await self._require_chain()
        block = await self._rpc.block_number()
        return parse_account(
            address=account,
            balance=await self._rpc.balance(account, block),
            nonce=await self._rpc.transaction_count(account, block),
            code=await self._rpc.code(account, block),
            block_number=block,
            chain=self.chain.name,
            network=self.network,
        )

    async def fetch_asset(self, asset_id: str) -> AssetRecord:
        raise UnsupportedChain(f"{self.chain.token_standard} token lookups are not available yet")

    async def fetch_contract(self, contract_id: str) -> ContractRecord:
        address = self.validate_address(contract_id)
        await self._require_chain()
        block = await self._rpc.block_number()
        code = await self._rpc.code(address, block)
        if not is_contract_code(code):
            raise NotFound(f"no contract is deployed at {address} on {self._network_title()}")
        return parse_contract(
            address=address,
            code=code,
            block_number=block,
            chain=self.chain.name,
            network=self.network,
        )

    async def chain_head(self) -> int:
        await self._require_chain()
        return await self._rpc.block_number()

    def iter_account_transactions(
        self,
        address: str,
        *,
        from_height: int | None = None,
        to_height: int | None = None,
        cursor: str | None = None,
    ) -> AsyncIterator[TransactionPage]:
        self.validate_address(address)
        raise UnsupportedChain(f"{self.chain.title} account history is not available yet")

    def chain_metrics(self, records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
        return {}

    def describe(
        self, record: TransactionRecord | AccountRecord | AssetRecord | ContractRecord
    ) -> dict[str, str]:
        if isinstance(record, AccountRecord):
            return self._describe_account(record)
        return _describe_contract(cast(ContractRecord, record))

    @property
    def requests_made(self) -> int:
        return self._rpc.requests_made

    async def aclose(self) -> None:
        await self._rpc.aclose()

    async def _require_chain(self) -> None:
        """Confirm once that the RPC endpoint serves the requested network's chain ID.

        Raises:
            WrongNetwork: The endpoint's chain ID belongs to another network.
        """
        if self._chain_confirmed:
            return
        chain_id = await self._rpc.chain_id()
        if not self._serves_requested_network(chain_id):
            raise WrongNetwork(self._wrong_network_message(chain_id))
        self._chain_confirmed = True

    def _serves_requested_network(self, chain_id: int) -> bool:
        if self.network == LOCALNET:
            return chain_id not in PUBLIC_NETWORKS_BY_CHAIN_ID
        return chain_id == self.chain.networks[self.network].chain_id

    def _wrong_network_message(self, chain_id: int) -> str:
        known = PUBLIC_NETWORKS_BY_CHAIN_ID.get(chain_id)
        served = known.describe() if known else f"an unknown network, chain ID {chain_id}"
        expected = (
            LOCALNET_TITLE
            if self.network == LOCALNET
            else self.chain.networks[self.network].describe()
        )
        setting = f"{self.chain.name}_{self.network}_RPC_SERVER".upper()
        return f"the RPC endpoint serves {served}, not {expected}. Check {setting}."

    def _network_title(self) -> str:
        if self.network == LOCALNET:
            return LOCALNET_TITLE
        return self.chain.networks[self.network].title

    def _describe_account(self, record: AccountRecord) -> dict[str, str]:
        native = cast(EvmAccount, record.native)
        if native.is_contract:
            kind = "contract"
        elif native.delegated_to:
            kind = f"externally owned account, delegated to {native.delegated_to} (EIP-7702)"
        else:
            kind = "externally owned account"
        return {
            "Address": record.address,
            "Kind": kind,
            "Balance": self.profile.format_native(record.balance),
            "Nonce": str(native.nonce),
            "As of block": str(native.block_number),
        }


def _describe_contract(record: ContractRecord) -> dict[str, str]:
    native = cast(EvmContract, record.native)
    return {
        "Contract": record.id,
        "Code size": f"{native.code_size} bytes",
        "Code hash": native.code_hash,
        "As of block": str(native.block_number),
    }


def indexer_class(chain: EvmChain) -> type[EvmIndexer]:
    """Return the adapter class for ``chain``, with its profile."""
    attributes = {"chain": chain, "profile": chain_profile(chain)}
    return type(f"{chain.name.capitalize()}Indexer", (EvmIndexer,), attributes)


def indexer_factory(chain: EvmChain) -> IndexerFactory:
    """Return the registry factory that builds ``chain``'s adapter, testnet by default."""
    adapter = indexer_class(chain)

    def create_indexer(
        network: str | None, settings: Settings, cache: ResponseStore | None = None
    ) -> EvmIndexer:
        chosen = network or TESTNET
        if chosen not in chain.network_names:
            raise InvalidInput(
                f"unknown {chain.title} network {chosen!r}: {', '.join(chain.network_names)}"
            )
        default_server = (
            LOCALNET_RPC_SERVER if chosen == LOCALNET else chain.networks[chosen].rpc_server
        )
        client = ApiClient(
            settings.endpoint(chain.name, chosen, "RPC_SERVER", default_server),
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
            limiter=RateLimiter(settings.requests_per_second),
            cache=cache,
        )
        return adapter(chosen, EvmRpc(client))

    return create_indexer
