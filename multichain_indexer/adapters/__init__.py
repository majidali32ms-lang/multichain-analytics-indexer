"""Chain adapters, made available through the registry and imported only when used."""

from multichain_indexer.core.registry import register_chain

register_chain("algorand", "multichain_indexer.adapters.algorand:create_indexer")
register_chain("solana", "multichain_indexer.adapters.solana:create_indexer")
register_chain("near", "multichain_indexer.adapters.near:create_indexer")
