"""Turns NEAR RPC results and executed transactions into records.

``status`` and ``actions`` follow nearcore's JSON views whichever service supplied them. A
transaction's status starts at its first receipt and follows ``SuccessReceiptId`` until a
value or a failure: a failure in any other receipt does not fail the transaction.
"""

from __future__ import annotations

import base64
import binascii
import re
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from pydantic_core import from_json

from multichain_indexer.adapters.near.history import (
    ExecutedReceipt,
    ExecutedTransaction,
    ExecutionOutcome,
)
from multichain_indexer.adapters.near.models import (
    NearAccessKey,
    NearAccount,
    NearAction,
    NearContract,
    NearReceipt,
    NearTransaction,
)
from multichain_indexer.core.errors import InvalidAddress, MalformedResponse, NotFound
from multichain_indexer.core.models import AccountRecord, ContractRecord, Status, TransactionRecord

CHAIN = "near"
NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MICROSECOND = 1_000

# From nomicon.io/DataStructures/Account.
MIN_ACCOUNT_ID_LENGTH = 2
MAX_ACCOUNT_ID_LENGTH = 64
ACCOUNT_ID = re.compile(r"(([a-z0-9]+[-_])*[a-z0-9]+\.)*([a-z0-9]+[-_])*[a-z0-9]+")
ACCOUNT_KINDS = (
    ("near-implicit", re.compile(r"[0-9a-f]{64}")),
    ("eth-implicit", re.compile(r"0x[0-9a-f]{40}")),
    ("deterministic", re.compile(r"0s[0-9a-f]{40}")),
    ("universal", re.compile(r"0u[a-z0-9]{52}")),
)
KIND_NAMED = "named"

# The protocol sends gas and deposit refunds from this account.
SYSTEM_ACCOUNT = "system"
# The code hash of an account with no contract: 32 zero bytes in base-58.
NO_CODE_HASH = "11111111111111111111111111111111"

MAX_ARGS_NESTING = 32
CODE_HASH_BASE64_LENGTH = 44

KIND_BATCH = "Batch"
KIND_NO_ACTIONS = "NoActions"
FUNCTION_CALL = "FunctionCall"

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_PENDING = "pending"
SUCCESS_VALUE = "SuccessValue"
SUCCESS_RECEIPT_ID = "SuccessReceiptId"
FAILURE = "Failure"
FULL_ACCESS = "FullAccess"

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _RawAccount(_Raw):
    amount: int
    locked: int = 0
    code_hash: str
    storage_usage: int
    block_height: int
    global_contract_hash: str | None = None
    global_contract_account_id: str | None = None


class _RawFunctionCallPermission(_Raw):
    allowance: int | None = None
    receiver_id: str
    method_names: list[str] = []


class _RawAccessKeyBody(_Raw):
    nonce: int | None = None
    permission: JsonValue


class _RawAccessKey(_Raw):
    public_key: str
    access_key: _RawAccessKeyBody


class _RawAccessKeyList(_Raw):
    keys: list[_RawAccessKey]


class _RawShardLayoutBody(_Raw):
    boundary_accounts: list[str]
    shard_ids: list[int]


class _RawFunctionCall(_Raw):
    method_name: str
    args: str = ""
    gas: int
    deposit: int


class _RawTransfer(_Raw):
    deposit: int


class _RawStake(_Raw):
    stake: int
    public_key: str


class _RawAddKey(_Raw):
    public_key: str
    access_key: _RawAccessKeyBody


class _RawDeleteKey(_Raw):
    public_key: str


class _RawDeleteAccount(_Raw):
    beneficiary_id: str


class _RawDelegateAction(_Raw):
    sender_id: str
    receiver_id: str
    actions: list[JsonValue]


class _RawDelegate(_Raw):
    delegate_action: _RawDelegateAction


def validated_account_id(value: str) -> str:
    """Return ``value`` if it follows NEAR's account ID rules.

    Raises:
        InvalidAddress: It does not.
    """
    if (
        not MIN_ACCOUNT_ID_LENGTH <= len(value) <= MAX_ACCOUNT_ID_LENGTH
        or ACCOUNT_ID.fullmatch(value) is None
    ):
        raise InvalidAddress(f"not a valid NEAR account ID: {value!r}")
    return value


