"""Smoke tests against the six EVM testnets. Run with ``pytest -m live --no-cov``.

History tests need the services' free keys in the environment (``BLOCKSCOUT_API_KEY``,
``ETHERSCAN_API_KEY``); without one they are skipped and say which key is missing.
"""

from __future__ import annotations

import asyncio
import os

import pytest
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


def require_key(name: str) -> None:
    if not os.environ.get(name):
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
    with Indexer("ethereum", settings=Settings.from_env(None)) as indexer:
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
