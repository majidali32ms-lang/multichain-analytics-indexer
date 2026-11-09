"""Smoke tests against Algorand testnet. Run with ``pytest -m live --no-cov``."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import nbformat
import pytest
from nbclient import NotebookClient

from multichain_indexer import Indexer, NotFound

pytestmark = pytest.mark.live

ACCOUNT = "JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM"
ASA_ID = 106414664
APP_ID = 600011887
EXAMPLES = Path(__file__).parent.parent / "examples" / "algorand"


def test_lookups_and_incremental_sync_on_testnet(tmp_path: Path) -> None:
    with Indexer("algorand", database=tmp_path / "live.db") as indexer:
        assert indexer.account(ACCOUNT).network == "testnet"
        assert indexer.asset(ASA_ID).id == str(ASA_ID)
        assert indexer.contract(APP_ID).id == str(APP_ID)

        first = indexer.sync(ACCOUNT)
        stored = len(indexer.stored_transactions(account=ACCOUNT))
        second = indexer.sync(ACCOUNT)

        assert first.records_fetched > 0
        assert stored == first.records_fetched
        assert second.from_height == first.to_height + 1
        assert len(indexer.stored_transactions(account=ACCOUNT)) == stored + second.records_fetched
        with pytest.raises(NotFound):
            indexer.asset(2**63)


def test_example_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["account_summary.py"])
    runpy.run_path(str(EXAMPLES / "account_summary.py"), run_name="__main__")


@pytest.mark.filterwarnings("ignore")
def test_example_notebook_runs() -> None:
    notebook = nbformat.read(EXAMPLES / "account_analysis.ipynb", as_version=4)
    NotebookClient(notebook, timeout=120, kernel_name="python3").execute()


def test_native_falcon_account_on_mainnet_is_post_quantum(tmp_path: Path) -> None:
    falcon_account = "PPF57PQNUKYUGNH7SENJFOJFFD7T2QFFCWDKAZ2TCPCURLS6QU7NRFNN7Y"
    with Indexer("algorand", "mainnet", database=tmp_path / "live.db") as indexer:
        account = indexer.account(falcon_account)
        recent = indexer.account_transactions(falcon_account, limit=5)
    assert account.native.pq is not None
    assert any(record.native.pq and record.native.pq.scheme == "falcon-1024" for record in recent)
