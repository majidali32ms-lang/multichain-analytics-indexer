"""Smoke tests against the six EVM testnets. Run with ``pytest -m live --no-cov``."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from multichain_indexer import Indexer, WrongNetwork
from multichain_indexer.adapters.evm.profiles import BNB, EVM_CHAINS
from multichain_indexer.core.config import Settings

pytestmark = pytest.mark.live

# Wrapped ETH, a predeploy at the same address on every OP Stack chain, Base included.
OP_STACK_WETH = "0x4200000000000000000000000000000000000006"


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
