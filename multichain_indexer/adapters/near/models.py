"""NEAR's own concepts, carried in the ``native`` block of each record."""

from __future__ import annotations

from pydantic import Field, JsonValue

from multichain_indexer.core.models import NativeData
from multichain_indexer.pq import PqSignature


class NearAccessKey(NativeData):
    """An access key. ``permission`` is ``FullAccess`` or ``FunctionCall``.

    A function-call key may only call ``receiver_id``, and only ``method_names`` when that
    list is not empty. It may spend ``allowance`` yoctoNEAR on gas, without limit when None.
    """

    public_key: str
    permission: str
    nonce: int | None = Field(default=None, ge=0)
    allowance: int | None = Field(default=None, ge=0)
    receiver_id: str | None = None
    method_names: tuple[str, ...] = ()
    pq: PqSignature | None = None


class NearAction(NativeData):
    """One action of a transaction or receipt. ``kind`` is the protocol's own action name.

    Function call arguments are in ``args`` when they are JSON, and in ``args_base64``
    otherwise. An action kind with no typed fields here keeps them in ``details``: for
    ``DeployContract``, that is ``code``, the base64 hash of the code.
    """

    kind: str
    method_name: str | None = None
    args: JsonValue = None
    args_base64: str | None = None
    gas: int | None = Field(default=None, ge=0)
    deposit: int = Field(default=0, ge=0)
    stake: int | None = Field(default=None, ge=0)
    public_key: str | None = None
    access_key: NearAccessKey | None = None
    beneficiary_id: str | None = None
    delegate_sender_id: str | None = None
    delegate_receiver_id: str | None = None
    delegated_actions: tuple[NearAction, ...] = ()
    details: dict[str, JsonValue] | None = None


class NearReceipt(NativeData):
    """An action receipt and its outcome. ``is_refund`` marks one the protocol itself sent.

    ``status`` is ``success``, ``failure`` or ``pending``. A receipt can fail while its
    transaction succeeds.
    """

    receipt_id: str
    predecessor_id: str
    receiver_id: str
    executor_id: str
    is_refund: bool
    status: str
    error: JsonValue = None
    gas_burnt: int = Field(ge=0)
    tokens_burnt: int = Field(ge=0)
    block_height: int = Field(ge=0)
    logs: tuple[str, ...] = ()
    produced_receipt_ids: tuple[str, ...] = ()
    actions: tuple[NearAction, ...] = ()


class NearTransaction(NativeData):
    """A transaction and every receipt it led to.

    ``gas_burnt`` and ``tokens_burnt`` cover the transaction's own conversion to a receipt
    and all its receipts. The shared ``fee`` is ``tokens_burnt``, in yoctoNEAR, and the shared
    ``amount`` is the NEAR attached by its actions, delegated ones included.
    """

    block_height: int = Field(ge=0)
    block_timestamp_ns: int = Field(ge=0)
    kind: str
    public_key: str
    nonce: int = Field(ge=0)
    error: JsonValue = None
    gas_burnt: int = Field(ge=0)
    tokens_burnt: int = Field(ge=0)
    conversion_gas_burnt: int = Field(ge=0)
    conversion_tokens_burnt: int = Field(ge=0)
    actions: tuple[NearAction, ...] = ()
    receipts: tuple[NearReceipt, ...] = ()
    pq: PqSignature | None = None


class NearAccount(NativeData):
    """Account state. ``exists`` is false for a valid ID nobody has created yet.

    ``shard_id`` is the shard the account is on under the current shard layout, and None if
    the node reports a layout without boundaries. ``kind`` is ``named``, ``near-implicit``,
    ``eth-implicit``, ``deterministic`` or ``universal``.
    """

    kind: str
    exists: bool
    block_height: int | None = None
    locked: int = Field(default=0, ge=0)
    storage_usage: int | None = None
    has_contract: bool = False
    code_hash: str | None = None
    global_contract_hash: str | None = None
    global_contract_account_id: str | None = None
    shard_id: int | None = None
    access_keys: tuple[NearAccessKey, ...] = ()
    pq: PqSignature | None = None


class NearContract(NativeData):
    """A deployed contract. The protocol records no creator for one.

    A contract that uses a global contract has its code named by ``global_contract_hash`` or
    ``global_contract_account_id`` rather than by ``code_hash``.
    """

    code_hash: str
    block_height: int = Field(ge=0)
    storage_usage: int = Field(ge=0)
    global_contract_hash: str | None = None
    global_contract_account_id: str | None = None