def account_kind(account: str) -> str:
    """Return ``named`` or the kind of implicit account an ID's form shows it to be."""
    for kind, pattern in ACCOUNT_KINDS:
        if pattern.fullmatch(account):
            return kind
    return KIND_NAMED


def parse_transaction(executed: ExecutedTransaction, network: str) -> TransactionRecord:
    """Build a record from a transaction and its receipts.

    Raises:
        MalformedResponse: An action or an amount does not match the documented format.
    """
    actions = _actions(executed.actions)
    outcomes = (executed.outcome, *(receipt.outcome for receipt in executed.receipts))
    final_status, error = _final_status(executed)
    tokens_burnt = sum(outcome.tokens_burnt for outcome in outcomes)
    native = _validate(
        NearTransaction,
        {
            "block_height": executed.outcome.block_height,
            "block_timestamp_ns": executed.outcome.block_timestamp_ns,
            "kind": _transaction_kind(actions),
            "public_key": executed.public_key,
            "nonce": executed.nonce,
            "error": error,
            "gas_burnt": sum(outcome.gas_burnt for outcome in outcomes),
            "tokens_burnt": tokens_burnt,
            "conversion_gas_burnt": executed.outcome.gas_burnt,
            "conversion_tokens_burnt": executed.outcome.tokens_burnt,
            "actions": actions,
            "receipts": tuple(_receipt(receipt) for receipt in executed.receipts),
        },
        f"transaction {executed.hash}",
    )
    return TransactionRecord(
        id=executed.hash,
        time=_time(executed.outcome.block_timestamp_ns),
        sender=executed.signer_id,
        receiver=executed.receiver_id,
        amount=_attached_near(actions),
        fee=tokens_burnt,
        status=final_status,
        chain=CHAIN,
        network=network,
        native=native,
    )


def parse_account(
    state: JsonValue | None,
    keys: JsonValue | None,
    shard_layout: JsonValue,
    account: str,
    network: str,
) -> AccountRecord:
    """Build an account record; ``state`` of None means nobody has created the account."""
    kind = account_kind(account)
    shard_id = _shard_of(account, shard_layout)
    if state is None:
        native = NearAccount(kind=kind, exists=False, shard_id=shard_id)
        return AccountRecord(
            address=account, balance=0, chain=CHAIN, network=network, native=native
        )
    raw = _validate(_RawAccount, state, "view_account")
    native = NearAccount(
        kind=kind,
        exists=True,
        block_height=raw.block_height,
        locked=raw.locked,
        storage_usage=raw.storage_usage,
        has_contract=_has_contract(raw),
        code_hash=raw.code_hash,
        global_contract_hash=raw.global_contract_hash,
        global_contract_account_id=raw.global_contract_account_id,
        shard_id=shard_id,
        access_keys=_access_keys(keys),
    )
    return AccountRecord(
        address=account, balance=raw.amount, chain=CHAIN, network=network, native=native
    )


def parse_contract(state: JsonValue, account: str, network: str) -> ContractRecord:
    """Build a contract record from an account's state.

    Raises:
        NotFound: The account holds no contract.
    """
    raw = _validate(_RawAccount, state, "view_account")
    if not _has_contract(raw):
        raise NotFound(f"no contract is deployed on {account}")
    native = NearContract(
        code_hash=raw.code_hash,
        block_height=raw.block_height,
        storage_usage=raw.storage_usage,
        global_contract_hash=raw.global_contract_hash,
        global_contract_account_id=raw.global_contract_account_id,
    )
    return ContractRecord(id=account, chain=CHAIN, network=network, native=native)


def _has_contract(raw: _RawAccount) -> bool:
    return (
        raw.code_hash != NO_CODE_HASH
        or raw.global_contract_hash is not None
        or raw.global_contract_account_id is not None
    )


def _shard_of(account: str, shard_layout: JsonValue) -> int | None:
    # A layout is an object with one key, its version. An account belongs to the shard
    # whose boundaries surround its ID in plain string order; versions without boundaries
    # are not used by any current network.
    versions = list(shard_layout.values()) if isinstance(shard_layout, dict) else []
    if len(versions) != 1:
        return None
    try:
        layout = _RawShardLayoutBody.model_validate(versions[0])
    except ValidationError:
        return None
    index = bisect_right(layout.boundary_accounts, account)
    return layout.shard_ids[index] if index < len(layout.shard_ids) else None


