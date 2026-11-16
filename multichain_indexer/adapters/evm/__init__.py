"""One adapter for every EVM chain, with a profile per chain; see ``profiles.py``."""

from multichain_indexer.adapters.evm.adapter import EvmIndexer, indexer_factory
from multichain_indexer.adapters.evm.profiles import (
    ARBITRUM,
    BASE,
    BNB,
    ETHEREUM,
    OPTIMISM,
    POLYGON,
)

create_ethereum_indexer = indexer_factory(ETHEREUM)
create_arbitrum_indexer = indexer_factory(ARBITRUM)
create_optimism_indexer = indexer_factory(OPTIMISM)
create_base_indexer = indexer_factory(BASE)
create_polygon_indexer = indexer_factory(POLYGON)
create_bnb_indexer = indexer_factory(BNB)

__all__ = [
    "EvmIndexer",
    "create_arbitrum_indexer",
    "create_base_indexer",
    "create_bnb_indexer",
    "create_ethereum_indexer",
    "create_optimism_indexer",
    "create_polygon_indexer",
]
