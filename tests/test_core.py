from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from pydantic import SecretStr, ValidationError

from multichain_indexer.core.base_indexer import ChainProfile
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError, InvalidInput, UnsupportedChain
from multichain_indexer.core.export import export_transactions
from multichain_indexer.core.metrics import build_report, transactions_frame
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    NativeData,
    Status,
    TransactionRecord,
)
from multichain_indexer.core.observability import (
    JsonFormatter,
    RedactingFilter,
    configure_logging,
    redact_url,
)
from multichain_indexer.core.registry import create_indexer
from multichain_indexer.core.storage import Backfill, SQLiteStorage, SyncState

NOW = datetime(2026, 1, 2, tzinfo=UTC)
PAST_64_BITS = 3 * 10**20


class ToyNative(NativeData):
    label: str = "x"


def transaction(
    tx_id: str = "t1",
    *,
    sender: str = "alice",
    fee: int = 10,
    status: Status = Status.FINAL,
    time: datetime = NOW,
    network: str = "testnet",
) -> TransactionRecord:
    return TransactionRecord(
        id=tx_id,
        time=time,
        sender=sender,
        receiver="bob",
        amount=5,
        fee=fee,
        status=status,
        chain="toy",
        network=network,
        native=ToyNative(),
    )


def test_assets_and_contracts_may_have_no_creator() -> None:
    asset = AssetRecord(
        id="mint", decimals=0, total=1, chain="toy", network="testnet", native=ToyNative()
    )
    contract = ContractRecord(id="program", chain="toy", network="testnet", native=ToyNative())
    assert asset.creator is None
    assert contract.creator is None


def test_records_reject_negative_amounts_and_naive_times() -> None:
    with pytest.raises(ValidationError):
        transaction(fee=-1)
    with pytest.raises(ValidationError):
        transaction(time=datetime(2026, 1, 1))


def test_storage_upsert_adds_no_duplicates_and_keeps_big_values(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "db" / "x.db")
    storage.save_transactions("toy", "testnet", "alice", [transaction(fee=PAST_64_BITS)])
    storage.save_transactions("toy", "testnet", "alice", [transaction(fee=PAST_64_BITS)])

    stored = storage.load_transactions("toy", "testnet", ToyNative)

    assert [record.id for record in stored] == ["t1"]
    assert stored[0].fee == PAST_64_BITS
    assert stored[0].native == ToyNative()
    storage.close()


def test_storage_upsert_replaces_a_pending_record(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "x.db")
    storage.save_transactions("toy", "testnet", "alice", [transaction(status=Status.PENDING)])
    storage.save_transactions("toy", "testnet", "alice", [transaction(status=Status.FINAL)])
    assert storage.load_transactions("toy", "testnet", ToyNative)[0].status is Status.FINAL
    storage.close()


def test_storage_filters_by_network_account_and_time(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "x.db")
    old = NOW - timedelta(days=10)
    storage.save_transactions("toy", "testnet", "alice", [transaction("a", time=old)])
    storage.save_transactions("toy", "testnet", "carol", [transaction("b")])
    storage.save_transactions("toy", "mainnet", "alice", [transaction("c", network="mainnet")])

    load = storage.load_transactions
    assert [r.id for r in load("toy", "testnet", ToyNative)] == ["b", "a"]
    assert [r.id for r in load("toy", "testnet", ToyNative, account="alice")] == ["a"]
    since = NOW - timedelta(days=1)
    assert [r.id for r in load("toy", "testnet", ToyNative, since=since)] == ["b"]
    storage.close()


def test_sync_state_is_kept_per_network(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "x.db")
    assert storage.load_sync_state("toy", "testnet", "alice") == SyncState()
    storage.finish_sync("toy", "testnet", "alice", 9)
    storage.finish_sync("toy", "testnet", "alice", 12)
    assert storage.load_sync_state("toy", "testnet", "alice").checkpoint == 12
    assert storage.load_sync_state("toy", "mainnet", "alice") == SyncState()
    storage.close()


