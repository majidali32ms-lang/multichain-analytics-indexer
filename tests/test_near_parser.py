from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from multichain_indexer import InvalidAddress, MalformedResponse, NotFound
from multichain_indexer.adapters.near.fastnear import _executed_transaction, _RawTransaction
from multichain_indexer.adapters.near.history import ExecutedTransaction
from multichain_indexer.adapters.near.metrics import near_metrics
from multichain_indexer.adapters.near.models import NearTransaction
from multichain_indexer.adapters.near.parser import (
    MAX_ARGS_NESTING,
    account_kind,
    parse_account,
    parse_contract,
    parse_transaction,
    validated_account_id,
)
from multichain_indexer.core.models import Status, TransactionRecord
from tests.conftest import NEAR_FIXTURES
from tests.conftest import load_near_fixture as fixture

TX_FIXTURES = sorted(path.stem for path in NEAR_FIXTURES.glob("tx_*.json"))
SHARD_LAYOUT = fixture("rpc_shard_layout")["result"]["shard_layout"]
NETWORK = "testnet"


def executed_from(raw: dict[str, Any]) -> ExecutedTransaction:
    return _executed_transaction(_RawTransaction.model_validate(raw))


def executed(name: str) -> ExecutedTransaction:
    return executed_from(fixture(name)["transaction"])


def record_of(name: str) -> TransactionRecord:
    return parse_transaction(executed(name), NETWORK)


def native_of(record: TransactionRecord) -> NearTransaction:
    assert isinstance(record.native, NearTransaction)
    return record.native


def encoded(text: bytes) -> str:
    return base64.b64encode(text).decode()


def function_call(args: str) -> dict[str, Any]:
    return {"FunctionCall": {"method_name": "go", "args": args, "gas": 30, "deposit": "0"}}


def with_actions(*actions: Any) -> ExecutedTransaction:
    return replace(executed("tx_transfer"), actions=actions)


def rows_with_their_transactions() -> list[tuple[bool, dict[str, Any]]]:
    pairs = [(fixture(name)["is_success"], fixture(name)["transaction"]) for name in TX_FIXTURES]
    for page in fixture("history_pages")["pages"]:
        flags = {row["transaction_hash"]: row["is_success"] for row in page["rows"]["account_txs"]}
        pairs += [(flags[tx["transaction"]["hash"]], tx) for tx in page["transactions"]]
    return pairs


@pytest.mark.parametrize(
    "account",
    [
        "ok",
        "bowen",
        "ek-2",
        "ek.near",
        "com",
        "google.com",
        "bowen.google.com",
        "near",
        "illia.cheapaccounts.near",
        "max_99.near",
        "100",
        "near2019",
        "over.9000",
        "a.bro",
        "bro.a",
        "0x85f17cf997934a597031b2e18a9ab6ebd4b9f6a4",
        "a" * 64,
    ],
)
def test_valid_account_ids_from_the_protocol_spec(account: str) -> None:
    assert validated_account_id(account) == account


@pytest.mark.parametrize(
    "account",
    [
        "",
        "a",
        "A",
        "Abc",
        "-near",
        "near-",
        "-near-",
        "near.",
        ".near",
        "a..near",
        "bo__wen",
        "100-",
        "_illia",
        "not ok",
        "a@b.near",
        "near\n",
        "a" * 65,
    ],
)
def test_invalid_account_ids_are_refused(account: str) -> None:
    with pytest.raises(InvalidAddress, match="not a valid NEAR account ID"):
        validated_account_id(account)


@pytest.mark.parametrize(
    ("account", "kind"),
    [
        ("alice.near", "named"),
        ("near", "named"),
        ("f781edbfe07287152e3912cb698af4155234df3c2d9c23bf625f675b8f6de5fa", "near-implicit"),
        ("0x85f17cf997934a597031b2e18a9ab6ebd4b9f6a4", "eth-implicit"),
        ("0s85f17cf997934a597031b2e18a9ab6ebd4b9f6a4", "deterministic"),
        ("0u" + "a1" * 26, "universal"),
        ("0x85f17cf9.near", "named"),
    ],
)
def test_account_kind_comes_from_the_shape_of_the_id(account: str, kind: str) -> None:
    assert account_kind(account) == kind


