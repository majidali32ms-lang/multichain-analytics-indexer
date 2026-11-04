"""Shared test helpers. ``FakeNode`` is a real HTTP server on localhost, so requests go
through the whole client stack, including status handling and retries."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiohttp import web
from pydantic import SecretStr

from multichain_indexer.core.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "algorand"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@dataclass
class Reply:
    body: object = field(default_factory=dict)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    raw: str | None = None


class FakeNode:
    """Serves scripted replies. A list is consumed one reply per request."""

    def __init__(self) -> None:
        self.routes: dict[str, Reply | list[Reply]] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
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
        reply = self._next_reply(request.path)
        if reply.delay:
            await asyncio.sleep(reply.delay)
        text = reply.raw if reply.raw is not None else json.dumps(reply.body)
        return web.Response(
            text=text,
            status=reply.status,
            headers=reply.headers,
            content_type="application/json",
        )

    def _next_reply(self, path: str) -> Reply:
        route = self.routes.get(path)
        if route is None:
            return Reply({"message": f"no route for {path}"}, status=404)
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
