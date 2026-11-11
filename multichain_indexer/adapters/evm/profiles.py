"""What makes each EVM chain different. A new EVM chain is one more profile here.

Chain IDs and public endpoints are from each chain's own developer docs. Ethereum's docs list
no public endpoint, so its defaults are a public provider's; every default answered
``eth_chainId`` with its documented chain ID when checked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MAINNET = "mainnet"
TESTNET = "testnet"
LOCALNET = "localnet"

# Anvil's defaults. A local node may use any chain ID that no public network uses.
LOCALNET_RPC_SERVER = "http://localhost:8545"
LOCALNET_TITLE = "a local network"

# Names in the adapter's HISTORY_SOURCES.
BLOCKSCOUT = "blockscout"
ETHERSCAN = "etherscan"
RPC_SCAN = "rpc-scan"


@dataclass(frozen=True)
class EvmNetwork:
    """A public network. ``history_source`` is the default for account history, or None when
    no free service lists it."""

    title: str
    chain_id: int
    rpc_server: str
    history_source: str | None = BLOCKSCOUT

    def describe(self) -> str:
        return f"{self.title} (chain ID {self.chain_id})"


@dataclass(frozen=True)
class EvmChain:
    name: str
    title: str
    native_symbol: str
    token_standard: str
    networks: Mapping[str, EvmNetwork]

    @property
    def network_names(self) -> tuple[str, ...]:
        return (*self.networks, LOCALNET)


ETHEREUM = EvmChain(
    name="ethereum",
    title="Ethereum",
    native_symbol="ETH",
    token_standard="ERC-20",
    networks={
        MAINNET: EvmNetwork("Ethereum mainnet", 1, "https://ethereum-rpc.publicnode.com"),
        TESTNET: EvmNetwork("Sepolia", 11155111, "https://ethereum-sepolia-rpc.publicnode.com"),
    },
)

# docs.arbitrum.io/for-devs/dev-tools-and-resources/chain-info
ARBITRUM = EvmChain(
    name="arbitrum",
    title="Arbitrum",
    native_symbol="ETH",
    token_standard="ERC-20",
    networks={
        MAINNET: EvmNetwork("Arbitrum One", 42161, "https://arb1.arbitrum.io/rpc"),
        TESTNET: EvmNetwork("Arbitrum Sepolia", 421614, "https://sepolia-rollup.arbitrum.io/rpc"),
    },
)

# docs.optimism.io/superchain/networks
OPTIMISM = EvmChain(
    name="optimism",
    title="OP Mainnet",
    native_symbol="ETH",
    token_standard="ERC-20",
    networks={
        MAINNET: EvmNetwork("OP Mainnet", 10, "https://mainnet.optimism.io"),
        TESTNET: EvmNetwork("OP Sepolia", 11155420, "https://sepolia.optimism.io"),
    },
)

# docs.base.org/base-chain/quickstart/connecting-to-base
BASE = EvmChain(
    name="base",
    title="Base",
    native_symbol="ETH",
    token_standard="ERC-20",
    networks={
        # Blockscout's free plan dropped Base on 2026-10-01 and Etherscan's never had it, so
        # neither is a default; the owner chooses a free source.
        MAINNET: EvmNetwork("Base Mainnet", 8453, "https://mainnet.base.org", None),
        TESTNET: EvmNetwork("Base Sepolia", 84532, "https://sepolia.base.org", None),
    },
)

# docs.polygon.technology/pos/reference/rpc-endpoints
POLYGON = EvmChain(
    name="polygon",
    title="Polygon PoS",
    native_symbol="POL",
    token_standard="ERC-20",
    networks={
        # Blockscout's free plan dropped Polygon on 2026-10-01 and lists no Amoy explorer;
        # Etherscan's free plan answers on both, as checked on 2026-11-11.
        MAINNET: EvmNetwork("Polygon PoS mainnet", 137, "https://polygon.drpc.org", ETHERSCAN),
        TESTNET: EvmNetwork("Amoy", 80002, "https://polygon-amoy.drpc.org", ETHERSCAN),
    },
)

# docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint
BNB = EvmChain(
    name="bnb",
    title="BNB Smart Chain",
    native_symbol="BNB",
    token_standard="BEP-20",
    networks={
        # Blockscout has no BNB Smart Chain explorer, and Etherscan serves it on paid plans only.
        MAINNET: EvmNetwork(
            "BNB Smart Chain mainnet", 56, "https://bsc-dataseed.bnbchain.org", None
        ),
        TESTNET: EvmNetwork(
            "BNB Smart Chain testnet", 97, "https://bsc-testnet-dataseed.bnbchain.org", None
        ),
    },
)

EVM_CHAINS: tuple[EvmChain, ...] = (ETHEREUM, ARBITRUM, OPTIMISM, BASE, POLYGON, BNB)

PUBLIC_NETWORKS_BY_CHAIN_ID: Mapping[int, EvmNetwork] = {
    network.chain_id: network for chain in EVM_CHAINS for network in chain.networks.values()
}