@pytest.mark.parametrize(("is_success", "raw"), rows_with_their_transactions())
def test_final_status_agrees_with_the_indexer_on_every_real_transaction(
    is_success: bool, raw: dict[str, Any]
) -> None:
    record = parse_transaction(executed_from(raw), NETWORK)
    assert record.status == (Status.FINAL if is_success else Status.FAILED)


@pytest.mark.parametrize(("_flag", "raw"), rows_with_their_transactions())
def test_fee_is_all_tokens_burnt_by_the_transaction_and_its_receipts(
    _flag: bool, raw: dict[str, Any]
) -> None:
    burnt = int(raw["execution_outcome"]["outcome"]["tokens_burnt"]) + sum(
        int(receipt["execution_outcome"]["outcome"]["tokens_burnt"]) for receipt in raw["receipts"]
    )
    record = parse_transaction(executed_from(raw), NETWORK)
    assert record.fee == burnt == native_of(record).tokens_burnt


def test_transfer_amount_and_shared_fields() -> None:
    raw = fixture("tx_transfer")["transaction"]
    record = record_of("tx_transfer")
    native = native_of(record)
    assert record.id == raw["transaction"]["hash"]
    assert record.sender == raw["transaction"]["signer_id"]
    assert record.receiver == raw["transaction"]["receiver_id"]
    assert record.amount == int(raw["transaction"]["actions"][0]["Transfer"]["deposit"])
    assert record.chain == "near"
    assert record.network == NETWORK
    assert native.kind == "Transfer"
    assert native.block_height == raw["execution_outcome"]["block_height"]
    assert native.conversion_gas_burnt == raw["execution_outcome"]["outcome"]["gas_burnt"]


def test_time_keeps_microseconds_from_the_nanosecond_timestamp() -> None:
    nanoseconds = 1792645500410736121
    outcome = replace(executed("tx_transfer").outcome, block_timestamp_ns=nanoseconds)
    record = parse_transaction(replace(executed("tx_transfer"), outcome=outcome), NETWORK)
    assert record.time == datetime(2026, 10, 22, 5, 5, 0, 410736, tzinfo=UTC)
    assert native_of(record).block_timestamp_ns == nanoseconds


def test_function_call_has_method_decoded_args_gas_and_deposit() -> None:
    raw = fixture("tx_function_call")["transaction"]["transaction"]["actions"][0]["FunctionCall"]
    action = native_of(record_of("tx_function_call")).actions[0]
    assert action.kind == "FunctionCall"
    assert action.method_name == raw["method_name"]
    assert action.args == json.loads(base64.b64decode(raw["args"]))
    assert action.args_base64 is None
    assert action.gas == raw["gas"]
    assert action.deposit == int(raw["deposit"])


def test_transaction_with_several_receipts_keeps_each_outcome() -> None:
    raw = fixture("tx_many_receipts")["transaction"]
    native = native_of(record_of("tx_many_receipts"))
    assert len(native.receipts) == len(raw["receipts"]) > 5
    first = native.receipts[0]
    source = raw["receipts"][0]
    assert first.receipt_id == source["receipt"]["receipt_id"]
    assert first.predecessor_id == source["receipt"]["predecessor_id"]
    assert first.executor_id == source["execution_outcome"]["outcome"]["executor_id"]
    assert first.produced_receipt_ids == tuple(
        source["execution_outcome"]["outcome"]["receipt_ids"]
    )
    assert native.gas_burnt == native.conversion_gas_burnt + sum(
        receipt.gas_burnt for receipt in native.receipts
    )


def test_failed_transaction_records_its_error() -> None:
    record = record_of("tx_failed")
    native = native_of(record)
    assert record.status == Status.FAILED
    assert native.error is not None
    assert any(receipt.status == "failure" for receipt in native.receipts)
    assert next(r for r in native.receipts if r.status == "failure").error == native.error


def test_failed_receipt_does_not_fail_a_successful_transaction() -> None:
    record = record_of("tx_failed_receipt_in_successful_transaction")
    native = native_of(record)
    assert record.status == Status.FINAL
    assert native.error is None
    failed = [r for r in native.receipts if r.status == "failure" and not r.is_refund]
    assert failed
    assert failed[0].error is not None


