from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from pydantic import SecretStr

from multichain_indexer.core.cache import ResponseCache
from multichain_indexer.core.errors import (
    InvalidInput,
    MalformedResponse,
    NetworkError,
    NotFound,
    RateLimited,
)
from multichain_indexer.core.http import ApiClient
from multichain_indexer.core.ratelimit import RateLimiter
from tests.conftest import FakeNode, Reply

ClientFactory = Callable[..., ApiClient]


@pytest.fixture
async def make_client(node: FakeNode) -> AsyncIterator[ClientFactory]:
    clients: list[ApiClient] = []

    def build(**options: object) -> ApiClient:
        defaults: dict[str, object] = {"timeout": 5.0, "max_retries": 2, "backoff_base": 0.0}
        client = ApiClient(SecretStr(node.url), **{**defaults, **options})  # type: ignore[arg-type]
        clients.append(client)
        return client

    yield build
    for client in clients:
        await client.aclose()


async def test_returns_decoded_json_and_sends_query_and_headers(
    node: FakeNode, make_client: ClientFactory
) -> None:
    node.routes["/health"] = Reply({"round": 7})
    client = make_client(headers={"X-Token": SecretStr("abc")})

    assert await client.get_json("/health", {"limit": 2}) == {"round": 7}
    assert node.requests == [("/health", {"limit": "2"})]
    assert node.received_headers[0]["X-Token"] == "abc"
    assert client.requests_made == 1


@pytest.mark.parametrize(
    ("reply", "error", "message"),
    [
        (Reply({"message": "no assets"}, status=404), NotFound, "no assets"),
        (Reply({"message": "bad limit"}, status=400), InvalidInput, "bad limit"),
        (Reply(raw="<html>", status=403), InvalidInput, "returned an error"),
        (Reply(raw="not json"), MalformedResponse, "not JSON"),
        (Reply(body=[1, 2]), MalformedResponse, "JSON object"),
    ],
)
async def test_failures_that_retrying_cannot_fix_raise_at_once(
    node: FakeNode, make_client: ClientFactory, reply: Reply, error: type[Exception], message: str
) -> None:
    node.routes["/x"] = reply
    client = make_client()
    with pytest.raises(error, match=message):
        await client.get_json("/x")
    assert client.requests_made == 1


async def test_server_errors_are_retried_then_succeed(
    node: FakeNode, make_client: ClientFactory
) -> None:
    node.routes["/x"] = [Reply({"message": "busy"}, status=503), Reply({"ok": True})]
    client = make_client()
    assert await client.get_json("/x") == {"ok": True}
    assert client.requests_made == 2


async def test_server_errors_surface_after_every_retry(
    node: FakeNode, make_client: ClientFactory
) -> None:
    node.routes["/x"] = Reply({"message": "statement timeout"}, status=500)
    client = make_client()
    with pytest.raises(NetworkError, match="statement timeout"):
        await client.get_json("/x")
    assert client.requests_made == 3


async def test_rate_limit_error_tells_the_user_what_to_do(
    node: FakeNode, make_client: ClientFactory
) -> None:
    node.routes["/x"] = Reply(status=429)
    with pytest.raises(RateLimited, match="dedicated endpoint"):
        await make_client().get_json("/x")


async def test_retry_after_holds_back_the_next_request(
    node: FakeNode, make_client: ClientFactory
) -> None:
    node.routes["/x"] = [Reply(status=429, headers={"Retry-After": "0.2"}), Reply({"ok": True})]
    client = make_client(limiter=RateLimiter(1000))
    started = asyncio.get_running_loop().time()
    assert await client.get_json("/x") == {"ok": True}
    assert asyncio.get_running_loop().time() - started >= 0.19


@pytest.mark.parametrize("header", ["soon", "-1"])
async def test_unusable_retry_after_is_ignored(
    node: FakeNode, make_client: ClientFactory, header: str
) -> None:
    node.routes["/x"] = [Reply(status=429, headers={"Retry-After": header}), Reply({"ok": True})]
    client = make_client(limiter=RateLimiter(1000))
    assert await client.get_json("/x") == {"ok": True}


async def test_timeout_becomes_a_network_error(node: FakeNode, make_client: ClientFactory) -> None:
    node.routes["/x"] = Reply(delay=0.5)
    client = make_client(timeout=0.05, max_retries=0)
    with pytest.raises(NetworkError, match="TimeoutError"):
        await client.get_json("/x")


async def test_connection_failure_never_names_the_base_url() -> None:
    client = ApiClient(
        SecretStr("http://127.0.0.1:9/secret-api-key"), max_retries=0, timeout=2.0, backoff_base=0.0
    )
    with pytest.raises(NetworkError) as caught:
        await client.get_json("/x")
    await client.aclose()
    assert "secret-api-key" not in str(caught.value)


async def test_cache_serves_immutable_responses_only(
    node: FakeNode, make_client: ClientFactory, tmp_path: Path
) -> None:
    node.routes["/x"] = Reply({"n": 1})
    cache = ResponseCache(tmp_path / "nested" / "cache.db")
    client = make_client(cache=cache)

    await client.get_json("/x", {"a": 1}, immutable=True)
    assert await client.get_json("/x", {"a": 1}, immutable=True) == {"n": 1}
    assert client.requests_made == 1

    await client.get_json("/x", {"a": 1})
    assert client.requests_made == 2
    cache.close()


async def test_rate_limiter_gives_concurrent_callers_their_own_slot() -> None:
    limiter = RateLimiter(10)
    started = asyncio.get_running_loop().time()
    await asyncio.gather(*(limiter.acquire() for _ in range(3)))
    assert asyncio.get_running_loop().time() - started >= 0.15


def test_rate_limiter_rejects_a_zero_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)
