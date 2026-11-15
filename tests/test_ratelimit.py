"""One limiter per provider key, or per keyless host, for the whole process."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from multichain_indexer.core import ratelimit
from multichain_indexer.core.config import Settings
from multichain_indexer.core.ratelimit import (
    RateLimiter,
    host_scope,
    key_scope,
    shared_limiter,
)
from multichain_indexer.core.registry import create_indexer

KEY = SecretStr("proapi_not_a_real_key")


def test_a_scope_has_one_limiter() -> None:
    first = shared_limiter("host:example.org", 4)
    assert shared_limiter("host:example.org", 4) is first
    assert shared_limiter("host:other.org", 4) is not first


def test_the_slower_rate_holds_for_every_user_of_a_scope() -> None:
    limiter = shared_limiter("host:example.org", 4)
    shared_limiter("host:example.org", 2)
    shared_limiter("host:example.org", 8)
    assert limiter.requests_per_second == pytest.approx(2)


def test_a_key_scope_keeps_only_a_digest_of_the_key() -> None:
    scope = key_scope("BLOCKSCOUT_API_KEY", KEY)
    assert scope.startswith("BLOCKSCOUT_API_KEY:")
    assert KEY.get_secret_value() not in scope
    assert scope == key_scope("BLOCKSCOUT_API_KEY", SecretStr(KEY.get_secret_value()))
    assert scope != key_scope("BLOCKSCOUT_API_KEY", SecretStr("another key"))


@pytest.mark.parametrize(
    ("url", "scope"),
    [
        ("https://mempool.space/testnet4/api", "host:mempool.space"),
        ("https://mempool.space/signet/api", "host:mempool.space"),
        ("https://User:secret@RPC.Example.org/v1/key?token=t", "host:rpc.example.org"),
        ("http://127.0.0.1:8545", "host:127.0.0.1:8545"),
    ],
)
def test_a_host_scope_is_the_host_and_port_only(url: str, scope: str) -> None:
    assert host_scope(SecretStr(url)) == scope


async def test_users_of_one_scope_share_its_pace() -> None:
    first, second = shared_limiter("host:a", 20), shared_limiter("host:a", 20)
    started = time.monotonic()
    await asyncio.gather(*(limiter.acquire() for limiter in (first, second, first, second)))
    assert time.monotonic() - started >= 0.14


def test_threads_with_their_own_event_loops_each_get_a_slot_of_their_own() -> None:
    limiter = RateLimiter(50)
    finished: list[float] = []

    def take_slots() -> None:
        async def three() -> None:
            for _ in range(3):
                await limiter.acquire()

        asyncio.run(three())
        finished.append(time.monotonic())

    started = time.monotonic()
    threads = [threading.Thread(target=take_slots) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(finished) - started >= 11 / 50 - 0.01


async def test_chains_on_one_blockscout_key_share_its_limiter(tmp_path: Path) -> None:
    settings = Settings(database=tmp_path / "db", env={"BLOCKSCOUT_API_KEY": KEY})
    indexers = [create_indexer(chain, None, settings) for chain in ("ethereum", "arbitrum")]
    try:
        for indexer in indexers:
            indexer._history_source()  # type: ignore[attr-defined]
        scopes = [scope for scope in ratelimit._shared if scope.startswith("BLOCKSCOUT")]
        assert scopes == [key_scope("BLOCKSCOUT_API_KEY", KEY)]
        assert all(KEY.get_secret_value() not in scope for scope in ratelimit._shared)
        hosts = sorted(scope for scope in ratelimit._shared if scope.startswith("host:"))
        assert len(hosts) == 2
    finally:
        for indexer in indexers:
            await indexer.aclose()
