"""Maps a chain name to the code that serves it.

Adapters are referenced as ``"module:factory"`` strings and imported on first use, so
callers never import an adapter directly and one adapter can serve several chains.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import cast

from multichain_indexer.core.base_indexer import BaseIndexer
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import UnsupportedChain
from multichain_indexer.core.http import ResponseStore

PLANNED_CHAINS = (
    "algorand",
    "solana",
    "near",
    "ethereum",
    "arbitrum",
    "optimism",
    "base",
    "polygon",
    "bnb",
    "bitcoin",
)

IndexerFactory = Callable[[str | None, Settings, ResponseStore | None], BaseIndexer]

_factories: dict[str, str] = {}


def register_chain(chain: str, factory_path: str) -> None:
    """Make ``chain`` available, served by the factory at ``"module:attribute"``."""
    _factories[chain] = factory_path


def create_indexer(
    chain: str, network: str | None, settings: Settings, cache: ResponseStore | None = None
) -> BaseIndexer:
    """Build the adapter for ``chain``; ``network`` of None means the chain's test network.

    Raises:
        UnsupportedChain: The chain is unknown or not available in this version.
    """
    return _load_factory(chain)(network, settings, cache)


def _load_factory(chain: str) -> IndexerFactory:
    factory_path = _factories.get(chain)
    if factory_path is None:
        raise UnsupportedChain(_unavailable_message(chain))
    module_name, attribute = factory_path.split(":")
    return cast(IndexerFactory, getattr(import_module(module_name), attribute))


def _unavailable_message(chain: str) -> str:
    if chain in PLANNED_CHAINS:
        return f"{chain} is available in a later version"
    return f"unknown chain {chain!r}; planned chains are: {', '.join(PLANNED_CHAINS)}"
