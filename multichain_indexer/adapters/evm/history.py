"""Where an EVM account's transaction list comes from.

Standard JSON-RPC cannot list an address's transactions, so a history source supplies the
transaction hashes, newest first. The transactions, receipts and blocks themselves always come
from the chain's own RPC. The source is chosen per chain and network with
``<CHAIN>_<NETWORK>_HISTORY_SOURCE``, so adding a service is one new class and one entry in
``HISTORY_SOURCES``, with no parser or core change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.profiles import EvmChain
from multichain_indexer.core.config import Settings


@dataclass(frozen=True)
class HistoryPage:
    """Transaction hashes in one page, newest first, and the cursor for the next page."""

    hashes: tuple[str, ...]
    next_cursor: str | None


class HistorySource(Protocol):
    @property
    def requests_made(self) -> int: ...

    @property
    def indexed_up_to(self) -> int | None:
        """The newest block the service had indexed when it could not reach the top asked for.

        None when it listed up to the top. A sync never moves its checkpoint past this block,
        so transactions above it are listed by the next sync.
        """
        ...

    async def account_page(
        self,
        address: str,
        *,
        from_height: int | None,
        to_height: int | None,
        cursor: str | None,
    ) -> HistoryPage:
        """Return one page of the transactions ``address`` sent or received.

        Raises:
            InvalidInput: The cursor is not one this source issued.
        """
        ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class SourceContext:
    """What a history source is built from."""

    chain: EvmChain
    network: str
    settings: Settings
    rpc: EvmRpc


HistorySourceFactory = Callable[[SourceContext], HistorySource]
