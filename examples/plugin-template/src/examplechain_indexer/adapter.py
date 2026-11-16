"""A template adapter for a chain served by a small JSON API.

Copy this package, rename ``examplechain``, and replace the three requests with your chain's
own API. Everything else, sync, storage, metrics, export and the command line, comes from
multichain-analytics-indexer once the entry point in ``pyproject.toml`` is installed.

The API this template expects:

- ``GET /head`` returns ``{"height": 120}``
- ``GET /accounts/<address>`` returns ``{"balance": 5000}``
- ``GET /accounts/<address>/transactions`` returns ``{"transactions": [...], "next": "..."}``,
  newest first, with the optional query parameters ``from``, ``to`` and ``cursor``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from pydantic import JsonValue

from examplechain_indexer.models import ExampleAccount, ExampleTransaction
from multichain_indexer.core.base_indexer import BaseIndexer, ChainProfile
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import InvalidAddress, InvalidInput, MalformedResponse
from multichain_indexer.core.http import ApiClient, JsonObject, ResponseStore
from multichain_indexer.core.models import (
    AccountRecord,
    AssetRecord,
    ContractRecord,
    Status,
    TransactionPage,
    TransactionRecord,
)
from multichain_indexer.core.ratelimit import host_scope, shared_limiter

CHAIN = "examplechain"
TESTNET = "testnet"
DEFAULT_SERVERS = {TESTNET: "https://api.testnet.example.org"}
# Measure the public endpoint's limit through ApiClient before choosing this number.
REQUESTS_PER_SECOND = 2.0
ADDRESS = re.compile(r"ex1[0-9a-z]{8,60}")

PROFILE = ChainProfile(
    name=CHAIN,
    native_symbol="EXC",
    base_unit="drop",
    native_decimals=6,
    networks=(TESTNET,),
    default_network=TESTNET,
    transaction_model=ExampleTransaction,
)


class ExampleIndexer(BaseIndexer):
    profile = PROFILE

    def __init__(self, network: str, client: ApiClient) -> None:
        super().__init__(network)
        self._client = client

    def validate_address(self, address: str) -> str:
        if not ADDRESS.fullmatch(address):
            raise InvalidAddress(f"not an examplechain address: {address!r}")
        return address

    async def fetch_account(self, address: str) -> AccountRecord:
        reply = await self._client.get_json(f"/accounts/{self.validate_address(address)}")
        return AccountRecord(
            address=address,
            balance=_integer(reply, "balance"),
            chain=CHAIN,
            network=self.network,
            native=ExampleAccount(),
        )

    async def fetch_asset(self, asset_id: str) -> AssetRecord:
        raise InvalidInput("examplechain has no assets")

    async def fetch_contract(self, contract_id: str) -> ContractRecord:
        raise InvalidInput("examplechain has no contracts")

    async def chain_head(self) -> int:
        return _integer(await self._client.get_json("/head"), "height")

    async def iter_account_transactions(
        self,
        address: str,
        *,
        from_height: int | None = None,
        to_height: int | None = None,
        cursor: str | None = None,
    ) -> AsyncIterator[TransactionPage]:
        path = f"/accounts/{self.validate_address(address)}/transactions"
        while True:
            query = {
                name: value
                for name, value in (("from", from_height), ("to", to_height), ("cursor", cursor))
                if value is not None
            }
            reply = await self._client.get_json(path, query)
            records = tuple(self._record(item) for item in _items(reply))
            next_cursor = reply.get("next")
            cursor = next_cursor if isinstance(next_cursor, str) else None
            yield TransactionPage(records=records, next_cursor=cursor)
            if cursor is None:
                return

    def chain_metrics(self, records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
        return {"memos": sum(1 for record in records if _native(record).memo)}

    def describe(
        self, record: TransactionRecord | AccountRecord | AssetRecord | ContractRecord
    ) -> dict[str, str]:
        if isinstance(record, TransactionRecord):
            return {
                "Amount": self.profile.format_native(record.amount),
                "Height": str(_native(record).height),
            }
        if isinstance(record, AccountRecord):
            return {"Address": record.address, "Balance": PROFILE.format_native(record.balance)}
        return {"ID": record.id}

    @property
    def requests_made(self) -> int:
        return self._client.requests_made

    async def aclose(self) -> None:
        await self._client.aclose()

    def _record(self, item: JsonObject) -> TransactionRecord:
        return TransactionRecord(
            id=str(item["id"]),
            time=datetime.fromtimestamp(_integer(item, "time"), UTC),
            sender=str(item["from"]),
            receiver=str(item["to"]) if item.get("to") else None,
            amount=_integer(item, "amount"),
            fee=_integer(item, "fee"),
            status=Status.FINAL,
            chain=CHAIN,
            network=self.network,
            native=ExampleTransaction(
                height=_integer(item, "height"),
                memo=str(item["memo"]) if item.get("memo") else None,
            ),
        )


def create_indexer(
    network: str | None, settings: Settings, cache: ResponseStore | None = None
) -> ExampleIndexer:
    """The factory the entry point names. ``network`` of None means the test network."""
    chosen = network or PROFILE.default_network
    if chosen not in PROFILE.networks:
        raise InvalidInput(f"unknown examplechain network {chosen!r}")
    server = settings.endpoint(CHAIN, chosen, "API_SERVER", DEFAULT_SERVERS[chosen])
    client = ApiClient(
        server,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        limiter=shared_limiter(host_scope(server), REQUESTS_PER_SECOND),
        cache=cache,
    )
    return ExampleIndexer(chosen, client)


def _native(record: TransactionRecord) -> ExampleTransaction:
    native = record.native
    if not isinstance(native, ExampleTransaction):
        raise MalformedResponse("not an examplechain record")
    return native


def _integer(reply: JsonObject, name: str) -> int:
    value = reply.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MalformedResponse(f"{name}: expected an integer")
    return value


def _items(reply: JsonObject) -> list[JsonObject]:
    items = reply.get("transactions")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise MalformedResponse("transactions: expected a list of objects")
    return [item for item in items if isinstance(item, dict)]
