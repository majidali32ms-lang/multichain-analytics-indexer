"""Smoke tests against mempool.space on testnet4, signet and mainnet. Run with
``pytest -m live --no-cov``. About 30 requests at one a second."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import nbformat
import pytest
from nbclient import NotebookClient
from pydantic import SecretStr

from multichain_indexer import Indexer, WrongNetwork
from multichain_indexer.adapters.bitcoin.models import BitcoinAddress, BitcoinTransaction
from multichain_indexer.core.config import Settings
from multichain_indexer.core.models import Status
from multichain_indexer.core.registry import create_indexer

pytestmark = pytest.mark.live

EXAMPLES = Path(__file__).parent.parent / "examples" / "bitcoin"
# A small testnet4 P2PKH address: four transactions on 2026-11-13.
SMALL = "mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu"
# A testnet4 transaction spending a P2TR output; mempool.space showed 310 sats, 2.01 sat/vB.
TAPROOT_TX = "23246681e815c1cc7f70a04f2feed025a9c3d6335aa3a4bcb8ba89692355296b"
# The key mainnet block 9 paid to: a P2PK script with six outputs received by 2026-11-13.
BLOCK_9_KEY = (
    "0411db93e1dcdb8a016b49840f8c53bc1eb68a382e97b1482ecad7b148a6909a5c"
    "b2e0eaddfb84ccf9744464f82e160bfa9b8b64f9d4c03f999b8643f656b412a3"
)


@pytest.mark.parametrize("network", ["testnet4", "signet", "mainnet"])
async def test_the_default_server_serves_the_network_asked_for(network: str) -> None:
    adapter = create_indexer("bitcoin", network, Settings())
    try:
        assert await adapter.chain_head() > 0
    finally:
        await adapter.aclose()


async def test_a_server_on_another_network_is_refused() -> None:
    settings = Settings(
        env={"BITCOIN_TESTNET4_ESPLORA_SERVER": SecretStr("https://mempool.space/signet/api")}
    )
    adapter = create_indexer("bitcoin", "testnet4", settings)
    try:
        with pytest.raises(WrongNetwork, match="serves Bitcoin signet"):
            await adapter.chain_head()
    finally:
        await adapter.aclose()


def test_an_address_and_its_transactions(tmp_path: Path) -> None:
    with Indexer("bitcoin", settings=Settings(), database=tmp_path / "live.db") as indexer:
        account = indexer.account(SMALL)
        result = indexer.sync(SMALL)
        stored = indexer.stored_transactions(account=SMALL)
    assert isinstance(account.native, BitcoinAddress)
    assert account.native.address_type == "p2pkh"
    assert account.native.transactions >= 4
    assert result.records_fetched == len(stored) >= 4
    assert all(SMALL in _parties(record.native) for record in stored)


def test_one_transaction_matches_mempool_space(tmp_path: Path) -> None:
    with Indexer("bitcoin", settings=Settings(), database=tmp_path / "live.db") as indexer:
        record = indexer.transaction(TAPROOT_TX)
    native = record.native
    assert isinstance(native, BitcoinTransaction)
    assert record.fee == 310
    assert f"{native.fee_rate:.3g}" == "2.01"
    assert native.taproot
    assert record.status is Status.FINAL


def test_a_p2pk_public_key_on_mainnet(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    with Indexer("bitcoin", "mainnet", settings=Settings(), database=database) as indexer:
        account = indexer.account(BLOCK_9_KEY)
    assert isinstance(account.native, BitcoinAddress)
    assert account.native.address_type == "p2pk"
    assert account.native.received_outputs >= 6


def _parties(native: object) -> set[str | None]:
    assert isinstance(native, BitcoinTransaction)
    return {entry.address for entry in native.inputs} | {out.address for out in native.outputs}


def test_example_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["address_summary.py"])
    runpy.run_path(str(EXAMPLES / "address_summary.py"), run_name="__main__")


# pyzmq warns about Windows' default event loop when the notebook kernel starts; it is not this
# project's warning, and the notebook still runs.
@pytest.mark.filterwarnings("ignore:Proactor event loop:RuntimeWarning")
def test_example_notebook_runs() -> None:
    notebook = nbformat.read(EXAMPLES / "address_analysis.ipynb", as_version=4)
    NotebookClient(notebook, timeout=300, kernel_name="python3").execute()
