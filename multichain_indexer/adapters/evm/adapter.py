"""The EVM chains behind the ``BaseIndexer`` contract: one adapter, one profile per chain.

Every lookup first confirms, once, that the RPC endpoint serves the chain ID of the requested
network, so pointing ``--chain base`` at a BNB Smart Chain endpoint fails before any data is
read. Account history takes its transaction list from a ``HistorySource`` and everything else
from the chain's RPC; the source is built on first use, so lookups need no history API key.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import ClassVar, cast

from pydantic import JsonValue

from multichain_indexer.adapters.evm.alchemy import create_alchemy_history
from multichain_indexer.adapters.evm.bsc import cas20_variant, read_validators
from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.encoding import parse_quantity
from multichain_indexer.adapters.evm.explorer import (
    create_blockscout_history,
    create_etherscan_history,
)
from multichain_indexer.adapters.evm.exposure import exposure_report
from multichain_indexer.adapters.evm.history import (
    HISTORY_SCOPES,
    SCOPE_DIRECT,
    HistorySource,
    HistorySourceFactory,
    SourceContext,
)
from multichain_indexer.adapters.evm.holders import HolderQuery, HolderSources, token_holders
from multichain_indexer.adapters.evm.logs import (
    NODEREAL_RPC_SERVERS,
    PUBLIC_LOGS_REFUSED,
    logs_rpc,
    nodereal_rpc,
)
from multichain_indexer.adapters.evm.metrics import evm_metrics
from multichain_indexer.adapters.evm.models import (
    EvmAccount,
    EvmContract,
    EvmExposureReport,
    EvmFinality,
    EvmToken,
    EvmTokenHolders,
    EvmTransaction,
    EvmValidatorSet,
)
from multichain_indexer.adapters.evm.nodereal import (
    HOLDER_COUNT_COMPUTE_UNITS,
    create_nodereal_history,
    nodereal_holder_count,
)
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
from multichain_indexer.adapters.evm.tokens import read_token
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
from multichain_indexer.core.http import ApiClient, JsonObject, ResponseStore
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    Status,
    TransactionPage,
    TransactionRecord,
)
from multichain_indexer.core.ratelimit import host_scope, shared_limiter
from multichain_indexer.core.registry import IndexerFactory

WEI_DECIMALS = 18
# Measured on 2026-11-11 against all twelve default endpoints, 30 requests each: none refused
# at 5 a second; at 10, Sepolia, Arbitrum Sepolia and Base Sepolia refused some.
# MCI_REQUESTS_PER_SECOND overrides it.
DEFAULT_RPC_REQUESTS_PER_SECOND = 5.0
HASH_HEX_DIGITS = 64
MILLISECONDS_PER_SECOND = 1000
# eth_getFinalizedHeader's -2: the highest block at least two thirds of the validators have
# built on, or the fast-finalized block if that is higher (docs.bnbchain.org, bsc-api-list).
TWO_THIRDS_OF_VALIDATORS = -2
ECONOMIC_FINALITY_SOURCE = "eth_getFinalizedHeader"
FINALIZED_TAG_SOURCE = "finalized tag"
BNB_CHAIN_NAME = "bnb"
MAX_EXPOSURE_ADDRESSES = 500
# Two requests an account at 5 a second: about 10 seconds, 22 blocks on BNB Smart Chain and 40 on
# opBNB, well inside the 100 blocks of state public endpoints keep.
EXPOSURE_ADDRESSES_PER_BLOCK = 25
HEX_CHARACTERS = frozenset("0123456789abcdefABCDEF")

HISTORY_SOURCES: dict[str, HistorySourceFactory] = {
    "alchemy": create_alchemy_history,
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
        self._extra_requests = 0

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
        """Return an ERC-20 (BEP-20 on BNB Smart Chain) token by its contract address.

        Raises:
            NotFound: No contract is deployed at the address.
            InvalidInput: The contract is not a token.
        """
        address = self.validate_address(asset_id)
        await self._require_chain()
        block = await self._rpc.block_number()
        code = await self._rpc.code(address, block)
        if not is_contract_code(code):
            raise NotFound(f"no contract is deployed at {address} on {self._network_title()}")

        async def call(data: bytes) -> bytes | None:
            return await self._rpc.call(address, data, block)

        record = await read_token(
            call,
            address=address,
            block_number=block,
            standard=self.chain.token_standard,
            chain=self.chain.name,
            network=self.network,
        )
        if self.chain.name != BNB_CHAIN_NAME:
            return record
        native = cast(EvmToken, record.native)
        variant = cas20_variant(address, code)
        return record.model_copy(
            update={"native": native.model_copy(update={"cas20_variant": variant})}
        )

    async def fetch_token_holders(
        self,
        token: str,
        *,
        from_block: int,
        to_block: int | None = None,
        nodereal_count: bool = False,
    ) -> EvmTokenHolders:
        """Return holder figures for a token over ``from_block`` to ``to_block``.

        Balances are read at ``to_block``. Public endpoints keep only recent state, so with
        ``NODEREAL_API_KEY`` set, BNB Smart Chain and opBNB read state from NodeReal, which
        keeps all of it, and ``to_block`` defaults to the newest finalized block; without it
        ``to_block`` defaults to the newest block. ``nodereal_count`` adds NodeReal's own
        full-chain holder count (free key, 50 compute units).

        Raises:
            InvalidInput: The range is outside the chain, too many addresses moved the token,
                or the endpoint no longer keeps the state of ``to_block``.
            ConfigError: The logs or the count need ``NODEREAL_API_KEY``.
        """
        address = self.validate_address(token)
        await self._require_chain()
        head = await self.chain_head()
        sources = self._holder_sources()
        try:
            last = self._holder_range_end(to_block, head, sources.state is not self._rpc)
            query = HolderQuery(
                chain=self.chain.name,
                network=self.network,
                token=address,
                standard=self.chain.token_standard,
                from_block=from_block,
                to_block=last,
            )
            report = await token_holders(sources, query)
        finally:
            await self._close_extra(sources)
        if not nodereal_count:
            return report
        count = await nodereal_holder_count(self._settings, self.chain.name, self.network, address)
        self._extra_requests += 1
        return report.model_copy(
            update={
                "nodereal_holder_count": count,
                "nodereal_compute_units": HOLDER_COUNT_COMPUTE_UNITS,
            }
        )

    def _holder_sources(self) -> HolderSources:
        # NodeReal keeps all state, so with its key it reads balances at any block; on BNB Smart
        # Chain it also serves the logs the public endpoints refuse.
        nodereal = nodereal_rpc(self.chain, self.network, self._settings)
        refused = (self.chain.name, self.network) in PUBLIC_LOGS_REFUSED
        if nodereal is not None and refused:
            logs = nodereal
        else:
            logs = logs_rpc(self.chain, self.network, self._settings, self._rpc, "Transfer logs")
        return HolderSources(
            blocks=self._rpc,
            state=nodereal or self._rpc,
            logs=logs,
            state_hint=self._state_hint(),
        )

    def _holder_range_end(self, to_block: int | None, head: int, archive_state: bool) -> int:
        """Return the report's last block; without archive state, the newest block, whose
        state every endpoint still keeps.

        Raises:
            InvalidInput: ``to_block`` is past the newest block.
        """
        if to_block is None:
            return self.final_height(head) if archive_state else head
        if to_block > head:
            raise InvalidInput(f"--to-block {to_block} is past the newest block, {head}")
        return to_block

    def _state_hint(self) -> str:
        if (self.chain.name, self.network) in NODEREAL_RPC_SERVERS:
            return (
                "set NODEREAL_API_KEY (free; NodeReal keeps all state), or end the range "
                "within about 100 blocks of the newest block"
            )
        return "end the range within the endpoint's recent blocks, or use an archive endpoint"

    async def _close_extra(self, sources: HolderSources) -> None:
        for extra in {id(rpc): rpc for rpc in (sources.state, sources.logs)}.values():
            if extra is not self._rpc:
                self._extra_requests += extra.requests_made
                await extra.aclose()

    async def fetch_exposure(self, addresses: Sequence[str]) -> EvmExposureReport:
        """Return whether each account has revealed its public key, read now.

        Public endpoints keep state for about the newest 100 blocks, so the newest block is
        read again every ``EXPOSURE_ADDRESSES_PER_BLOCK`` accounts; a revealed key stays
        revealed, so accounts read at different blocks still add up.

        Raises:
            InvalidInput: No address, more than ``MAX_EXPOSURE_ADDRESSES``, or a bad address.
        """
        accounts = [self.validate_address(address) for address in addresses]
        if not 0 < len(accounts) <= MAX_EXPOSURE_ADDRESSES:
            raise InvalidInput(f"give between 1 and {MAX_EXPOSURE_ADDRESSES} addresses")
        await self._require_chain()
        states: list[EvmAccount] = []
        block = 0
        for position, address in enumerate(accounts):
            if position % EXPOSURE_ADDRESSES_PER_BLOCK == 0:
                block = await self._rpc.block_number()
            states.append(await self._account_state(address, block))
        return exposure_report(
            chain=self.chain.name, network=self.network, accounts=states, addresses=accounts
        )

    async def _account_state(self, address: str, block: int) -> EvmAccount:
        record = parse_account(
            address=address,
            balance=0,
            nonce=await self._rpc.transaction_count(address, block),
            code=await self._rpc.code(address, block),
            block_number=block,
            chain=self.chain.name,
            network=self.network,
        )
        return cast(EvmAccount, record.native)

    async def fetch_validators(self) -> EvmValidatorSet:
        """Return BNB Smart Chain's current validators with their registered names.

        Raises:
            UnsupportedChain: The chain is not BNB Smart Chain.
        """
        if self.chain.name != BNB_CHAIN_NAME:
            raise UnsupportedChain(f"{self.chain.title} has no BNB Smart Chain validator set")
        await self._require_chain()
        block = await self._rpc.block_number()
        return EvmValidatorSet(
            chain=self.chain.name,
            network=self.network,
            block_number=block,
            observed_at=datetime.now(UTC),
            validators=await read_validators(self._rpc, block),
        )

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

    async def fetch_finality(self) -> EvmFinality:
        """Return how far the finalized block trails the newest block, read now.

        BNB Smart Chain asks ``eth_getFinalizedHeader`` first and falls back to the
        ``finalized`` tag when the endpoint does not serve it; other chains use the tag.
        """
        await self._require_chain()
        finalized, source = await self._finalized_header()
        head = await self._rpc.latest_header()
        head_block = _header_number(head)
        reported = _header_number(finalized)
        shown = finalized if reported <= head_block else head
        head_ms, finalized_ms = _header_milliseconds(head), _header_milliseconds(shown)
        return EvmFinality(
            chain=self.chain.name,
            network=self.network,
            source=source,
            head_block=head_block,
            head_time=_time_of(head_ms),
            finalized_block=min(reported, head_block),
            finalized_time=_time_of(finalized_ms),
            lag_blocks=max(head_block - reported, 0),
            lag_milliseconds=max(head_ms - finalized_ms, 0),
            finalized_above_head=reported > head_block,
            observed_at=datetime.now(UTC),
        )

    def final_height(self, head: int) -> int:
        # Blocks up to the endpoint's "finalized" block can no longer be replaced, and a
        # checkpoint must not pass what the history service has indexed: transactions above
        # it would never be listed.
        indexed = self._history.indexed_up_to if self._history else None
        return min(head, self._finalized_height, head if indexed is None else indexed)

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
        # A transaction with several token transfers can be listed on two pages of the "all"
        # scope; it is read once.
        listed: set[str] = set()
        while True:
            page = await history.account_page(
                account, from_height=from_height, to_height=to_height, cursor=next_cursor
            )
            new = [tx_hash for tx_hash in page.hashes if tx_hash not in listed]
            listed.update(new)
            found = tuple(
                [
                    record
                    for tx_hash in new
                    if (record := await self._listed_record(tx_hash)) is not None
                ]
            )
            if found:
                yield TransactionPage(records=found, next_cursor=page.next_cursor)
            if page.next_cursor is None:
                return
            next_cursor = page.next_cursor

    async def recheck_pending(
        self, records: Sequence[TransactionRecord]
    ) -> list[TransactionRecord]:
        """Read stored pending transactions again from the RPC.

        A transaction found again comes back with its current block and status. One the
        RPC no longer has becomes orphaned only when its block's height is finalized and
        holds another block now, which proves a reorg; a missing receipt alone could also
        mean an endpoint that dropped old receipts, so such a record is left as it is.
        """
        await self._require_chain()
        await self._refresh_finalized_height()
        changed = []
        for record in records:
            native = cast(EvmTransaction, record.native)
            current = await self._transaction_record(record.id, native.history_source)
            if current is not None:
                if current != record:
                    changed.append(current)
            elif await self._block_was_replaced(native):
                changed.append(record.model_copy(update={"status": Status.ORPHANED}))
        return changed

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
        if isinstance(record, AssetRecord):
            return _describe_token(record)
        return _describe_contract(record)

    @property
    def requests_made(self) -> int:
        history = self._history.requests_made if self._history else 0
        return self._rpc.requests_made + history + self._extra_requests

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
        context = SourceContext(
            self.chain, self.network, self._settings, self._rpc, self._history_scope()
        )
        source = factory(context)
        self._history_name = name
        return source

    def _history_scope(self) -> str:
        configured = self._settings.chain_setting(self.chain.name, self.network, "HISTORY_SCOPE")
        scope = configured.get_secret_value() if configured else SCOPE_DIRECT
        if scope not in HISTORY_SCOPES:
            setting = f"{self.chain.name}_{self.network}_HISTORY_SCOPE".upper()
            raise ConfigError(f"{setting} must be one of: {', '.join(HISTORY_SCOPES)}")
        return scope

    def _default_history_source(self) -> str | None:
        if self.network == LOCALNET:
            return RPC_SCAN
        return self.chain.networks[self.network].history_source

    async def _refresh_finalized_height(self) -> None:
        header, _ = await self._finalized_header()
        self._finalized_height = _header_number(header)

    async def _finalized_header(self) -> tuple[JsonObject, str]:
        if self.chain.economic_finality:
            header = await self._rpc.economic_finalized_header(TWO_THIRDS_OF_VALIDATORS)
            if header is not None:
                return header, ECONOMIC_FINALITY_SOURCE
            _log_no_economic_finality(self._network_title())
        return await self._rpc.finalized_header(), FINALIZED_TAG_SOURCE

    async def _listed_record(self, tx_hash: str) -> TransactionRecord | None:
        record = await self._transaction_record(tx_hash, self._history_name)
        if record is None:
            _log_missing(tx_hash)
        return record

    async def _block_was_replaced(self, native: EvmTransaction) -> bool:
        if native.block_number > self._finalized_height:
            return False
        block = await self._rpc.block_header(native.block_number, immutable=True)
        return block.get("hash") != native.block_hash

    async def _transaction_record(
        self, tx_hash: str, history_source: str | None = None
    ) -> TransactionRecord | None:
        receipt = await self._rpc.receipt(tx_hash)
        transaction = await self._rpc.transaction(tx_hash) if receipt else None
        if receipt is None or transaction is None:
            return None
        block_number = _block_number(receipt)
        final = block_number <= self._finalized_height
        block = await self._rpc.block_header(block_number, immutable=final)
        if block.get("hash") != receipt.get("blockHash"):
            _log_replaced(tx_hash, block_number)
            return None
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
        if native.bnb:
            fields["Validator"] = native.bnb.validator
            if native.bnb.system_transaction:
                fields["System transaction"] = f"yes, to {native.bnb.system_contract}"
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
        fields = {
            "Address": record.address,
            "Kind": kind,
            "Balance": self.profile.format_native(record.balance),
            "Nonce": str(native.nonce),
            "As of block": str(native.block_number),
        }
        if native.key_exposure:
            fields["Public key"] = (
                f"{native.key_exposure}, as of block {native.block_number} (query time)"
            )
        return fields


def _rpc_requests_per_second(settings: Settings) -> float:
    if "requests_per_second" in settings.model_fields_set:
        return settings.requests_per_second
    return DEFAULT_RPC_REQUESTS_PER_SECOND


def _log_replaced(tx_hash: str, block_number: int) -> None:
    # The receipt names a block that is no longer the chain's block at that height: a reorg
    # replaced it. Such a block was never final, so the next sync looks the transaction up
    # again and stores it wherever it landed.
    logger.warning(
        "receipt is from a replaced block",
        extra={"transaction": tx_hash, "block": block_number},
    )


def _log_missing(tx_hash: str) -> None:
    # The history service can be ahead of the RPC, or list a block that was replaced. Neither
    # block is final, and a sync never moves its checkpoint past the final height, so the
    # transaction is looked up again on the next sync.
    logger.warning(
        "history lists a transaction the RPC does not have yet", extra={"transaction": tx_hash}
    )


def _log_no_economic_finality(network: str) -> None:
    # A third-party endpoint may not serve BNB Smart Chain's own finality method, or may name no
    # block; the standard tag still names a finalized block, without the probabilistic fallback.
    logger.info(
        "no block from eth_getFinalizedHeader; using the finalized tag",
        extra={"network": network},
    )


def _header_number(header: JsonObject) -> int:
    return parse_quantity(header.get("number"), "eth_getBlockByNumber")


def _header_milliseconds(header: JsonObject) -> int:
    if "milliTimestamp" in header:
        return parse_quantity(header.get("milliTimestamp"), "eth_getBlockByNumber")
    seconds = parse_quantity(header.get("timestamp"), "eth_getBlockByNumber")
    return seconds * MILLISECONDS_PER_SECOND


def _time_of(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / MILLISECONDS_PER_SECOND, tz=UTC)


def _validate_hash(tx_hash: str) -> None:
    digits = tx_hash.removeprefix("0x")
    if not tx_hash.startswith("0x") or len(digits) != HASH_HEX_DIGITS:
        raise InvalidInput(f"not a transaction hash: {tx_hash!r}")
    if any(digit not in HEX_CHARACTERS for digit in digits):
        raise InvalidInput(f"not a transaction hash: {tx_hash!r}")


def _block_number(receipt: dict[str, JsonValue]) -> int:
    return parse_quantity(receipt.get("blockNumber"), "eth_getTransactionReceipt")


def _describe_token(record: AssetRecord) -> dict[str, str]:
    native = cast(EvmToken, record.native)
    decimals = str(record.decimals) if native.decimals_declared else "not declared, so 0"
    standard = native.standard
    if native.cas20_variant:
        standard = f"{standard} (CAS20 {native.cas20_variant}, BEP-702)"
    return {
        "Token": record.id,
        "Standard": standard,
        "Name": record.name or "not declared",
        "Symbol": record.unit or "not declared",
        "Decimals": decimals,
        "Total supply": f"{record.total} base units",
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
        server = settings.endpoint(chain.name, chosen, "RPC_SERVER", default_server)
        client = ApiClient(
            server,
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
            limiter=shared_limiter(host_scope(server), _rpc_requests_per_second(settings)),
            cache=cache,
        )
        return adapter(chosen, EvmRpc(client), settings)

    return create_indexer
