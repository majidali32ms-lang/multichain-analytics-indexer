"""Account history from an explorer's Etherscan-compatible API: Blockscout or Etherscan.

Both serve ``module=account&action=txlist``, the normal transactions an address sent or
received, with a block range and newest first. With the ``all`` scope, ``action=tokentx`` is
walked alongside: the ERC-20 transfers to or from the address, including those inside other
accounts' contract calls. Pages are walked by lowering the range's top block, so a cursor stays
valid while new blocks arrive. A cursor is ``<block>:<skip>``, one skip per list: the next page
ends at that block and leaves out the rows of that block each list has already returned.

A page holds every row down to the highest block where a full list stopped, so rows of a
lower block that another list has not reached yet wait for the next page. A transaction with
several token transfers is several ``tokentx`` rows and can be split across two pages; the
adapter reads it once.

Each service needs its own free API key: ``BLOCKSCOUT_API_KEY`` (dev.blockscout.com, sent as a
bearer token) or ``ETHERSCAN_API_KEY`` (etherscan.io, sent as ``apikey``). Etherscan's free plan
returns at most 1,000 records a request and serves only some chains.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue, SecretStr

from multichain_indexer.adapters.evm.history import (
    SCOPE_ALL,
    HistoryPage,
    HistorySource,
    SourceContext,
)
from multichain_indexer.adapters.evm.profiles import LOCALNET
from multichain_indexer.core.errors import (
    ConfigError,
    InvalidInput,
    MalformedResponse,
    NetworkError,
    RateLimited,
)
from multichain_indexer.core.http import ApiClient
from multichain_indexer.core.ratelimit import RateLimiter

PAGE_SIZE = 100
TXLIST = "txlist"
TOKENTX = "tokentx"
OK_STATUS = "1"
NO_RECORDS = "No transactions found"
RATE_LIMIT_MARKERS = ("rate limit", "Max calls per sec")


@dataclass(frozen=True)
class ExplorerService:
    name: str
    title: str
    server: str
    path: str
    chain_id_parameter: str
    key_setting: str
    key_in_header: bool
    key_url: str
    # Measured or documented free-plan rate, below the limit so retries stay rare.
    requests_per_second: float


BLOCKSCOUT = ExplorerService(
    name="blockscout",
    title="Blockscout",
    server="https://api.blockscout.com",
    path="/v2/api",
    chain_id_parameter="chain_id",
    key_setting="BLOCKSCOUT_API_KEY",
    key_in_header=True,
    key_url="https://dev.blockscout.com",
    requests_per_second=4.0,
)

ETHERSCAN = ExplorerService(
    name="etherscan",
    title="Etherscan",
    server="https://api.etherscan.io",
    path="/v2/api",
    chain_id_parameter="chainid",
    key_setting="ETHERSCAN_API_KEY",
    key_in_header=False,
    key_url="https://etherscan.io/apidashboard",
    requests_per_second=2.0,
)


@dataclass(frozen=True)
class _Cursor:
    block: int
    skips: tuple[int, ...]

    @classmethod
    def parse(cls, text: str, lists: int) -> _Cursor:
        """Read a cursor issued for ``lists`` lists.

        Raises:
            InvalidInput: It is not such a cursor, for example one from another scope.
        """
        parts = text.split(":")
        if len(parts) != lists + 1 or not all(part.isdigit() for part in parts):
            raise InvalidInput(f"not a history cursor: {text!r}")
        return cls(int(parts[0]), tuple(int(part) for part in parts[1:]))

    def __str__(self) -> str:
        return ":".join(str(number) for number in (self.block, *self.skips))


@dataclass(frozen=True)
class _Row:
    hash: str
    block: int


class ExplorerHistory:
    """One explorer service, for one chain ID."""

    def __init__(
        self,
        service: ExplorerService,
        chain_id: int,
        client: ApiClient,
        key: SecretStr,
        actions: tuple[str, ...] = (TXLIST,),
    ) -> None:
        self._service = service
        self._chain_id = chain_id
        self._client = client
        self._key = key
        self._actions = actions

    @property
    def requests_made(self) -> int:
        return self._client.requests_made

    @property
    def indexed_up_to(self) -> int | None:
        return None

    async def account_page(
        self,
        address: str,
        *,
        from_height: int | None,
        to_height: int | None,
        cursor: str | None,
    ) -> HistoryPage:
        position = _Cursor.parse(cursor, len(self._actions)) if cursor else None
        top = position.block if position else to_height
        skips = position.skips if position else (0,) * len(self._actions)
        lists = [
            await self._rows(action, address, from_height, top, page=skip // PAGE_SIZE + 1)
            for action, skip in zip(self._actions, skips, strict=True)
        ]
        fresh = [
            _without_returned(rows, top, skip % PAGE_SIZE)
            for rows, skip in zip(lists, skips, strict=True)
        ]
        stops = [rows[-1].block for rows in lists if len(rows) == PAGE_SIZE]
        if not stops:
            return HistoryPage(hashes=_newest_first(fresh), next_cursor=None)
        boundary = max(stops)
        kept = [[row for row in rows if row.block >= boundary] for rows in fresh]
        return HistoryPage(
            hashes=_newest_first(kept),
            next_cursor=str(_Cursor(boundary, _next_skips(kept, skips, boundary, top))),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rows(
        self, action: str, address: str, from_height: int | None, top: int | None, *, page: int
    ) -> list[_Row]:
        query = {
            self._service.chain_id_parameter: str(self._chain_id),
            "module": "account",
            "action": action,
            "address": address,
            "startblock": str(from_height or 0),
            "endblock": "latest" if top is None else str(top),
            "sort": "desc",
            "page": str(page),
            "offset": str(PAGE_SIZE),
        }
        if not self._service.key_in_header:
            query["apikey"] = self._key.get_secret_value()
        reply = await self._client.get_json(self._service.path, query)
        return _parse_rows(self._service, action, reply)


def create_explorer_history(service: ExplorerService, context: SourceContext) -> HistorySource:
    """Build ``service``'s history source for the context's chain and network.

    Raises:
        ConfigError: The network has no public chain ID, or the service's key is missing.
    """
    if context.network == LOCALNET:
        raise ConfigError(
            f"{service.title} does not index local networks; "
            f"set {_source_setting(context)} to rpc-scan"
        )
    key = context.settings.env.get(service.key_setting)
    if key is None:
        raise ConfigError(_missing_key_message(service))
    headers = {"Authorization": SecretStr(f"Bearer {key.get_secret_value()}")}
    client = ApiClient(
        context.settings.endpoint(
            context.chain.name, context.network, "HISTORY_SERVER", service.server
        ),
        headers=headers if service.key_in_header else None,
        timeout=context.settings.request_timeout,
        max_retries=context.settings.max_retries,
        limiter=RateLimiter(service.requests_per_second),
    )
    chain_id = context.chain.networks[context.network].chain_id
    actions = (TXLIST, TOKENTX) if context.scope == SCOPE_ALL else (TXLIST,)
    return ExplorerHistory(service, chain_id, client, key, actions)


def create_blockscout_history(context: SourceContext) -> HistorySource:
    return create_explorer_history(BLOCKSCOUT, context)


def create_etherscan_history(context: SourceContext) -> HistorySource:
    return create_explorer_history(ETHERSCAN, context)


def _parse_rows(service: ExplorerService, action: str, reply: dict[str, JsonValue]) -> list[_Row]:
    result = reply.get("result")
    if reply.get("status") != OK_STATUS:
        if reply.get("message") == NO_RECORDS or result == []:
            return []
        raise _error_for(service, action, str(result or reply.get("message")))
    if not isinstance(result, list):
        raise MalformedResponse(f"{service.title} {action}: expected a list of transactions")
    return [_row(service, action, item) for item in result]


def _row(service: ExplorerService, action: str, item: JsonValue) -> _Row:
    tx_hash = item.get("hash") if isinstance(item, dict) else None
    block = item.get("blockNumber") if isinstance(item, dict) else None
    if not isinstance(tx_hash, str) or not isinstance(block, str) or not block.isdigit():
        raise MalformedResponse(f"{service.title} {action}: expected a hash and a block number")
    return _Row(tx_hash, int(block))


def _without_returned(rows: list[_Row], top: int | None, skip: int) -> list[_Row]:
    # Rows of the top block come first, in the same order every time.
    if top is None or skip == 0:
        return rows
    in_top = [row for row in rows if row.block == top][:skip]
    return [row for row in rows if row not in in_top]


def _next_skips(
    kept: list[list[_Row]], skips: tuple[int, ...], boundary: int, top: int | None
) -> tuple[int, ...]:
    # Still inside the top block, every list adds what it returned there to what it had
    # returned before; at a lower block each list starts counting again.
    if boundary == top:
        return tuple(skip + len(rows) for skip, rows in zip(skips, kept, strict=True))
    return tuple(sum(1 for row in rows if row.block == boundary) for rows in kept)


def _newest_first(lists: list[list[_Row]]) -> tuple[str, ...]:
    # A transaction with several token transfers is several rows, and can be in both lists.
    rows = sorted((row for rows in lists for row in rows), key=lambda row: -row.block)
    return tuple(dict.fromkeys(row.hash for row in rows))


def _error_for(service: ExplorerService, action: str, detail: str) -> Exception:
    message = f"{service.title} {action}: {detail}"
    if "API Key" in detail:
        return ConfigError(f"{message}. Check {service.key_setting}.")
    if any(marker in detail for marker in RATE_LIMIT_MARKERS):
        return RateLimited(message)
    return NetworkError(message)


def _missing_key_message(service: ExplorerService) -> str:
    return (
        f"{service.title} needs a free API key for account history: create one at "
        f"{service.key_url} and set {service.key_setting}"
    )


def _source_setting(context: SourceContext) -> str:
    return f"{context.chain.name}_{context.network}_HISTORY_SOURCE".upper()