def test_backfill_is_saved_with_its_records_and_cleared_on_finish(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "x.db")
    backfill = Backfill(None, 100, "next")
    storage.save_transactions("toy", "testnet", "alice", [transaction()], backfill=backfill)
    assert storage.load_sync_state("toy", "testnet", "alice").backfill == backfill

    storage.finish_sync("toy", "testnet", "alice", 100)
    state = storage.load_sync_state("toy", "testnet", "alice")
    assert state == SyncState(checkpoint=100)
    storage.close()


def test_failed_write_leaves_neither_records_nor_cursor(tmp_path: Path) -> None:
    def records_then_crash() -> Iterator[TransactionRecord]:
        yield transaction("a")
        raise RuntimeError("disk full")

    storage = SQLiteStorage(tmp_path / "x.db")
    with pytest.raises(RuntimeError):
        storage.save_transactions(
            "toy", "testnet", "alice", records_then_crash(), backfill=Backfill(None, 100, "next")
        )
    assert storage.load_transactions("toy", "testnet", ToyNative) == []
    assert storage.load_sync_state("toy", "testnet", "alice") == SyncState()
    storage.close()


def test_snapshot_keeps_the_latest_state(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "x.db")
    for balance in (1, 2):
        record = AccountRecord(
            address="alice", balance=balance, chain="toy", network="testnet", native=ToyNative()
        )
        storage.save_snapshot(record)
    rows = storage._connection.execute("SELECT record FROM snapshots").fetchall()  # type: ignore[attr-defined]
    assert [json.loads(row[0])["balance"] for row in rows] == [2]
    storage.close()


def test_report_counts_days_senders_fees_and_failures() -> None:
    records = [
        transaction("a", sender="alice", fee=PAST_64_BITS),
        transaction("b", sender="alice", fee=1, status=Status.FAILED),
        transaction("c", sender="carol", fee=1, time=NOW + timedelta(days=1)),
        transaction("d", sender="dave", fee=1, time=NOW + timedelta(days=1)),
    ]
    report = build_report(
        records, chain="toy", network="testnet", days=7, account=None, native={"extra": 1}
    )
    assert report.transactions == 4
    assert report.transactions_per_day == {"2026-01-02": 2, "2026-01-03": 2}
    assert report.active_accounts == 3
    assert report.total_fees == PAST_64_BITS + 3
    assert report.failure_rate == 0.25
    assert report.native == {"extra": 1}


def test_report_on_no_records() -> None:
    report = build_report([], chain="toy", network="testnet", days=7, account="a", native={})
    assert report.transactions == 0
    assert report.transactions_per_day == {}
    assert report.failure_rate is None


def test_frame_has_shared_columns_and_exact_amounts() -> None:
    frame = transactions_frame([transaction(fee=PAST_64_BITS)])
    assert frame.loc[0, "fee"] == PAST_64_BITS
    assert frame.loc[0, "native"] == {"label": "x"}
    assert frame.loc[0, "network"] == "testnet"


def test_export_csv_and_json_as_text() -> None:
    records = [transaction(fee=PAST_64_BITS)]
    assert str(PAST_64_BITS) in (export_transactions(records, "csv") or "")
    exported = json.loads(export_transactions(records, "json") or "")
    assert exported[0]["fee"] == str(PAST_64_BITS)
    assert exported[0]["network"] == "testnet"
    assert json.loads(exported[0]["native"]) == {"label": "x"}


@pytest.mark.parametrize("export_format", ["csv", "json", "parquet"])
def test_export_to_a_file(tmp_path: Path, export_format: str) -> None:
    output = tmp_path / "out" / f"records.{export_format}"
    assert export_transactions([transaction()], export_format, output) is None
    assert output.stat().st_size > 0


def test_parquet_round_trips_big_values(tmp_path: Path) -> None:
    output = tmp_path / "records.parquet"
    export_transactions([transaction(fee=PAST_64_BITS)], "parquet", output)
    assert pd.read_parquet(output).loc[0, "fee"] == str(PAST_64_BITS)


def test_export_refuses_what_it_cannot_do() -> None:
    with pytest.raises(InvalidInput, match="unknown export format"):
        export_transactions([], "xml")
    with pytest.raises(InvalidInput, match="--output"):
        export_transactions([], "parquet")


