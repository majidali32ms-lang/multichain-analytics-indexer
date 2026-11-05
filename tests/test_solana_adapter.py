from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from pydantic import SecretStr

from multichain_indexer import (
    ConfigError,
    Indexer,
    InvalidAddress,
    InvalidInput,
    MalformedResponse,
    NetworkError,
    NotFound,
    RateLimited,
)
from multichain_indexer.adapters.solana import SolanaIndexer, create_indexer
from multichain_indexer.adapters.solana import adapter as adapter_module
from multichain_indexer.adapters.solana.client import MAX_TRANSACTION_VERSION
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from tests.conftest import FakeNode, Reply
from tests.conftest import load_solana_fixture as fixture

ADDRESS = fixture("signatures_first_page")["address"]
MINT = "So11111111111111111111111111111111111111112"
PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HEAD = 519_100_000
PAGE_SIZE = 2


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "result": result})


def rpc_error(code: int, message: str = "refused") -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


def first_page() -> list[dict[str, Any]]:
    return fixture("signatures_first_page")["rows"]


def next_page() -> list[dict[str, Any]]:
    return fixture("signatures_next_page")["rows"]


def history_replies(numbers: range | list[int]) -> list[Reply]:
    return [rpc_ok(fixture(f"history_{number}")) for number in numbers]


@pytest.fixture
def small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "SIGNATURES_PER_PAGE", PAGE_SIZE)


@pytest.fixture
def solana_settings(node: FakeNode, tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "solana.db",
        requests_per_second=1000,
        request_timeout=5,
        max_retries=0,
        env={"SOLANA_DEVNET_RPC_SERVER": SecretStr(node.url)},
    )


@pytest.fixture
async def indexer(solana_settings: Settings) -> AsyncIterator[SolanaIndexer]:
    adapter = create_indexer(None, solana_settings)
    yield adapter
    await adapter.aclose()


async def test_defaults_to_devnet_and_confirmed(indexer: SolanaIndexer, node: FakeNode) -> None:
    assert indexer.network == "devnet"
    node.routes["rpc:getSlot"] = rpc_ok(HEAD)
    assert await indexer.chain_head() == HEAD
    assert node.rpc_params("getSlot")[0] == [{"commitment": "confirmed"}]


def test_public_endpoints_come_from_the_cluster_docs() -> None:
    client = create_indexer("mainnet", Settings())._rpc._client
    assert client._base_url.get_secret_value() == "https://api.mainnet.solana.com"
    local = create_indexer("localnet", Settings())._rpc._client
    assert local._base_url.get_secret_value() == "http://localhost:8899"


def test_default_rate_is_conservative_and_configurable() -> None:
    measured_safe_seconds_between_requests = 2
    default = create_indexer(None, Settings())._rpc._client._limiter
    assert default._interval == pytest.approx(measured_safe_seconds_between_requests)

    configured = Settings.from_env(None, environ={"MCI_REQUESTS_PER_SECOND": "50"})
    assert create_indexer(None, configured)._rpc._client._limiter._interval == pytest.approx(0.02)


def test_unknown_cluster_and_commitment_are_refused() -> None:
    with pytest.raises(InvalidInput, match="unknown Solana cluster"):
        create_indexer("betanet", Settings())
    bad = Settings(env={"SOLANA_DEVNET_COMMITMENT": SecretStr("processed")})
    with pytest.raises(ConfigError, match="SOLANA_DEVNET_COMMITMENT"):
        create_indexer(None, bad)


async def test_finalized_commitment_is_used_and_needs_no_margin(
    node: FakeNode, solana_settings: Settings
) -> None:
    env = {**solana_settings.env, "SOLANA_DEVNET_COMMITMENT": SecretStr("finalized")}
    adapter = create_indexer(None, solana_settings.model_copy(update={"env": env}))
    node.routes["rpc:getSlot"] = rpc_ok(HEAD)
    await adapter.chain_head()
    await adapter.aclose()
    assert node.rpc_params("getSlot")[0] == [{"commitment": "finalized"}]
    assert adapter.final_height(HEAD) == HEAD


def test_confirmed_commitment_keeps_a_finality_margin(indexer_sync: SolanaIndexer) -> None:
    assert indexer_sync.final_height(HEAD) == HEAD - adapter_module.FINALITY_MARGIN_SLOTS
    assert indexer_sync.final_height(5) == 0