def test_refund_receipts_are_marked() -> None:
    native = native_of(record_of("tx_transfer"))
    refunds = [receipt for receipt in native.receipts if receipt.is_refund]
    assert refunds
    assert all(receipt.predecessor_id == "system" for receipt in refunds)
    assert not all(receipt.is_refund for receipt in native.receipts)


def test_delegate_action_keeps_the_delegated_call() -> None:
    action = native_of(record_of("tx_delegate")).actions[0]
    assert action.kind == "Delegate"
    assert action.delegate_sender_id == "agent1.ghspikeo50ekc.testnet"
    assert action.delegate_receiver_id == "guest-book.testnet"
    assert action.delegated_actions[0].method_name == "add_message"


def test_amount_includes_delegated_deposits() -> None:
    delegated = {
        "Delegate": {
            "delegate_action": {
                "sender_id": "a.near",
                "receiver_id": "b.near",
                "actions": [{"Transfer": {"deposit": "7"}}],
            }
        }
    }
    record = parse_transaction(with_actions(delegated, {"Transfer": {"deposit": "5"}}), NETWORK)
    assert record.amount == 12


def test_batch_with_create_account_and_add_key() -> None:
    native = native_of(record_of("tx_create_account"))
    kinds = [action.kind for action in native.actions]
    assert native.kind == "Batch"
    assert "CreateAccount" in kinds
    added = next(action for action in native.actions if action.kind == "AddKey")
    assert added.public_key is not None
    assert added.access_key is not None
    assert added.access_key.public_key == added.public_key


def test_deployment_keeps_the_code_hash() -> None:
    native = native_of(record_of("tx_deploy_contract"))
    deployment = next(action for action in native.actions if action.kind == "DeployContract")
    assert deployment.details == {"code": "YI/GB5mY3crqPGXUmKaUSQ+wyHzURtj2FsnuWq9WHmo="}


def test_whole_contract_binary_is_left_out() -> None:
    action = {"DeployContract": {"code": "A" * 1000}}
    native = native_of(parse_transaction(with_actions(action), NETWORK))
    assert native.actions[0].details is None


@pytest.mark.parametrize(
    ("action", "field", "value"),
    [
        ({"Stake": {"stake": "5", "public_key": "ed25519:k"}}, "stake", 5),
        ({"DeleteKey": {"public_key": "ed25519:k"}}, "public_key", "ed25519:k"),
        ({"DeleteAccount": {"beneficiary_id": "b.near"}}, "beneficiary_id", "b.near"),
    ],
)
def test_other_documented_actions(action: dict[str, Any], field: str, value: object) -> None:
    native = native_of(parse_transaction(with_actions(action), NETWORK))
    assert getattr(native.actions[0], field) == value


def test_add_key_with_a_function_call_permission() -> None:
    permission = {
        "FunctionCall": {"allowance": "9", "receiver_id": "c.near", "method_names": ["go"]}
    }
    action = {"AddKey": {"public_key": "ed25519:k", "access_key": {"permission": permission}}}
    native = native_of(parse_transaction(with_actions(action), NETWORK))
    key = native.actions[0].access_key
    assert key is not None
    assert (key.permission, key.allowance, key.receiver_id) == ("FunctionCall", 9, "c.near")
    assert key.method_names == ("go",)


def test_an_action_kind_not_yet_known_does_not_stop_a_sync() -> None:
    future = {"TransferToGasKey": {"public_key": "ed25519:k", "deposit": "5"}}
    native = native_of(parse_transaction(with_actions(future, "BareFutureAction"), NETWORK))
    assert native.actions[0].kind == "TransferToGasKey"
    assert native.actions[0].details == {"public_key": "ed25519:k", "deposit": "5"}
    assert native.actions[1].kind == "BareFutureAction"
    assert native.kind == "Batch"


def test_no_actions_at_all() -> None:
    assert native_of(parse_transaction(with_actions(), NETWORK)).kind == "NoActions"


@pytest.mark.parametrize("action", [5, {"Two": {}, "Keys": {}}, {}, None])
def test_malformed_action_is_reported(action: object) -> None:
    with pytest.raises(MalformedResponse, match="action"):
        parse_transaction(with_actions(action), NETWORK)