def test_settings_merge_env_file_and_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MCI_MAX_RETRIES=2\nMCI_REQUEST_TIMEOUT=9\nALGORAND_TESTNET_INDEXER_TOKEN=tok-123\n"
    )
    settings = Settings.from_env(env_file, environ={"MCI_MAX_RETRIES": "3", "EMPTY": ""})
    assert settings.max_retries == 3
    assert settings.request_timeout == 9
    token = settings.chain_setting("algorand", "testnet", "INDEXER_TOKEN")
    assert token is not None and token.get_secret_value() == "tok-123"
    assert "tok-123" not in repr(settings.env)


def test_settings_without_a_file_use_defaults() -> None:
    settings = Settings.from_env("no-such-file.env", environ={})
    assert settings.requests_per_second == 10


def test_invalid_setting_is_named_without_its_value() -> None:
    with pytest.raises(ConfigError, match="MCI_MAX_RETRIES") as caught:
        Settings.from_env(None, environ={"MCI_MAX_RETRIES": "secret-looking"})
    assert "secret-looking" not in str(caught.value)


def test_endpoint_requires_https_except_on_localhost() -> None:
    env = {"TOY_TESTNET_SERVER": SecretStr("http://remote.example")}
    settings = Settings(env=env)
    with pytest.raises(ConfigError, match="TOY_TESTNET_SERVER"):
        settings.endpoint("toy", "testnet", "SERVER", "https://default.example")
    default = settings.endpoint("toy", "mainnet", "SERVER", "http://localhost:1")
    assert default.get_secret_value() == "http://localhost:1"


def test_secret_values_lists_endpoints_and_tokens_only() -> None:
    settings = Settings(
        env={
            "X_SERVER": SecretStr("https://a"),
            "X_TOKEN": SecretStr("b"),
            "EMPTY_API_KEY": SecretStr(""),
            "PATH": SecretStr("c"),
        }
    )
    assert sorted(settings.secret_values()) == ["b", "https://a"]


def test_redact_url_keeps_only_scheme_and_host() -> None:
    assert redact_url("https://user:pw@rpc.io:8443/v1/KEY?a=1") == "https://rpc.io:8443/***"
    assert redact_url("https://rpc.io") == "https://rpc.io"


def _log_record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, message, args or None, None)


def test_redaction_covers_message_arguments_and_extra_fields() -> None:
    record = _log_record("calling %s with %s", "https://h.io/abc", "tok-9999")
    record.reason = "tok-9999 failed"
    RedactingFilter(["tok-9999", "ab"]).filter(record)
    assert record.getMessage() == "calling https://h.io/*** with ***"
    assert record.reason == "*** failed"


def test_json_logs_keep_the_error_type_but_never_the_frames() -> None:
    try:
        raise KeyError("x")
    except KeyError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    record.chain = "toy"
    entry = json.loads(JsonFormatter().format(record))
    assert (entry["event"], entry["chain"], entry["error"]) == ("boom", "toy", "KeyError")
    assert "Traceback" not in json.dumps(entry)


@pytest.mark.parametrize(
    ("verbose", "quiet", "level"),
    [(False, False, logging.WARNING), (True, False, logging.INFO), (False, True, logging.ERROR)],
)
def test_configure_logging_levels(verbose: bool, quiet: bool, level: int) -> None:
    configure_logging(verbose=verbose, quiet=quiet)
    assert logging.getLogger().level == level


def test_configured_handler_masks_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(secrets=["hunter22"])
    logging.getLogger("x").warning("password is hunter22")
    assert "hunter22" not in capsys.readouterr().err


def test_format_native_trims_trailing_zeros() -> None:
    profile = ChainProfile("toy", "TOY", "units", 6, ("testnet",), "testnet", ToyNative)
    assert profile.format_native(1_500_000) == "1.5 TOY"
    assert profile.format_native(2_000_000) == "2 TOY"
    assert profile.format_native(0) == "0 TOY"


def test_registry_reports_planned_and_unknown_chains() -> None:
    with pytest.raises(UnsupportedChain, match="later version"):
        create_indexer("near", None, Settings())
    with pytest.raises(UnsupportedChain, match="unknown chain"):
        create_indexer("dogechain", None, Settings())
