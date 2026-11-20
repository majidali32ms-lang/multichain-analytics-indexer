"""Where an EVM network's ``eth_getLogs`` and old state are read, and how a long range of logs
is read in parts.

BNB Smart Chain's public endpoints refuse ``eth_getLogs`` ("limit exceeded" even for ten
blocks), so on BNB Smart Chain the logs come from NodeReal with the free ``NODEREAL_API_KEY``;
every other network uses its own RPC. ``<CHAIN>_<NETWORK>_LOGS_SERVER`` replaces the server.

Endpoints cap a single call: NodeReal at 50,000 blocks or 50,000 logs (measured 2026-11-16),
opBNB's public RPC below 50,000 blocks, BSC nodes at 5,000 blocks by default. ``iter_logs``
therefore asks for a window of blocks at a time and halves the window when an endpoint refuses
it as too large or times out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from pydantic import JsonValue, SecretStr

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.profiles import EvmChain
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError, InvalidInput, NetworkError
from multichain_indexer.core.http import ApiClient, JsonObject
from multichain_indexer.core.ratelimit import key_scope, shared_limiter

LOGS_KEY_SETTING = "NODEREAL_API_KEY"
LOGS_KEY_URL = "https://dashboard.nodereal.io"
# The free plan allows 150 compute units a second per key and eth_getLogs costs 50.
LOGS_REQUESTS_PER_SECOND = 2.0
LOGS_COMPUTE_UNITS = 50
# NodeReal's RPC servers for BNB Smart Chain and opBNB; the key follows "/v1/". With the free
# key they also serve old state (eth_call and balances at any block, checked on 2026-11-19),
# which the public endpoints keep for only about the newest 100 blocks.
NODEREAL_RPC_SERVERS: Mapping[tuple[str, str], str] = {
    ("bnb", "mainnet"): "https://bsc-mainnet.nodereal.io",
    ("bnb", "testnet"): "https://bsc-testnet.nodereal.io",
    ("opbnb", "mainnet"): "https://opbnb-mainnet.nodereal.io",
    ("opbnb", "testnet"): "https://opbnb-testnet.nodereal.io",
}
# Networks whose own public endpoints refuse eth_getLogs.
PUBLIC_LOGS_REFUSED = frozenset({("bnb", "mainnet"), ("bnb", "testnet")})

FIRST_WINDOW_BLOCKS = 5_000
SMALLEST_WINDOW_BLOCKS = 1
# Words in the refusals seen for a range that is too long or holds too many logs: "logs count
# exceeds the limit 50000", "exceed maximum block range", "timeout".
TOO_LARGE_HINTS = ("exceed", "too many", "timeout", "range")


def nodereal_rpc(chain: EvmChain, network: str, settings: Settings) -> EvmRpc | None:
    """Return a NodeReal RPC for ``network``, or None when NodeReal does not serve it or
    ``NODEREAL_API_KEY`` is not set. ``<CHAIN>_<NETWORK>_LOGS_SERVER`` replaces the server."""
    server = NODEREAL_RPC_SERVERS.get((chain.name, network))
    key = settings.env.get(LOGS_KEY_SETTING)
    if server is None or key is None:
        return None
    configured = settings.endpoint(chain.name, network, "LOGS_SERVER", server)
    client = ApiClient(
        SecretStr(f"{configured.get_secret_value()}/v1/{key.get_secret_value()}"),
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        limiter=shared_limiter(key_scope(LOGS_KEY_SETTING, key), LOGS_REQUESTS_PER_SECOND),
    )
    return EvmRpc(client)


def logs_rpc(chain: EvmChain, network: str, settings: Settings, own: EvmRpc, why: str) -> EvmRpc:
    """Return the RPC to read ``network``'s logs from: its own, or NodeReal's.

    Args:
        why: What the logs are for, named in the error when the key is missing.

    Raises:
        ConfigError: NodeReal is needed and ``NODEREAL_API_KEY`` is not set.
    """
    if (chain.name, network) not in PUBLIC_LOGS_REFUSED:
        return own
    nodereal = nodereal_rpc(chain, network, settings)
    if nodereal is None:
        raise ConfigError(
            f"{why} on {chain.networks[network].title} come from NodeReal's eth_getLogs, which "
            f"needs a free API key: create one at {LOGS_KEY_URL} and set {LOGS_KEY_SETTING}"
        )
    return nodereal


async def iter_logs(
    rpc: EvmRpc,
    from_block: int,
    to_block: int,
    topics: list[JsonValue],
    address: str | None = None,
) -> AsyncIterator[list[JsonObject]]:
    """Yield the matching logs from ``from_block`` to ``to_block``, one window at a time,
    oldest first, so a caller can stop early without holding every log.

    Raises:
        NetworkError: The endpoint refuses even a one-block window.
    """
    window = FIRST_WINDOW_BLOCKS
    start = from_block
    while start <= to_block:
        end = min(to_block, start + window - 1)
        try:
            found = await rpc.logs(start, end, topics, address)
        except (InvalidInput, NetworkError) as refused:
            if window == SMALLEST_WINDOW_BLOCKS or not _too_large(refused):
                raise
            window = max(SMALLEST_WINDOW_BLOCKS, window // 2)
            continue
        yield found
        start = end + 1


def _too_large(refused: Exception) -> bool:
    text = str(refused).lower()
    return any(hint in text for hint in TOO_LARGE_HINTS)