@pytest.fixture
def indexer_sync(solana_settings: Settings) -> SolanaIndexer:
    return create_indexer(None, solana_settings)


async def test_transactions_ask_for_version_ceiling_as_the_integer_one(
    node: FakeNode, indexer: SolanaIndexer, small_pages: None
) -> None:
    node.routes["rpc:getSignaturesForAddress"] = rpc_ok(first_page()[:1])
    node.routes["rpc:getTransaction"] = rpc_ok(fixture("history_0"))
    [page async for page in indexer.iter_account_transactions(ADDRESS)]

    options = node.rpc_params("getTransaction")[0][1]
    assert options["maxSupportedTransactionVersion"] == MAX_TRANSACTION_VERSION == 1
    assert isinstance(options["maxSupportedTransactionVersion"], int)
    assert options["encoding"] == "jsonParsed"


async def test_block_containing_a_v1_transaction_is_read_without_error(
    node: FakeNode, indexer: SolanaIndexer
) -> None:
    saved = fixture("block_with_v1")
    node.routes["rpc:getBlock"] = rpc_ok(saved["block"])
    records = await indexer.fetch_block(saved["slot"])

    assert "1" in {record.native.version for record in records}
    slot, options = node.rpc_params("getBlock")[0]
    assert slot == saved["slot"]
    assert options["maxSupportedTransactionVersion"] == 1
    assert isinstance(options["maxSupportedTransactionVersion"], int)
    assert options["transactionDetails"] == "full"


@pytest.mark.parametrize("code", [-32004, -32007, -32009])
async def test_skipped_slot_is_not_found(node: FakeNode, indexer: SolanaIndexer, code: int) -> None:
    node.routes["rpc:getBlock"] = rpc_error(code, "Slot was skipped")
    with pytest.raises(NotFound, match="skipped"):
        await indexer.fetch_block(5)


async def test_empty_block_result_and_negative_slot(node: FakeNode, indexer: SolanaIndexer) -> None:
    node.routes["rpc:getBlock"] = rpc_ok(None)
    with pytest.raises(NotFound, match="no block"):
        await indexer.fetch_block(5)
    with pytest.raises(InvalidInput, match="negative"):
        await indexer.fetch_block(-1)


async def test_pagination_walks_with_before_across_pages(
    node: FakeNode, indexer: SolanaIndexer, small_pages: None
) -> None:
    node.routes["rpc:getSignaturesForAddress"] = [
        rpc_ok(first_page()),
        rpc_ok(next_page()),
        rpc_ok([]),
    ]
    node.routes["rpc:getTransaction"] = history_replies(range(4))

    pages = [page async for page in indexer.iter_account_transactions(ADDRESS)]

    assert [len(page.records) for page in pages] == [2, 2]
    calls = node.rpc_params("getSignaturesForAddress")
    assert "before" not in calls[0][1]
    assert calls[1][1]["before"] == first_page()[-1]["signature"]
    assert calls[2][1]["before"] == next_page()[-1]["signature"]
    assert all(call[1]["limit"] == PAGE_SIZE for call in calls)


async def test_short_page_ends_the_walk(
    node: FakeNode, indexer: SolanaIndexer, small_pages: None
) -> None:
    node.routes["rpc:getSignaturesForAddress"] = rpc_ok(first_page()[:1])
    node.routes["rpc:getTransaction"] = rpc_ok(fixture("history_0"))
    pages = [page async for page in indexer.iter_account_transactions(ADDRESS)]
    assert pages[0].next_cursor is None
    assert len(node.rpc_params("getSignaturesForAddress")) == 1


async def test_range_is_applied_to_the_rows(
    node: FakeNode, indexer: SolanaIndexer, small_pages: None
) -> None:
    rows = [*first_page(), *next_page()]
    node.routes["rpc:getSignaturesForAddress"] = [rpc_ok(first_page()), rpc_ok(next_page())]
    node.routes["rpc:getTransaction"] = history_replies([1, 2])

    pages = [
        page
        async for page in indexer.iter_account_transactions(
            ADDRESS, from_height=rows[2]["slot"], to_height=rows[1]["slot"]
        )
    ]

    fetched = [record.id for page in pages for record in page.records]
    assert fetched == [rows[1]["signature"], rows[2]["signature"]]
    assert pages[-1].next_cursor is None


