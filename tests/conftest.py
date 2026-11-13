"""Shared test helpers. ``FakeNode`` is a real HTTP server on localhost, so requests go
through the whole client stack, including status handling and retries."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from pydantic import SecretStr

from multichain_indexer.adapters.bitcoin import BitcoinIndexer, create_indexer
from multichain_indexer.core.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "algorand"
SOLANA_FIXTURES = Path(__file__).parent / "fixtures" / "solana"
NEAR_FIXTURES = Path(__file__).parent / "fixtures" / "near"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load_solana_fixture(name: str) -> dict[str, Any]:
    return json.loads((SOLANA_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load_near_fixture(name: str) -> dict[str, Any]:
    return json.loads((NEAR_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@dataclass
class Reply:
    body: object = field(default_factory=dict)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    raw: str | None = None


class FakeNode:
    """Serves scripted replies. A list is consumed one reply per request.

    A handler answers from the request instead: it gets a GET request's query, or a JSON-RPC
    call's params, and wins over a route with the same key.
    """

    def __init__(self) -> None:
        self.routes: dict[str, Reply | list[Reply]] = {}
        self.handlers: dict[str, Callable[[Any], Reply]] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.rpc_calls: list[tuple[str, list[object]]] = []
        self.post_bodies: list[tuple[str, dict[str, Any]]] = []
        self.received_headers: list[dict[str, str]] = []
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.url = ""

    def start(self) -> None:
        self._thread.start()
        self._started.wait()

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    def requests_to(self, path: str) -> list[dict[str, str]]:
        return [query for requested, query in self.requests if requested == path]

    def rpc_params(self, method: str) -> list[list[object]]:
        return [params for called, params in self.rpc_calls if called == method]

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self._started.set()
        self._loop.run_forever()

    async def _serve(self) -> None:
        application = web.Application()
        application.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(application)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}"

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, dict(request.query)))
        self.received_headers.append(dict(request.headers))
        key = await self._route_key(request)
        handler = self.handlers.get(key)
        reply = handler(await self._request_input(request)) if handler else self._next_reply(key)
        if reply.delay:
            await asyncio.sleep(reply.delay)
        text = reply.raw if reply.raw is not None else json.dumps(reply.body)
        return web.Response(
            text=text,
            status=reply.status,
            headers=reply.headers,
            content_type="application/json",
        )

    async def _route_key(self, request: web.Request) -> str:
        """A JSON-RPC call is routed by its method, everything else by its path."""
        if request.method != "POST":
            return request.path
        body = json.loads(await request.text())
        if "method" not in body:
            self.post_bodies.append((request.path, body))
            return request.path
        self.rpc_calls.append((body["method"], body["params"]))
        return f"rpc:{body['method']}"

    def posted_to(self, path: str) -> list[dict[str, Any]]:
        return [body for requested, body in self.post_bodies if requested == path]

    async def _request_input(self, request: web.Request) -> Any:
        if request.method != "POST":
            return dict(request.query)
        return json.loads(await request.text()).get("params")

    def _next_reply(self, key: str) -> Reply:
        route = self.routes.get(key)
        if route is None:
            return Reply({"message": f"no route for {key}"}, status=404)
        return route.pop(0) if isinstance(route, list) else route


@pytest.fixture
def node() -> Iterator[FakeNode]:
    fake = FakeNode()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def settings(node: FakeNode, tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "indexer.db",
        requests_per_second=1000,
        request_timeout=5,
        max_retries=1,
        env={"ALGORAND_TESTNET_INDEXER_SERVER": SecretStr(node.url)},
    )


@pytest.fixture
def btc_settings(node: FakeNode, tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "indexer.db",
        requests_per_second=1000,
        request_timeout=5,
        max_retries=0,
        env={"BITCOIN_TESTNET4_ESPLORA_SERVER": SecretStr(node.url)},
    )


@pytest.fixture
async def adapter(btc_settings: Settings) -> AsyncIterator[BitcoinIndexer]:
    built = create_indexer(None, btc_settings)
    yield built
    await built.aclose()
