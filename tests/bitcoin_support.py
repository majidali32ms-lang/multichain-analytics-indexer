"""Saved Esplora replies and the routes that serve them, shared by the Bitcoin tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from multichain_indexer.adapters.bitcoin.networks import NETWORKS, TESTNET4
from multichain_indexer.adapters.bitcoin.scripts import parse_locator
from tests.conftest import FakeNode, Reply

FIXTURES = Path(__file__).parent / "fixtures" / "bitcoin"
SMALL = "mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu"
BUSY = "tb1qlj64u6fqutr0xue85kl55fx0gt4m4urun25p7q"
TIP = 161_111
SERVER_SETTING = "BITCOIN_TESTNET4_ESPLORA_SERVER"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def script_path(address: str, suffix: str = "") -> str:
    return f"/scripthash/{parse_locator(address, NETWORKS[TESTNET4]).script_hash}{suffix}"


def serve_chain(node: FakeNode, tip: int = TIP, genesis: str = "testnet4_blocks_0") -> None:
    node.routes["/blocks/0"] = Reply(fixture(genesis))
    node.routes["/blocks/tip/height"] = Reply(raw=str(tip))


def serve_small(node: FakeNode) -> None:
    serve_chain(node)
    node.routes[script_path(SMALL)] = Reply(fixture("testnet4_scripthash_small"))
    node.routes[script_path(SMALL, "/txs/mempool")] = Reply([])
    node.routes[script_path(SMALL, "/txs/chain")] = Reply(
        fixture("testnet4_scripthash_small_chain")
    )
