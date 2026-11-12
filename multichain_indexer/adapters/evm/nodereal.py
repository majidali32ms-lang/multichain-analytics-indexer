"""Account history on BNB Smart Chain mainnet from NodeReal's ``nr_getAssetTransfers``.

NodeReal serves the method on BSC mainnet only, not on BSC testnet; ``transfers.py`` walks it.
The range must be shorter than 2,000,000 blocks, as measured on 2026-11-11 (the docs say
100,000, which is not enforced). NodeReal indexes a few blocks behind the chain and refuses a
range that ends past what it has indexed with "blockNum not reached". The free API key is
``NODEREAL_API_KEY``. The ``all`` scope adds category ``20``, BEP-20 transfers.
"""

from __future__ import annotations

from multichain_indexer.adapters.evm.history import HistorySource, SourceContext
from multichain_indexer.adapters.evm.transfers import TransferApi, create_transfer_history

WINDOW_BLOCKS = 1_000_000
# The free plan allows 150 compute units a second per key and this method costs 250, so
# about one call every 1.7 seconds; slower still leaves room for retries.
REQUESTS_PER_SECOND = 0.5

NODEREAL = TransferApi(
    title="NodeReal",
    method="nr_getAssetTransfers",
    key_setting="NODEREAL_API_KEY",
    key_url="https://dashboard.nodereal.io",
    servers={("bnb", "mainnet"): "https://bsc-mainnet.nodereal.io"},
    key_path="/v1/",
    serves="BNB Smart Chain mainnet",
    token_category="20",
    window_blocks=WINDOW_BLOCKS,
    not_reached="blockNum not reached",
)


def create_nodereal_history(context: SourceContext) -> HistorySource:
    return create_transfer_history(NODEREAL, context, REQUESTS_PER_SECOND)