def test_malformed_action_fields_are_reported() -> None:
    with pytest.raises(MalformedResponse, match="Transfer"):
        parse_transaction(with_actions({"Transfer": {"deposit": "lots"}}), NETWORK)


def test_negative_gas_is_reported() -> None:
    outcome = replace(executed("tx_transfer").outcome, gas_burnt=-1)
    with pytest.raises(MalformedResponse, match="conversion_gas_burnt"):
        parse_transaction(replace(executed("tx_transfer"), outcome=outcome), NETWORK)


HOSTILE_ARGS = {
    "lone surrogate": b'{"a": "\\ud800"}',
    "not a number": b'{"a": NaN}',
    "infinity": b"[Infinity]",
    "binary": b"\x00\xff\xfe",
    "invalid utf-8": b'"\xff"',
    "too deep to parse": b"[" * 100_000 + b"]" * 100_000,
    "too deep to read back": b"[" * 150 + b"]" * 150,
    "just too deep": b"[" * (MAX_ARGS_NESTING + 1) + b"]" * (MAX_ARGS_NESTING + 1),
}


@pytest.mark.parametrize("args", HOSTILE_ARGS.values(), ids=HOSTILE_ARGS.keys())
def test_arguments_anyone_can_craft_stay_encoded(args: bytes) -> None:
    text = encoded(args)
    action = native_of(parse_transaction(with_actions(function_call(text)), NETWORK)).actions[0]
    assert action.args is None
    assert action.args_base64 == text


@pytest.mark.parametrize("args", HOSTILE_ARGS.values(), ids=HOSTILE_ARGS.keys())
def test_crafted_arguments_survive_a_round_trip_through_json(args: bytes) -> None:
    record = parse_transaction(with_actions(function_call(encoded(args))), NETWORK)
    assert NearTransaction.model_validate_json(record.native.model_dump_json()) == record.native
    json.dumps(record.native.model_dump())


def test_deepest_arguments_allowed_survive_a_round_trip() -> None:
    args = b"[" * MAX_ARGS_NESTING + b"]" * MAX_ARGS_NESTING
    record = parse_transaction(with_actions(function_call(encoded(args))), NETWORK)
    native = native_of(record)
    assert native.actions[0].args is not None
    assert NearTransaction.model_validate_json(native.model_dump_json()) == native


@pytest.mark.parametrize(
    ("args", "decoded"), [(b"5", 5), (b'"text"', "text"), (b"null", None), (b"[]", [])]
)
def test_plain_json_arguments_of_any_type_are_decoded(args: bytes, decoded: object) -> None:
    action = native_of(
        parse_transaction(with_actions(function_call(encoded(args))), NETWORK)
    ).actions[0]
    assert action.args == decoded
    assert action.args_base64 is None


def test_empty_and_invalid_base64_arguments() -> None:
    empty = native_of(parse_transaction(with_actions(function_call("")), NETWORK)).actions[0]
    assert empty.args is None
    assert empty.args_base64 is None
    invalid = native_of(parse_transaction(with_actions(function_call("!!")), NETWORK)).actions[0]
    assert invalid.args_base64 == "!!"


def test_receipt_not_yet_executed_leaves_the_transaction_pending() -> None:
    full = executed("tx_transfer")
    first = full.outcome.status["SuccessReceiptId"]  # type: ignore[index,call-overload]
    without_first = tuple(r for r in full.receipts if r.receipt_id != first)
    record = parse_transaction(replace(full, receipts=without_first), NETWORK)
    assert record.status == Status.PENDING
    assert native_of(record).error is None


@pytest.mark.parametrize("status", ["Unknown", "Started", {"Odd": 1}, None])
def test_outcome_with_no_result_yet_is_pending(status: Any) -> None:
    full = executed("tx_transfer")
    outcome = replace(full.outcome, status=status)
    record = parse_transaction(replace(full, outcome=outcome), NETWORK)
    assert record.status == Status.PENDING


