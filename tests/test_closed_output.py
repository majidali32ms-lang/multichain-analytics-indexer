"""A reader closing the output pipe, as in ``... | head``, must end the command quietly.

Most of these tests start the real command line with a real pipe, because the failure only
appears on real streams, and differs between Windows and Linux.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multichain_indexer import cli as cli_module
from multichain_indexer.adapters.algorand.parser import parse_transaction as parse_algorand
from multichain_indexer.adapters.near.fastnear import _executed_transaction, _RawTransaction
from multichain_indexer.adapters.near.parser import parse_transaction as parse_near
from multichain_indexer.adapters.solana.parser import parse_transaction as parse_solana
from multichain_indexer.core.models import TransactionRecord
from multichain_indexer.core.storage import SQLiteStorage
from tests.conftest import (
    FakeNode,
    Reply,
    load_fixture,
    load_near_fixture,
    load_solana_fixture,
)

COMMAND = [sys.executable, "-c", "from multichain_indexer.cli import main; main()"]
TIMEOUT_SECONDS = 60
ACCOUNT = "JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM"
SIGNS_OF_A_RAW_FAILURE = ("Traceback", "Exception ignored", "OSError", "BrokenPipeError")
# Larger than any pipe buffer (64 KiB on Linux, 4 KiB from subprocess on Windows) and than
# Python's own 8 KiB stream buffer, so one write goes straight to the pipe and fails there.
LARGE_OUTPUT_BYTES = 256 * 1024
CHAINS = ("algorand", "solana", "near")


def environment(node: FakeNode | None = None) -> dict[str, str]:
    extra = {"ALGORAND_TESTNET_INDEXER_SERVER": node.url} if node else {}
    return {**os.environ, "MCI_REQUESTS_PER_SECOND": "1000", **extra}


def start(arguments: list[str], tmp_path: Path, node: FakeNode | None = None) -> subprocess.Popen:
    options = ["--db", str(tmp_path / "x.db"), "--env-file", str(tmp_path / "none.env")]
    return subprocess.Popen(
        [*COMMAND, *options, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment(node),
    )


def finish(process: subprocess.Popen) -> tuple[int, str]:
    errors = process.stderr.read().decode(errors="replace")
    process.stderr.close()
    return process.wait(timeout=TIMEOUT_SECONDS), errors


def assert_quiet_exit(code: int, errors: str) -> None:
    assert code == cli_module.EXIT_OUTPUT_CLOSED
    for sign in SIGNS_OF_A_RAW_FAILURE:
        assert sign not in errors, errors


@pytest.mark.parametrize(
    "arguments",
    [
        ["metrics"],
        ["export", "--format", "csv"],
        ["export", "--format", "json"],
        ["--help"],
        ["index", "--help"],
    ],
)
def test_reader_gone_before_any_output(arguments: list[str], tmp_path: Path) -> None:
    process = start(arguments, tmp_path)
    process.stdout.close()
    assert_quiet_exit(*finish(process))


def test_reader_gone_part_way_through_the_output(node: FakeNode, tmp_path: Path) -> None:
    """The first lines are read, then the reader leaves before the transactions are written."""
    node.routes[f"/v2/accounts/{ACCOUNT}"] = Reply(load_fixture("account"))
    node.routes[f"/v2/accounts/{ACCOUNT}/transactions"] = Reply(
        {"transactions": [load_fixture("tx_pay")]}, delay=1.0
    )
    process = start(["index", "account", ACCOUNT], tmp_path, node)
    assert process.stdout.readline().startswith(b"Address")
    process.stdout.close()
    assert_quiet_exit(*finish(process))


def test_reader_of_merged_output_gone(node: FakeNode, tmp_path: Path) -> None:
    """With ``2>&1 | head`` the progress lines on standard error have nowhere to go either."""
    node.routes["/health"] = Reply({"round": 1000})
    node.routes[f"/v2/accounts/{ACCOUNT}/transactions"] = Reply(
        {"transactions": [load_fixture("tx_pay")]}
    )
    options = ["--db", str(tmp_path / "x.db"), "--env-file", str(tmp_path / "none.env")]
    process = subprocess.Popen(
        [*COMMAND, *options, "sync", ACCOUNT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment(node),
    )
    process.stdout.close()
    assert process.wait(timeout=TIMEOUT_SECONDS) == cli_module.EXIT_OUTPUT_CLOSED


def test_output_read_to_the_end_is_untouched(tmp_path: Path) -> None:
    process = start(["metrics"], tmp_path)
    output, errors = process.communicate(timeout=TIMEOUT_SECONDS)
    assert process.returncode == 0
    assert b'"transactions": 0' in output
    assert errors == b""


def test_exit_code_of_a_failed_command_is_unchanged(tmp_path: Path) -> None:
    process = start(["index", "account", "BAD"], tmp_path)
    output, errors = process.communicate(timeout=TIMEOUT_SECONDS)
    assert process.returncode == 1
    assert b"not a valid Algorand address" in errors
    assert output == b""


class Stream:
    def __init__(self, *, closed: bool) -> None:
        self.closed_pipe = closed

    def flush(self) -> None:
        if self.closed_pipe:
            raise OSError(errno.EINVAL, "Invalid argument")

    def fileno(self) -> int:
        return 1


def raising(error: OSError) -> object:
    def run() -> None:
        raise error

    return run


@pytest.mark.parametrize("code", [errno.EPIPE, errno.EINVAL])
def test_main_ends_quietly_when_the_output_is_closed(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    redirected: list[int] = []
    monkeypatch.setattr(cli_module, "cli", raising(OSError(code, "closed")))
    monkeypatch.setattr(sys, "stdout", Stream(closed=True))
    monkeypatch.setattr(sys, "stderr", Stream(closed=False))
    monkeypatch.setattr(os, "dup2", lambda source, target: redirected.append(target))

    with pytest.raises(SystemExit) as stopped:
        cli_module.main()

    assert stopped.value.code == cli_module.EXIT_OUTPUT_CLOSED
    assert redirected == [1, 1]


def test_invalid_argument_with_healthy_output_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EINVAL also comes from a bad file path. That must not pass for a closed pipe."""
    monkeypatch.setattr(cli_module, "cli", raising(OSError(errno.EINVAL, "bad path")))
    monkeypatch.setattr(sys, "stdout", Stream(closed=False))
    monkeypatch.setattr(sys, "stderr", Stream(closed=False))
    with pytest.raises(OSError, match="bad path"):
        cli_module.main()


