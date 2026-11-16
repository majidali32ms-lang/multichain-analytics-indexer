"""Follow mode: rounds of the ordinary sync, surviving network failures, stopped by Ctrl-C."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from multichain_indexer import AsyncIndexer, Indexer
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import InvalidAddress, InvalidInput, NetworkError
from multichain_indexer.core.models import SyncResult
from multichain_indexer.follow import default_interval, follow_account
from tests.bitcoin_support import SMALL, serve_small
from tests.conftest import FakeNode


def result(fetched: int, requests: int = 2) -> SyncResult:
    return SyncResult(
        chain="bitcoin",
        network="testnet4",
        account=SMALL,
        from_height=1,
        to_height=9,
        resumed=False,
        records_fetched=fetched,
        requests_made=requests,
        elapsed_seconds=0.1,
    )


class Script:
    """Answers each sync round in turn, and records the pauses between them."""

    def __init__(self, *outcomes: SyncResult | Exception) -> None:
        self.outcomes = list(outcomes)
        self.pauses: list[float] = []

    async def sync(self, address: str) -> SyncResult:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)


async def test_rounds_run_with_a_pause_between_them_and_add_up() -> None:
    script = Script(result(4), result(0), result(1))
    seen: list[int] = []
    summary = await follow_account(
        script.sync,
        SMALL,
        interval=60,
        on_sync=lambda done: seen.append(done.records_fetched),
        rounds=3,
        sleep=script.sleep,
    )
    assert seen == [4, 0, 1]
    assert script.pauses == [60, 60]
    assert (summary.rounds, summary.records_fetched, summary.requests_made) == (3, 5, 6)


async def test_a_network_failure_is_retried_next_round() -> None:
    script = Script(result(1), NetworkError("timed out"), result(2))
    summary = await follow_account(script.sync, SMALL, interval=5, rounds=3, sleep=script.sleep)
    assert (summary.rounds, summary.failed_rounds, summary.records_fetched) == (2, 1, 3)


async def test_any_other_failure_stops_the_follow() -> None:
    script = Script(InvalidAddress("not a Bitcoin address"))
    with pytest.raises(InvalidAddress):
        await follow_account(script.sync, "x", interval=5, sleep=script.sleep)


@pytest.mark.parametrize(("interval", "rounds"), [(0, None), (-1, None), (5, 0)])
async def test_the_interval_and_rounds_must_be_positive(
    interval: float, rounds: int | None
) -> None:
    with pytest.raises(InvalidInput):
        await follow_account(Script().sync, SMALL, interval=interval, rounds=rounds)


def test_each_chain_has_its_own_default_interval() -> None:
    assert default_interval("bitcoin") == 60
    assert default_interval("algorand") == 10
    assert default_interval("examplechain") == 30


async def test_following_a_real_sync_stores_each_transaction_once(
    node: FakeNode, btc_settings: Settings
) -> None:
    serve_small(node)
    async with AsyncIndexer("bitcoin", settings=btc_settings) as indexer:
        summary = await indexer.follow(SMALL, interval=0.01, rounds=2)
        stored = indexer.stored_transactions(account=SMALL)
    assert summary.rounds == 2
    assert summary.results[0].records_fetched == len(stored) == 4


def test_ctrl_c_ends_the_follow_with_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, node: FakeNode
) -> None:
    serve_small(node)
    calls: list[dict[str, Any]] = []

    def interrupted(self: Indexer, address: str, **options: Any) -> None:
        calls.append(options)
        options["on_sync"](result(3, requests=5))
        options["on_sync"](result(0, requests=1))
        raise KeyboardInterrupt

    monkeypatch.setattr(Indexer, "follow", interrupted)
    monkeypatch.setenv("BITCOIN_TESTNET4_ESPLORA_SERVER", node.url)
    options = [
        "--chain",
        "bitcoin",
        "--db",
        str(tmp_path / "f.db"),
        "--env-file",
        str(tmp_path / "e"),
    ]
    outcome = CliRunner().invoke(cli, [*options, "sync", SMALL, "--follow"])
    assert outcome.exit_code == 0, outcome.output
    assert calls[0]["interval"] == 60
    assert outcome.stdout.count("Synced") == 1
    assert "Following" in outcome.stderr
    assert "Stopped after 2 rounds: 3 transactions, 6 requests." in outcome.stderr

    custom = CliRunner().invoke(cli, [*options, "sync", SMALL, "--follow", "--interval", "5"])
    assert custom.exit_code == 0
    assert calls[1]["interval"] == 5


def test_follow_is_for_one_account_of_one_chain(tmp_path: Path) -> None:
    options = ["--chain", "all", "--env-file", str(tmp_path / "e"), "--db", str(tmp_path / "d.db")]
    refused = CliRunner().invoke(cli, [*options, "sync", "--follow"])
    assert refused.exit_code == 2
    assert "--follow works on one account" in refused.output


def test_the_blocking_api_follows_too(node: FakeNode, btc_settings: Settings) -> None:
    serve_small(node)
    with Indexer("bitcoin", settings=btc_settings) as indexer:
        assert indexer.follow(SMALL, interval=0.01, rounds=1).records_fetched == 4
