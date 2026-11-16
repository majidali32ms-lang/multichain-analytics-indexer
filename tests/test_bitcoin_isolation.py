"""The Bitcoin adapter needs only ``core/``, so it can later ship as a package of its own."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).parent.parent / "multichain_indexer"
BITCOIN = PACKAGE / "adapters" / "bitcoin"
# Every other chain's adapter, the post-quantum package, and the libraries only they use.
ABSENT = (
    "multichain_indexer.adapters.algorand",
    "multichain_indexer.adapters.solana",
    "multichain_indexer.adapters.near",
    "multichain_indexer.adapters.evm",
    "multichain_indexer.pq",
    "algosdk",
    "solders",
    "web3",
    "eth_utils",
    "eth_abi",
    "base58",
)
SCRIPT = f"""
import importlib.abc
import sys

ABSENT = {ABSENT!r}


class NotInstalled(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == absent or name.startswith(absent + ".") for absent in ABSENT):
            raise ModuleNotFoundError(f"No module named {{name!r}}")
        return None


sys.meta_path.insert(0, NotInstalled())

from multichain_indexer.core.config import Settings
from multichain_indexer.core.registry import create_indexer
import multichain_indexer.adapters

indexer = create_indexer("bitcoin", None, Settings())
print(indexer.network, indexer.validate_address("tb1qlj64u6fqutr0xue85kl55fx0gt4m4urun25p7q"))
loaded = sorted(name for name in sys.modules if name.startswith(ABSENT))
print("loaded:", loaded)
"""


def test_the_adapter_works_with_no_other_adapter_installed() -> None:
    finished = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=False, timeout=60
    )
    assert finished.returncode == 0, finished.stderr
    assert finished.stdout.splitlines() == [
        "testnet4 tb1qlj64u6fqutr0xue85kl55fx0gt4m4urun25p7q",
        "loaded: []",
    ]


def test_the_adapter_imports_nothing_outside_core_and_itself() -> None:
    allowed = ("multichain_indexer.core", "multichain_indexer.adapters.bitcoin")
    for path in BITCOIN.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        own = [module for module in modules if module.startswith("multichain_indexer")]
        assert all(module.startswith(allowed) for module in own), (path.name, own)
