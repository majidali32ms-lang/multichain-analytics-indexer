"""Records every chain produces.

Shared fields are the ones that mean the same thing everywhere. Anything specific
to a chain goes in ``native``, a model the adapter defines by subclassing
``NativeData``, so adding a chain never changes this module.

Amounts and fees are integers in base units. Some chains exceed 64 bits (a NEAR fee
in yoctoNEAR, an EVM amount in wei), so they are never held as floats.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, SerializeAsAny


class Status(StrEnum):
    """Where a transaction stands. ``PENDING`` records are fetched again on the next sync."""

    PENDING = "pending"
    FINAL = "final"
    FAILED = "failed"
    ORPHANED = "orphaned"


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NativeData(_Record):
    """Base class for the chain-specific block carried by every record."""


class TransactionRecord(_Record):
    id: str = Field(min_length=1)
    time: AwareDatetime
    sender: str = Field(min_length=1)
    receiver: str | None = None
    amount: int = Field(ge=0)
    fee: int = Field(ge=0)
    status: Status
    chain: str = Field(min_length=1)
    native: SerializeAsAny[NativeData]


class AccountRecord(_Record):
    address: str = Field(min_length=1)
    balance: int = Field(ge=0)
    chain: str = Field(min_length=1)
    native: SerializeAsAny[NativeData]


class AssetRecord(_Record):
    id: str = Field(min_length=1)
    name: str | None = None
    unit: str | None = None
    decimals: int = Field(ge=0)
    total: int = Field(ge=0)
    creator: str = Field(min_length=1)
    chain: str = Field(min_length=1)
    native: SerializeAsAny[NativeData]


class ContractRecord(_Record):
    id: str = Field(min_length=1)
    creator: str = Field(min_length=1)
    chain: str = Field(min_length=1)
    native: SerializeAsAny[NativeData]


class SyncResult(_Record):
    """Summary of one incremental sync."""

    chain: str
    network: str
    account: str
    from_height: int | None
    to_height: int
    records_fetched: int = Field(ge=0)
    requests_made: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)


class MetricsReport(_Record):
    """Metrics over a time window; ``native`` holds what the chain's adapter adds."""

    chain: str
    network: str
    days: int = Field(ge=1)
    account: str | None
    transactions: int = Field(ge=0)
    transactions_per_day: dict[str, int]
    active_accounts: int = Field(ge=0)
    total_fees: int = Field(ge=0)
    failure_rate: float | None
    native: dict[str, JsonValue]
