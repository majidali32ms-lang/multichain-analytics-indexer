from __future__ import annotations

import base64
import copy
import json
from typing import Any, cast

import base58
import pytest

from multichain_indexer.adapters.solana.metrics import solana_metrics
from multichain_indexer.adapters.solana.models import (
    SolanaAccount,
    SolanaProgram,
    SolanaTransaction,
    SplMint,
)
from multichain_indexer.adapters.solana.parser import (
    LAMPORTS_PER_SIGNATURE,
    PRECOMPILE_PROGRAMS,
    SYSTEM_PROGRAM,
    parse_account,
    parse_block,
    parse_mint,
    parse_program,
    parse_signature_rows,
    parse_transaction,
    program_data_address,
)
from multichain_indexer.core.errors import InvalidInput, MalformedResponse
from multichain_indexer.core.models import Status, TransactionRecord
from tests.conftest import load_solana_fixture as fixture

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

ALL_TRANSACTIONS = [
    "tx_sol_transfer",
    "tx_spl_transfer",
    "tx_token2022_transfer",
    "tx_inner_instructions",
    "tx_failed",
    "tx_legacy",
    "tx_legacy_compute_budget",
    "tx_v0",
    "tx_v0_compute_budget",
    "tx_v1",
    "tx_v1_priority_fee",
    "tx_precompile_legacy",
    "tx_precompile_v0",
    "tx_precompile_v1",
    "tx_price_without_limit",
    "tx_limit_above_cap",
    "history_0",
    "history_1",
    "history_2",
    "history_3",
]
PRECOMPILE_TRANSACTIONS = ["tx_precompile_legacy", "tx_precompile_v0", "tx_precompile_v1"]


def parse(name: str, confirmation: str = "finalized") -> tuple[dict[str, Any], TransactionRecord]:
    raw = fixture(name)
    return raw, parse_transaction(raw, "devnet", confirmation)


def native_of(record: TransactionRecord) -> SolanaTransaction:
    return cast(SolanaTransaction, record.native)


@pytest.mark.parametrize("name", ALL_TRANSACTIONS)
def test_every_real_transaction_keeps_the_shared_fields(name: str) -> None:
    raw, record = parse(name)
    message = raw["transaction"]["message"]
    assert record.id == raw["transaction"]["signatures"][0]
    assert record.sender == message["accountKeys"][0]["pubkey"]
    assert record.time.timestamp() == raw["blockTime"]
    assert record.fee == raw["meta"]["fee"]
    assert (record.chain, record.network) == ("solana", "devnet")
    assert native_of(record).slot == raw["slot"]
    assert native_of(record).version == str(raw["version"])


@pytest.mark.parametrize(
    ("name", "version"), [("tx_legacy", "legacy"), ("tx_v0", "0"), ("tx_v1", "1")]
)
def test_all_three_transaction_versions_are_read(name: str, version: str) -> None:
    assert native_of(parse(name)[1]).version == version


def test_v1_reads_limits_from_transaction_config() -> None:
    raw, record = parse("tx_v1_priority_fee")
    config = raw["transaction"]["message"]["transactionConfig"]
    native = native_of(record)
    assert native.compute_unit_limit == config["computeUnitLimit"]
    assert native.priority_fee_lamports == config["priorityFee"]
    assert native.priority_fee_lamports > 0
    assert native.loaded_accounts_data_size_limit == config["loadedAccountsDataSizeLimit"]
    assert native.compute_unit_price_micro_lamports is None
    assert native.address_lookup_tables == ()


def test_v1_priority_fee_is_a_total_not_a_price() -> None:
    raw, record = parse("tx_v1_priority_fee")
    signatures = len(raw["transaction"]["signatures"])
    assert (
        record.fee == LAMPORTS_PER_SIGNATURE * signatures + native_of(record).priority_fee_lamports
    )


