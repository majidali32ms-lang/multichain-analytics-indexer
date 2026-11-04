from __future__ import annotations

import base64
from typing import cast

import pytest

from multichain_indexer.adapters.algorand.metrics import algorand_metrics
from multichain_indexer.adapters.algorand.models import (
    AlgorandAccount,
    AlgorandApplication,
    AlgorandAsset,
    AlgorandTransaction,
    AsaAction,
    LogKind,
)
from multichain_indexer.adapters.algorand.parser import (
    ARC4_RETURN_PREFIX,
    decode_log,
    parse_account,
    parse_application,
    parse_asset,
    parse_transaction,
)
from multichain_indexer.core.errors import MalformedResponse, WrongNetwork
from multichain_indexer.core.models import Status, TransactionRecord
from tests.conftest import load_fixture


def native_of(record: TransactionRecord) -> AlgorandTransaction:
    return cast(AlgorandTransaction, record.native)


def parse_fixture(name: str) -> tuple[dict, TransactionRecord]:
    raw = load_fixture(name)
    return raw, parse_transaction(raw, "testnet")


@pytest.mark.parametrize(
    "name", ["tx_pay", "tx_axfer", "tx_acfg", "tx_afrz", "tx_appl", "tx_keyreg", "tx_rekey"]
)
def test_every_transaction_type_keeps_the_shared_fields(name: str) -> None:
    raw, record = parse_fixture(name)
    assert record.id == raw["id"]
    assert record.sender == raw["sender"]
    assert record.time.timestamp() == raw["round-time"]
    assert record.status is Status.FINAL
    assert record.chain == "algorand"
    assert native_of(record).round == raw["confirmed-round"]
    assert native_of(record).tx_type == raw["tx-type"]


def test_payment_fields() -> None:
    raw, record = parse_fixture("tx_pay")
    payment = raw["payment-transaction"]
    assert record.receiver == payment["receiver"]
    assert record.amount == payment["amount"]
    assert native_of(record).own_fee == raw["fee"]


def test_asa_transfer_fields() -> None:
    raw, record = parse_fixture("tx_axfer")
    transfer = raw["asset-transfer-transaction"]
    assert record.receiver == transfer["receiver"]
    assert record.amount == transfer["amount"]
    assert native_of(record).asa_id == transfer["asset-id"]
    assert native_of(record).asa_action is not None


def test_asa_config_records_an_asa_and_an_action() -> None:
    raw, record = parse_fixture("tx_acfg")
    native = native_of(record)
    assert native.asa_action in {AsaAction.CREATE, AsaAction.RECONFIGURE, AsaAction.DESTROY}
    expected = raw.get("created-asset-index") or raw["asset-config-transaction"]["asset-id"]
    assert native.asa_id == expected


def test_asa_freeze_names_its_target() -> None:
    raw, record = parse_fixture("tx_afrz")
    freeze = raw["asset-freeze-transaction"]
    assert native_of(record).freeze_target == freeze["address"]
    assert native_of(record).asa_action in {AsaAction.FREEZE, AsaAction.UNFREEZE}
    assert record.receiver is None


def test_keyreg_says_whether_the_account_went_online() -> None:
    raw, record = parse_fixture("tx_keyreg")
    keyreg = raw["keyreg-transaction"]
    expected = bool(keyreg.get("selection-participation-key")) and not keyreg.get(
        "non-participation", False
    )
    assert native_of(record).going_online is expected


def test_app_call_records_app_id() -> None:
    raw, record = parse_fixture("tx_appl")
    app = raw["application-transaction"]
    assert native_of(record).app_id == (app["application-id"] or raw["created-application-index"])
    assert native_of(record).on_completion == app["on-completion"]


def test_atomic_group_is_recorded() -> None:
    raw, record = parse_fixture("tx_group")
    assert native_of(record).group == raw["group"]


