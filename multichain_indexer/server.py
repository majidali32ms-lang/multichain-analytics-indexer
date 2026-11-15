"""A read-only HTTP API over the local database: ``multichain-indexer serve``.

Needs the ``serve`` extra: ``pip install multichain-analytics-indexer[serve]``. Every route
is a GET and none changes stored transactions; an account lookup reads the chain and keeps
the account's latest state, as ``index account`` does. It listens on 127.0.0.1 unless told
otherwise, and each client is held to a request rate so one script cannot hammer the public
endpoints that account lookups reach. OpenAPI is served at ``/openapi.json`` and ``/docs``.

Amounts and fees in transactions are strings, as in exports, because JSON readers such as
JavaScript lose integers past 2**53.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, JsonValue

from multichain_indexer import __version__
from multichain_indexer.api import AsyncIndexer
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import (
    ConfigError,
    IndexerError,
    InvalidInput,
    NetworkError,
    NotFound,
    UnsupportedChain,
)
from multichain_indexer.core.models import AccountRecord, MetricsReport, TransactionRecord
from multichain_indexer.core.registry import available_chains
from multichain_indexer.crosschain import AsyncCrossChain, CrossChainReport
from multichain_indexer.db import Database, chosen_database

LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_REQUESTS_PER_MINUTE = 120
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 1000
SECONDS_PER_MINUTE = 60
HTTP_TOO_MANY_REQUESTS = 429
# Most specific first: NotFound and UnsupportedChain are kinds of other errors.
ERROR_STATUS: tuple[tuple[type[IndexerError], int], ...] = (
    (NotFound, 404),
    (UnsupportedChain, 404),
    (InvalidInput, 400),
    (NetworkError, 502),
    (ConfigError, 500),
)

Clock = Callable[[], float]


class Chains(BaseModel):
    """The chains this install can read, and those the database holds data for."""

    available: list[str]
    stored: list[dict[str, str]]


class TransactionList(BaseModel):
    """One page of stored transactions, newest first."""

    total: int
    offset: int
    limit: int
    next_offset: int | None
    items: list[dict[str, JsonValue]]


class ClientRateLimit:
    """Allows each client ``requests_per_minute`` requests, refilled steadily (token bucket)."""

    def __init__(self, requests_per_minute: int, clock: Clock = time.monotonic) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be at least 1")
        self._capacity = float(requests_per_minute)
        self._refill_per_second = requests_per_minute / SECONDS_PER_MINUTE
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def wait_seconds(self, client: str) -> float:
        """Take one request from ``client``'s allowance; return 0, or how long to wait."""
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(client, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill_per_second)
            if tokens >= 1:
                self._buckets[client] = (tokens - 1, now)
                return 0.0
            self._buckets[client] = (tokens, now)
            return (1 - tokens) / self._refill_per_second


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
    clock: Clock = time.monotonic,
) -> FastAPI:
    """Build the API over ``database``, or the configured one.

    Args:
        settings: Configuration; read from ``config.toml``, ``.env`` and the environment
            when None.
        database: SQLite file or PostgreSQL URL.
        requests_per_minute: What each client may send.
        clock: Time source for the rate limit, replaceable in tests.
    """
    config = settings or Settings.from_env()
    target = chosen_database(config, database)
    allowance = ClientRateLimit(requests_per_minute, clock)

    @asynccontextmanager
    async def opened(chain: str, network: str) -> AsyncIterator[AsyncIndexer]:
        async with AsyncIndexer(chain, network, settings=config, database=target) as indexer:
            yield indexer

    app = FastAPI(
        title="multichain-analytics-indexer",
        version=__version__,
        description="Read-only access to the transactions and metrics in the local database.",
    )

    @app.middleware("http")
    async def rate_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        client = request.client.host if request.client else "unknown"
        wait = allowance.wait_seconds(client)
        if wait > 0:
            return JSONResponse(
                {"detail": "too many requests from this client"},
                status_code=HTTP_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(math.ceil(wait))},
            )
        return await call_next(request)

    @app.exception_handler(IndexerError)
    async def indexer_error(request: Request, error: IndexerError) -> JSONResponse:
        status = next(code for kind, code in ERROR_STATUS if isinstance(error, kind))
        return JSONResponse({"detail": str(error)}, status_code=status)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/chains")
    async def chains() -> Chains:
        stored = AsyncCrossChain(settings=config, database=target).networks()
        return Chains(
            available=list(available_chains()),
            stored=[{"chain": chain, "network": network} for chain, network in stored],
        )

    @app.get("/metrics")
    async def cross_chain_metrics(
        days: Annotated[int, Query(ge=1)] = 30,
    ) -> CrossChainReport:
        return await AsyncCrossChain(settings=config, database=target).metrics(days)

    @app.get("/chains/{chain}/{network}/metrics")
    async def chain_metrics(
        chain: str,
        network: str,
        days: Annotated[int, Query(ge=1)] = 30,
        account: str | None = None,
    ) -> MetricsReport:
        async with opened(chain, network) as indexer:
            return indexer.metrics(days, account=account)

    @app.get("/chains/{chain}/{network}/transactions")
    async def transactions(
        chain: str,
        network: str,
        account: str | None = None,
        days: Annotated[int | None, Query(ge=1)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    ) -> TransactionList:
        async with opened(chain, network) as indexer:
            stored = indexer.stored_transactions(account=account, days=days)
        page = stored[offset : offset + limit]
        following = offset + limit
        return TransactionList(
            total=len(stored),
            offset=offset,
            limit=limit,
            next_offset=following if following < len(stored) else None,
            items=[_transaction_item(record) for record in page],
        )

    @app.get("/chains/{chain}/{network}/accounts/{address}")
    async def account(chain: str, network: str, address: str) -> AccountRecord:
        """Read an account's current state from the chain, as ``index account`` does."""
        async with opened(chain, network) as indexer:
            return await indexer.account(address)

    return app


def _transaction_item(record: TransactionRecord) -> dict[str, JsonValue]:
    item: dict[str, JsonValue] = record.model_dump(mode="json")
    item["amount"] = str(record.amount)
    item["fee"] = str(record.fee)
    return item