@pytest.mark.parametrize("name", ["tx_legacy_compute_budget", "tx_v0_compute_budget"])
def test_legacy_and_v0_work_out_the_priority_fee_in_lamports(name: str) -> None:
    raw, record = parse(name)
    native = native_of(record)
    assert native.compute_unit_limit is not None
    assert native.compute_unit_price_micro_lamports is not None
    assert "transactionConfig" not in raw["transaction"]["message"]
    expected = -(-native.compute_unit_limit * native.compute_unit_price_micro_lamports // 1_000_000)
    assert native.priority_fee_lamports == expected
    assert record.fee == LAMPORTS_PER_SIGNATURE + expected


def test_v0_reports_its_address_lookup_tables() -> None:
    raw, record = parse("tx_v0")
    lookups = raw["transaction"]["message"]["addressTableLookups"]
    assert native_of(record).address_lookup_tables == tuple(x["accountKey"] for x in lookups)
    assert lookups


@pytest.mark.parametrize("name", ALL_TRANSACTIONS)
def test_fee_is_always_signatures_including_precompiles_plus_priority_fee(name: str) -> None:
    """The formula holds on every real transaction, of every version."""
    _, record = parse(name)
    native = native_of(record)
    paid_for = native.signature_count + native.precompile_signature_count
    assert record.fee == LAMPORTS_PER_SIGNATURE * paid_for + native.priority_fee_lamports


@pytest.mark.parametrize("name", PRECOMPILE_TRANSACTIONS)
def test_precompile_signatures_are_the_cause_of_the_fee_mismatch(name: str) -> None:
    """Counting only the signers leaves exactly 5000 lamports per precompile signature."""
    raw, record = parse(name)
    native = native_of(record)
    signers = len(raw["transaction"]["signatures"])

    from_signers_alone = LAMPORTS_PER_SIGNATURE * signers + native.priority_fee_lamports
    unexplained = record.fee - from_signers_alone

    stated_in_instructions = sum(
        base58.b58decode(instruction["data"])[0]
        for instruction in raw["transaction"]["message"]["instructions"]
        if instruction.get("programId") in PRECOMPILE_PROGRAMS
    )
    assert stated_in_instructions > 0
    assert unexplained == LAMPORTS_PER_SIGNATURE * stated_in_instructions
    assert native.precompile_signature_count == stated_in_instructions


def test_transactions_without_precompiles_match_on_signers_alone() -> None:
    for name in ("tx_sol_transfer", "tx_legacy_compute_budget", "tx_v0_compute_budget", "tx_v1"):
        raw, record = parse(name)
        assert native_of(record).precompile_signature_count == 0
        signers = len(raw["transaction"]["signatures"])
        assert (
            record.fee == LAMPORTS_PER_SIGNATURE * signers + native_of(record).priority_fee_lamports
        )


def test_price_without_a_limit_is_read_back_from_the_fee() -> None:
    raw, record = parse("tx_price_without_limit")
    native = native_of(record)
    assert native.compute_unit_limit is None
    assert native.compute_unit_price_micro_lamports
    assert native.priority_fee_lamports == raw["meta"]["fee"] - LAMPORTS_PER_SIGNATURE
    # The node's default limit is at least 200,000 compute units for a program call.
    implied_limit = (
        native.priority_fee_lamports * 1_000_000 / native.compute_unit_price_micro_lamports
    )
    assert implied_limit >= 200_000


def test_priority_fee_is_charged_on_the_capped_compute_unit_limit() -> None:
    """A real mainnet transaction: price 500 and a limit above the 1,400,000 cap."""
    _, record = parse("tx_limit_above_cap")
    native = native_of(record)
    assert native.compute_unit_limit == 1_436_872
    assert native.compute_unit_price_micro_lamports == 500
    assert native.priority_fee_lamports == 700  # not 719, which the uncapped limit gives
    assert record.fee == LAMPORTS_PER_SIGNATURE * native.signature_count + 700


def test_no_compute_budget_means_no_priority_fee() -> None:
    _, record = parse("tx_token2022_transfer")
    assert native_of(record).priority_fee_lamports == 0
    assert native_of(record).compute_unit_limit is None


def test_block_containing_a_v1_transaction_is_read() -> None:
    saved = fixture("block_with_v1")
    records = parse_block(saved["block"], saved["slot"], "devnet", "finalized")
    versions = {native_of(record).version for record in records}
    assert {"legacy", "0", "1"} <= versions
    assert len(records) == len(saved["block"]["transactions"])
    assert all(native_of(record).slot == saved["slot"] for record in records)


def test_block_without_a_time_is_refused() -> None:
    saved = fixture("block_with_v1")
    saved["block"]["blockTime"] = None
    with pytest.raises(MalformedResponse, match="block time"):
        parse_block(saved["block"], saved["slot"], "devnet", "finalized")


def test_sol_transfer_fields() -> None:
    raw, record = parse("tx_sol_transfer")
    transfer = next(
        ix["parsed"]["info"]
        for ix in raw["transaction"]["message"]["instructions"]
        if ix.get("programId") == SYSTEM_PROGRAM
    )
    assert record.receiver == transfer["destination"]
    assert record.amount == transfer["lamports"]
    assert native_of(record).kind == "sol-transfer"
    assert native_of(record).amount_mint is None


def test_spl_transfer_finds_its_mint_from_the_token_balances() -> None:
    raw, record = parse("tx_spl_transfer")
    native = native_of(record)
    transfer = native.token_transfers[0]
    assert transfer.token_program == TOKEN_PROGRAM
    assert transfer.mint == raw["meta"]["postTokenBalances"][0]["mint"]
    assert record.amount == transfer.amount
    assert record.receiver == transfer.destination
    assert native.amount_mint == transfer.mint


def test_token_2022_transfer_is_told_apart() -> None:
    _, record = parse("tx_token2022_transfer")
    assert native_of(record).token_transfers[0].token_program == TOKEN_2022_PROGRAM


def test_inner_instructions_are_kept_and_marked() -> None:
    raw, record = parse("tx_inner_instructions")
    inner = [i for i in native_of(record).instructions if i.inner]
    expected = sum(len(group["instructions"]) for group in raw["meta"]["innerInstructions"])
    assert len(inner) == expected > 0


def test_failed_transaction_keeps_its_error() -> None:
    raw, record = parse("tx_failed")
    assert record.status is Status.FAILED
    assert json.loads(native_of(record).error or "") == raw["meta"]["err"]


def test_confirmed_transactions_are_pending_until_finalized() -> None:
    assert parse("tx_sol_transfer", "finalized")[1].status is Status.FINAL
    assert parse("tx_sol_transfer", "confirmed")[1].status is Status.PENDING
    assert parse("tx_failed", "confirmed")[1].status is Status.PENDING
    assert native_of(parse("tx_failed", "confirmed")[1]).error


def test_plain_spl_transfer_without_a_known_mint_or_amount() -> None:
    raw = fixture("tx_token2022_transfer")
    instruction = raw["transaction"]["message"]["instructions"][0]
    instruction.clear()
    instruction.update(
        {
            "program": "spl-token",
            "programId": TOKEN_PROGRAM,
            "parsed": {
                "type": "transfer",
                "info": {"source": "A", "destination": "B", "amount": "9"},
            },
        }
    )
    raw["meta"]["innerInstructions"] = []
    raw["meta"]["preTokenBalances"] = raw["meta"]["postTokenBalances"] = []
    record = parse_transaction(raw, "devnet", "finalized")
    assert native_of(record).kind == "spl-transfer"
    assert native_of(record).token_transfers[0].mint is None
    assert record.amount == 9

    del instruction["parsed"]["info"]["amount"]
    with pytest.raises(MalformedResponse, match="no amount"):
        parse_transaction(raw, "devnet", "finalized")


@pytest.mark.parametrize(
    ("programs", "kind"),
    [
        (["Vote111111111111111111111111111111111111111"], "vote"),
        ([], "no-instructions"),
        (["ComputeBudget111111111111111111111111111111"], "no-instructions"),
    ],
)
def test_kind_of_unusual_transactions(programs: list[str], kind: str) -> None:
    raw = fixture("tx_v1")
    raw["transaction"]["message"]["instructions"] = [
        {"programId": program, "data": base58.b58encode(b"\x09").decode(), "accounts": []}
        for program in programs
    ]
    assert native_of(parse_transaction(raw, "devnet", "finalized")).kind == kind


def test_memo_instruction_with_a_text_parsed_form_is_accepted() -> None:
    raw = fixture("tx_v1")
    raw["transaction"]["message"]["instructions"] = [
        {
            "program": "spl-memo",
            "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
            "parsed": "",
        }
    ]
    instruction = native_of(parse_transaction(raw, "devnet", "finalized")).instructions[0]
    assert (instruction.program, instruction.kind) == ("spl-memo", None)


def mutated(name: str, change: Any) -> dict[str, Any]:
    raw = copy.deepcopy(fixture(name))
    change(raw)
    return raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (mutated("tx_v1", lambda r: r.update(slot=None)), "no slot"),
        (mutated("tx_v1", lambda r: r.update(blockTime=None)), "block time"),
        (mutated("tx_v1", lambda r: r["meta"].update(fee="lots")), "meta.fee"),
        (mutated("tx_v1", lambda r: r.pop("meta")), "meta"),
        (mutated("tx_v1", lambda r: r["transaction"].update(signatures=[])), "without signatures"),
        (
            mutated(
                "tx_legacy_compute_budget",
                lambda r: r["transaction"]["message"]["instructions"][0].update(data="0OIl"),
            ),
            "not base58",
        ),
        (
            mutated(
                "tx_legacy_compute_budget",
                lambda r: r["transaction"]["message"]["instructions"][0].update(data=""),
            ),
            "has no data",
        ),
    ],
)
def test_malformed_transactions_are_refused(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(MalformedResponse, match=message):
        parse_transaction(raw, "devnet", "finalized")


def test_malformed_message_names_fields_not_values() -> None:
    raw = mutated("tx_v1", lambda r: r["meta"].update(fee="secret-value"))
    with pytest.raises(MalformedResponse) as caught:
        parse_transaction(raw, "devnet", "finalized")
    assert "secret-value" not in str(caught.value)


def test_signature_rows() -> None:
    saved = fixture("signatures_first_page")
    rows = parse_signature_rows(saved["rows"])
    assert [row.signature for row in rows] == [row["signature"] for row in saved["rows"]]
    assert rows[0].confirmation == "finalized"
    with pytest.raises(MalformedResponse, match="expected a list"):
        parse_signature_rows({"rows": []})


def test_account_fields() -> None:
    raw = fixture("account")
    record = parse_account(raw, [fixture("token_accounts")], "ADDRESS", "devnet")
    native = cast(SolanaAccount, record.native)
    assert record.balance == raw["value"]["lamports"]
    assert (record.chain, record.network) == ("solana", "devnet")
    assert native.exists
    assert native.owner == raw["value"]["owner"]
    assert native.slot == raw["context"]["slot"]


def test_token_accounts_are_listed() -> None:
    token_accounts = {
        "value": [
            {
                "pubkey": "TOKENACCOUNT",
                "account": {
                    "lamports": 1,
                    "owner": TOKEN_2022_PROGRAM,
                    "data": {
                        "program": "spl-token-2022",
                        "parsed": {
                            "type": "account",
                            "info": {
                                "mint": "MINT",
                                "state": "initialized",
                                "tokenAmount": {"amount": "12", "decimals": 2},
                            },
                        },
                    },
                },
            }
        ]
    }
    record = parse_account(fixture("account"), [token_accounts], "ADDRESS", "devnet")
    held = cast(SolanaAccount, record.native).token_accounts[0]
    assert (held.address, held.mint, held.amount, held.decimals) == ("TOKENACCOUNT", "MINT", 12, 2)
    assert held.token_program == TOKEN_2022_PROGRAM


def test_undecoded_token_account_is_refused() -> None:
    broken = {
        "value": [{"pubkey": "X", "account": {"lamports": 1, "owner": "O", "data": ["", "base64"]}}]
    }
    with pytest.raises(MalformedResponse, match="did not decode"):
        parse_account(fixture("account"), [broken], "ADDRESS", "devnet")


def test_address_that_never_held_lamports_has_zero_balance() -> None:
    record = parse_account({"context": {"slot": 5}, "value": None}, [], "ADDRESS", "devnet")
    assert record.balance == 0
    assert not cast(SolanaAccount, record.native).exists


def test_mint_keeps_authorities_in_native_and_has_no_creator() -> None:
    raw = fixture("mint")
    info = raw["value"]["data"]["parsed"]["info"]
    record = parse_mint(raw, "MINT", "devnet")
    native = cast(SplMint, record.native)
    assert record.creator is None
    assert record.decimals == info["decimals"]
    assert record.total == int(info["supply"])
    assert native.mint_authority == info["mintAuthority"]
    assert native.freeze_authority == info["freezeAuthority"]
    assert native.token_program == TOKEN_PROGRAM


def test_token_2022_mint_reads_metadata_extension() -> None:
    raw = fixture("mint_token2022")
    assert (
        cast(SplMint, parse_mint(raw, "MINT", "devnet").native).token_program == TOKEN_2022_PROGRAM
    )

    raw["value"]["data"]["parsed"]["info"]["extensions"] = [
        {"extension": "transferFeeConfig", "state": {}},
        {"extension": "tokenMetadata", "state": {"name": "Coin", "symbol": "CN", "uri": 7}},
    ]
    record = parse_mint(raw, "MINT", "devnet")
    assert (record.name, record.unit) == ("Coin", "CN")
    assert cast(SplMint, record.native).extensions == ("transferFeeConfig", "tokenMetadata")


def test_account_that_is_not_a_mint_is_refused() -> None:
    with pytest.raises(InvalidInput, match="not an SPL token mint"):
        parse_mint(fixture("not_a_mint"), "X", "devnet")
    with pytest.raises(InvalidInput, match="no account"):
        parse_mint({"context": {"slot": 1}, "value": None}, "X", "devnet")


def test_upgradeable_program_reads_its_program_data_header() -> None:
    info = fixture("program")
    header = fixture("program_data")
    record = parse_program(info, header, "PROGRAM", "devnet")
    native = cast(SolanaProgram, record.native)
    assert record.creator is None
    assert native.program_data == info["value"]["data"]["parsed"]["info"]["programData"]
    decoded = base64.b64decode(header["value"]["data"][0])
    assert native.last_deployed_slot == int.from_bytes(decoded[4:12], "little")
    assert native.upgrade_authority is None


def test_program_with_an_upgrade_authority() -> None:
    key = bytes(range(32))
    data = (3).to_bytes(4, "little") + (77).to_bytes(8, "little") + b"\x01" + key
    header = {
        "context": {"slot": 1},
        "value": {"lamports": 1, "owner": "L", "data": [base64.b64encode(data).decode(), "base64"]},
    }
    native = cast(SolanaProgram, parse_program(fixture("program"), header, "P", "devnet").native)
    assert native.last_deployed_slot == 77
    assert native.upgrade_authority == base58.b58encode(key).decode()


def test_program_without_program_data() -> None:
    info = fixture("not_a_mint")
    assert program_data_address(info, "P") is None
    assert cast(SolanaProgram, parse_program(info, None, "P", "devnet").native).program_data is None


def test_account_that_is_not_a_program_is_refused() -> None:
    with pytest.raises(InvalidInput, match="not an executable"):
        program_data_address(fixture("mint"), "X")
    with pytest.raises(InvalidInput, match="no account"):
        program_data_address({"context": {"slot": 1}, "value": None}, "X")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "no data"),
        ({"lamports": 1, "owner": "L", "data": ["%%%", "base64"]}, "not base64"),
        ({"lamports": 1, "owner": "L", "data": ["AAAA", "base64"]}, "too short"),
    ],
)
def test_bad_program_data_is_refused(value: dict[str, Any] | None, message: str) -> None:
    header = {"context": {"slot": 1}, "value": value}
    with pytest.raises(MalformedResponse, match=message):
        parse_program(fixture("program"), header, "P", "devnet")


