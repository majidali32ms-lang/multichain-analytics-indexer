"""Account history on Base from Alchemy's ``alchemy_getAssetTransfers``.

Blockscout's free plan dropped Base on 2026-10-01 and Etherscan serves it on paid plans only;
Alchemy's free plan lists Base Mainnet and Base Sepolia transfers, as checked with a key on
2026-11-12. ``transfers.py`` walks it. ``excludeZeroValue`` must be false, or calls that move
no ETH are left out of ``external``. The ``internal`` category is refused on Base ("only
supported for ETH and MATIC"), so internal transfers are not listed. A range ending past the
head is accepted rather than refused, so the checkpoint is kept safe by the finalized block
alone. The free API key is ``ALCHEMY_API_KEY``. The ``all`` scope adds ``erc20``.
"""

from __future__ import annotations

from multichain_indexer.adapters.evm.history import HistorySource, SourceContext
from multichain_indexer.adapters.evm.transfers import TransferApi, create_transfer_history

WINDOW_BLOCKS = 1_000_000
# The free plan gives 300 compute units a second and this method costs 120, about 2.5 calls
# a second; 30 calls at 2, 4 and 8 a second drew no refusal on 2026-11-12.
REQUESTS_PER_SECOND = 2.0

ALCHEMY = TransferApi(
    title="Alchemy",
    method="alchemy_getAssetTransfers",
    key_setting="ALCHEMY_API_KEY",
    key_url="https://dashboard.alchemy.com",
    servers={
        ("base", "mainnet"): "https://base-mainnet.g.alchemy.com",
        ("base", "testnet"): "https://base-sepolia.g.alchemy.com",
    },
    key_path="/v2/",
    serves="Base Mainnet and Base Sepolia",
    token_category="erc20",
    window_blocks=WINDOW_BLOCKS,
    extra_params={"excludeZeroValue": False},
)


def create_alchemy_history(context: SourceContext) -> HistorySource:
    return create_transfer_history(ALCHEMY, context, REQUESTS_PER_SECOND)
