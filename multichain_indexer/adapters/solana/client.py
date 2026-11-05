"""Calls the Solana JSON-RPC API.

Requests go through ``ApiClient``, which handles timeouts, retries and ``Retry-After``.
``maxSupportedTransactionVersion`` is the integer 1, a ceiling: legacy and v0 still come
back, and the node refuses any block or transaction newer than the ceiling.
"""

from __future__ import annotations

from pydantic import JsonValue

from multichain_indexer.core.errors import (
    InvalidInput,
    MalformedResponse,
    NetworkError,
    NotFound,
    RateLimited,
)
from multichain_indexer.core.http import ApiClient, JsonObject

MAX_TRANSACTION_VERSION = 1
RPC_PATH = "/"
JSON_PARSED = "jsonParsed"

INVALID_PARAMS = -32602
BLOCK_NOT_AVAILABLE = -32004
SLOT_SKIPPED = -32007
LONG_TERM_STORAGE_SLOT_SKIPPED = -32009
RATE_LIMITED = 429
MISSING_SLOT_CODES = frozenset({BLOCK_NOT_AVAILABLE, SLOT_SKIPPED, LONG_TERM_STORAGE_SLOT_SKIPPED})

PROGRAM_DATA_HEADER_SLICE: dict[str, JsonValue] = {"offset": 0, "length": 45}


class SolanaRpc:
    """One method per RPC call the adapter makes."""

    def __init__(self, client: ApiClient, commitment: str) -> None:
        self._client = client
        self._commitment = commitment

    @property
    def requests_made(self) -> int:
        return self._client.requests_made

    async def slot(self) -> int:
        result = await self._call("getSlot", [{"commitment": self._commitment}])
        if not isinstance(result, int):
            raise MalformedResponse("getSlot: expected a slot number")
        return result

    async def signatures_for_address(
        self, address: str, *, limit: int, before: str | None
    ) -> JsonValue:
        options: dict[str, JsonValue] = {"limit": limit, "commitment": self._commitment}
        if before is not None:
            options["before"] = before
        return await self._call("getSignaturesForAddress", [address, options])

    async def transaction(self, signature: str, *, immutable: bool) -> JsonValue:
        """Return a transaction; ``immutable`` allows the cache once it is finalized.

        Raises:
            NotFound: The node no longer holds this transaction.
        """
        options: dict[str, JsonValue] = {
            "encoding": JSON_PARSED,
            "commitment": self._commitment,
            "maxSupportedTransactionVersion": MAX_TRANSACTION_VERSION,
        }
        result = await self._call("getTransaction", [signature, options], immutable=immutable)
        if result is None:
            raise NotFound(
                f"transaction {signature} is not available from this RPC endpoint; "
                "an archival endpoint may still hold it"
            )
        return result

    async def block(self, slot: int) -> JsonValue:
        """Return every transaction in a block.

        Raises:
            NotFound: The slot was skipped, or the node does not hold its block.
        """
        options: dict[str, JsonValue] = {
            "encoding": JSON_PARSED,
            "commitment": self._commitment,
            "transactionDetails": "full",
            "rewards": False,
            "maxSupportedTransactionVersion": MAX_TRANSACTION_VERSION,
        }
        result = await self._call("getBlock", [slot, options])
        if result is None:
            raise NotFound(f"slot {slot} has no block on this RPC endpoint")
        return result

    async def account_info(self, address: str) -> JsonValue:
        return await self._call("getAccountInfo", [address, {"encoding": JSON_PARSED}])

    async def program_data_header(self, address: str) -> JsonValue:
        # A program's data holds its whole binary; only the header is needed.
        options: dict[str, JsonValue] = {
            "encoding": "base64",
            "dataSlice": PROGRAM_DATA_HEADER_SLICE,
        }
        return await self._call("getAccountInfo", [address, options])

    async def token_accounts_by_owner(self, owner: str, token_program: str) -> JsonValue:
        filters: dict[str, JsonValue] = {"programId": token_program}
        options: dict[str, JsonValue] = {"encoding": JSON_PARSED}
        return await self._call("getTokenAccountsByOwner", [owner, filters, options])

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self, method: str, params: list[JsonValue], *, immutable: bool = False
    ) -> JsonValue:
        payload: JsonObject = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        reply = await self._client.post_json(RPC_PATH, payload, immutable=immutable)
        error = reply.get("error")
        if error is not None:
            raise _error_for(method, error)
        if "result" not in reply:
            raise MalformedResponse(f"{method}: the reply has neither a result nor an error")
        return reply["result"]


def _error_for(method: str, error: JsonValue) -> Exception:
    code = error.get("code") if isinstance(error, dict) else None
    message = f"{method}: {error.get('message') if isinstance(error, dict) else error}"
    if code == INVALID_PARAMS:
        return InvalidInput(message)
    if code in MISSING_SLOT_CODES:
        return NotFound(message)
    if code == RATE_LIMITED:
        return RateLimited(f"{message}. Lower MCI_REQUESTS_PER_SECOND or use a dedicated endpoint.")
    return NetworkError(message)