async def test_page_entirely_newer_than_the_range_keeps_walking(
    node: FakeNode, indexer: SolanaIndexer, small_pages: None
) -> None:
    rows = next_page()
    node.routes["rpc:getSignaturesForAddress"] = [rpc_ok(first_page()), rpc_ok(rows), rpc_ok([])]
    node.routes["rpc:getTransaction"] = history_replies([2, 3])
    pages = [
        page async for page in indexer.iter_account_transactions(ADDRESS, to_height=rows[0]["slot"])
    ]
    assert sum(len(page.records) for page in pages) == 2


async def test_pruned_transaction_is_a_clear_error(
    node: FakeNode, indexer: SolanaIndexer, small_pages: None
) -> None:
    node.routes["rpc:getSignaturesForAddress"] = rpc_ok(first_page()[:1])
    node.routes["rpc:getTransaction"] = rpc_ok(None)
    with pytest.raises(NotFound, match="archival endpoint"):
        [page async for page in indexer.iter_account_transactions(ADDRESS)]


async def test_row_without_a_confirmation_status_uses_the_commitment(
    node: FakeNode, indexer: SolanaIndexer, small_pages: None
) -> None:
    row = {**first_page()[0], "confirmationStatus": None}
    node.routes["rpc:getSignaturesForAddress"] = rpc_ok([row])
    node.routes["rpc:getTransaction"] = rpc_ok(fixture("history_0"))
    pages = [page async for page in indexer.iter_account_transactions(ADDRESS)]
    assert pages[0].records[0].native.confirmation == "confirmed"


@pytest.mark.parametrize(
    ("reply", "error", "message"),
    [
        (rpc_error(-32602, "Invalid param"), InvalidInput, "Invalid param"),
        (rpc_error(429, "Too many requests"), RateLimited, "dedicated endpoint"),
        (rpc_error(-32005, "Node is behind"), NetworkError, "Node is behind"),
        (Reply({"jsonrpc": "2.0", "id": 1, "error": "flat"}), NetworkError, "flat"),
        (Reply({"jsonrpc": "2.0", "id": 1}), MalformedResponse, "neither a result"),
        (Reply(status=429), RateLimited, "dedicated endpoint"),
    ],
)
async def test_rpc_failures_are_classified(
    node: FakeNode,
    indexer: SolanaIndexer,
    reply: Reply,
    error: type[Exception],
    message: str,
) -> None:
    node.routes["rpc:getSlot"] = reply
    with pytest.raises(error, match=message):
        await indexer.chain_head()


async def test_slot_that_is_not_a_number_is_refused(node: FakeNode, indexer: SolanaIndexer) -> None:
    node.routes["rpc:getSlot"] = rpc_ok("soon")
    with pytest.raises(MalformedResponse, match="slot number"):
        await indexer.chain_head()


async def test_bad_public_keys_never_reach_the_network(
    node: FakeNode, indexer: SolanaIndexer
) -> None:
    with pytest.raises(InvalidAddress, match="Solana address"):
        await indexer.fetch_account("0OIl")
    with pytest.raises(InvalidAddress, match="SPL token mint"):
        await indexer.fetch_asset("short")
    with pytest.raises(InvalidAddress, match="program ID"):
        await indexer.fetch_contract("short")
    with pytest.raises(InvalidAddress):
        [page async for page in indexer.iter_account_transactions("short")]
    assert node.rpc_calls == []


