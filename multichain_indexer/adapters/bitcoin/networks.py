"""The Bitcoin networks this adapter reads, and the genesis block that identifies each.

testnet4 (BIP-94) is the default: it replaces testnet3, which Bitcoin Core plans to phase
out. Signet is the other public test network. ``localnet`` is a regtest node with a local
Esplora server, which electrs serves on port 3002 for regtest.
"""

from __future__ import annotations

from dataclasses import dataclass

TESTNET4 = "testnet4"
SIGNET = "signet"
MAINNET = "mainnet"
LOCALNET = "localnet"


@dataclass(frozen=True)
class BitcoinNetwork:
    """A network: its name here, its name in embit, its genesis hash and default server."""

    name: str
    embit_name: str
    genesis_hash: str
    esplora_server: str


NETWORKS = {
    TESTNET4: BitcoinNetwork(
        TESTNET4,
        "test",
        "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043",
        "https://mempool.space/testnet4/api",
    ),
    SIGNET: BitcoinNetwork(
        SIGNET,
        "signet",
        "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6",
        "https://mempool.space/signet/api",
    ),
    MAINNET: BitcoinNetwork(
        MAINNET,
        "main",
        "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
        "https://mempool.space/api",
    ),
    LOCALNET: BitcoinNetwork(
        LOCALNET,
        "regtest",
        "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206",
        "http://localhost:3002",
    ),
}

# Named in a wrong-network message when a server's genesis block is one of these.
KNOWN_GENESIS = {
    NETWORKS[TESTNET4].genesis_hash: "Bitcoin testnet4",
    NETWORKS[SIGNET].genesis_hash: "Bitcoin signet",
    NETWORKS[MAINNET].genesis_hash: "Bitcoin mainnet",
    NETWORKS[LOCALNET].genesis_hash: "a Bitcoin regtest network",
    "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943": "Bitcoin testnet3",
}
