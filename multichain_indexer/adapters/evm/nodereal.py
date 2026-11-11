"""Account history on BNB Smart Chain mainnet from NodeReal's ``nr_getAssetTransfers``.

The method lists transfers in a block range, filtered by sender or by receiver; asking for
both means both at once, so an address's sent and received transactions are two queries. The
``external`` category holds the address's own transactions, the same set as an explorer's
``txlist``. NodeReal serves it on BSC mainnet only, not on BSC testnet.

The range must be shorter than 2,000,000 blocks, as measured on 2026-11-11 (the docs say
100,000, which is not enforced). Pages walk down one window of blocks at a time, so a cursor
is the top block of the next window. The free API key (``NODEREAL_API_KEY``) goes in the URL
path, which this module keeps inside the endpoint secret so it never reaches a message.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue, SecretStr

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.encoding import parse_quantity
from multichain_indexer.adapters.evm.history import HistoryPage, HistorySource, SourceContext
from multichain_indexer.core.errors import (
    ConfigError,
    InvalidInput,
    MalformedResponse,
    NetworkError,
    RateLimited,
)
from multichain_indexer.core.http import ApiClient, JsonObject
from multichain_indexer.core.ratelimit import RateLimiter

KEY_SETTING = "NODEREAL_API_KEY"
KEY_URL = "https://dashboard.nodereal.io"
SERVERS = {("bnb", "mainnet"): "https://bsc-mainnet.nodereal.io"}
METHOD = "nr_getAssetTransfers"
WINDOW_BLOCKS = 1_000_000
MAX_COUNT = 1000
DIRECTIONS = ("fromAddress", "toAddress")
# The free plan allows 150 compute units a second per key and this method costs 250, so
# about one call every 1.7 seconds; slower still leaves room for retries.
REQUESTS_PER_SECOND = 0.5
OUT_OF_COMPUTE_UNITS = -32005
METHOD_NOT_AVAILABLE = -32601
# NodeReal indexes a few blocks behind the chain and refuses a range that ends past what it
# has indexed; the top is lowered by this many blocks (about 45 seconds on BSC) until it is
# accepted, at most MAX_LOWERINGS times.
NOT_REACHED = "blockNum not reached"
LOWER_BY_BLOCKS = 100
MAX_LOWERINGS = 10


@dataclass(frozen=True)
class _Transfer:
    hash: str
    block: int


class NodeRealHistory:
    def __init__(self, client: ApiClient, rpc: EvmRpc) -> None:
        self._client = client
        self._rpc = rpc
        self._indexed_up_to: int | None = None

    @property
    def requests_made(self) -> int:
        return self._client.requests_made

    @property
    def indexed_up_to(self) -> int | None:
        return self._indexed_up_to

    async def account_page(
        self,
        address: str,
        *,
        from_height: int | None,
        to_height: int | None,
        cursor: str | None,
    ) -> HistoryPage:
        lowest = from_height or 0
        top = _parse_cursor(cursor) if cursor else to_height
        if top is None:
            top = await self._rpc.block_number()
        for _ in range(MAX_LOWERINGS):
            bottom = max(lowest, top - WINDOW_BLOCKS + 1)
            try:
                transfers = [
                    transfer
                    for direction in DIRECTIONS
                    for transfer in await self._window(address, direction, bottom, top)
                ]
                break
            except _NotReached:
                top = max(lowest, top - LOWER_BY_BLOCKS)
                self._indexed_up_to = top
        else:
            raise NetworkError(f"{METHOD}: NodeReal has not indexed the newest blocks yet")
        return HistoryPage(
            hashes=_newest_first(transfers),
            next_cursor=str(bottom - 1) if bottom > lowest else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _window(self, address: str, direction: str, bottom: int, top: int) -> list[_Transfer]:
        transfers: list[_Transfer] = []
        page_key: str | None = None
        while True:
            params: JsonObject = {
                "category": ["external"],
                "fromBlock": hex(bottom),
                "toBlock": hex(top),
                "order": "desc",
                "maxCount": hex(MAX_COUNT),
                direction: address,
            }
            if page_key:
                params["pageKey"] = page_key
            result = await self._call(params)
            transfers += _transfers(result.get("transfers"))
            page_key = _page_key(result.get("pageKey"))
            if page_key is None:
                return transfers

    async def _call(self, params: JsonObject) -> JsonObject:
        payload: JsonObject = {"jsonrpc": "2.0", "id": 1, "method": METHOD, "params": [params]}
        reply = await self._client.post_json("/", payload)
        error = reply.get("error")
        if error is not None:
            raise _error_for(error)
        result = reply.get("result")
        if not isinstance(result, dict):
            raise MalformedResponse(f"{METHOD}: expected a result object")
        return result


def create_nodereal_history(context: SourceContext) -> HistorySource:
    """Build the source for BNB Smart Chain mainnet.

    Raises:
        ConfigError: Another chain or network, or no key.
    """
    server = SERVERS.get((context.chain.name, context.network))
    if server is None:
        raise ConfigError("NodeReal account history serves BNB Smart Chain mainnet only")
    key = context.settings.env.get(KEY_SETTING)
    if key is None:
        raise ConfigError(
            f"NodeReal needs a free API key for account history: create one at {KEY_URL} "
            f"and set {KEY_SETTING}"
        )
    configured = context.settings.endpoint(
        context.chain.name, context.network, "HISTORY_SERVER", server
    )
    endpoint = SecretStr(f"{configured.get_secret_value()}/v1/{key.get_secret_value()}")
    client = ApiClient(
        endpoint,
        timeout=context.settings.request_timeout,
        max_retries=context.settings.max_retries,
        limiter=RateLimiter(REQUESTS_PER_SECOND),
    )
    return NodeRealHistory(client, context.rpc)


def _parse_cursor(cursor: str) -> int:
    if not cursor.isdigit():
        raise InvalidInput(f"not a NodeReal history cursor: {cursor!r}")
    return int(cursor)


def _transfers(items: JsonValue) -> list[_Transfer]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise MalformedResponse(f"{METHOD}: expected a list of transfers")
    return [_transfer(item) for item in items]


def _transfer(item: JsonValue) -> _Transfer:
    tx_hash = item.get("hash") if isinstance(item, dict) else None
    block = item.get("blockNum") if isinstance(item, dict) else None
    if not isinstance(tx_hash, str):
        raise MalformedResponse(f"{METHOD}: expected a transaction hash")
    return _Transfer(tx_hash, parse_quantity(block, METHOD))


def _page_key(value: JsonValue) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MalformedResponse(f"{METHOD}: expected a page key")
    return value


def _newest_first(transfers: list[_Transfer]) -> tuple[str, ...]:
    # An address that sends to itself is listed in both directions.
    unique = {transfer.hash: transfer for transfer in transfers}
    ordered = sorted(unique.values(), key=lambda transfer: transfer.block, reverse=True)
    return tuple(transfer.hash for transfer in ordered)


class _NotReached(Exception):
    """The range ends past the newest block NodeReal has indexed."""


def _error_for(error: JsonValue) -> Exception:
    code = error.get("code") if isinstance(error, dict) else None
    detail = error.get("message") if isinstance(error, dict) else error
    message = f"{METHOD}: {detail}"
    if NOT_REACHED in str(detail):
        return _NotReached(message)
    if code == OUT_OF_COMPUTE_UNITS:
        return RateLimited(f"{message}. The key's compute units may be used up for the month.")
    if code == METHOD_NOT_AVAILABLE:
        return ConfigError(f"{message}. NodeReal serves it on BSC mainnet only.")
    return NetworkError(message)
