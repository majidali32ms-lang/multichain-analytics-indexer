"""JSON over HTTP with timeouts, retries, rate limiting and an optional cache.

Error messages name the request path and never the base URL, which may contain an
API key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeVar

import aiohttp
from pydantic import JsonValue, SecretStr

from multichain_indexer.core.errors import (
    InvalidInput,
    MalformedResponse,
    NetworkError,
    NotFound,
    RateLimited,
)
from multichain_indexer.core.ratelimit import RateLimiter

JsonObject = dict[str, JsonValue]
QueryParams = Mapping[str, str | int]
_Decoded = TypeVar("_Decoded")

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
RETRYABLE_STATUS = frozenset({408, 425, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 60.0

logger = logging.getLogger(__name__)


class ResponseStore(Protocol):
    """What ``ApiClient`` needs from a cache."""

    def get(self, key: str) -> str | None: ...

    def put(self, key: str, body: str) -> None: ...


@dataclass(frozen=True)
class _Reply:
    status: int
    body: str
    retry_after: float | None


class ApiClient:
    """Talks to one HTTP endpoint and counts every request it sends."""

    def __init__(
        self,
        base_url: SecretStr,
        *,
        headers: Mapping[str, SecretStr] | None = None,
        timeout: float = 20.0,
        max_retries: int = 4,
        limiter: RateLimiter | None = None,
        cache: ResponseStore | None = None,
        backoff_base: float = BACKOFF_BASE_SECONDS,
    ) -> None:
        self._base_url = base_url
        self._headers = headers or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max_retries
        self._limiter = limiter
        self._cache = cache
        self._backoff_base = backoff_base
        self._session: aiohttp.ClientSession | None = None
        self.requests_made = 0

    async def get_json(
        self, path: str, params: QueryParams | None = None, *, immutable: bool = False
    ) -> JsonObject:
        """GET ``path`` and return the decoded JSON object.

        Args:
            path: Request path, appended to the base URL.
            params: Query parameters.
            immutable: The response can never change, so it may be read from and
                written to the cache.

        Raises:
            NotFound: HTTP 404.
            InvalidInput: The node rejected the request as invalid.
            RateLimited: HTTP 429 after every retry.
            NetworkError: Timeouts, connection failures or 5xx after every retry.
            MalformedResponse: The body is not a JSON object.
        """
        query = dict(params or {})
        return await self._cached_request(
            "GET", path, query, None, immutable=immutable, decode=_decode_object
        )

    async def get_json_value(
        self, path: str, params: QueryParams | None = None, *, immutable: bool = False
    ) -> JsonValue:
        """GET ``path`` and return the decoded JSON value, whatever its type.

        For APIs that answer with an array, or with a bare number sent as plain text.
        Takes the same arguments and raises the same errors as ``get_json``, except that
        any JSON value is accepted.
        """
        query = dict(params or {})
        return await self._cached_request(
            "GET", path, query, None, immutable=immutable, decode=_decode_value
        )

    async def post_json(
        self, path: str, payload: JsonObject, *, immutable: bool = False
    ) -> JsonObject:
        """POST a JSON body and return the decoded JSON object.

        Args:
            path: Request path, appended to the base URL.
            payload: The JSON body.
            immutable: The response can never change, so it may be read from and
                written to the cache.

        Raises:
            The same errors as ``get_json``.
        """
        return await self._cached_request(
            "POST", path, {}, payload, immutable=immutable, decode=_decode_object
        )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None

    async def _cached_request(
        self,
        method: str,
        path: str,
        query: QueryParams,
        payload: JsonObject | None,
        *,
        immutable: bool,
        decode: Callable[[str, str], _Decoded],
    ) -> _Decoded:
        key = self._cache_key(method, path, query, payload) if immutable and self._cache else None
        cached = self._cache.get(key) if self._cache and key else None
        if cached is not None:
            return decode(cached, path)

        body = await self._request_with_retries(method, path, query, payload)
        decoded = decode(body, path)
        if self._cache and key:
            self._cache.put(key, body)
        return decoded

    async def _request_with_retries(
        self, method: str, path: str, query: QueryParams, payload: JsonObject | None
    ) -> str:
        failure: NetworkError = NetworkError(f"{path}: no attempt was made")
        for attempt in range(self._max_retries + 1):
            if attempt:
                await self._wait_before_retry(attempt, failure, path)
            try:
                reply = await self._send(method, path, query, payload)
            except (TimeoutError, aiohttp.ClientError) as exc:
                failure = NetworkError(f"{path}: {type(exc).__name__} while calling the node")
                continue
            if reply.status == HTTP_OK:
                return reply.body
            failure = _failure_for(reply, path)
            if reply.status == HTTP_TOO_MANY_REQUESTS and self._limiter:
                self._limiter.pause(_bounded_retry_after(reply))
        raise failure

    async def _wait_before_retry(self, attempt: int, failure: NetworkError, path: str) -> None:
        delay = self._backoff_base * 2 ** (attempt - 1)
        logger.warning("retrying request", extra={"path": path, "reason": str(failure)})
        await asyncio.sleep(delay)

    async def _send(
        self, method: str, path: str, query: QueryParams, payload: JsonObject | None
    ) -> _Reply:
        if self._limiter:
            await self._limiter.acquire()
        url = self._base_url.get_secret_value().rstrip("/") + path
        headers = {name: value.get_secret_value() for name, value in self._headers.items()}
        self.requests_made += 1
        async with self._client().request(
            method, url, params=query or None, json=payload, headers=headers, timeout=self._timeout
        ) as response:
            return _Reply(
                status=response.status,
                body=await response.text(),
                retry_after=_parse_retry_after(response.headers.get("Retry-After")),
            )

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    def _cache_key(
        self, method: str, path: str, query: QueryParams, payload: JsonObject | None
    ) -> str:
        identity = [self._base_url.get_secret_value(), method, path, sorted(query.items()), payload]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _failure_for(reply: _Reply, path: str) -> NetworkError:
    """Return the error to retry on, or raise at once if retrying cannot help."""
    message = f"{path}: {_server_message(reply.body)}"
    if reply.status == HTTP_NOT_FOUND:
        raise NotFound(message)
    if reply.status == HTTP_TOO_MANY_REQUESTS:
        return RateLimited(
            f"{path}: the node is rate limiting requests. Wait a little, lower "
            "MCI_REQUESTS_PER_SECOND, or configure a dedicated endpoint."
        )
    if reply.status in RETRYABLE_STATUS:
        return NetworkError(f"{message} (HTTP {reply.status})")
    raise InvalidInput(f"{message} (HTTP {reply.status})")


def _server_message(body: str) -> str:
    try:
        decoded = json.loads(body)
    except ValueError:
        return "the node returned an error"
    message = decoded.get("message") if isinstance(decoded, dict) else None
    return str(message) if message else "the node returned an error"


def _bounded_retry_after(reply: _Reply) -> float:
    return min(reply.retry_after or 0.0, MAX_RETRY_AFTER_SECONDS)


def _parse_retry_after(value: str | None) -> float | None:
    # Only the delta-seconds form; RPC gateways rarely send an HTTP date.
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _decode_object(body: str, path: str) -> JsonObject:
    decoded = _decode_value(body, path)
    if not isinstance(decoded, dict):
        raise MalformedResponse(f"{path}: expected a JSON object")
    return decoded


def _decode_value(body: str, path: str) -> JsonValue:
    try:
        decoded: JsonValue = json.loads(body)
    except ValueError:
        raise MalformedResponse(f"{path}: the response is not JSON") from None
    return decoded
