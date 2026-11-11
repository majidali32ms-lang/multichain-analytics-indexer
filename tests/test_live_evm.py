"""Smoke tests against the six EVM testnets. Run with ``pytest -m live --no-cov``.

History tests need the services' free keys in the environment or in .env
(``BLOCKSCOUT_API_KEY``, ``ETHERSCAN_API_KEY``, ``NODEREAL_API_KEY``); without one they are
skipped and say which key is missing.
"""

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
from multichain_indexer.adapters.evm import EvmIndexer
from multichain_indexer.adapters.evm.profiles import BNB, EVM_CHAINS
from multichain_indexer.core.config import Settings
from multichain_indexer.core.registry import create_indexer

pytestmark = pytest.mark.live

# Wrapped ETH, a predeploy at the same address on every OP Stack chain, Base included.
OP_STACK_WETH = "0x4200000000000000000000000000000000000006"
# A small Sepolia account: 20 transactions on 2026-11-09.
SEPOLIA_ACCOUNT = "0xAA37651031A4b641962550415dB2C43E8d962232"
# A small BNB Smart Chain mainnet sender, two transactions in its newest million blocks.
BSC_ACCOUNT = "0x5664a59eee2ab649c218da75c47805935c4a20cb"
EXAMPLES = Path(__file__).parent.parent / "examples" / "evm"


def require_key(name: str) -> None:
    if name not in Settings.from_env().env:
        pytest.skip(f"{name} is not set, so this history test is skipped")


@pytest.mark.parametrize("chain", [chain.name for chain in EVM_CHAINS])
def test_default_testnet_endpoint_serves_its_chain_id(chain: str) -> None:
    with Indexer(chain, settings=Settings()) as indexer:
        assert indexer.network == "testnet"
        assert indexer.account(OP_STACK_WETH).native.block_number > 0


@pytest.mark.parametrize("chain", ["optimism", "base"])
def test_contract_on_an_op_stack_testnet(chain: str) -> None:
    with Indexer(chain, settings=Settings()) as indexer:
        assert indexer.contract(OP_STACK_WETH).native.code_size > 0


def test_endpoint_of_another_chain_is_refused() -> None:
    env = {"BASE_TESTNET_RPC_SERVER": SecretStr(BNB.networks["testnet"].rpc_server)}
    with (
        Indexer("base", settings=Settings(env=env)) as indexer,
        pytest.raises(WrongNetwork, match="serves BNB Smart Chain testnet"),
    ):
        indexer.account(OP_STACK_WETH)


def test_sepolia_history_from_blockscout() -> None:
    require_key("BLOCKSCOUT_API_KEY")
    with Indexer("ethereum", settings=Settings.from_env()) as indexer:
        records = indexer.account_transactions(SEPOLIA_ACCOUNT, limit=3)
    assert len(records) == 3
    assert all(SEPOLIA_ACCOUNT in (record.sender, record.receiver) for record in records)


async def test_bnb_testnet_reads_a_recent_transaction_by_hash() -> None:
    adapter = create_indexer("bnb", None, Settings())
    assert isinstance(adapter, EvmIndexer)
    try:
        head = await adapter.chain_head()
        tx_hash = await _first_transaction_hash(adapter, head - 5)
        record = await adapter.fetch_transaction(tx_hash)
    finally:
        await adapter.aclose()
    assert record.id == tx_hash
    assert record.chain == "bnb"
    assert record.native.block_number <= head


async def _first_transaction_hash(adapter: EvmIndexer, start: int) -> str:
    for number in range(start, start - 20, -1):
        transactions = await adapter._rpc.block_transactions(number)
        if transactions:
            return str(transactions[0]["hash"])
        await asyncio.sleep(0.5)
    pytest.fail("no transaction in 20 recent BNB Smart Chain testnet blocks")


def test_sepolia_sync_stores_every_transaction_with_its_receipt(tmp_path: Path) -> None:
    require_key("BLOCKSCOUT_API_KEY")
    settings = Settings.from_env().model_copy(update={"database": tmp_path / "live.db"})
    with Indexer("ethereum", settings=settings) as indexer:
        first = indexer.sync(SEPOLIA_ACCOUNT)
        stored = indexer.stored_transactions(account=first.account)
    assert first.records_fetched >= 20
    assert len(stored) == first.records_fetched


def test_polygon_amoy_history_from_etherscan() -> None:
    require_key("ETHERSCAN_API_KEY")
    with Indexer("polygon", settings=Settings.from_env()) as indexer:
        head = indexer.account_transactions(OP_STACK_WETH, limit=1)
    assert all(record.native.history_source == "etherscan" for record in head)


def test_bnb_mainnet_history_from_nodereal() -> None:
    require_key("NODEREAL_API_KEY")
    with Indexer("bnb", "mainnet", settings=Settings.from_env()) as indexer:
        records = indexer.account_transactions(BSC_ACCOUNT, limit=1)
    assert records
    assert records[0].native.history_source == "nodereal"


def test_example_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    require_key("BLOCKSCOUT_API_KEY")
    monkeypatch.setattr(sys, "argv", ["account_summary.py"])
    runpy.run_path(str(EXAMPLES / "account_summary.py"), run_name="__main__")


# pyzmq warns about Windows' default event loop when the notebook kernel starts; it is not this
# project's warning, and the notebook still runs.
@pytest.mark.filterwarnings("ignore:Proactor event loop:RuntimeWarning")
def test_example_notebook_runs() -> None:
    require_key("BLOCKSCOUT_API_KEY")
    notebook = nbformat.read(EXAMPLES / "account_analysis.ipynb", as_version=4)
    NotebookClient(notebook, timeout=300, kernel_name="python3").execute()


# On Arbitrum Sepolia, this transaction moved tokens to INNER_RECEIVER inside another account's
# call: the "all" scope lists it for INNER_RECEIVER, "direct" does not.
INNER_TX = "0x67398b1eef1d67328d1f558e7d5131ec08f3cd9b8d909a38c281ec287a2f0e08"
INNER_RECEIVER = "0xc4a2695fc61c77b0a2cdf85a2348d3fbd6a58776"
INNER_BLOCK = 328259024


@pytest.mark.parametrize(("scope", "listed"), [("direct", False), ("all", True)])
async def test_all_scope_lists_a_token_transfer_inside_another_call(
    scope: str, listed: bool
) -> None:
    require_key("BLOCKSCOUT_API_KEY")
    settings = Settings.from_env()
    env = {**settings.env, "ARBITRUM_TESTNET_HISTORY_SCOPE": SecretStr(scope)}
    adapter = create_indexer("arbitrum", None, settings.model_copy(update={"env": env}))
    try:
        hashes = [
            record.id
            async for page in adapter.iter_account_transactions(
                INNER_RECEIVER, from_height=INNER_BLOCK, to_height=INNER_BLOCK
            )
            for record in page.records
        ]
    finally:
        await adapter.aclose()
    assert (INNER_TX in hashes) is listed