def test_other_os_errors_are_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "cli", raising(OSError(errno.ENOSPC, "disk full")))
    monkeypatch.setattr(sys, "stdout", Stream(closed=True))
    with pytest.raises(OSError, match="disk full"):
        cli_module.main()


def real_records(chain: str) -> list[TransactionRecord]:
    """A few real transactions of ``chain``, parsed as a sync would store them."""
    if chain == "algorand":
        return [parse_algorand(load_fixture(name), "testnet") for name in ("tx_pay", "tx_appl")]
    if chain == "solana":
        return [
            parse_solana(load_solana_fixture(name), "devnet", "finalized")
            for name in ("history_0", "tx_spl_transfer")
        ]
    raws = (load_near_fixture(name)["transaction"] for name in ("tx_transfer", "tx_delegate"))
    return [
        parse_near(_executed_transaction(_RawTransaction.model_validate(raw)), "testnet")
        for raw in raws
    ]


def store_large_history(chain: str, tmp_path: Path) -> None:
    """Fill the database the command line will read with more than any pipe can hold."""
    originals = real_records(chain)
    size = sum(len(record.model_dump_json()) for record in originals)
    copies = LARGE_OUTPUT_BYTES // size + 1
    records = [
        record.model_copy(update={"id": f"{record.id}-{copy}"})
        for copy in range(copies)
        for record in originals
    ]
    storage = SQLiteStorage(tmp_path / "x.db")
    storage.save_transactions(chain, records[0].network, "account", records)
    storage.close()


@pytest.mark.parametrize("chain", CHAINS)
def test_large_export_read_to_the_end_is_untouched(chain: str, tmp_path: Path) -> None:
    store_large_history(chain, tmp_path)
    process = start(["--chain", chain, "export", "--format", "json"], tmp_path)
    output, errors = process.communicate(timeout=TIMEOUT_SECONDS)
    assert process.returncode == 0
    assert len(output) > LARGE_OUTPUT_BYTES
    assert errors == b""


@pytest.mark.parametrize("chain", CHAINS)
def test_reader_gone_before_a_write_larger_than_the_buffer(chain: str, tmp_path: Path) -> None:
    """As in ``export ... | true``: the one large write fails, and nothing is left to flush."""
    store_large_history(chain, tmp_path)
    process = start(["--chain", chain, "export", "--format", "json"], tmp_path)
    process.stdout.close()
    assert_quiet_exit(*finish(process))


@pytest.mark.parametrize("chain", CHAINS)
def test_reader_gone_before_a_small_output(chain: str, tmp_path: Path) -> None:
    """Small output is still in the stream's buffer when it fails, at the flush."""
    store_large_history(chain, tmp_path)
    process = start(["--chain", chain, "metrics", "--days", "36500"], tmp_path)
    process.stdout.close()
    assert_quiet_exit(*finish(process))


@pytest.mark.parametrize("chain", CHAINS)
def test_reader_gone_after_the_first_line_of_a_large_export(chain: str, tmp_path: Path) -> None:
    """As in ``export ... | head -1``."""
    store_large_history(chain, tmp_path)
    process = start(["--chain", chain, "export", "--format", "json"], tmp_path)
    assert process.stdout.readline().strip() == b"["
    process.stdout.close()
    assert_quiet_exit(*finish(process))


def test_pipe_with_its_reader_gone_is_recognised_without_writing() -> None:
    reader, writer = os.pipe()
    try:
        assert not cli_module._pipe_closed(writer)
        os.close(reader)
        assert cli_module._pipe_closed(writer)
    finally:
        os.close(writer)


def test_healthy_file_is_not_taken_for_a_closed_pipe(tmp_path: Path) -> None:
    with (tmp_path / "out.txt").open("w") as healthy:
        assert not cli_module._pipe_closed(healthy.fileno())
    assert (tmp_path / "out.txt").read_text() == ""


def test_stream_without_a_descriptor_is_not_taken_for_a_closed_pipe() -> None:
    class Detached:
        def fileno(self) -> int:
            raise ValueError("no descriptor")

    assert not cli_module._reader_gone(Detached())  # type: ignore[arg-type]


def test_closed_pipe_with_nothing_left_to_flush_ends_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "cli", raising(OSError(errno.EINVAL, "closed")))
    monkeypatch.setattr(sys, "stdout", Stream(closed=False))
    monkeypatch.setattr(sys, "stderr", Stream(closed=False))
    monkeypatch.setattr(cli_module, "_reader_gone", lambda stream: stream is sys.stdout)
    monkeypatch.setattr(os, "dup2", lambda source, target: None)

    with pytest.raises(SystemExit) as stopped:
        cli_module.main()

    assert stopped.value.code == cli_module.EXIT_OUTPUT_CLOSED