def test_inner_transactions_are_kept_and_their_fees_charged_to_the_outer_one() -> None:
    raw, record = parse_fixture("tx_inner")
    native = native_of(record)
    assert len(native.inner_transactions) == len(raw["inner-txns"])
    assert native.inner_transactions[0].sender == raw["inner-txns"][0]["sender"]
    assert native.inner_count >= len(raw["inner-txns"])

    def tree_fee(tx: dict) -> int:
        return tx["fee"] + sum(tree_fee(inner) for inner in tx.get("inner-txns", []))

    assert record.fee == tree_fee(raw)
    assert native.own_fee == raw["fee"]


def test_rekey_transaction_names_the_new_signer() -> None:
    raw, record = parse_fixture("tx_rekey")
    assert native_of(record).rekey_to == raw["rekey-to"]


def test_rekeyed_account_shows_its_auth_addr() -> None:
    raw = load_fixture("account_rekeyed")
    native = cast(AlgorandAccount, parse_account(raw).native)
    assert native.auth_addr == raw["account"]["auth-addr"]
    assert native.auth_addr


def test_account_fields() -> None:
    raw = load_fixture("account")
    account = raw["account"]
    record = parse_account(raw)
    native = cast(AlgorandAccount, record.native)
    assert record.address == account["address"]
    assert record.balance == account["amount"]
    assert native.min_balance == account["min-balance"]
    assert native.participation_status == account["status"]
    assert len(native.holdings) == len(account.get("assets", []))


def test_asset_fields() -> None:
    raw = load_fixture("asset")
    params = raw["asset"]["params"]
    record = parse_asset(raw)
    assert record.id == str(raw["asset"]["index"])
    assert record.decimals == params["decimals"]
    assert record.total == params["total"]
    assert record.creator == params["creator"]
    assert cast(AlgorandAsset, record.native).manager == params.get("manager")


def test_application_fields() -> None:
    raw = load_fixture("application")
    record = parse_application(raw)
    assert record.id == str(raw["application"]["id"])
    assert record.creator == raw["application"]["params"]["creator"]
    assert isinstance(record.native, AlgorandApplication)


BASE = {
    "id": "TX",
    "sender": "ALICE",
    "fee": 1000,
    "confirmed-round": 5,
    "round-time": 1_700_000_000,
}


def transfer(**fields: object) -> dict:
    details = {"asset-id": 9, "amount": 3, "receiver": "BOB", **fields}
    return {**BASE, "tx-type": "axfer", "asset-transfer-transaction": details}


@pytest.mark.parametrize(
    ("raw", "action"),
    [
        (transfer(), AsaAction.TRANSFER),
        (transfer(amount=0, receiver="ALICE"), AsaAction.OPT_IN),
        (transfer(amount=0, receiver="ALICE", **{"close-to": "CAROL"}), AsaAction.OPT_OUT),
        (transfer(sender="DAVE"), AsaAction.CLAWBACK),
    ],
)
def test_asa_transfer_actions(raw: dict, action: AsaAction) -> None:
    assert native_of(parse_transaction(raw, "testnet")).asa_action is action


@pytest.mark.parametrize(
    ("fields", "action"),
    [
        (
            {"asset-config-transaction": {"asset-id": 0, "params": {}}, "created-asset-index": 7},
            "create",
        ),
        ({"asset-config-transaction": {"asset-id": 7, "params": {"manager": "M"}}}, "reconfigure"),
        ({"asset-config-transaction": {"asset-id": 7}}, "destroy"),
    ],
)
def test_asa_config_actions(fields: dict, action: str) -> None:
    raw = {**BASE, "tx-type": "acfg", **fields}
    native = native_of(parse_transaction(raw, "testnet"))
    assert native.asa_action == action
    assert native.asa_id == 7


@pytest.mark.parametrize(("status", "action"), [(True, "freeze"), (False, "unfreeze")])
def test_asa_freeze_actions(status: bool, action: str) -> None:
    details = {"address": "BOB", "asset-id": 7, "new-freeze-status": status}
    raw = {**BASE, "tx-type": "afrz", "asset-freeze-transaction": details}
    assert native_of(parse_transaction(raw, "testnet")).asa_action == action


