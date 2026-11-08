"""Calls NEAR's standard JSON-RPC API, for account state, access keys and the chain head.

Requests go through ``ApiClient``, which handles timeouts, retries and ``Retry-After``.
Transaction history does not come from here; see ``history.py``.
"""

from __future__ import annotations

from pydantic import JsonValue

from multichain_indexer.core.errors import InvalidInput, MalformedResponse, NetworkError, NotFound
from multichain_indexer.core.http import ApiClient, JsonObject

RPC_PATH = "/"
FINAL = "final"
UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
REQUEST_VALIDATION_ERROR = "REQUEST_VALIDATION_ERROR"


class NearRpc:
    """One method per RPC call the adapter makes."""

    def __init__(self, client: ApiClient) -> None:
        self._client = client

    @property
    def requests_made(self) -> int:
        return self._client.requests_made

    async def chain_id(self) -> str:
        status = await self._call("status", [])
        chain_id = status.get("chain_id") if isinstance(status, dict) else None
        if not isinstance(chain_id, str):
            raise MalformedResponse("status: expected a chain_id")
        return chain_id

    async def final_height(self) -> int:
        block = await self._call("block", {"finality": FINAL})
        header = block.get("header") if isinstance(block, dict) else None
        height = header.get("height") if isinstance(header, dict) else None
        if not isinstance(height, int):
            raise MalformedResponse("block: expected a block height")
        return height

    async def view_account(self, account: str) -> JsonValue:
        """Return an account's state.

        Raises:
            NotFound: Nobody has created this account.
        """
        return await self._query("view_account", account)

    async def view_access_key_list(self, account: str) -> JsonValue:
        return await self._query("view_access_key_list", account)

    async def shard_layout(self) -> JsonValue:
        config = await self._call("EXPERIMENTAL_protocol_config", {"finality": FINAL})
        return config.get("shard_layout") if isinstance(config, dict) else None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _query(self, request_type: str, account: str) -> JsonValue:
        params: JsonObject = {
            "request_type": request_type,
            "finality": FINAL,
            "account_id": account,
        }
        return await self._call("query", params)

    async def _call(self, method: str, params: JsonValue) -> JsonValue:
        payload: JsonObject = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
        reply = await self._client.post_json(RPC_PATH, payload)
        error = reply.get("error")
        if error is not None:
            raise _error_for(method, error)
        if "result" not in reply:
            raise MalformedResponse(f"{method}: the reply has neither a result nor an error")
        return reply["result"]


def _error_for(method: str, error: JsonValue) -> Exception:
    details = error if isinstance(error, dict) else {}
    cause = details.get("cause")
    cause_name = cause.get("name") if isinstance(cause, dict) else None
    message = f"{method}: {details.get('data') or cause_name or details.get('message') or error}"
    if cause_name == UNKNOWN_ACCOUNT:
        return NotFound(message)
    if details.get("name") == REQUEST_VALIDATION_ERROR:
        return InvalidInput(message)
    return NetworkError(message)
