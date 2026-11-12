"""Compare EVM account history from Blockscout with the same history from Etherscan.

Both serve the Etherscan-compatible txlist. For sample addresses on Ethereum, Arbitrum and
Polygon PoS mainnet, it walks each address's list up to the chain's finalized block through
this project's own history source and reports the transactions only one service lists. Newer
blocks are left out: an explorer indexes them a few minutes late (Blockscout was about 22
blocks behind on Ethereum on 2026-11-11), which would look like a difference. It needs
BLOCKSCOUT_API_KEY and ETHERSCAN_API_KEY in the environment or in .env. It is a sample, not
proof of completeness. Blockscout's free plan no longer serves Polygon PoS, so those samples
fail with HTTP 402.

    python scripts/crosscheck_evm_history.py
    python scripts/crosscheck_evm_history.py ethereum 0xAddress arbitrum 0xAddress

Without arguments it takes, on each chain, the senders of recent transactions whose nonce
shows a short history, so the walk stays within the free plans.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.explorer import (
    BLOCKSCOUT,
    ETHERSCAN,
    ExplorerService,
    create_explorer_history,
)
from multichain_indexer.adapters.evm.history import HistorySource, SourceContext
from multichain_indexer.adapters.evm.profiles import ARBITRUM, ETHEREUM, POLYGON, EvmChain
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import IndexerError
from multichain_indexer.core.http import ApiClient
from multichain_indexer.core.ratelimit import RateLimiter

CHAINS = {chain.name: chain for chain in (ETHEREUM, ARBITRUM, POLYGON)}
NETWORK = "mainnet"
SAMPLES_PER_CHAIN = 3
# A nonce in this range means a short history: enough to compare, cheap to walk.
NONCE_RANGE = range(5, 150)
BLOCKS_TO_SEARCH = 10
RPC_REQUESTS_PER_SECOND = 2.0


@dataclass(frozen=True)
class Sample:
    chain: EvmChain
    address: str


@dataclass(frozen=True)
class Comparison:
    sample: Sample
    up_to_block: int
    blockscout: frozenset[str]
    etherscan: frozenset[str]

    def report(self) -> str:
        only_blockscout = sorted(self.blockscout - self.etherscan)
        only_etherscan = sorted(self.etherscan - self.blockscout)
        verdict = "same" if not (only_blockscout or only_etherscan) else "DIFFERENT"
        lines = [
            f"{self.sample.chain.name} {self.sample.address} up to block {self.up_to_block}: "
            "Blockscout "
            f"{len(self.blockscout)}, Etherscan {len(self.etherscan)}, {verdict}"
        ]
        lines += [f"  only Blockscout: {tx_hash}" for tx_hash in only_blockscout]
        lines += [f"  only Etherscan:  {tx_hash}" for tx_hash in only_etherscan]
        return "\n".join(lines)


async def main(arguments: list[str]) -> int:
    settings = Settings.from_env()
    missing = [
        service.key_setting
        for service in (BLOCKSCOUT, ETHERSCAN)
        if service.key_setting not in settings.env
    ]
    if missing:
        print(f"set {' and '.join(missing)} first", file=sys.stderr)
        return 2
    samples = _samples_from(arguments) if arguments else await _recent_samples(settings)
    for sample in samples:
        try:
            print((await _compare(sample, settings)).report(), flush=True)
        except IndexerError as error:
            print(f"{sample.chain.name} {sample.address}: failed: {error}", flush=True)
    return 0


def _samples_from(arguments: list[str]) -> list[Sample]:
    pairs = zip(arguments[::2], arguments[1::2], strict=True)
    return [Sample(CHAINS[chain], address) for chain, address in pairs]


async def _recent_samples(settings: Settings) -> list[Sample]:
    samples: list[Sample] = []
    for chain in CHAINS.values():
        rpc = _rpc(chain, settings)
        try:
            samples += await _short_histories(chain, rpc)
        finally:
            await rpc.aclose()
    return samples


async def _short_histories(chain: EvmChain, rpc: EvmRpc) -> list[Sample]:
    found: list[Sample] = []
    head = await rpc.block_number()
    for number in range(head - BLOCKS_TO_SEARCH, head):
        for transaction in await rpc.block_transactions(number):
            sender, nonce = transaction.get("from"), transaction.get("nonce")
            if not isinstance(sender, str) or not isinstance(nonce, str):
                continue
            if int(nonce, 16) in NONCE_RANGE and sender not in {s.address for s in found}:
                found.append(Sample(chain, sender))
            if len(found) == SAMPLES_PER_CHAIN:
                return found
    return found


async def _compare(sample: Sample, settings: Settings) -> Comparison:
    rpc = _rpc(sample.chain, settings)
    try:
        finalized = await rpc.finalized_block_number()
        listed = [
            await _all_hashes(
                _history(service, sample.chain, settings, rpc), sample.address, finalized
            )
            for service in (BLOCKSCOUT, ETHERSCAN)
        ]
    finally:
        await rpc.aclose()
    return Comparison(sample, finalized, listed[0], listed[1])


async def _all_hashes(history: HistorySource, address: str, up_to: int) -> frozenset[str]:
    hashes: set[str] = set()
    cursor = None
    try:
        while True:
            page = await history.account_page(
                address, from_height=None, to_height=up_to, cursor=cursor
            )
            hashes.update(tx_hash.lower() for tx_hash in page.hashes)
            if page.next_cursor is None:
                return frozenset(hashes)
            cursor = page.next_cursor
    finally:
        await history.aclose()


def _history(
    service: ExplorerService, chain: EvmChain, settings: Settings, rpc: EvmRpc
) -> HistorySource:
    return create_explorer_history(service, SourceContext(chain, NETWORK, settings, rpc))


def _rpc(chain: EvmChain, settings: Settings) -> EvmRpc:
    server = chain.networks[NETWORK].rpc_server
    client = ApiClient(
        settings.endpoint(chain.name, NETWORK, "RPC_SERVER", server),
        limiter=RateLimiter(RPC_REQUESTS_PER_SECOND),
    )
    return EvmRpc(client)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
