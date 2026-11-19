"""A BNB token report: finality, holders over recent blocks and the top holders' public keys.

python examples/bnb/token_report.py [CHAIN] [NETWORK] [TOKEN] [BLOCKS]

The defaults read USDT on opBNB mainnet over its newest 1,200 blocks (about five minutes),
which needs no key: opBNB's public RPC serves the logs and the newest blocks' state. On BNB
Smart Chain the logs need NODEREAL_API_KEY (free), in the environment or in .env. Nothing is
stored.
"""

from __future__ import annotations

import sys

from multichain_indexer import Indexer
from multichain_indexer.core.config import Settings

DEFAULT_CHAIN = "opbnb"
DEFAULT_NETWORK = "mainnet"
# Tether USD on opBNB mainnet.
DEFAULT_BEP20 = "0x9e5aac1ba1a2e6aed6b32689dfcf62a509ca96f3"
DEFAULT_BLOCKS = 1_200


def report(chain: str, network: str, token: str, blocks: int) -> None:
    with Indexer(chain, network, settings=Settings.from_env()) as indexer:
        finality = indexer.finality()
        print(f"Finality: {finality.lag_blocks} blocks, {finality.lag_milliseconds} ms behind")

        holders = indexer.token_holders(token, from_block=finality.head_block - blocks + 1)
        print(f"Blocks {holders.from_block} to {holders.to_block}")
        print(
            f"Time covered: {holders.from_time:%Y-%m-%d %H:%M:%S} to {holders.to_time:%H:%M:%S} UTC"
        )
        print(f"Transfers: {holders.transfers}, addresses active: {holders.active_addresses}")
        print(f"Holders at the end: {holders.holders_at_end}, new: {holders.new_holders}")
        if holders.top_share_of_supply is not None:
            print(f"Top-ten share of supply (at least): {holders.top_share_of_supply:.2%}")

        top = [holder.address for holder in holders.top_holders]
        if top:
            exposure = indexer.key_exposure(top)
            print("Public keys of the largest holders:")
            for row in exposure.accounts:
                print(f"  {row.address}  {row.status}")
            print(exposure.note)


if __name__ == "__main__":
    arguments = sys.argv[1:]
    report(
        arguments[0] if len(arguments) > 0 else DEFAULT_CHAIN,
        arguments[1] if len(arguments) > 1 else DEFAULT_NETWORK,
        arguments[2] if len(arguments) > 2 else DEFAULT_BEP20,
        int(arguments[3]) if len(arguments) > 3 else DEFAULT_BLOCKS,
    )
