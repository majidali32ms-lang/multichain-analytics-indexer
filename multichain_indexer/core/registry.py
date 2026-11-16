"""Maps a chain name to the code that serves it.

Built-in adapters are referenced as ``"module:factory"`` strings and imported on first use,
so callers never import an adapter directly and one adapter can serve several chains.
Other packages add chains through the entry point group ``multichain_indexer.adapters``:
the entry point's name is the chain, its value the factory. A plugin is imported only when
its chain is used, and it can never take the name of a built-in chain.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from importlib.metadata import EntryPoint, entry_points
from typing import cast

from multichain_indexer.core.base_indexer import BaseIndexer
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError, UnsupportedChain
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
ENTRY_POINT_GROUP = "multichain_indexer.adapters"

IndexerFactory = Callable[[str | None, Settings, ResponseStore | None], BaseIndexer]

_factories: dict[str, str] = {}


def register_chain(chain: str, factory_path: str) -> None:
    """Make ``chain`` available, served by the factory at ``"module:attribute"``."""
    _factories[chain] = factory_path


def available_chains() -> tuple[str, ...]:
    """Return the planned built-in chains, then installed plugin chains by name.

    Raises:
        ConfigError: An installed plugin uses the name of a built-in chain.
    """
    return (*PLANNED_CHAINS, *sorted(_plugins()))


def create_indexer(
    chain: str, network: str | None, settings: Settings, cache: ResponseStore | None = None
) -> BaseIndexer:
    """Build the adapter for ``chain``; ``network`` of None means the chain's test network.

    Raises:
        UnsupportedChain: The chain is unknown or not available in this version.
        ConfigError: A plugin uses a built-in chain's name, or its factory does not
            return an adapter.
    """
    indexer = _load_factory(chain)(network, settings, cache)
    if not isinstance(indexer, BaseIndexer):
        raise ConfigError(f"the factory for chain {chain!r} did not return a BaseIndexer")
    return indexer


def _load_factory(chain: str) -> IndexerFactory:
    plugins = _plugins()
    factory_path = _factories.get(chain)
    if factory_path is not None:
        module_name, attribute = factory_path.split(":")
        return cast(IndexerFactory, getattr(import_module(module_name), attribute))
    plugin = plugins.get(chain)
    if plugin is None:
        raise UnsupportedChain(_unavailable_message(chain, plugins))
    return cast(IndexerFactory, plugin.load())


def _plugins() -> dict[str, EntryPoint]:
    found: dict[str, EntryPoint] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        if entry.name in PLANNED_CHAINS or entry.name in _factories:
            owner = entry.dist.name if entry.dist else "an installed package"
            raise ConfigError(
                f"{owner} registers chain {entry.name!r}, which is built in; "
                "a plugin chain needs a name of its own"
            )
        found[entry.name] = entry
    return found


def _unavailable_message(chain: str, plugins: dict[str, EntryPoint]) -> str:
    if chain in PLANNED_CHAINS:
        return f"{chain} is available in a later version"
    known = ", ".join((*PLANNED_CHAINS, *sorted(plugins)))
    return f"unknown chain {chain!r}; available chains are: {known}"
