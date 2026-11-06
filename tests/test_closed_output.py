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
from tests.conftest import FakeNode, Reply, load_fixture

COMMAND = [sys.executable, "-c", "from multichain_indexer.cli import main; main()"]
TIMEOUT_SECONDS = 60
ACCOUNT = "JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM"
SIGNS_OF_A_RAW_FAILURE = ("Traceback", "Exception ignored", "OSError", "BrokenPipeError")


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
