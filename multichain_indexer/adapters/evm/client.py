"""Calls the standard Ethereum JSON-RPC API, which every EVM chain serves.

Requests go through ``ApiClient``, which handles timeouts, retries and ``Retry-After``.
Quantities come back as hex strings and are turned into integers here, so nothing past this
module sees the wire format.
"""

from __future__ import annotations

import re

from pydantic import JsonValue

from multichain_indexer.core.errors import (
    InvalidInput,
    MalformedResponse,
    NetworkError,
    RateLimited,
)
from multichain_indexer.core.http import ApiClient, JsonObject

RPC_PATH = "/"
HEX_PREFIX = "0x"
HEX_DIGITS = re.compile(r"[0-9a-fA-F]*")

INVALID_PARAMS = -32602
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

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, params: list[JsonValue]) -> JsonValue:
        payload: JsonObject = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        reply = await self._client.post_json(RPC_PATH, payload)
        error = reply.get("error")
        if error is not None:
            raise _error_for(method, error)
        if "result" not in reply:
            raise MalformedResponse(f"{method}: the reply has neither a result nor an error")
        return reply["result"]


def parse_quantity(value: JsonValue, method: str) -> int:
    """Read a JSON-RPC quantity, a hex string such as ``0x1a``.

    Raises:
        MalformedResponse: ``value`` is not a hex quantity.
    """
    digits = _hex_digits(value, method, "a hex quantity")
    if not digits:
        raise MalformedResponse(f"{method}: expected a hex quantity, got {value!r}")
    return int(digits, 16)


def parse_data(value: JsonValue, method: str) -> bytes:
    """Read JSON-RPC data, a hex string of whole bytes such as ``0x60806040``.

    Raises:
        MalformedResponse: ``value`` is not hex data.
    """
    digits = _hex_digits(value, method, "hex data")
    if len(digits) % 2:
        raise MalformedResponse(f"{method}: expected whole bytes of hex data")
    return bytes.fromhex(digits)


def _hex_digits(value: JsonValue, method: str, expected: str) -> str:
    if not isinstance(value, str) or not value.startswith(HEX_PREFIX):
        raise MalformedResponse(f"{method}: expected {expected}, got {value!r}")
    digits = value.removeprefix(HEX_PREFIX)
    if not HEX_DIGITS.fullmatch(digits):
        raise MalformedResponse(f"{method}: expected {expected}, got {value!r}")
    return digits


def _error_for(method: str, error: JsonValue) -> Exception:
    code = error.get("code") if isinstance(error, dict) else None
    message = f"{method}: {error.get('message') if isinstance(error, dict) else error}"
    if code == INVALID_PARAMS:
        return InvalidInput(message)
    if code == RATE_LIMITED:
        return RateLimited(f"{message}. Lower MCI_REQUESTS_PER_SECOND or use a dedicated endpoint.")
    return NetworkError(message)
