"""Smoke tests against Solana devnet. Run with ``pytest -m live --no-cov``."""

from __future__ import annotations

import asyncio
import runpy
import sys
from pathlib import Path

import nbformat
import pytest
from nbclient import NotebookClient

from multichain_indexer import Indexer, NotFound
from multichain_indexer.adapters.solana import create_indexer
from multichain_indexer.core.config import Settings

pytestmark = pytest.mark.live

ACCOUNT = "FwcsNmTLbf8VVqxuBMjEDTh39gXR87n3vAnAwvAiPtRy"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
RECENT_BLOCKS_TO_TRY = 20
SLOTS_BEHIND_HEAD = 200
EXAMPLES = Path(__file__).parent.parent / "examples" / "solana"


def test_lookups_and_incremental_sync_on_devnet(tmp_path: Path) -> None:
    with Indexer("solana", database=tmp_path / "live.db") as indexer:
        assert indexer.account(ACCOUNT).network == "devnet"
        assert indexer.asset(WRAPPED_SOL_MINT).decimals == 9
        assert indexer.contract(TOKEN_PROGRAM).native.program_data

        first = indexer.sync(ACCOUNT)
        stored = len(indexer.stored_transactions(account=ACCOUNT))
        second = indexer.sync(ACCOUNT)

        assert first.records_fetched > 0
        assert stored == first.records_fetched
        assert second.from_height is not None
        assert indexer.metrics(days=3650, account=ACCOUNT).chain == "solana"


@pytest.mark.parametrize("cluster", ["devnet", "testnet", "mainnet"])
def test_public_endpoint_serves_its_recorded_genesis_hash(cluster: str) -> None:
    async def head() -> int:
        adapter = create_indexer(cluster, Settings())
        try:
            return await adapter.chain_head()
        finally:
            await adapter.aclose()

    assert asyncio.run(head()) > 0


def test_recent_block_with_a_v1_transaction_is_read_on_devnet() -> None:
    async def find_v1() -> set[str]:
        adapter = create_indexer("devnet", Settings())
        head = await adapter.chain_head()
        versions: set[str] = set()
        for offset in range(RECENT_BLOCKS_TO_TRY):
            try:
                records = await adapter.fetch_block(head - SLOTS_BEHIND_HEAD - offset)
            except NotFound:
                continue
            versions |= {record.native.version for record in records}
            if "1" in versions:
                break
        await adapter.aclose()
        return versions

    assert "1" in asyncio.run(find_v1())


def test_example_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["account_summary.py"])
    runpy.run_path(str(EXAMPLES / "account_summary.py"), run_name="__main__")


@pytest.mark.filterwarnings("ignore")
def test_example_notebook_runs() -> None:
    notebook = nbformat.read(EXAMPLES / "account_analysis.ipynb", as_version=4)
    NotebookClient(notebook, timeout=180, kernel_name="python3").execute()
