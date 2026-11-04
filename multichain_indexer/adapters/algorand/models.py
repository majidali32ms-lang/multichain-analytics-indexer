"""Algorand's own concepts, carried in the ``native`` block of each record."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from multichain_indexer.core.models import NativeData


class AsaAction(StrEnum):
    CREATE = "create"
    RECONFIGURE = "reconfigure"
    DESTROY = "destroy"
    OPT_IN = "opt-in"
    OPT_OUT = "opt-out"
    TRANSFER = "transfer"
    CLAWBACK = "clawback"
    FREEZE = "freeze"
    UNFREEZE = "unfreeze"


class LogKind(StrEnum):
    ARC4_RETURN = "arc4-return"
    TEXT = "text"
    BYTES = "bytes"


class AppLog(NativeData):
    """One application log line; ``payload`` is text, or hex for binary data."""

    app_id: int = Field(ge=0)
    kind: LogKind
    payload: str


class AlgorandTransaction(NativeData):
    """A top-level transaction.

    The shared ``amount`` is in microAlgos for a payment and in the ASA's base units for
    an asset transfer. The shared ``fee`` covers this transaction and every inner
    transaction it spawned; ``own_fee`` is this transaction's part alone.
    """

    round: int = Field(ge=0)
    tx_type: str
    own_fee: int = Field(ge=0)
    group: str | None = None
    asa_id: int | None = None
    asa_action: AsaAction | None = None
    clawback_from: str | None = None
    freeze_target: str | None = None
    close_to: str | None = None
    close_amount: int = Field(default=0, ge=0)
    app_id: int | None = None
    on_completion: str | None = None
    going_online: bool | None = None
    rekey_to: str | None = None
    auth_addr: str | None = None
    logs: tuple[AppLog, ...] = ()
    inner_transactions: tuple[AlgorandInnerTransaction, ...] = ()

    @property
    def inner_count(self) -> int:
        """Inner transactions at every depth."""
        return sum(1 + inner.inner_count for inner in self.inner_transactions)


class AlgorandInnerTransaction(AlgorandTransaction):
    """A transaction spawned by an application. Inner transactions have no ID of their own."""

    sender: str
    receiver: str | None = None
    amount: int = Field(ge=0)


class AsaHolding(NativeData):
    asa_id: int = Field(ge=0)
    amount: int = Field(ge=0)
    is_frozen: bool = False


class AlgorandAccount(NativeData):
    """Account state. ``auth_addr`` is set only on a rekeyed account."""

    round: int = Field(ge=0)
    participation_status: str
    min_balance: int = Field(ge=0)
    auth_addr: str | None = None
    rewards: int = Field(ge=0)
    pending_rewards: int = Field(ge=0)
    holdings: tuple[AsaHolding, ...] = ()
    apps_opted_in: int = Field(ge=0)
    created_asas: int = Field(ge=0)
    created_apps: int = Field(ge=0)
    deleted: bool = False


class AlgorandAsset(NativeData):
    url: str | None = None
    manager: str | None = None
    reserve: str | None = None
    freeze: str | None = None
    clawback: str | None = None
    default_frozen: bool = False
    deleted: bool = False


class AlgorandApplication(NativeData):
    created_at_round: int | None = None
    global_uints: int = Field(ge=0)
    global_byte_slices: int = Field(ge=0)
    local_uints: int = Field(ge=0)
    local_byte_slices: int = Field(ge=0)
    deleted: bool = False
