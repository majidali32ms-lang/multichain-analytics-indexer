"""Chain adapters, made available through the registry and imported only when used."""

from multichain_indexer.core.registry import register_chain

register_chain("algorand", "multichain_indexer.adapters.algorand:create_indexer")
register_chain("solana", "multichain_indexer.adapters.solana:create_indexer")
register_chain("near", "multichain_indexer.adapters.near:create_indexer")
register_chain("ethereum", "multichain_indexer.adapters.evm:create_ethereum_indexer")
register_chain("arbitrum", "multichain_indexer.adapters.evm:create_arbitrum_indexer")
register_chain("optimism", "multichain_indexer.adapters.evm:create_optimism_indexer")
register_chain("base", "multichain_indexer.adapters.evm:create_base_indexer")
register_chain("polygon", "multichain_indexer.adapters.evm:create_polygon_indexer")
register_chain("bnb", "multichain_indexer.adapters.evm:create_bnb_indexer")
