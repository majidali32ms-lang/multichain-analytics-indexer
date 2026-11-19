"""Account history on BNB Smart Chain mainnet and opBNB from NodeReal's ``nr_getAssetTransfers``.

NodeReal serves the method on BSC mainnet and on opBNB mainnet and testnet, not on BSC testnet;
``transfers.py`` walks it. Its method reference still says "BSC and ETH mainnet only", but its
opBNB Enhanced API page lists both opBNB networks, and both answered with a free key on
2026-11-16. The range must be shorter than 2,000,000 blocks, as measured on 2026-11-11 (the docs
say 100,000, which is not enforced). NodeReal indexes a few blocks behind the chain and refuses a
range that ends past what it has indexed with "blockNum not reached". The free API key is
``NODEREAL_API_KEY``. The ``all`` scope adds category ``20``, BEP-20 transfers.
"""

from __future__ import annotations

from pydantic import SecretStr

from multichain_indexer.adapters.evm.encoding import parse_quantity
from multichain_indexer.adapters.evm.history import HistorySource, SourceContext
from multichain_indexer.adapters.evm.transfers import TransferApi, create_transfer_history
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError
from multichain_indexer.core.http import ApiClient, JsonObject
from multichain_indexer.core.ratelimit import key_scope, shared_limiter

# At 0.45 s a block on BNB Smart Chain this is about five days of blocks; at 0.25 s on opBNB,
# about three.
WINDOW_BLOCKS = 1_000_000
# The free plan allows 150 compute units a second per key and this method costs 250, so
# about one call every 1.7 seconds; slower still leaves room for retries.
REQUESTS_PER_SECOND = 0.5
HOLDER_COUNT_METHOD = "nr_getTokenHolderCount"
# NodeReal's compute-unit table (docs.nodereal.io/docs/compute-units-cus, 2026-11-01).
HOLDER_COUNT_COMPUTE_UNITS = 50

NODEREAL = TransferApi(
    title="NodeReal",
    method="nr_getAssetTransfers",
    key_setting="NODEREAL_API_KEY",
    key_url="https://dashboard.nodereal.io",
    servers={
        ("bnb", "mainnet"): "https://bsc-mainnet.nodereal.io",
        ("opbnb", "mainnet"): "https://opbnb-mainnet.nodereal.io",
        ("opbnb", "testnet"): "https://opbnb-testnet.nodereal.io",
    },
    key_path="/v1/",
    serves="BNB Smart Chain mainnet, opBNB mainnet and opBNB testnet",
    token_category="20",
    window_blocks=WINDOW_BLOCKS,
    not_reached="blockNum not reached",
)


def create_nodereal_history(context: SourceContext) -> HistorySource:
    return create_transfer_history(NODEREAL, context, REQUESTS_PER_SECOND)


async def nodereal_holder_count(
    settings: Settings, chain: str, network: str, token: str
) -> int | None:
    """Return NodeReal's own full-chain holder count for ``token``, or None when it has none.

    NodeReal nests the count in its result (``{"result": {"result": "0x1cecbe"}}``, read on
    2026-11-19).

    Raises:
        ConfigError: NodeReal does not serve the network, or the key is missing.
    """
    server = NODEREAL.servers.get((chain, network))
    if server is None:
        raise ConfigError(f"NodeReal's holder count serves {NODEREAL.serves} only")
    key = settings.env.get(NODEREAL.key_setting)
    if key is None:
        raise ConfigError(
            f"NodeReal's holder count needs a free API key: create one at {NODEREAL.key_url} "
            f"and set {NODEREAL.key_setting}"
        )
    configured = settings.endpoint(chain, network, "HISTORY_SERVER", server).get_secret_value()
    client = ApiClient(
        SecretStr(f"{configured}{NODEREAL.key_path}{key.get_secret_value()}"),
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        limiter=shared_limiter(key_scope(NODEREAL.key_setting, key), REQUESTS_PER_SECOND),
    )
    payload: JsonObject = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": HOLDER_COUNT_METHOD,
        "params": [token],
    }
    try:
        reply = await client.post_json("/", payload)
    finally:
        await client.aclose()
    return _holder_count(reply)


def _holder_count(reply: JsonObject) -> int | None:
    error = reply.get("error")
    if isinstance(error, dict):
        raise ConfigError(f"{HOLDER_COUNT_METHOD}: {error.get('message')}")
    result = reply.get("result")
    if result is None:
        return None
    count = result.get("result") if isinstance(result, dict) else None
    return parse_quantity(count, HOLDER_COUNT_METHOD)
