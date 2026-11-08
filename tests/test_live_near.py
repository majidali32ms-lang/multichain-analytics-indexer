"""Smoke tests against NEAR. Run with ``pytest -m live --no-cov``."""

from __future__ import annotations

import asyncio
import runpy
import sys
from pathlib import Path

import nbformat
import pytest
from nbclient import NotebookClient
from pydantic import SecretStr

from multichain_indexer import Indexer, WrongNetwork
from multichain_indexer.adapters.near import create_indexer
from multichain_indexer.core.config import Settings

pytestmark = pytest.mark.live

TESTNET_ACCOUNT = "f781edbfe07287152e3912cb698af4155234df3c2d9c23bf625f675b8f6de5fa"
TESTNET_CONTRACT = "guest-book.testnet"
MAINNET_ACCOUNT = "root.near"
MAINNET_CONTRACT = "wrap.near"
EXAMPLES = Path(__file__).parent.parent / "examples" / "near"


def test_lookups_and_incremental_sync_on_testnet(tmp_path: Path) -> None:
    with Indexer("near", database=tmp_path / "live.db") as indexer:
        account = indexer.account(TESTNET_ACCOUNT)
        assert account.network == "testnet"
        assert account.native.exists
        assert account.native.shard_id is not None
        assert indexer.contract(TESTNET_CONTRACT).native.code_hash

        first = indexer.sync(TESTNET_ACCOUNT)
        stored = indexer.stored_transactions(account=TESTNET_ACCOUNT)
        second = indexer.sync(TESTNET_ACCOUNT)

        assert first.records_fetched > 0
        assert len(stored) == first.records_fetched
        assert second.from_height is not None
        assert indexer.metrics(days=3650, account=TESTNET_ACCOUNT).chain == "near"


def test_lookups_and_recent_history_on_mainnet(tmp_path: Path) -> None:
    with Indexer("near", "mainnet", database=tmp_path / "live.db") as indexer:
        assert indexer.account(MAINNET_ACCOUNT).native.exists
        assert indexer.contract(MAINNET_CONTRACT).native.code_hash
        recent = indexer.account_transactions(MAINNET_ACCOUNT, limit=5)
        assert len(recent) == 5
        assert all(record.fee > 0 for record in recent)


def test_rpc_endpoint_of_the_other_network_is_refused() -> None:
    async def head() -> int:
        env = {"NEAR_TESTNET_RPC_SERVER": SecretStr("https://near.drpc.org")}
        adapter = create_indexer("testnet", Settings(env=env))
        try:
            return await adapter.chain_head()
        finally:
            await adapter.aclose()

    with pytest.raises(WrongNetwork, match="serves NEAR mainnet, not NEAR testnet"):
        asyncio.run(head())


def test_example_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["account_summary.py"])
    runpy.run_path(str(EXAMPLES / "account_summary.py"), run_name="__main__")


@pytest.mark.filterwarnings("ignore")
def test_example_notebook_runs() -> None:
    notebook = nbformat.read(EXAMPLES / "account_analysis.ipynb", as_version=4)
    NotebookClient(notebook, timeout=240, kernel_name="python3").execute()
