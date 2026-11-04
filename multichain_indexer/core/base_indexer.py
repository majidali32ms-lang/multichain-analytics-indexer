"""The contract every chain adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from pydantic import JsonValue

from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    NativeData,
    TransactionPage,
    TransactionRecord,
)


@dataclass(frozen=True)
class ChainProfile:
    """What the shared code needs to know about a chain."""

    name: str
    native_symbol: str
    base_unit: str
    native_decimals: int
    networks: tuple[str, ...]
    default_network: str
    transaction_model: type[NativeData]

    def format_native(self, base_units: int) -> str:
        """Render an amount in base units as whole tokens, e.g. ``1.5 ALGO``."""
        whole = Decimal(base_units).scaleb(-self.native_decimals)
        text = format(whole, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text} {self.native_symbol}"


class BaseIndexer(ABC):
    """Reads one network of one chain and returns normalised records."""

    profile: ClassVar[ChainProfile]

    def __init__(self, network: str) -> None:
        self.network = network

    @abstractmethod
    def validate_address(self, address: str) -> str:
        """Return ``address`` if valid for the chain.

        Raises:
            InvalidAddress: The address is malformed.
        """

    @abstractmethod
    async def fetch_account(self, address: str) -> AccountRecord:
        """Return the current state of an account."""

    @abstractmethod
    async def fetch_asset(self, asset_id: str) -> AssetRecord:
        """Return a token or asset by the chain's own identifier."""

    @abstractmethod
    async def fetch_contract(self, contract_id: str) -> ContractRecord:
        """Return an application, program or contract by the chain's own identifier."""

    @abstractmethod
    async def chain_head(self) -> int:
        """Return the newest height the endpoint can serve."""

    @abstractmethod
    def iter_account_transactions(
        self,
        address: str,
        *,
        from_height: int | None = None,
        to_height: int | None = None,
        cursor: str | None = None,
    ) -> AsyncIterator[TransactionPage]:
        """Yield an account's transactions one page at a time, newest first.

        Args:
            address: The account.
            from_height: Oldest height to include.
            to_height: Newest height to include.
            cursor: ``next_cursor`` of an earlier page with the same range, to continue
                from where that page ended.
        """

    @abstractmethod
    def chain_metrics(self, records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
        """Return the metrics that only make sense on this chain."""

    @abstractmethod
    def describe(
        self, record: TransactionRecord | AccountRecord | AssetRecord | ContractRecord
    ) -> dict[str, str]:
        """Return label and value pairs for display, in the chain's own terms."""

    @property
    @abstractmethod
    def requests_made(self) -> int:
        """Return the number of HTTP requests sent so far, retries included."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release network resources."""

    def final_height(self, head: int) -> int:
        """Return the newest height at or below ``head`` that can never change.

        Chains without instant finality override this; the pipeline stores newer
        records as pending and fetches them again on the next sync.
        """
        return head