def _access_keys(keys: JsonValue | None) -> tuple[NearAccessKey, ...]:
    raw = _validate(_RawAccessKeyList, keys, "view_access_key_list")
    return tuple(_access_key(key.public_key, key.access_key) for key in raw.keys)


def _access_key(public_key: str, body: _RawAccessKeyBody) -> NearAccessKey:
    permission = body.permission
    if isinstance(permission, str):
        return NearAccessKey(public_key=public_key, permission=permission, nonce=body.nonce)
    kind, fields = _single_entry(permission, "access key permission")
    if kind != FUNCTION_CALL:
        return NearAccessKey(public_key=public_key, permission=kind, nonce=body.nonce)
    call = _validate(_RawFunctionCallPermission, fields, "access key permission")
    return NearAccessKey(
        public_key=public_key,
        permission=kind,
        nonce=body.nonce,
        allowance=call.allowance,
        receiver_id=call.receiver_id,
        method_names=tuple(call.method_names),
    )


def _time(nanoseconds: int) -> datetime:
    seconds, remainder = divmod(nanoseconds, NANOSECONDS_PER_SECOND)
    microseconds = remainder // NANOSECONDS_PER_MICROSECOND
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=microseconds)


def _final_status(executed: ExecutedTransaction) -> tuple[Status, JsonValue]:
    outcomes = {receipt.receipt_id: receipt.outcome for receipt in executed.receipts}
    current: ExecutionOutcome | None = executed.outcome
    # Bounded, so a malformed reply with a cycle in it cannot hang the sync.
    for _ in range(len(outcomes) + 1):
        if current is None:
            return Status.PENDING, None
        next_receipt = _next_receipt(current.status)
        if next_receipt is None:
            return _terminal_status(current.status)
        current = outcomes.get(next_receipt)
    return Status.PENDING, None


def _next_receipt(status: JsonValue) -> str | None:
    next_receipt = status.get(SUCCESS_RECEIPT_ID) if isinstance(status, dict) else None
    return next_receipt if isinstance(next_receipt, str) else None


def _terminal_status(status: JsonValue) -> tuple[Status, JsonValue]:
    if isinstance(status, dict) and SUCCESS_VALUE in status:
        return Status.FINAL, None
    if isinstance(status, dict) and FAILURE in status:
        return Status.FAILED, status[FAILURE]
    return Status.PENDING, None


def _receipt(receipt: ExecutedReceipt) -> NearReceipt:
    status, error = _receipt_status(receipt.outcome.status)
    return _validate(
        NearReceipt,
        {
            "receipt_id": receipt.receipt_id,
            "predecessor_id": receipt.predecessor_id,
            "receiver_id": receipt.receiver_id,
            "executor_id": receipt.outcome.executor_id,
            "is_refund": receipt.predecessor_id == SYSTEM_ACCOUNT,
            "status": status,
            "error": error,
            "gas_burnt": receipt.outcome.gas_burnt,
            "tokens_burnt": receipt.outcome.tokens_burnt,
            "block_height": receipt.outcome.block_height,
            "logs": receipt.outcome.logs,
            "produced_receipt_ids": receipt.outcome.receipt_ids,
            "actions": _actions(receipt.actions),
        },
        f"receipt {receipt.receipt_id}",
    )


def _receipt_status(status: JsonValue) -> tuple[str, JsonValue]:
    if isinstance(status, dict) and FAILURE in status:
        return STATUS_FAILURE, status[FAILURE]
    if isinstance(status, dict) and (SUCCESS_VALUE in status or SUCCESS_RECEIPT_ID in status):
        return STATUS_SUCCESS, None
    return STATUS_PENDING, None


def _transaction_kind(actions: tuple[NearAction, ...]) -> str:
    if not actions:
        return KIND_NO_ACTIONS
    return actions[0].kind if len(actions) == 1 else KIND_BATCH


def _attached_near(actions: Iterable[NearAction]) -> int:
    return sum(action.deposit + _attached_near(action.delegated_actions) for action in actions)


def _actions(raw_actions: Iterable[JsonValue]) -> tuple[NearAction, ...]:
    return tuple(_action(raw) for raw in raw_actions)