async def test_account_lookup_reads_both_token_programs(
    node: FakeNode, indexer: SolanaIndexer
) -> None:
    node.routes["rpc:getAccountInfo"] = rpc_ok(fixture("account"))
    node.routes["rpc:getTokenAccountsByOwner"] = [
        rpc_ok(fixture("token_accounts")),
        rpc_ok(fixture("token2022_accounts")),
    ]
    record = await indexer.fetch_account(ADDRESS)
    assert record.balance == fixture("account")["value"]["lamports"]
    programs = {call[1]["programId"] for call in node.rpc_params("getTokenAccountsByOwner")}
    assert programs == {PROGRAM, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


async def test_describe_uses_solana_terms(node: FakeNode, indexer: SolanaIndexer) -> None:
    node.routes["rpc:getAccountInfo"] = [
        rpc_ok(fixture("account")),
        rpc_ok(fixture("mint")),
        rpc_ok(fixture("program")),
        rpc_ok(fixture("program_data")),
    ]
    node.routes["rpc:getTokenAccountsByOwner"] = rpc_ok({"value": []})

    assert "SOL" in indexer.describe(await indexer.fetch_account(ADDRESS))["Balance"]
    assert "Mint authority" in indexer.describe(await indexer.fetch_asset(MINT))
    described = indexer.describe(await indexer.fetch_contract(PROGRAM))
    assert described["Program ID"] == PROGRAM
    assert described["Upgrade authority"] == "none"


async def test_describe_transaction_amounts(node: FakeNode, indexer: SolanaIndexer) -> None:
    node.routes["rpc:getBlock"] = rpc_ok(fixture("block_with_v1")["block"])
    records = await indexer.fetch_block(5)
    described = indexer.describe(records[0])
    assert described["Fee"].endswith("lamports")
    assert described["Priority fee"].endswith("lamports")

    node.routes["rpc:getSignaturesForAddress"] = rpc_ok(first_page()[:1])
    node.routes["rpc:getTransaction"] = rpc_ok(fixture("tx_spl_transfer"))
    page = await anext(indexer.iter_account_transactions(ADDRESS))
    assert "of mint" in indexer.describe(page.records[0])["Amount"]
    node.routes["rpc:getTransaction"] = rpc_ok(fixture("tx_sol_transfer"))
    page = await anext(indexer.iter_account_transactions(ADDRESS))
    assert indexer.describe(page.records[0])["Amount"].endswith("SOL")


def test_sync_stores_history_once_and_only_asks_for_new_rows_after(
    node: FakeNode, solana_settings: Settings, small_pages: None
) -> None:
    node.routes["rpc:getSlot"] = rpc_ok(HEAD)
    node.routes["rpc:getSignaturesForAddress"] = [
        rpc_ok(first_page()),
        rpc_ok(next_page()),
        rpc_ok([]),
    ]
    node.routes["rpc:getTransaction"] = history_replies(range(4))
    with Indexer("solana", settings=solana_settings) as indexer:
        first = indexer.sync(ADDRESS)
        assert first.records_fetched == 4
        assert indexer.network == "devnet"

        node.routes["rpc:getSignaturesForAddress"] = rpc_ok([])
        node.routes["rpc:getSlot"] = rpc_ok(HEAD + 500)
        second = indexer.sync(ADDRESS)
        stored = indexer.stored_transactions(account=ADDRESS)

    assert second.from_height == HEAD - adapter_module.FINALITY_MARGIN_SLOTS + 1
    assert second.records_fetched == 0
    assert len(stored) == 4
    assert {record.network for record in stored} == {"devnet"}


def test_interrupted_sync_resumes_from_the_signature_cursor(
    node: FakeNode, solana_settings: Settings, small_pages: None
) -> None:
    node.routes["rpc:getSlot"] = rpc_ok(HEAD)
    node.routes["rpc:getSignaturesForAddress"] = [
        rpc_ok(first_page()),
        Reply({"message": "restarting"}, status=503),
    ]
    node.routes["rpc:getTransaction"] = history_replies([0, 1])
    with Indexer("solana", settings=solana_settings) as indexer, pytest.raises(NetworkError):
        indexer.sync(ADDRESS)

    node.rpc_calls.clear()
    node.routes["rpc:getSignaturesForAddress"] = [rpc_ok(next_page()), rpc_ok([])]
    node.routes["rpc:getTransaction"] = history_replies([2, 3])
    with Indexer("solana", settings=solana_settings) as indexer:
        resumed = indexer.sync(ADDRESS)

    assert resumed.resumed
    assert resumed.records_fetched == 2
    assert (
        node.rpc_params("getSignaturesForAddress")[0][1]["before"] == first_page()[-1]["signature"]
    )
    assert node.rpc_params("getSlot") == []

    connection = sqlite3.connect(solana_settings.database)
    ids = sorted(row[0] for row in connection.execute("SELECT tx_id FROM transactions"))
    connection.close()
    assert ids == sorted(row["signature"] for row in [*first_page(), *next_page()])


def test_pending_records_become_final_on_the_next_sync(
    node: FakeNode, solana_settings: Settings, small_pages: None
) -> None:
    row = first_page()[0]
    node.routes["rpc:getSlot"] = rpc_ok(HEAD)
    node.routes["rpc:getSignaturesForAddress"] = rpc_ok(
        [{**row, "confirmationStatus": "confirmed"}]
    )
    node.routes["rpc:getTransaction"] = rpc_ok(fixture("history_0"))
    with Indexer("solana", settings=solana_settings) as indexer:
        indexer.sync(ADDRESS)
        assert indexer.stored_transactions()[0].status.value == "pending"

        node.routes["rpc:getSignaturesForAddress"] = rpc_ok([{**row, "slot": HEAD - 1}])
        indexer.sync(ADDRESS)
        statuses = [record.status.value for record in indexer.stored_transactions()]
    assert statuses == ["final"]


def test_metrics_through_the_api(
    node: FakeNode, solana_settings: Settings, small_pages: None
) -> None:
    node.routes["rpc:getSlot"] = rpc_ok(HEAD)
    node.routes["rpc:getSignaturesForAddress"] = rpc_ok(first_page()[:1])
    node.routes["rpc:getTransaction"] = rpc_ok(fixture("history_0"))
    with Indexer("solana", settings=solana_settings) as indexer:
        indexer.sync(ADDRESS)
        report = indexer.metrics(days=36500, account=ADDRESS)
    assert report.chain == "solana"
    assert report.transactions == 1
    assert "priority_fees_lamports" in report.native


@pytest.fixture
def invoke(node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SOLANA_DEVNET_RPC_SERVER", node.url)
    monkeypatch.setenv("ALGORAND_TESTNET_INDEXER_SERVER", node.url)
    monkeypatch.setenv("MCI_MAX_RETRIES", "0")
    monkeypatch.setenv("MCI_REQUESTS_PER_SECOND", "1000")

    def run(*arguments: str) -> Result:
        options = ["--db", str(tmp_path / "cli.db"), "--env-file", str(tmp_path / "none.env")]
        return CliRunner().invoke(cli, [*options, *arguments])

    return run


def test_cli_index_account_shows_sol_and_lamports(
    node: FakeNode, invoke: Any, small_pages: None
) -> None:
    node.routes["rpc:getAccountInfo"] = rpc_ok(fixture("account"))
    node.routes["rpc:getTokenAccountsByOwner"] = rpc_ok({"value": []})
    node.routes["rpc:getSignaturesForAddress"] = rpc_ok(first_page()[:1])
    node.routes["rpc:getTransaction"] = rpc_ok(fixture("history_0"))
    result = invoke("--chain", "solana", "index", "account", ADDRESS, "--limit", "1")
    assert result.exit_code == 0, result.output
    assert "SOL" in result.stdout
    assert "lamports" in result.stdout
    assert first_page()[0]["signature"] in result.stdout


def test_cli_index_token_and_program(node: FakeNode, invoke: Any) -> None:
    node.routes["rpc:getAccountInfo"] = rpc_ok(fixture("mint"))
    assert "Mint authority" in invoke("--chain", "solana", "index", "token", MINT).stdout

    node.routes["rpc:getAccountInfo"] = [
        rpc_ok(fixture("program")),
        rpc_ok(fixture("program_data")),
    ]
    assert "Upgrade authority" in invoke("--chain", "solana", "index", "program", PROGRAM).stdout


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--chain", "solana", "index", "account", "BAD"), "not a valid Solana address"),
        (("--chain", "solana", "index", "token", "BAD"), "SPL token mint"),
        (("--chain", "solana", "index", "program", "BAD"), "program ID"),
        (("--chain", "solana", "index", "asset", "7"), "index asset is for algorand"),
        (("--chain", "solana", "index", "app", "7"), "index app is for algorand"),
        (("index", "token", MINT), "index token is for solana"),
        (("index", "program", PROGRAM), "index program is for solana"),
        (("--network", "devnet", "metrics"), "unknown Algorand network"),
    ],
)
def test_cli_reports_bad_input_in_one_clean_line(
    invoke: Any, arguments: tuple[str, ...], message: str
) -> None:
    result = invoke(*arguments)
    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output


def test_cli_metrics_and_export(node: FakeNode, invoke: Any, small_pages: None) -> None:
    node.routes["rpc:getSlot"] = rpc_ok(HEAD)
    node.routes["rpc:getSignaturesForAddress"] = rpc_ok(first_page()[:1])
    node.routes["rpc:getTransaction"] = rpc_ok(fixture("history_0"))
    assert invoke("--chain", "solana", "sync", ADDRESS).exit_code == 0

    report = json.loads(invoke("--chain", "solana", "metrics", "--days", "36500").stdout)
    assert report["chain"] == "solana"
    assert report["network"] == "devnet"
    exported = invoke("--chain", "solana", "export", "--format", "csv").stdout
    assert first_page()[0]["signature"] in exported
    assert ",solana,devnet," in exported
