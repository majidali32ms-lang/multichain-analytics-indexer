from __future__ import annotations

import copy

import pytest

from multichain_indexer.adapters.algorand.adapter import (
    PROFILE,
    _describe_account,
    _describe_transaction,
)
from multichain_indexer.adapters.algorand.metrics import algorand_metrics
from multichain_indexer.adapters.algorand.models import AlgorandAccount, AlgorandTransaction
from multichain_indexer.adapters.algorand.parser import parse_account, parse_transaction
from multichain_indexer.adapters.algorand.pq import account_pq, transaction_pq
from tests.conftest import load_fixture


def native_of(name: str, network: str) -> AlgorandTransaction:
    native = parse_transaction(load_fixture(name), network).native
    assert isinstance(native, AlgorandTransaction)
    return native


@pytest.mark.parametrize("network", ["mainnet", "testnet"])
def test_native_falcon_transaction_is_found(network: str) -> None:
    raw = load_fixture(f"tx_pqsig_{network}")
    assert raw["signature"]["pqsig"]["scheme"] == "f1"  # type: ignore[index]
    pq = native_of(f"tx_pqsig_{network}", network).pq
    assert pq is not None
    assert pq.scheme == "falcon-1024"
    assert pq.evidence == "signed with pqsig, scheme f1"


@pytest.mark.parametrize(
    ("name", "network"),
    [("tx_sig", "testnet"), ("tx_lsig", "mainnet"), ("tx_pay", "testnet"), ("tx_stpf", "testnet")],
)
def test_ordinary_signatures_are_not_post_quantum(name: str, network: str) -> None:
    assert native_of(name, network).pq is None


def test_state_proof_never_counts_even_with_a_pqsig() -> None:
    raw = copy.deepcopy(load_fixture("tx_stpf"))
    raw["signature"] = {"pqsig": {"scheme": "f1"}}
    native = parse_transaction(raw, "testnet").native
    assert isinstance(native, AlgorandTransaction)
    assert native.tx_type == "stpf"
    assert native.pq is None


def test_scheme_not_enabled_is_shown_as_given_not_named() -> None:
    pq = transaction_pq("pay", "f5")
    assert pq is not None
    assert pq.scheme == "pqsig scheme f5"
    assert transaction_pq("pay", None) is None


@pytest.mark.parametrize("network", ["mainnet", "testnet"])
def test_account_listed_as_pqsig_is_post_quantum(network: str) -> None:
    native = parse_account(load_fixture(f"account_pqsig_{network}"), network).native
    assert isinstance(native, AlgorandAccount)
    assert native.pq is not None
    assert native.pq.scheme == "pqsig"


@pytest.mark.parametrize("signature_type", ["sig", "msig", "lsig", None])
def test_other_accounts_are_not_post_quantum(signature_type: str | None) -> None:
    assert account_pq(signature_type) is None


def test_ordinary_account_fixture_is_not_post_quantum() -> None:
    native = parse_account(load_fixture("account"), "testnet").native
    assert isinstance(native, AlgorandAccount)
    assert native.pq is None


def test_metrics_report_the_post_quantum_share() -> None:
    records = [
        parse_transaction(load_fixture("tx_pqsig_testnet"), "testnet"),
        parse_transaction(load_fixture("tx_sig"), "testnet"),
        parse_transaction(load_fixture("tx_pay"), "testnet"),
        parse_transaction(load_fixture("tx_stpf"), "testnet"),
    ]
    assert algorand_metrics(records)["post_quantum"] == {
        "transactions": 1,
        "share": 0.25,
        "schemes": {"falcon-1024": 1},
    }


def test_describe_names_the_scheme() -> None:
    falcon = parse_transaction(load_fixture("tx_pqsig_mainnet"), "mainnet")
    ordinary = parse_transaction(load_fixture("tx_sig"), "testnet")
    assert _describe_transaction(falcon)["Post-quantum"] == "falcon-1024"
    assert _describe_transaction(ordinary)["Post-quantum"] == "no"
    account = parse_account(load_fixture("account_pqsig_mainnet"), "mainnet")
    assert _describe_account(account)["Post-quantum"] == "yes, pqsig"
    assert (
        _describe_account(parse_account(load_fixture("account"), "testnet"))["Post-quantum"] == "no"
    )


def test_records_stored_before_detection_still_load() -> None:
    native = native_of("tx_pay", "testnet")
    stored = native.model_dump(mode="json")
    stored.pop("pq")
    assert AlgorandTransaction.model_validate(stored).pq is None
    assert PROFILE.transaction_model is AlgorandTransaction
