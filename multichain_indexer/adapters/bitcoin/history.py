"""Where Bitcoin data comes from, and the form every source hands it over in.

A source reads a script's history, its funded and spent outputs, single transactions and
whether a block is still on the best chain. It returns the plain transaction structure
below, so the parser and ``core/`` know nothing about any service. Esplora is the one
source today; a Bitcoin Core node is planned. To add one, write a class that meets
``BitcoinSource`` and add its factory to ``HISTORY_SOURCES`` in ``adapter.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from multichain_indexer.adapters.bitcoin.networks import BitcoinNetwork
from multichain_indexer.adapters.bitcoin.scripts import Locator
from multichain_indexer.core.config import Settings
from multichain_indexer.core.http import ResponseStore


@dataclass(frozen=True)
class SourceInput:
    """An input and the output it spends. A coinbase input spends nothing."""

    txid: str | None
    vout: int | None
    address: str | None
    script_type: str | None
    value: int
    sequence: int
    witness_items: int
    coinbase: bool


@dataclass(frozen=True)
class SourceOutput:
    """An output. ``address`` is the public key in hex for P2PK, and None without one."""

    index: int
    address: str | None
    script_type: str
    script_pubkey: str
    value: int


@dataclass(frozen=True)
class BlockRef:
    height: int
    hash: str
    time: int


@dataclass(frozen=True)
class SourceTransaction:
    """A transaction; ``block`` is None while it waits in the mempool.

    ``reported_fee`` is the fee the source states, checked against the inputs and outputs.
    """

    txid: str
    version: int
    locktime: int
    size: int
    weight: int
    inputs: tuple[SourceInput, ...]
    outputs: tuple[SourceOutput, ...]
    block: BlockRef | None
    reported_fee: int | None


@dataclass(frozen=True)
class OutputTotals:
    """Outputs a script received and spent, and the transactions involved."""

    funded_count: int
    funded_sum: int
    spent_count: int
    spent_sum: int
    tx_count: int


@dataclass(frozen=True)
class ScriptActivity:
    """A script's totals in confirmed blocks and in the mempool."""

    confirmed: OutputTotals
    mempool: OutputTotals


@dataclass(frozen=True)
class HistoryPage:
    """Transactions newest first, mempool before confirmed, and the cursor for the next."""

    transactions: tuple[SourceTransaction, ...]
    next_cursor: str | None


class BitcoinSource(Protocol):
    """What the adapter needs from a data source."""

    name: str

    @property
    def requests_made(self) -> int: ...

    async def genesis_hash(self) -> str:
        """Return the hash of block 0, which identifies the network."""
        ...

    async def tip_height(self) -> int:
        """Return the height of the newest block."""
        ...

    async def script_activity(self, locator: Locator) -> ScriptActivity:
        """Return what a script has received and spent."""
        ...

    async def history_page(
        self, locator: Locator, *, cursor: str | None, include_mempool: bool
    ) -> HistoryPage:
        """Return one page of a script's transactions.

        Args:
            locator: The address or public key.
            cursor: ``next_cursor`` of an earlier page.
            include_mempool: Put unconfirmed transactions first on the first page.

        Raises:
            InvalidInput: The source refused the cursor.
        """
        ...

    async def transaction(self, txid: str) -> SourceTransaction:
        """Return one transaction.

        Raises:
            NotFound: Neither a block on the best chain nor the mempool holds it.
        """
        ...

    async def block_in_best_chain(self, block_hash: str) -> bool:
        """Say whether the block is still on the best chain, rather than replaced."""
        ...

    async def aclose(self) -> None: ...


BitcoinSourceFactory = Callable[[BitcoinNetwork, Settings, ResponseStore | None], BitcoinSource]