def test_solana_metrics_compare_priority_fees_across_versions() -> None:
    records = [
        parse(name)[1]
        for name in (
            "tx_legacy_compute_budget",
            "tx_v0_compute_budget",
            "tx_v1_priority_fee",
            "tx_v1",
        )
    ]
    metrics = solana_metrics(records)
    fees = sorted(native_of(record).priority_fee_lamports for record in records)
    stats = metrics["priority_fees_lamports"]
    assert stats["total"] == sum(fees)
    assert stats["max"] == fees[-1]
    assert stats["transactions_paying"] == 3
    assert metrics["fees_lamports"] == sum(record.fee for record in records)
    assert metrics["transaction_versions"] == {"legacy": 1, "0": 1, "1": 2}
    assert metrics["compute_units"]["total"] == sum(
        native_of(record).compute_units_consumed or 0 for record in records
    )
    assert metrics["program_calls"][0]["calls"] >= 1


def test_solana_metrics_rank_spl_tokens() -> None:
    records = [parse("tx_spl_transfer")[1], parse("tx_inner_instructions")[1]]
    top = solana_metrics(records)["top_spl_tokens_by_transfers"]
    assert top[0]["mint"] == native_of(records[0]).token_transfers[0].mint
    assert top[0]["transfers"] == 2


def test_solana_metrics_on_no_records() -> None:
    metrics = solana_metrics([])
    assert metrics["compute_units"] == {"total": 0, "mean": None}
    assert metrics["priority_fees_lamports"]["mean"] is None
    assert metrics["top_spl_tokens_by_transfers"] == []
