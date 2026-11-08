"""Turns Indexer REST responses into records.

The ``_Raw*`` models mirror the documented JSON, so a response that breaks the format
fails here instead of further down as a wrong number.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from multichain_indexer.adapters.algorand.models import (
    AlgorandAccount,
    AlgorandApplication,
    AlgorandAsset,
    AlgorandInnerTransaction,
    AlgorandTransaction,
    AppLog,
    AsaAction,
    AsaHolding,
    LogKind,
)
from multichain_indexer.adapters.algorand.pq import account_pq, transaction_pq
from multichain_indexer.core.errors import MalformedResponse, WrongNetwork
from multichain_indexer.core.http import JsonObject
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    Status,
    TransactionRecord,
)

CHAIN = "algorand"

# sha512_256("return")[:4], the prefix ARC-4 puts on an ABI method's return value.
ARC4_RETURN_PREFIX = bytes.fromhex("151f7c75")

GENESIS_HASH_BY_NETWORK = {
    "mainnet": "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
    "testnet": "SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
}

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class _Raw(BaseModel):
    model_config = ConfigDict(alias_generator=_kebab, populate_by_name=True, extra="ignore")


class _RawPayment(_Raw):
    amount: int
    receiver: str
    close_remainder_to: str | None = None
    close_amount: int = 0


class _RawAssetTransfer(_Raw):
    asset_id: int
    amount: int
    receiver: str
    sender: str | None = None
    close_to: str | None = None
    close_amount: int = 0


class _RawAssetConfig(_Raw):
    asset_id: int = 0
    params: dict[str, object] | None = None


class _RawAssetFreeze(_Raw):
    address: str
    asset_id: int
    new_freeze_status: bool


class _RawApplicationCall(_Raw):
    application_id: int
    on_completion: str | None = None


class _RawKeyreg(_Raw):
    non_participation: bool = False
    selection_participation_key: str | None = None


class _RawPqSignature(_Raw):
    scheme: str


class _RawSignature(_Raw):
    pqsig: _RawPqSignature | None = None


class _RawTransaction(_Raw):
    id: str | None = None
    sender: str
    fee: int
    tx_type: str
    confirmed_round: int | None = None
    round_time: int | None = None
    genesis_hash: str | None = None
    group: str | None = None
    rekey_to: str | None = None
    auth_addr: str | None = None
    logs: list[str] = []
    inner_txns: list[_RawTransaction] = []
    created_asset_index: int | None = None
    created_application_index: int | None = None
    payment_transaction: _RawPayment | None = None
    asset_transfer_transaction: _RawAssetTransfer | None = None
    asset_config_transaction: _RawAssetConfig | None = None
    asset_freeze_transaction: _RawAssetFreeze | None = None
    application_transaction: _RawApplicationCall | None = None
    keyreg_transaction: _RawKeyreg | None = None
    signature: _RawSignature | None = None


class _RawHolding(_Raw):
    asset_id: int
    amount: int
    is_frozen: bool = False


class _RawAccount(_Raw):
    address: str
    amount: int
    round: int
    status: str
    min_balance: int
    auth_addr: str | None = None
    rewards: int = 0
    pending_rewards: int = 0
    deleted: bool = False
    assets: list[_RawHolding] = []
    total_apps_opted_in: int = 0
    total_created_assets: int = 0
    total_created_apps: int = 0
    sig_type: str | None = None


class _RawAssetParams(_Raw):
    creator: str
    decimals: int
    total: int
    name: str | None = None
    unit_name: str | None = None
    url: str | None = None
    manager: str | None = None
    reserve: str | None = None
    freeze: str | None = None
    clawback: str | None = None
    default_frozen: bool = False


class _RawAsset(_Raw):
    index: int
    deleted: bool = False
    params: _RawAssetParams


class _RawStateSchema(_Raw):
    num_uint: int = 0
    num_byte_slice: int = 0


class _RawApplicationParams(_Raw):
    creator: str
    global_state_schema: _RawStateSchema = _RawStateSchema()
    local_state_schema: _RawStateSchema = _RawStateSchema()


class _RawApplication(_Raw):
    id: int
    deleted: bool = False
    created_at_round: int | None = None
    params: _RawApplicationParams


def parse_transaction(body: JsonObject, network: str) -> TransactionRecord:
    """Normalise one confirmed transaction.

    Raises:
        MalformedResponse: The JSON does not match the Indexer's format.
        WrongNetwork: The transaction belongs to a different network.
    """
    raw = _validate(_RawTransaction, body, "transaction")
    if raw.id is None or raw.confirmed_round is None or raw.round_time is None:
        raise MalformedResponse("transaction: id, confirmed-round and round-time are required")
    _require_network(raw, network)
    receiver, amount = _receiver_and_amount(raw)
    return TransactionRecord(
        id=raw.id,
        time=datetime.fromtimestamp(raw.round_time, tz=UTC),
        sender=raw.sender,
        receiver=receiver,
        amount=amount,
        fee=_tree_fee(raw),
        # A transaction that fails is never written to a block on Algorand.
        status=Status.FINAL,
        chain=CHAIN,
        network=network,
        native=AlgorandTransaction(
            **_native_fields(raw, raw.confirmed_round, raw.id),
            pq=transaction_pq(raw.tx_type, _pq_scheme(raw)),
        ),
    )


def parse_account(body: JsonObject, network: str) -> AccountRecord:
    """Normalise an ``/v2/accounts/{address}`` response."""
    raw = _validate(_RawAccount, body.get("account"), "account")
    return AccountRecord(
        address=raw.address,
        balance=raw.amount,
        chain=CHAIN,
        network=network,
        native=AlgorandAccount(
            round=raw.round,
            participation_status=raw.status,
            min_balance=raw.min_balance,
            auth_addr=raw.auth_addr,
            rewards=raw.rewards,
            pending_rewards=raw.pending_rewards,
            holdings=tuple(
                AsaHolding(asa_id=held.asset_id, amount=held.amount, is_frozen=held.is_frozen)
                for held in raw.assets
            ),
            apps_opted_in=raw.total_apps_opted_in,
            created_asas=raw.total_created_assets,
            created_apps=raw.total_created_apps,
            deleted=raw.deleted,
            pq=account_pq(raw.sig_type),
        ),
    )


def parse_asset(body: JsonObject, network: str) -> AssetRecord:
    """Normalise an ``/v2/assets/{id}`` response."""
    raw = _validate(_RawAsset, body.get("asset"), "asset")
    params = raw.params
    return AssetRecord(
        id=str(raw.index),
        name=params.name,
        unit=params.unit_name,
        decimals=params.decimals,
        total=params.total,
        creator=params.creator,
        chain=CHAIN,
        network=network,
        native=AlgorandAsset(
            url=params.url,
            manager=params.manager,
            reserve=params.reserve,
            freeze=params.freeze,
            clawback=params.clawback,
            default_frozen=params.default_frozen,
            deleted=raw.deleted,
        ),
    )


def parse_application(body: JsonObject, network: str) -> ContractRecord:
    """Normalise an ``/v2/applications/{id}`` response."""
    raw = _validate(_RawApplication, body.get("application"), "application")
    params = raw.params
    return ContractRecord(
        id=str(raw.id),
        creator=params.creator,
        chain=CHAIN,
        network=network,
        native=AlgorandApplication(
            created_at_round=raw.created_at_round,
            global_uints=params.global_state_schema.num_uint,
            global_byte_slices=params.global_state_schema.num_byte_slice,
            local_uints=params.local_state_schema.num_uint,
            local_byte_slices=params.local_state_schema.num_byte_slice,
            deleted=raw.deleted,
        ),
    )


def decode_log(entry: str, tx_id: str) -> tuple[LogKind, str]:
    """Classify one base64 log line as an ARC-4 return value, text, or raw bytes.

    Raises:
        MalformedResponse: The line is not valid base64.
    """
    try:
        data = base64.b64decode(entry, validate=True)
    except (binascii.Error, ValueError):
        raise MalformedResponse(f"transaction {tx_id}: log line is not base64") from None
    if data.startswith(ARC4_RETURN_PREFIX):
        return LogKind.ARC4_RETURN, data[len(ARC4_RETURN_PREFIX) :].hex()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return LogKind.BYTES, data.hex()
    return (LogKind.TEXT, text) if text.isprintable() else (LogKind.BYTES, data.hex())


def _validate(model: type[_ModelT], data: object, what: str) -> _ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise MalformedResponse(f"{what}: unexpected fields: {', '.join(fields)}") from None


def _require_network(raw: _RawTransaction, network: str) -> None:
    expected = GENESIS_HASH_BY_NETWORK.get(network)
    if expected and raw.genesis_hash and raw.genesis_hash != expected:
        raise WrongNetwork(f"the configured Indexer endpoint does not serve Algorand {network}")


def _pq_scheme(raw: _RawTransaction) -> str | None:
    pqsig = raw.signature.pqsig if raw.signature else None
    return pqsig.scheme if pqsig else None


def _receiver_and_amount(raw: _RawTransaction) -> tuple[str | None, int]:
    if raw.payment_transaction:
        return raw.payment_transaction.receiver, raw.payment_transaction.amount
    if raw.asset_transfer_transaction:
        return raw.asset_transfer_transaction.receiver, raw.asset_transfer_transaction.amount
    return None, 0


def _tree_fee(raw: _RawTransaction) -> int:
    return raw.fee + sum(_tree_fee(inner) for inner in raw.inner_txns)


def _native_fields(raw: _RawTransaction, round_: int, tx_id: str) -> dict[str, object]:
    return {
        "round": round_,
        "tx_type": raw.tx_type,
        "own_fee": raw.fee,
        "group": raw.group,
        "rekey_to": raw.rekey_to,
        "auth_addr": raw.auth_addr,
        "logs": tuple(_app_logs(raw, tx_id)),
        "inner_transactions": tuple(_inner(inner, round_, tx_id) for inner in raw.inner_txns),
        **_payment_fields(raw),
        **_asa_fields(raw),
        **_application_fields(raw),
        **_keyreg_fields(raw),
    }


def _inner(raw: _RawTransaction, round_: int, tx_id: str) -> AlgorandInnerTransaction:
    receiver, amount = _receiver_and_amount(raw)
    return AlgorandInnerTransaction(
        sender=raw.sender,
        receiver=receiver,
        amount=amount,
        **_native_fields(raw, round_, tx_id),
    )


def _app_logs(raw: _RawTransaction, tx_id: str) -> Iterator[AppLog]:
    app_id = _application_id(raw) or 0
    for entry in raw.logs:
        kind, payload = decode_log(entry, tx_id)
        yield AppLog(app_id=app_id, kind=kind, payload=payload)


def _application_id(raw: _RawTransaction) -> int | None:
    # A creation call targets app 0; the ID it received is recorded separately.
    if raw.application_transaction is None:
        return None
    return raw.application_transaction.application_id or raw.created_application_index


def _payment_fields(raw: _RawTransaction) -> dict[str, object]:
    payment = raw.payment_transaction
    if payment is None:
        return {}
    return {"close_to": payment.close_remainder_to, "close_amount": payment.close_amount}


def _asa_fields(raw: _RawTransaction) -> dict[str, object]:
    if raw.asset_transfer_transaction:
        return _asa_transfer_fields(raw, raw.asset_transfer_transaction)
    if raw.asset_config_transaction:
        return _asa_config_fields(raw, raw.asset_config_transaction)
    if raw.asset_freeze_transaction:
        freeze = raw.asset_freeze_transaction
        action = AsaAction.FREEZE if freeze.new_freeze_status else AsaAction.UNFREEZE
        return {"asa_id": freeze.asset_id, "asa_action": action, "freeze_target": freeze.address}
    return {}


def _asa_transfer_fields(raw: _RawTransaction, transfer: _RawAssetTransfer) -> dict[str, object]:
    return {
        "asa_id": transfer.asset_id,
        "asa_action": _transfer_action(raw, transfer),
        "clawback_from": transfer.sender,
        "close_to": transfer.close_to,
        "close_amount": transfer.close_amount,
    }


def _transfer_action(raw: _RawTransaction, transfer: _RawAssetTransfer) -> AsaAction:
    if transfer.sender:
        return AsaAction.CLAWBACK
    if transfer.close_to:
        return AsaAction.OPT_OUT
    if transfer.amount == 0 and transfer.receiver == raw.sender:
        return AsaAction.OPT_IN
    return AsaAction.TRANSFER


def _asa_config_fields(raw: _RawTransaction, config: _RawAssetConfig) -> dict[str, object]:
    if raw.created_asset_index:
        return {"asa_id": raw.created_asset_index, "asa_action": AsaAction.CREATE}
    action = AsaAction.RECONFIGURE if config.params else AsaAction.DESTROY
    return {"asa_id": config.asset_id, "asa_action": action}


def _application_fields(raw: _RawTransaction) -> dict[str, object]:
    if raw.application_transaction is None:
        return {}
    return {
        "app_id": _application_id(raw),
        "on_completion": raw.application_transaction.on_completion,
    }


def _keyreg_fields(raw: _RawTransaction) -> dict[str, object]:
    if raw.keyreg_transaction is None:
        return {}
    keyreg = raw.keyreg_transaction
    going_online = bool(keyreg.selection_participation_key) and not keyreg.non_participation
    return {"going_online": going_online}
