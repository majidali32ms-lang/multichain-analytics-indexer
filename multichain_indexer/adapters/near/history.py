"""Where a NEAR account's transaction history comes from.

NEAR's RPC cannot list an account's transactions, so an indexing service has to. Every
service is a ``HistorySource`` handing over the same records, which follow the RPC's own
views: ``status`` and ``actions`` are the JSON nearcore defines. The parser and ``core/``
therefore know nothing about any service. To add one, write a class that meets this
protocol and add its factory to ``HISTORY_SOURCES`` in ``adapter.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError
from multichain_indexer.core.http import ResponseStore


@dataclass(frozen=True)
class ExecutionOutcome:
    """The result of converting a transaction, or of executing a receipt."""

    id: str
    executor_id: str
    gas_burnt: int
    tokens_burnt: int
    status: JsonValue
    receipt_ids: tuple[str, ...]
    logs: tuple[str, ...]
    block_height: int
    block_timestamp_ns: int


@dataclass(frozen=True)
class ExecutedReceipt:
    receipt_id: str
    predecessor_id: str
    receiver_id: str
    actions: tuple[JsonValue, ...]
    outcome: ExecutionOutcome


@dataclass(frozen=True)
class ExecutedTransaction:
    hash: str
    signer_id: str
    receiver_id: str
    public_key: str
    nonce: int
    actions: tuple[JsonValue, ...]
    outcome: ExecutionOutcome
    receipts: tuple[ExecutedReceipt, ...]


@dataclass(frozen=True)
class HistoryPage:
    """Transactions newest first, and the opaque cursor for the next page, if any."""

    transactions: tuple[ExecutedTransaction, ...]
    next_cursor: str | None


class HistorySource(Protocol):
    """What the adapter needs from an indexing service."""

    @property
    def requests_made(self) -> int: ...

    async def indexed_height(self) -> int:
        """Return the newest block height the service has indexed."""
        ...

    async def account_page(
        self,
        account: str,
        *,
        from_height: int | None,
        to_height: int | None,
        cursor: str | None,
        immutable_up_to: int | None,
    ) -> HistoryPage:
        """Return one page of an account's transactions, with their receipts.

        Args:
            account: The account ID.
            from_height: Oldest block height to include.
            to_height: Newest block height to include.
            cursor: ``next_cursor`` of an earlier page with the same range.
            immutable_up_to: Transactions at or below this height can no longer change,
                so they may be cached.

        Raises:
            MalformedResponse: A transaction the service listed could not be read.
            InvalidInput: The service refused the cursor.
        """
        ...

    async def aclose(self) -> None: ...


HistorySourceFactory = Callable[[str, Settings, ResponseStore | None], HistorySource]


class UnavailableHistory:
    """Stands in on a network with no indexing service, such as a local sandbox.

    Account and contract lookups only need the RPC, so they keep working.
    """

    def __init__(self, message: str) -> None:
        self._message = message

    @property
    def requests_made(self) -> int:
        return 0

    async def indexed_height(self) -> int:
        raise ConfigError(self._message)

    async def account_page(
        self,
        account: str,
        *,
        from_height: int | None,
        to_height: int | None,
        cursor: str | None,
        immutable_up_to: int | None,
    ) -> HistoryPage:
        raise ConfigError(self._message)

    async def aclose(self) -> None:
        return None
