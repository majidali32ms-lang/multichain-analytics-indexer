"""An interrupted sync must end with the full history: no gaps and no duplicates."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from algosdk import account as algosdk_account

from multichain_indexer import Indexer, InvalidInput, NetworkError
from multichain_indexer.core.config import Settings
from tests.conftest import FakeNode, Reply

ADDRESS = algosdk_account.generate_account()[1]
TRANSACTIONS_PATH = f"/v2/accounts/{ADDRESS}/transactions"
HEAD = 5000
PAGE_SIZE = 3
PAGES = 4
FAILURE = Reply({"message": "node restarting"}, status=503)
ROUND_TIME_BASE = 1_700_000_000


def history() -> list[dict[str, object]]:
    """Transactions newest first, the way the Indexer returns them."""
    total = PAGE_SIZE * PAGES
    return [
        {
            "id": f"TX{index:03d}",
            "sender": ADDRESS,
            "fee": 1000,
            "tx-type": "pay",
            "confirmed-round": HEAD - index,
            "round-time": ROUND_TIME_BASE - index,
            "payment-transaction": {"amount": index, "receiver": ADDRESS},
        }
        for index in range(total)
    ]


def token_after(page_number: int) -> str:
    return f"TOKEN{page_number}"


def page_replies() -> list[Reply]:
    transactions = history()
    replies = []
    for number in range(PAGES):
        body: dict[str, object] = {
            "transactions": transactions[number * PAGE_SIZE : (number + 1) * PAGE_SIZE]
        }
        if number < PAGES - 1:
            body["next-token"] = token_after(number)
        replies.append(Reply(body))
    return replies


def stored_ids(database: Path) -> list[str]:
    """Read the table directly, so a duplicate could not be hidden by the API."""
    connection = sqlite3.connect(database)
    rows = connection.execute("SELECT tx_id FROM transactions").fetchall()
    connection.close()
    return sorted(row[0] for row in rows)


def expected_ids() -> list[str]:
    return sorted(str(transaction["id"]) for transaction in history())


@pytest.fixture
def no_retries(settings: Settings) -> Settings:
    return settings.model_copy(update={"max_retries": 0})


@pytest.mark.parametrize("pages_before_failure", range(1, PAGES))
def test_failure_at_any_page_resumes_with_no_gaps_or_duplicates(
    node: FakeNode, no_retries: Settings, pages_before_failure: int
) -> None:
    replies = page_replies()
    node.routes["/health"] = Reply({"round": HEAD})
    node.routes[TRANSACTIONS_PATH] = [*replies[:pages_before_failure], FAILURE]

    with Indexer("algorand", settings=no_retries) as indexer, pytest.raises(NetworkError):
        indexer.sync(ADDRESS)

    stored_before_resume = stored_ids(no_retries.database)
    assert len(stored_before_resume) == pages_before_failure * PAGE_SIZE

    node.routes[TRANSACTIONS_PATH] = replies[pages_before_failure:]
    node.requests.clear()
    with Indexer("algorand", settings=no_retries) as indexer:
        result = indexer.sync(ADDRESS)

    assert result.resumed
    assert result.records_fetched == (PAGES - pages_before_failure) * PAGE_SIZE
    assert stored_ids(no_retries.database) == expected_ids()

    queries = node.requests_to(TRANSACTIONS_PATH)
    assert queries[0]["next"] == token_after(pages_before_failure - 1)
    assert queries[0]["max-round"] == str(HEAD)
    assert node.requests_to("/health") == []


def test_stored_history_has_no_gap_in_rounds(node: FakeNode, no_retries: Settings) -> None:
    replies = page_replies()
    node.routes["/health"] = Reply({"round": HEAD})
    node.routes[TRANSACTIONS_PATH] = [replies[0], FAILURE]
    with Indexer("algorand", settings=no_retries) as indexer:
        with pytest.raises(NetworkError):
            indexer.sync(ADDRESS)
        node.routes[TRANSACTIONS_PATH] = replies[1:]
        indexer.sync(ADDRESS)
        rounds = [record.native.round for record in indexer.stored_transactions(account=ADDRESS)]

    assert rounds == list(range(HEAD, HEAD - PAGE_SIZE * PAGES, -1))


def test_several_interruptions_in_a_row(node: FakeNode, no_retries: Settings) -> None:
    replies = page_replies()
    node.routes["/health"] = Reply({"round": HEAD})
    for reply in replies[:-1]:
        node.routes[TRANSACTIONS_PATH] = [reply, FAILURE]
        with Indexer("algorand", settings=no_retries) as indexer, pytest.raises(NetworkError):
            indexer.sync(ADDRESS)
    node.routes[TRANSACTIONS_PATH] = [replies[-1]]
    with Indexer("algorand", settings=no_retries) as indexer:
        assert indexer.sync(ADDRESS).resumed

    assert stored_ids(no_retries.database) == expected_ids()


def test_chain_moving_on_during_an_interruption_loses_nothing(
    node: FakeNode, no_retries: Settings
) -> None:
    replies = page_replies()
    node.routes["/health"] = Reply({"round": HEAD})
    node.routes[TRANSACTIONS_PATH] = [replies[0], FAILURE]
    with Indexer("algorand", settings=no_retries) as indexer:
        with pytest.raises(NetworkError):
            indexer.sync(ADDRESS)

        # The resumed sync finishes the original range, whatever the head is by now.
        node.routes["/health"] = Reply({"round": HEAD + 100})
        node.routes[TRANSACTIONS_PATH] = replies[1:]
        resumed = indexer.sync(ADDRESS)
        assert resumed.to_height == HEAD

        # The next sync picks up from there.
        node.routes[TRANSACTIONS_PATH] = Reply({"transactions": []})
        node.requests.clear()
        following = indexer.sync(ADDRESS)

    assert not following.resumed
    query = node.requests_to(TRANSACTIONS_PATH)[0]
    assert (query["min-round"], query["max-round"]) == (str(HEAD + 1), str(HEAD + 100))


def test_interrupted_incremental_sync_keeps_its_lower_bound(
    node: FakeNode, no_retries: Settings
) -> None:
    node.routes["/health"] = Reply({"round": HEAD - 50})
    node.routes[TRANSACTIONS_PATH] = Reply({"transactions": []})
    replies = page_replies()
    with Indexer("algorand", settings=no_retries) as indexer:
        indexer.sync(ADDRESS)
        node.routes["/health"] = Reply({"round": HEAD})
        node.routes[TRANSACTIONS_PATH] = [replies[0], FAILURE]
        with pytest.raises(NetworkError):
            indexer.sync(ADDRESS)
        node.routes[TRANSACTIONS_PATH] = replies[1:]
        node.requests.clear()
        indexer.sync(ADDRESS)

    query = node.requests_to(TRANSACTIONS_PATH)[0]
    assert query["min-round"] == str(HEAD - 49)
    assert query["next"] == token_after(0)


def test_rejected_cursor_restarts_the_same_range_without_losing_anything(
    node: FakeNode, no_retries: Settings
) -> None:
    replies = page_replies()
    node.routes["/health"] = Reply({"round": HEAD})
    node.routes[TRANSACTIONS_PATH] = [replies[0], FAILURE]
    with Indexer("algorand", settings=no_retries) as indexer:
        with pytest.raises(NetworkError):
            indexer.sync(ADDRESS)

        rejected = Reply({"message": "invalid next token"}, status=400)
        node.routes[TRANSACTIONS_PATH] = [rejected, *replies]
        node.requests.clear()
        result = indexer.sync(ADDRESS)

    assert result.resumed
    queries = node.requests_to(TRANSACTIONS_PATH)
    assert "next" not in queries[1]
    assert queries[1]["max-round"] == str(HEAD)
    assert stored_ids(no_retries.database) == expected_ids()


def test_bad_request_on_a_fresh_sync_is_not_swallowed(node: FakeNode, no_retries: Settings) -> None:
    node.routes["/health"] = Reply({"round": HEAD})
    node.routes[TRANSACTIONS_PATH] = Reply({"message": "bad request"}, status=400)
    with Indexer("algorand", settings=no_retries) as indexer, pytest.raises(InvalidInput):
        indexer.sync(ADDRESS)


def test_killed_process_resumes_with_no_gaps_or_duplicates(
    node: FakeNode, no_retries: Settings, tmp_path: Path
) -> None:
    """The real thing: the command line is killed while waiting for a page."""
    replies = page_replies()
    node.routes["/health"] = Reply({"round": HEAD})
    node.routes[TRANSACTIONS_PATH] = [*replies[:2], Reply(delay=5)]
    command = [
        sys.executable,
        "-c",
        "from multichain_indexer.cli import main; main()",
        "--db",
        str(no_retries.database),
        "--env-file",
        str(tmp_path / "none.env"),
    ]
    environment = {
        **os.environ,
        "ALGORAND_TESTNET_INDEXER_SERVER": node.url,
        "MCI_REQUESTS_PER_SECOND": "1000",
    }

    process = subprocess.Popen(
        [*command, "sync", ADDRESS],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_until(lambda: len(node.requests_to(TRANSACTIONS_PATH)) == 3)
    process.kill()
    process.wait()

    assert len(stored_ids(no_retries.database)) == 2 * PAGE_SIZE

    node.routes[TRANSACTIONS_PATH] = replies[2:]
    finished = subprocess.run(
        [*command, "sync", ADDRESS], env=environment, capture_output=True, text=True, timeout=60
    )
    assert finished.returncode == 0, finished.stderr
    assert "Resumed" in finished.stdout
    assert stored_ids(no_retries.database) == expected_ids()


def _wait_until(condition: object, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():  # type: ignore[operator]
        assert time.monotonic() < deadline, "the sync never reached the expected page"
        time.sleep(0.05)