def test_receipt_status_still_unknown_is_pending() -> None:
    full = executed("tx_transfer")
    receipt = full.receipts[0]
    unknown = replace(receipt, outcome=replace(receipt.outcome, status="Unknown"))
    record = parse_transaction(replace(full, receipts=(unknown, *full.receipts[1:])), NETWORK)
    assert native_of(record).receipts[0].status == "pending"


def test_a_cycle_in_a_malformed_reply_cannot_hang_the_parser() -> None:
    full = executed("tx_transfer")
    receipt = full.receipts[0]
    looped = replace(
        receipt, outcome=replace(receipt.outcome, status={"SuccessReceiptId": receipt.receipt_id})
    )
    outcome = replace(full.outcome, status={"SuccessReceiptId": receipt.receipt_id})
    record = parse_transaction(replace(full, outcome=outcome, receipts=(looped,)), NETWORK)
    assert record.status == Status.PENDING


def test_account_with_function_call_keys() -> None:
    record = parse_account(
        fixture("account_named")["result"],
        fixture("access_keys_function_call")["result"],
        SHARD_LAYOUT,
        "calmhare3618.testnet",
        NETWORK,
    )
    native = record.native
    assert record.balance == int(fixture("account_named")["result"]["amount"])
    assert native.kind == "named"
    assert native.exists
    assert not native.has_contract
    permissions = {key.permission for key in native.access_keys}
    assert permissions == {"FullAccess", "FunctionCall"}
    limited = next(key for key in native.access_keys if key.permission == "FunctionCall")
    assert limited.receiver_id == "meteor-account-verifier.testnet"
    assert limited.allowance == 0


def test_implicit_account() -> None:
    account = "f781edbfe07287152e3912cb698af4155234df3c2d9c23bf625f675b8f6de5fa"
    record = parse_account(
        fixture("account_implicit")["result"],
        fixture("access_keys_implicit")["result"],
        SHARD_LAYOUT,
        account,
        NETWORK,
    )
    assert record.native.kind == "near-implicit"
    assert record.native.exists
    assert [key.permission for key in record.native.access_keys] == ["FullAccess"]


def test_account_nobody_has_created_has_no_balance() -> None:
    account = "a" * 64
    record = parse_account(None, None, SHARD_LAYOUT, account, NETWORK)
    assert record.balance == 0
    assert not record.native.exists
    assert record.native.kind == "near-implicit"
    assert record.native.access_keys == ()


def test_access_key_of_a_kind_not_yet_known_is_kept() -> None:
    keys = {
        "keys": [
            {"public_key": "ed25519:k", "access_key": {"nonce": 1, "permission": {"GasKey": {}}}}
        ]
    }
    record = parse_account(
        fixture("account_named")["result"], keys, SHARD_LAYOUT, "ab.testnet", NETWORK
    )
    assert record.native.access_keys[0].permission == "GasKey"


# The shards NearBlocks reports for transactions signed by these accounts on testnet.
@pytest.mark.parametrize(
    ("account", "shard"), [("calmhare3618.testnet", 12), ("nkeytest1793742888.testnet", 4)]
)
def test_shard_matches_what_an_explorer_reports(account: str, shard: int) -> None:
    assert parse_account(None, None, SHARD_LAYOUT, account, NETWORK).native.shard_id == shard


@pytest.mark.parametrize(
    ("account", "index"),
    [
        ("0", 0),
        ("650", 1),
        ("aurora", 2),
        ("aurora-0", 3),
        ("zzz.testnet", 9),
    ],
)
def test_account_on_a_boundary_belongs_to_the_shard_after_it(account: str, index: int) -> None:
    shard_ids = SHARD_LAYOUT["V3"]["shard_ids"]
    assert (
        parse_account(None, None, SHARD_LAYOUT, account, NETWORK).native.shard_id
        == (shard_ids[index])
    )


@pytest.mark.parametrize(
    "layout",
    [
        None,
        {},
        {"V0": {"num_shards": 1, "version": 0}},
        {"V1": {"boundary_accounts": ["m"]}},
        {"V2": {"boundary_accounts": ["m"], "shard_ids": [1]}, "Other": {}},
        {"V3": {"boundary_accounts": ["a", "b"], "shard_ids": [1]}},
    ],
)
def test_layout_without_usable_boundaries_gives_no_shard(layout: Any) -> None:
    assert parse_account(None, None, layout, "zz.testnet", NETWORK).native.shard_id is None