def _action(raw: JsonValue) -> NearAction:
    # An action with no fields, such as CreateAccount, is a bare string in nearcore's JSON.
    if isinstance(raw, str):
        return NearAction(kind=raw)
    kind, fields = _single_entry(raw, "action")
    typed = _TYPED_ACTIONS.get(kind)
    return typed(kind, fields) if typed else _untyped_action(kind, fields)


def _function_call(kind: str, fields: JsonValue) -> NearAction:
    call = _validate(_RawFunctionCall, fields, kind)
    args, args_base64 = _decoded_args(call.args)
    return NearAction(
        kind=kind,
        method_name=call.method_name,
        args=args,
        args_base64=args_base64,
        gas=call.gas,
        deposit=call.deposit,
    )


def _transfer(kind: str, fields: JsonValue) -> NearAction:
    return NearAction(kind=kind, deposit=_validate(_RawTransfer, fields, kind).deposit)


def _stake(kind: str, fields: JsonValue) -> NearAction:
    stake = _validate(_RawStake, fields, kind)
    return NearAction(kind=kind, stake=stake.stake, public_key=stake.public_key)


def _add_key(kind: str, fields: JsonValue) -> NearAction:
    added = _validate(_RawAddKey, fields, kind)
    return NearAction(
        kind=kind,
        public_key=added.public_key,
        access_key=_access_key(added.public_key, added.access_key),
    )


def _delete_key(kind: str, fields: JsonValue) -> NearAction:
    return NearAction(kind=kind, public_key=_validate(_RawDeleteKey, fields, kind).public_key)


def _delete_account(kind: str, fields: JsonValue) -> NearAction:
    beneficiary = _validate(_RawDeleteAccount, fields, kind).beneficiary_id
    return NearAction(kind=kind, beneficiary_id=beneficiary)


def _delegate(kind: str, fields: JsonValue) -> NearAction:
    delegated = _validate(_RawDelegate, fields, kind).delegate_action
    return NearAction(
        kind=kind,
        delegate_sender_id=delegated.sender_id,
        delegate_receiver_id=delegated.receiver_id,
        delegated_actions=_actions(delegated.actions),
    )


def _untyped_action(kind: str, fields: JsonValue) -> NearAction:
    # New action kinds reach mainnet between releases, so one must not stop a sync. The
    # services report a contract's code as its hash; a service that sends the whole binary,
    # which can run to megabytes, has it left out.
    details = (
        {name: value for name, value in fields.items() if not _is_contract_binary(name, value)}
        if isinstance(fields, dict)
        else None
    )
    return NearAction(kind=kind, details=details or None)


def _is_contract_binary(name: str, value: JsonValue) -> bool:
    return name == "code" and isinstance(value, str) and len(value) > CODE_HASH_BASE64_LENGTH


_TYPED_ACTIONS = {
    FUNCTION_CALL: _function_call,
    "Transfer": _transfer,
    "Stake": _stake,
    "AddKey": _add_key,
    "DeleteKey": _delete_key,
    "DeleteAccount": _delete_account,
    "Delegate": _delegate,
}


def _decoded_args(encoded: str) -> tuple[JsonValue, str | None]:
    # Anyone can send a contract any bytes. Arguments that are not plain JSON, or are nested
    # too deeply to store and read back, stay encoded, so they cannot stop a sync.
    try:
        decoded = from_json(base64.b64decode(encoded, validate=True), allow_inf_nan=False)
    except (binascii.Error, ValueError):
        return None, encoded or None
    if _nesting_depth(decoded) > MAX_ARGS_NESTING:
        return None, encoded
    return decoded, None


def _nesting_depth(value: JsonValue) -> int:
    deepest = 0
    pending: list[tuple[JsonValue, int]] = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict | list):
            deepest = max(deepest, depth)
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    return deepest


def _single_entry(value: JsonValue, what: str) -> tuple[str, JsonValue]:
    if not isinstance(value, Mapping) or len(value) != 1:
        raise MalformedResponse(f"{what}: expected an object with one key")
    return next(iter(value.items()))


def _validate(model: type[_ModelT], data: object, what: str) -> _ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise MalformedResponse(f"{what}: unexpected fields: {', '.join(fields)}") from None
