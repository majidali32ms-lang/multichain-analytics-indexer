"""The EVM chains behind the ``BaseIndexer`` contract: one adapter, one profile per chain.

Every lookup first confirms, once, that the RPC endpoint serves the chain ID of the requested
network, so pointing ``--chain base`` at a BNB Smart Chain endpoint fails before any data is
read. Account history takes its transaction list from a ``HistorySource`` and everything else
from the chain's RPC; the source is built on first use, so lookups need no history API key.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import ClassVar, cast

from pydantic import JsonValue

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.encoding import parse_quantity
from multichain_indexer.adapters.evm.explorer import (
    create_blockscout_history,
    create_etherscan_history,
)
from multichain_indexer.adapters.evm.history import (
    HistorySource,
    HistorySourceFactory,
    SourceContext,
)
from multichain_indexer.adapters.evm.metrics import evm_metrics
from multichain_indexer.adapters.evm.models import EvmAccount, EvmContract, EvmTransaction
from multichain_indexer.adapters.evm.nodereal import create_nodereal_history
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
    RPC_SCAN,
    TESTNET,
    EvmChain,
)
from multichain_indexer.adapters.evm.scan import create_rpc_scan_history
from multichain_indexer.adapters.evm.transactions import RawTransaction, parse_transaction
from multichain_indexer.core.base_indexer import BaseIndexer, ChainProfile
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import (
    ConfigError,
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
HASH_HEX_DIGITS = 64
HEX_CHARACTERS = frozenset("0123456789abcdefABCDEF")

HISTORY_SOURCES: dict[str, HistorySourceFactory] = {
    "blockscout": create_blockscout_history,
    "etherscan": create_etherscan_history,
    "nodereal": create_nodereal_history,
    RPC_SCAN: create_rpc_scan_history,
}

logger = logging.getLogger(__name__)


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

    def __init__(self, network: str, rpc: EvmRpc, settings: Settings) -> None:
        super().__init__(network)
        self._rpc = rpc
        self._settings = settings
        self._history: HistorySource | None = None
        self._history_name: str | None = None
        self._chain_confirmed = False
        self._finalized_height = 0

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

    async def fetch_transaction(self, tx_hash: str) -> TransactionRecord:
        """Return one transaction with its receipt, read from the chain's RPC.

        Raises:
            InvalidInput: ``tx_hash`` is not a 32-byte hex hash.
            NotFound: The endpoint does not know the transaction, or it is not in a block yet.
        """
        _validate_hash(tx_hash)
        await self._require_chain()
        await self._refresh_finalized_height()
        record = await self._transaction_record(tx_hash)
        if record is None:
            raise NotFound(f"transaction {tx_hash} is not in a block on {self._network_title()}")
        return record

    async def chain_head(self) -> int:
        await self._require_chain()
        await self._refresh_finalized_height()
        return await self._rpc.block_number()

    def final_height(self, head: int) -> int:
        # Blocks up to the endpoint's "finalized" block can no longer be replaced.
        return min(head, self._finalized_height)

    async def iter_account_transactions(
        self,
        address: str,
        *,
        from_height: int | None = None,
        to_height: int | None = None,
        cursor: str | None = None,
    ) -> AsyncIterator[TransactionPage]:
        account = self.validate_address(address)
        await self._require_chain()
        history = self._history_source()
        await self._refresh_finalized_height()
        next_cursor = cursor
        while True:
            page = await history.account_page(
                account, from_height=from_height, to_height=to_height, cursor=next_cursor
            )
            records = [
                await self._transaction_record(tx_hash, self._history_name)
                for tx_hash in page.hashes
            ]
            found = tuple(record for record in records if record is not None)
            if found:
                yield TransactionPage(records=found, next_cursor=page.next_cursor)
            if page.next_cursor is None:
                return
            next_cursor = page.next_cursor

    def chain_metrics(self, records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
        return evm_metrics(
            records,
            native_symbol=self.chain.native_symbol,
            token_standard=self.chain.token_standard,
        )

    def describe(
        self, record: TransactionRecord | AccountRecord | AssetRecord | ContractRecord
    ) -> dict[str, str]:
        if isinstance(record, TransactionRecord):
            return self._describe_transaction(record)
        if isinstance(record, AccountRecord):
            return self._describe_account(record)
        return _describe_contract(cast(ContractRecord, record))

    @property
    def requests_made(self) -> int:
        history = self._history.requests_made if self._history else 0
        return self._rpc.requests_made + history

    async def aclose(self) -> None:
        await self._rpc.aclose()
        if self._history is not None:
            await self._history.aclose()

    def _history_source(self) -> HistorySource:
        if self._history is None:
            self._history = self._build_history_source()
        return self._history

    def _build_history_source(self) -> HistorySource:
        setting = f"{self.chain.name}_{self.network}_HISTORY_SOURCE".upper()
        configured = self._settings.chain_setting(self.chain.name, self.network, "HISTORY_SOURCE")
        name = configured.get_secret_value() if configured else self._default_history_source()
        choices = ", ".join(sorted(HISTORY_SOURCES))
        if name is None:
            raise UnsupportedChain(
                f"{self._network_title()} has no free account history service; its "
                f"transactions can still be read by hash. {setting} chooses one of: {choices}"
            )
        factory = HISTORY_SOURCES.get(name)
        if factory is None:
            raise ConfigError(f"{setting} must be one of: {choices}")
        source = factory(SourceContext(self.chain, self.network, self._settings, self._rpc))
        self._history_name = name
        return source

    def _default_history_source(self) -> str | None:
        if self.network == LOCALNET:
            return RPC_SCAN
        return self.chain.networks[self.network].history_source

    async def _refresh_finalized_height(self) -> None:
        self._finalized_height = await self._rpc.finalized_block_number()

    async def _transaction_record(
        self, tx_hash: str, history_source: str | None = None
    ) -> TransactionRecord | None:
        receipt = await self._rpc.receipt(tx_hash)
        transaction = await self._rpc.transaction(tx_hash) if receipt else None
        if receipt is None or transaction is None:
            _log_missing(tx_hash)
            return None
        block_number = _block_number(receipt)
        final = block_number <= self._finalized_height
        block = await self._rpc.block_header(block_number, immutable=final)
        return parse_transaction(
            RawTransaction(transaction, receipt, block),
            chain=self.chain.name,
            network=self.network,
            final=final,
            history_source=history_source,
        )

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

    def _describe_transaction(self, record: TransactionRecord) -> dict[str, str]:
        native = cast(EvmTransaction, record.native)
        fields = {
            "Type": native.kind,
            "Status": record.status.value,
            "Amount": self.profile.format_native(record.amount),
            "Fee": self.profile.format_native(record.fee),
            "Gas used": str(native.gas_used),
        }
        if native.l1_data:
            fields["L1 data fee"] = self.profile.format_native(native.l1_data.l1_fee)
        if native.blob:
            fields["Blobs"] = str(native.blob.blob_count)
        if native.token_transfers:
            fields["Token transfers"] = self._token_transfer_summary(native)
        return fields

    def _token_transfer_summary(self, native: EvmTransaction) -> str:
        fungible = sum(1 for transfer in native.token_transfers if transfer.kind == "fungible")
        nft = len(native.token_transfers) - fungible
        parts = [f"{fungible} {self.chain.token_standard}"] if fungible else []
        return ", ".join([*parts, f"{nft} NFT"] if nft else parts)

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


def _log_missing(tx_hash: str) -> None:
    # The history service can be ahead of the RPC, or list a block that was replaced. Neither
    # block is final, and a sync never moves its checkpoint past the final height, so the
    # transaction is looked up again on the next sync.
    logger.warning(
        "history lists a transaction the RPC does not have yet", extra={"transaction": tx_hash}
    )


def _validate_hash(tx_hash: str) -> None:
    digits = tx_hash.removeprefix("0x")
    if not tx_hash.startswith("0x") or len(digits) != HASH_HEX_DIGITS:
        raise InvalidInput(f"not a transaction hash: {tx_hash!r}")
    if any(digit not in HEX_CHARACTERS for digit in digits):
        raise InvalidInput(f"not a transaction hash: {tx_hash!r}")


def _block_number(receipt: dict[str, JsonValue]) -> int:
    return parse_quantity(receipt.get("blockNumber"), "eth_getTransactionReceipt")


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
        return adapter(chosen, EvmRpc(client), settings)

    return create_indexer
