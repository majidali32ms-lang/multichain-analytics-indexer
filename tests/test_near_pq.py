from __future__ import annotations

from typing import Any

import pytest

from multichain_indexer.adapters.near.adapter import _describe_account, _describe_transaction
from multichain_indexer.adapters.near.fastnear import _executed_transaction, _RawTransaction
from multichain_indexer.adapters.near.metrics import near_metrics
from multichain_indexer.adapters.near.models import NearAccount, NearTransaction
from multichain_indexer.adapters.near.parser import parse_account, parse_transaction
from multichain_indexer.adapters.near.pq import account_pq, key_pq, transaction_pq
from multichain_indexer.core.models import TransactionRecord
from multichain_indexer.pq import PqSignature
from tests.conftest import load_near_fixture as fixture

ML_DSA_ACCOUNT = "zf-b6bc410f46.testnet"
SHARD_LAYOUT = fixture("rpc_shard_layout")["result"]["shard_layout"]


def record_of(name: str) -> TransactionRecord:
    raw = fixture(name)["transaction"]
    return parse_transaction(_executed_transaction(_RawTransaction.model_validate(raw)), "testnet")


def native_of(name: str) -> NearTransaction:
    native = record_of(name).native
    assert isinstance(native, NearTransaction)
    return native


def account_of(state: str, keys: str, account: str) -> NearAccount:
    record = parse_account(
        fixture(state)["result"], fixture(keys)["result"], SHARD_LAYOUT, account, "testnet"
    )
    assert isinstance(record.native, NearAccount)
    return record.native


def test_transaction_signed_with_an_ml_dsa_key_is_found() -> None:
    raw: dict[str, Any] = fixture("tx_function_call")["transaction"]["transaction"]
    assert raw["public_key"].startswith("ml-dsa-65:")
    pq = native_of("tx_function_call").pq
    assert pq == PqSignature(scheme="ml-dsa-65", evidence="signed with an ml-dsa-65 key")


@pytest.mark.parametrize("name", ["tx_transfer", "tx_delegate", "tx_mainnet", "tx_failed"])
def test_transactions_signed_with_ed25519_are_not_post_quantum(name: str) -> None:
    raw: dict[str, Any] = fixture(name)["transaction"]["transaction"]
    assert raw["public_key"].startswith("ed25519:")
    assert native_of(name).pq is None


def test_account_whose_keys_are_listed_by_their_ml_dsa_hash() -> None:
    native = account_of("account_ml_dsa", "access_keys_ml_dsa", ML_DSA_ACCOUNT)
    assert all(key.public_key.startswith("ml-dsa-65-hash:") for key in native.access_keys)
    assert all(key.pq is not None for key in native.access_keys)
    count = len(native.access_keys)
    assert native.pq == PqSignature(
        scheme="ml-dsa-65", evidence=f"{count} of {count} access keys use ml-dsa-65"
    )


def test_account_with_only_an_ed25519_key_is_not_post_quantum() -> None:
    implicit = "f781edbfe07287152e3912cb698af4155234df3c2d9c23bf625f675b8f6de5fa"
    native = account_of("account_implicit", "access_keys_implicit", implicit)
    assert [key.public_key.split(":")[0] for key in native.access_keys] == ["ed25519"]
    assert native.access_keys[0].pq is None
    assert native.pq is None


def test_real_account_with_both_kinds_of_key() -> None:
    """calmhare3618.testnet holds eight ed25519 keys and one ML-DSA-65 key."""
    native = account_of("account_named", "access_keys_function_call", "calmhare3618.testnet")
    post_quantum = [key for key in native.access_keys if key.pq is not None]
    assert len(native.access_keys) == 9
    assert [key.public_key.split(":")[0] for key in post_quantum] == ["ml-dsa-65-hash"]
    assert native.pq == PqSignature(scheme="ml-dsa-65", evidence="1 of 9 access keys use ml-dsa-65")


def test_account_nobody_created_is_not_post_quantum() -> None:
    native = parse_account(None, None, SHARD_LAYOUT, "a" * 64, "testnet").native
    assert isinstance(native, NearAccount)
    assert native.pq is None


@pytest.mark.parametrize(
    ("key", "evidence"),
    [
        ("ml-dsa-65:7Bg5Wqqoz", "ml-dsa-65 public key"),
        ("ml-dsa-65-hash:2A4BC96GM", "key listed by its ml-dsa-65 hash"),
    ],
)
def test_both_forms_of_an_ml_dsa_key(key: str, evidence: str) -> None:
    assert key_pq(key) == PqSignature(scheme="ml-dsa-65", evidence=evidence)


@pytest.mark.parametrize(
    "key", ["ed25519:5GEVLfwg", "secp256k1:abc", "ml-dsa-87:abc", "ml-dsa-65", ""]
)
def test_other_keys_are_not_post_quantum(key: str) -> None:
    assert key_pq(key) is None
    assert transaction_pq(key) is None


def test_account_with_a_mix_of_keys_counts_the_post_quantum_ones() -> None:
    found = key_pq("ml-dsa-65:abc")
    summary = account_pq([found, None, None])
    assert summary == PqSignature(scheme="ml-dsa-65", evidence="1 of 3 access keys use ml-dsa-65")
    assert account_pq([None, None]) is None
    assert account_pq([]) is None


def test_metrics_report_the_post_quantum_share() -> None:
    records = [record_of(name) for name in ("tx_function_call", "tx_transfer", "tx_delegate")]
    assert near_metrics(records)["post_quantum"] == {
        "transactions": 1,
        "share": 1 / 3,
        "schemes": {"ml-dsa-65": 1},
    }


def test_describe_says_what_was_found() -> None:
    assert _describe_transaction(record_of("tx_function_call"))["Post-quantum"] == "ml-dsa-65"
    assert _describe_transaction(record_of("tx_transfer"))["Post-quantum"] == "no"
    account = parse_account(
        fixture("account_ml_dsa")["result"],
        fixture("access_keys_ml_dsa")["result"],
        SHARD_LAYOUT,
        ML_DSA_ACCOUNT,
        "testnet",
    )
    assert "use ml-dsa-65" in _describe_account(account)["Post-quantum"]


def test_records_stored_before_detection_still_load() -> None:
    stored = native_of("tx_transfer").model_dump(mode="json")
    stored.pop("pq")
    assert NearTransaction.model_validate(stored).pq is None
