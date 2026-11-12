"""Account history from a transfers API: NodeReal's ``nr_getAssetTransfers`` or Alchemy's
``alchemy_getAssetTransfers``.

Both list transfers in a block range, filtered by sender or by receiver; asking for both means
both at once, so an address's sent and received transactions are two queries per window. The
``external`` category holds the address's own transactions, the same set as an explorer's
``txlist``; the ``all`` scope adds the fungible token category in the same calls. Pages walk
down one window of blocks at a time, so a cursor is the top block of the next window, and a
service's short-lived page keys are only used inside one window. The API key goes in the URL
path, which stays inside the endpoint secret so it never reaches a message.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue, SecretStr

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.encoding import parse_quantity
from multichain_indexer.adapters.evm.history import (
    SCOPE_ALL,
    HistoryPage,
    HistorySource,
    SourceContext,
)
from multichain_indexer.core.errors import (
    ConfigError,
    InvalidInput,
    MalformedResponse,
    NetworkError,
    RateLimited,
)
from multichain_indexer.core.http import ApiClient, JsonObject
from multichain_indexer.core.ratelimit import RateLimiter

DIRECTIONS = ("fromAddress", "toAddress")
EXTERNAL = "external"
MAX_COUNT = 1000
OUT_OF_COMPUTE_UNITS = -32005
TOO_MANY_REQUESTS = 429
METHOD_NOT_AVAILABLE = -32601
# A service that indexes behind the chain and refuses a range ending past what it has indexed
# gets the top lowered by this many blocks until it is accepted, at most MAX_LOWERINGS times.
LOWER_BY_BLOCKS = 100
MAX_LOWERINGS = 10


@dataclass(frozen=True)
class TransferApi:
    """What differs between transfers APIs."""

    title: str
    method: str
    key_setting: str
    key_url: str
    # Chain and network to the service's server; the key follows ``key_path``.
    servers: Mapping[tuple[str, str], str]
    key_path: str
    serves: str
    token_category: str
    window_blocks: int
    extra_params: Mapping[str, JsonValue] = field(default_factory=dict)
    not_reached: str | None = None


@dataclass(frozen=True)
class _Transfer:
    hash: str
    block: int


class TransferHistory:
    def __init__(
        self,
        api: TransferApi,
        client: ApiClient,
        rpc: EvmRpc,
        categories: tuple[str, ...] = (EXTERNAL,),
    ) -> None:
        self._api = api
        self._client = client
        self._rpc = rpc
        self._categories = categories
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
        top = self._parse_cursor(cursor) if cursor else to_height
        if top is None:
            top = await self._rpc.block_number()
        for _ in range(MAX_LOWERINGS):
            bottom = max(lowest, top - self._api.window_blocks + 1)
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
            raise NetworkError(
                f"{self._api.method}: {self._api.title} has not indexed the newest blocks yet"
            )
        return HistoryPage(
            hashes=_newest_first(transfers),
            next_cursor=str(bottom - 1) if bottom > lowest else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _parse_cursor(self, cursor: str) -> int:
        if not cursor.isdigit():
            raise InvalidInput(f"not a {self._api.title} history cursor: {cursor!r}")
        return int(cursor)

    async def _window(self, address: str, direction: str, bottom: int, top: int) -> list[_Transfer]:
        transfers: list[_Transfer] = []
        page_key: str | None = None
        while True:
            params: JsonObject = {
                "category": list(self._categories),
                "fromBlock": hex(bottom),
                "toBlock": hex(top),
                "order": "desc",
                "maxCount": hex(MAX_COUNT),
                **self._api.extra_params,
                direction: address,
            }
            if page_key:
                params["pageKey"] = page_key
            result = await self._call(params)
            transfers += self._transfers(result.get("transfers"))
            page_key = self._page_key(result.get("pageKey"))
            if page_key is None:
                return transfers

    async def _call(self, params: JsonObject) -> JsonObject:
        method = self._api.method
        payload: JsonObject = {"jsonrpc": "2.0", "id": 1, "method": method, "params": [params]}
        reply = await self._client.post_json("/", payload)
        error = reply.get("error")
        if error is not None:
            raise _error_for(self._api, error)
        result = reply.get("result")
        if not isinstance(result, dict):
            raise MalformedResponse(f"{method}: expected a result object")
        return result

    def _transfers(self, items: JsonValue) -> list[_Transfer]:
        if items is None:
            return []
        if not isinstance(items, list):
            raise MalformedResponse(f"{self._api.method}: expected a list of transfers")
        return [self._transfer(item) for item in items]

    def _transfer(self, item: JsonValue) -> _Transfer:
        tx_hash = item.get("hash") if isinstance(item, dict) else None
        block = item.get("blockNum") if isinstance(item, dict) else None
        if not isinstance(tx_hash, str):
            raise MalformedResponse(f"{self._api.method}: expected a transaction hash")
        return _Transfer(tx_hash, parse_quantity(block, self._api.method))

    def _page_key(self, value: JsonValue) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise MalformedResponse(f"{self._api.method}: expected a page key")
        return value


def create_transfer_history(
    api: TransferApi, context: SourceContext, requests_per_second: float
) -> HistorySource:
    """Build ``api``'s source for the context's chain and network.

    Raises:
        ConfigError: The service does not serve the network, or its key is missing.
    """
    server = api.servers.get((context.chain.name, context.network))
    if server is None:
        raise ConfigError(f"{api.title} account history serves {api.serves} only")
    key = context.settings.env.get(api.key_setting)
    if key is None:
        raise ConfigError(
            f"{api.title} needs a free API key for account history: create one at "
            f"{api.key_url} and set {api.key_setting}"
        )
    configured = context.settings.endpoint(
        context.chain.name, context.network, "HISTORY_SERVER", server
    )
    endpoint = SecretStr(f"{configured.get_secret_value()}{api.key_path}{key.get_secret_value()}")
    client = ApiClient(
        endpoint,
        timeout=context.settings.request_timeout,
        max_retries=context.settings.max_retries,
        limiter=RateLimiter(requests_per_second),
    )
    categories = (EXTERNAL, api.token_category) if context.scope == SCOPE_ALL else (EXTERNAL,)
    return TransferHistory(api, client, context.rpc, categories)


def _newest_first(transfers: list[_Transfer]) -> tuple[str, ...]:
    # An address that sends to itself is listed in both directions, and a transaction with
    # several transfers is listed once for each.
    unique = {transfer.hash: transfer for transfer in transfers}
    ordered = sorted(unique.values(), key=lambda transfer: transfer.block, reverse=True)
    return tuple(transfer.hash for transfer in ordered)


class _NotReached(Exception):
    """The range ends past the newest block the service has indexed."""


def _error_for(api: TransferApi, error: JsonValue) -> Exception:
    code = error.get("code") if isinstance(error, dict) else None
    detail = error.get("message") if isinstance(error, dict) else error
    message = f"{api.method}: {detail}"
    if api.not_reached and api.not_reached in str(detail):
        return _NotReached(message)
    if code in (OUT_OF_COMPUTE_UNITS, TOO_MANY_REQUESTS):
        return RateLimited(f"{message}. The key's compute units may be used up for the month.")
    if code == METHOD_NOT_AVAILABLE:
        return ConfigError(f"{message}. {api.title} serves it on {api.serves} only.")
    return NetworkError(message)