def test_malformed_account_state_is_reported() -> None:
    with pytest.raises(MalformedResponse, match="view_account"):
        parse_account({"amount": "x"}, {"keys": []}, None, "ab", NETWORK)
    with pytest.raises(MalformedResponse, match="view_access_key_list"):
        parse_account(fixture("account_named")["result"], {"nope": 1}, None, "ab", NETWORK)
    with pytest.raises(MalformedResponse, match="permission"):
        parse_account(
            fixture("account_named")["result"],
            {"keys": [{"public_key": "k", "access_key": {"permission": 5}}]},
            None,
            "ab",
            NETWORK,
        )
    with pytest.raises(MalformedResponse, match="permission"):
        parse_account(
            fixture("account_named")["result"],
            {"keys": [{"public_key": "k", "access_key": {"permission": {"FunctionCall": {}}}}]},
            None,
            "ab",
            NETWORK,
        )


def test_contract_has_its_code_hash() -> None:
    state = fixture("account_contract")["result"]
    record = parse_contract(state, "guest-book.testnet", NETWORK)
    assert record.id == "guest-book.testnet"
    assert record.creator is None
    assert record.native.code_hash == state["code_hash"]
    assert record.native.storage_usage == state["storage_usage"]


def test_account_without_code_is_not_a_contract() -> None:
    with pytest.raises(NotFound, match="no contract"):
        parse_contract(fixture("account_named")["result"], "ab.testnet", NETWORK)


def test_account_using_a_global_contract_is_a_contract() -> None:
    state = {**fixture("account_named")["result"], "global_contract_account_id": "code.near"}
    record = parse_contract(state, "ab.testnet", NETWORK)
    assert record.native.global_contract_account_id == "code.near"
    assert parse_account(state, {"keys": []}, None, "ab.testnet", NETWORK).native.has_contract


def test_metrics_cover_fees_gas_calls_and_receipts() -> None:
    records = [record_of(name) for name in TX_FIXTURES]
    metrics = near_metrics(records)
    natives = [native_of(record) for record in records]

    assert metrics["fees_yoctonear"] == sum(record.fee for record in records)
    assert metrics["fees_near"].startswith("0.")
    assert metrics["gas_burnt"]["gas"] == sum(native.gas_burnt for native in natives)
    assert metrics["gas_burnt"]["tgas"] == metrics["gas_burnt"]["gas"] / 10**12

    calls = metrics["function_calls_by_method"]
    assert calls == sorted(calls, key=lambda row: (-row["calls"], row["method"]))
    assert metrics["top_contracts_by_calls"][0]["calls"] >= 1

    receipts = metrics["receipts"]
    real = [r for native in natives for r in native.receipts if not r.is_refund]
    assert receipts["total"] == len(real)
    assert receipts["failed"] == sum(1 for r in real if r.status == "failure")
    assert receipts["succeeded"] + receipts["failed"] == receipts["total"]
    assert receipts["failure_rate"] + receipts["success_rate"] == pytest.approx(1)
    assert receipts["failed"] >= 2


def test_metrics_with_no_records() -> None:
    metrics = near_metrics([])
    assert metrics["fees_near"] == "0"
    assert metrics["receipts"]["failure_rate"] is None
    assert metrics["top_contracts_by_calls"] == []


def test_whole_near_of_an_exact_amount_has_no_trailing_zeros() -> None:
    record = record_of("tx_transfer").model_copy(update={"fee": 10**24})
    assert near_metrics([record])["fees_near"] == "1"
    half = record.model_copy(update={"fee": 5 * 10**23})
    assert near_metrics([half])["fees_near"] == "0.5"


def test_fixtures_were_scrubbed() -> None:
    for path in Path(NEAR_FIXTURES).glob("tx_*.json"):
        text = path.read_text(encoding="utf-8")
        assert '"signature"' not in text
        raw = json.loads(text)["transaction"]
        for receipt in raw["receipts"]:
            logs = receipt["execution_outcome"]["outcome"]["logs"]
            assert set(logs) <= {"redacted log"}