def test_newly_created_app_takes_its_new_id() -> None:
    log = base64.b64encode(b"init").decode()
    raw = {
        **BASE,
        "tx-type": "appl",
        "application-transaction": {"application-id": 0},
        "created-application-index": 42,
        "logs": [log],
    }
    native = native_of(parse_transaction(raw, "testnet"))
    assert native.app_id == 42
    assert native.logs[0].app_id == 42


@pytest.mark.parametrize(
    ("data", "kind", "payload"),
    [
        (ARC4_RETURN_PREFIX + b"\x00\x2a", LogKind.ARC4_RETURN, "002a"),
        (b"minted", LogKind.TEXT, "minted"),
        (b"\xff\x00", LogKind.BYTES, "ff00"),
        (b"tab\t", LogKind.BYTES, b"tab\t".hex()),
    ],
)
def test_log_decoding(data: bytes, kind: LogKind, payload: str) -> None:
    assert decode_log(base64.b64encode(data).decode(), "TX") == (kind, payload)


def test_log_that_is_not_base64_is_refused() -> None:
    with pytest.raises(MalformedResponse, match="not base64"):
        decode_log("%%%", "TX")


def test_endpoint_serving_another_network_is_detected() -> None:
    raw = {**transfer(), "genesis-hash": "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="}
    with pytest.raises(WrongNetwork, match="testnet"):
        parse_transaction(raw, "testnet")
    assert parse_transaction(raw, "mainnet").id == "TX"
    assert parse_transaction(raw, "localnet").id == "TX"


@pytest.mark.parametrize(
    "raw",
    [
        {"sender": "ALICE"},
        {**BASE, "tx-type": "pay", "fee": "lots"},
        {**transfer(), "confirmed-round": None},
    ],
)
def test_malformed_transactions_are_refused(raw: dict) -> None:
    with pytest.raises(MalformedResponse):
        parse_transaction(raw, "testnet")


def test_malformed_message_names_fields_not_values() -> None:
    with pytest.raises(MalformedResponse, match="fee") as caught:
        parse_transaction({**BASE, "tx-type": "pay", "fee": "secret-value"}, "testnet")
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize("parse", [parse_account, parse_asset, parse_application])
def test_lookups_refuse_a_missing_body(parse: object) -> None:
    with pytest.raises(MalformedResponse):
        parse({})  # type: ignore[operator]


def test_algorand_metrics() -> None:
    records = [
        parse_transaction(transfer(), "testnet"),
        parse_transaction(transfer(), "testnet"),
        parse_transaction(transfer(**{"asset-id": 8}), "testnet"),
        parse_transaction(transfer(amount=0, receiver="ALICE"), "testnet"),
        parse_transaction(
            {
                **BASE,
                "tx-type": "appl",
                "group": "G",
                "application-transaction": {"application-id": 5},
                "inner-txns": [
                    {
                        "sender": "APP",
                        "fee": 500,
                        "tx-type": "pay",
                        "inner-txns": [
                            {"sender": "APP", "fee": 0, "tx-type": "pay"},
                        ],
                    },
                ],
            },
            "testnet",
        ),
    ]
    metrics = algorand_metrics(records)
    assert metrics["app_calls"] == 1
    assert metrics["asa_opt_ins"] == 1
    assert metrics["group_transaction_share"] == 0.2
    assert metrics["inner_transactions"] == 2
    assert metrics["fees_microalgos"] == 5500
    assert metrics["top_asas_by_transfers"] == [
        {"asa_id": 9, "transfers": 2},
        {"asa_id": 8, "transfers": 1},
    ]


def test_algorand_metrics_on_no_records() -> None:
    assert algorand_metrics([])["group_transaction_share"] is None
