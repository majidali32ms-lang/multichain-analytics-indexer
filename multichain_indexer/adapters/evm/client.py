"""Calls the standard Ethereum JSON-RPC API, which every EVM chain serves.

Requests go through ``ApiClient``, which handles timeouts, retries and ``Retry-After``.
Quantities come back as hex strings and are read with ``encoding.py``, so nothing past this
module sees the wire format.
"""

from __future__ import annotations

from pydantic import JsonValue

from multichain_indexer.adapters.evm.encoding import parse_data, parse_quantity
from multichain_indexer.core.errors import (
    InvalidInput,
    MalformedResponse,
    NetworkError,
    NotFound,
    RateLimited,
)
from multichain_indexer.core.http import ApiClient, JsonObject

RPC_PATH = "/"

INVALID_PARAMS = -32602
# eth_call reports a revert as error 3, with the revert data; some nodes use -32000.
EXECUTION_REVERTED = 3
SERVER_ERROR = -32000
RATE_LIMITED = 429


class EvmRpc:
    """One method per RPC call the adapter makes. State is read at an explicit block."""

    def __init__(self, client: ApiClient) -> None:
        self._client = client

    @property
    def requests_made(self) -> int:
        return self._client.requests_made

    async def chain_id(self) -> int:
        return parse_quantity(await self._call("eth_chainId", []), "eth_chainId")

    async def block_number(self) -> int:
        return parse_quantity(await self._call("eth_blockNumber", []), "eth_blockNumber")

    async def balance(self, address: str, block: int) -> int:
        result = await self._call("eth_getBalance", [address, hex(block)])
        return parse_quantity(result, "eth_getBalance")

    async def transaction_count(self, address: str, block: int) -> int:
        result = await self._call("eth_getTransactionCount", [address, hex(block)])
        return parse_quantity(result, "eth_getTransactionCount")

    async def code(self, address: str, block: int) -> bytes:
        return parse_data(await self._call("eth_getCode", [address, hex(block)]), "eth_getCode")

    async def call(self, to: str, data: bytes, block: int) -> bytes | None:
        """Run a read-only call; None when the contract reverts or has no such function."""
        params: list[JsonValue] = [{"to": to, "data": "0x" + data.hex()}, hex(block)]
        try:
            result = await self._call("eth_call", params)
        except _Reverted:
            return None
        return parse_data(result, "eth_call")

    async def finalized_block_number(self) -> int:
        block = await self._block("finalized", full=False)
        return parse_quantity(block.get("number"), "eth_getBlockByNumber")

    async def block_header(self, number: int, *, immutable: bool) -> JsonObject:
        return await self._block(hex(number), full=False, immutable=immutable)

    async def block_transactions(self, number: int) -> list[JsonObject]:
        transactions = (await self._block(hex(number), full=True)).get("transactions")
        if not isinstance(transactions, list) or not all(isinstance(t, dict) for t in transactions):
            raise MalformedResponse("eth_getBlockByNumber: expected a list of transactions")
        return [transaction for transaction in transactions if isinstance(transaction, dict)]

    async def transaction(self, tx_hash: str) -> JsonObject | None:
        """Return a transaction, or None when the node does not know it."""
        return await self._object_or_none("eth_getTransactionByHash", tx_hash)

    async def receipt(self, tx_hash: str) -> JsonObject | None:
        """Return a receipt, or None while the transaction is not in a block."""
        return await self._object_or_none("eth_getTransactionReceipt", tx_hash)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _block(self, tag: str, *, full: bool, immutable: bool = False) -> JsonObject:
        block = await self._call("eth_getBlockByNumber", [tag, full], immutable=immutable)
        if not isinstance(block, dict):
            raise NotFound(f"block {tag} is not available from this RPC endpoint")
        return block

    async def _object_or_none(self, method: str, tx_hash: str) -> JsonObject | None:
        result = await self._call(method, [tx_hash])
        if result is None:
            return None
        if not isinstance(result, dict):
            raise MalformedResponse(f"{method}: expected an object")
        return result

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


class _Reverted(Exception):
    """An eth_call that reverted; ``call`` turns it into None."""


def _error_for(method: str, error: JsonValue) -> Exception:
    code = error.get("code") if isinstance(error, dict) else None
    detail = error.get("message") if isinstance(error, dict) else error
    message = f"{method}: {detail}"
    if method == "eth_call" and _is_revert(code, detail):
        return _Reverted(message)
    if code == INVALID_PARAMS:
        return InvalidInput(message)
    if code == RATE_LIMITED:
        return RateLimited(f"{message}. Lower MCI_REQUESTS_PER_SECOND or use a dedicated endpoint.")
    return NetworkError(message)


def _is_revert(code: JsonValue, detail: JsonValue) -> bool:
    if code == EXECUTION_REVERTED:
        return True
    return code == SERVER_ERROR and "revert" in str(detail).lower()
