"""Template plugin: a third-party chain for multichain-analytics-indexer."""

from examplechain_indexer.adapter import ExampleIndexer, create_indexer

__all__ = ["ExampleIndexer", "create_indexer"]
