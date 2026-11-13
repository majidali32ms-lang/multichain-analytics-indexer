"""Bitcoin behind the ``BaseIndexer`` contract.

Everything is read from a ``BitcoinSource`` chosen by ``BITCOIN_<NETWORK>_HISTORY_SOURCE``;
Esplora is the default and today the only one. An account is an address, or a public key for
P2PK outputs, and its history is every transaction that pays or spends its script.
Transactions with fewer than six confirmations are pending and read again on the next sync.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from typing import cast

from pydantic import JsonValue

from multichain_indexer.adapters.bitcoin.esplora import SOURCE_NAME, create_esplora_source
from multichain_indexer.adapters.bitcoin.history import (
    BitcoinSource,
    BitcoinSourceFactory,
    OutputTotals,
    SourceTransaction,
)
from multichain_indexer.adapters.bitcoin.metrics import SATS_DECIMALS, bitcoin_metrics
from multichain_indexer.adapters.bitcoin.models import BitcoinAddress, BitcoinTransaction
from multichain_indexer.adapters.bitcoin.networks import (
    KNOWN_GENESIS,
    NETWORKS,
    TESTNET4,
    BitcoinNetwork,
)
from multichain_indexer.adapters.bitcoin.parser import (
    CHAIN,
    CONFIRMATION_DEPTH,
    parse_transaction,
)
from multichain_indexer.adapters.bitcoin.scripts import parse_locator
from multichain_indexer.core.base_indexer import BaseIndexer, ChainProfile
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError, InvalidInput, NotFound, WrongNetwork
from multichain_indexer.core.http import ResponseStore
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    Status,
    TransactionPage,
    TransactionRecord,
)

DEFAULT_HISTORY_SOURCE = SOURCE_NAME
HISTORY_SOURCES: dict[str, BitcoinSourceFactory] = {SOURCE_NAME: create_esplora_source}
TXID = re.compile(r"[0-9a-fA-F]{64}")

PROFILE = ChainProfile(
    name=CHAIN,
    native_symbol="BTC",
    base_unit="sats",
    native_decimals=SATS_DECIMALS,
    networks=tuple(NETWORKS),
    default_network=TESTNET4,
    transaction_model=BitcoinTransaction,
)


class BitcoinIndexer(BaseIndexer):
    profile = PROFILE

    def __init__(self, network: BitcoinNetwork, source: BitcoinSource) -> None:
        super().__init__(network.name)
        self._network = network
        self._source = source
        self._network_confirmed = False
        self._tip_height = 0

    def validate_address(self, address: str) -> str:
        return parse_locator(address, self._network).text

    async def fetch_account(self, address: str) -> AccountRecord:
        locator = parse_locator(address, self._network)
        await self._require_network()
        activity = await self._source.script_activity(locator)
        confirmed, mempool = activity.confirmed, activity.mempool
        return AccountRecord(
            address=locator.text,
            balance=_held(confirmed),
            chain=CHAIN,
            network=self.network,
            native=BitcoinAddress(
                address_type=locator.script_type,
                script_hash=locator.script_hash,
                transactions=confirmed.tx_count + mempool.tx_count,
                utxo_count=confirmed.funded_count - confirmed.spent_count,
                received_outputs=confirmed.funded_count + mempool.funded_count,
                spent_outputs=confirmed.spent_count + mempool.spent_count,
                unconfirmed_change=mempool.funded_sum - mempool.spent_sum,
                mempool_transactions=mempool.tx_count,
                history_source=self._source.name,
            ),
        )

    async def fetch_asset(self, asset_id: str) -> AssetRecord:
        raise InvalidInput("Bitcoin has no tokens to look up; look up an address or a txid")

    async def fetch_contract(self, contract_id: str) -> ContractRecord:
        raise InvalidInput("Bitcoin has no contracts to look up; look up an address or a txid")

    async def fetch_transaction(self, txid: str) -> TransactionRecord:
        """Return one transaction, confirmed or in the mempool.

        Raises:
            InvalidInput: ``txid`` is not 64 hex digits.
            NotFound: Neither the best chain nor the mempool holds it.
        """
        if not TXID.fullmatch(txid):
            raise InvalidInput(f"{txid!r} is not a txid: expected 64 hex digits")
        await self._require_network()
        await self._refresh_tip()
        return self._record(await self._source.transaction(txid.lower()))

    async def chain_head(self) -> int:
        await self._require_network()
        return await self._refresh_tip()

    def final_height(self, head: int) -> int:
        return max(head - CONFIRMATION_DEPTH + 1, 0)

    async def iter_account_transactions(
        self,
        address: str,
        *,
        from_height: int | None = None,
        to_height: int | None = None,
        cursor: str | None = None,
    ) -> AsyncIterator[TransactionPage]:
        locator = parse_locator(address, self._network)
        await self._require_network()
        tip = await self._refresh_tip()
        include_mempool = to_height is None or to_height >= tip
        next_cursor = cursor
        while True:
            page = await self._source.history_page(
                locator, cursor=next_cursor, include_mempool=include_mempool
            )
            records = tuple(
                self._record(tx)
                for tx in page.transactions
                if _in_range(tx, from_height, to_height)
            )
            done = page.next_cursor is None or _below(page.transactions[-1], from_height)
            if records:
                yield TransactionPage(
                    records=records, next_cursor=None if done else page.next_cursor
                )
            if done:
                return
            next_cursor = page.next_cursor

    async def recheck_pending(
        self, records: Sequence[TransactionRecord]
    ) -> list[TransactionRecord]:
        """Read stored pending transactions again.

        One found again comes back with its current block and confirmations. A confirmed one
        that is gone becomes orphaned only when its block is no longer on the best chain,
        which proves a reorg. One that was in the mempool and is gone was dropped, as by a
        replacement or eviction, and becomes orphaned; if it confirms after all, a later
        sync stores it again.
        """
        await self._require_network()
        await self._refresh_tip()
        changed = []
        for record in records:
            native = cast(BitcoinTransaction, record.native)
            try:
                current = self._record(await self._source.transaction(record.id))
            except NotFound:
                if await self._was_dropped(native):
                    changed.append(record.model_copy(update={"status": Status.ORPHANED}))
                continue
            if current != record:
                changed.append(current)
        return changed

    def chain_metrics(self, records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
        return bitcoin_metrics(records)

    def describe(
        self, record: TransactionRecord | AccountRecord | AssetRecord | ContractRecord
    ) -> dict[str, str]:
        if isinstance(record, TransactionRecord):
            return _describe_transaction(record)
        return _describe_address(cast(AccountRecord, record))

    @property
    def requests_made(self) -> int:
        return self._source.requests_made

    async def aclose(self) -> None:
        await self._source.aclose()

    def _record(self, tx: SourceTransaction) -> TransactionRecord:
        return parse_transaction(
            tx, self.network, tip_height=self._tip_height, history_source=self._source.name
        )

    async def _refresh_tip(self) -> int:
        self._tip_height = await self._source.tip_height()
        return self._tip_height

    async def _was_dropped(self, native: BitcoinTransaction) -> bool:
        if native.block_hash is None:
            return True
        return not await self._source.block_in_best_chain(native.block_hash)

    async def _require_network(self) -> None:
        """Confirm once, before the first lookup, that the server serves the network.

        Raises:
            WrongNetwork: Its genesis block belongs to another network.
        """
        if self._network_confirmed:
            return
        genesis = await self._source.genesis_hash()
        if genesis != self._network.genesis_hash:
            raise WrongNetwork(_wrong_network_message(self._network, genesis, self._source.name))
        self._network_confirmed = True


def create_indexer(
    network: str | None, settings: Settings, cache: ResponseStore | None = None
) -> BitcoinIndexer:
    """Build the adapter for ``network``, testnet4 when None."""
    chosen = network or PROFILE.default_network
    if chosen not in NETWORKS:
        raise InvalidInput(f"unknown Bitcoin network {chosen!r}: {', '.join(PROFILE.networks)}")
    bitcoin_network = NETWORKS[chosen]
    return BitcoinIndexer(bitcoin_network, _source(bitcoin_network, settings, cache))


def _source(
    network: BitcoinNetwork, settings: Settings, cache: ResponseStore | None
) -> BitcoinSource:
    configured = settings.chain_setting(CHAIN, network.name, "HISTORY_SOURCE")
    name = configured.get_secret_value() if configured else DEFAULT_HISTORY_SOURCE
    factory = HISTORY_SOURCES.get(name)
    if factory is None:
        setting = f"{CHAIN}_{network.name}_HISTORY_SOURCE".upper()
        raise ConfigError(f"{setting} must be one of: {', '.join(sorted(HISTORY_SOURCES))}")
    return factory(network, settings, cache)


def _held(totals: OutputTotals) -> int:
    return totals.funded_sum - totals.spent_sum


def _in_range(tx: SourceTransaction, from_height: int | None, to_height: int | None) -> bool:
    if tx.block is None:
        return True
    above_floor = from_height is None or tx.block.height >= from_height
    below_ceiling = to_height is None or tx.block.height <= to_height
    return above_floor and below_ceiling


def _below(tx: SourceTransaction, from_height: int | None) -> bool:
    return from_height is not None and tx.block is not None and tx.block.height < from_height


def _wrong_network_message(network: BitcoinNetwork, genesis: str, source: str) -> str:
    served = KNOWN_GENESIS.get(genesis, "another network")
    setting = f"{CHAIN}_{network.name}_{source}_SERVER".upper()
    return f"the {source} server serves {served}, not Bitcoin {network.name}. Check {setting}."


def _describe_transaction(record: TransactionRecord) -> dict[str, str]:
    native = cast(BitcoinTransaction, record.native)
    return {
        "Status": _confirmation_text(native),
        "Amount": PROFILE.format_native(record.amount),
        "Fee": f"{record.fee} sats",
        "Fee rate": f"{native.fee_rate:.2f} sat/vB",
        "Size": f"{native.vsize} vB, {native.weight} WU",
        "Inputs and outputs": f"{len(native.inputs)} in, {len(native.outputs)} out",
        "RBF": "yes" if native.rbf else "no",
    }


def _describe_address(record: AccountRecord) -> dict[str, str]:
    native = cast(BitcoinAddress, record.native)
    return {
        "Address" if native.address_type != "p2pk" else "Public key": record.address,
        "Type": native.address_type.upper(),
        "Balance": PROFILE.format_native(record.balance),
        "Unconfirmed": f"{native.unconfirmed_change:+} sats",
        "UTXOs": str(native.utxo_count),
        "Transactions": str(native.transactions),
        "Key revealed": "yes" if _key_revealed(native) else "no",
    }


def _key_revealed(native: BitcoinAddress) -> bool:
    return native.address_type in ("p2pk", "p2tr") or native.spent_outputs > 0


def _confirmation_text(native: BitcoinTransaction) -> str:
    if not native.confirmed:
        return "unconfirmed (mempool)"
    return f"{native.confirmations} confirmations, block {native.block_height}"
