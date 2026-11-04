"""Calls the Algorand Indexer REST API.

Requests go through ``ApiClient`` rather than the SDK's ``IndexerClient``, which blocks
the event loop and drops the HTTP status, so a rate limit could not be told from any
other failure. The SDK is still used for address validation and encoding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from multichain_indexer.core.errors import MalformedResponse
from multichain_indexer.core.http import ApiClient, JsonObject

PAGE_SIZE = 1000


class IndexerApi:
    def __init__(self, client: ApiClient) -> None:
        self._client = client

    @property
    def requests_made(self) -> int:
        return self._client.requests_made

    async def latest_round(self) -> int:
        health = await self._client.get_json("/health")
        latest = health.get("round")
        if not isinstance(latest, int):
            raise MalformedResponse("/health: the response has no round")
        return latest

    async def account(self, address: str) -> JsonObject:
        return await self._client.get_json(f"/v2/accounts/{address}")

    async def asset(self, asset_id: int) -> JsonObject:
        return await self._client.get_json(f"/v2/assets/{asset_id}")

    async def application(self, app_id: int) -> JsonObject:
        return await self._client.get_json(f"/v2/applications/{app_id}")

    async def account_transaction_pages(
        self,
        address: str,
        *,
        min_round: int | None,
        max_round: int | None,
        next_token: str | None = None,
    ) -> AsyncIterator[tuple[list[JsonObject], str | None]]:
        """Yield each page with its ``next-token``, until the Indexer stops returning one.

        A last page can still carry a token and an empty list, so an empty page also
        ends the walk.
        """
        params: dict[str, str | int] = {"limit": PAGE_SIZE}
        if min_round is not None:
            params["min-round"] = min_round
        if max_round is not None:
            params["max-round"] = max_round
        if next_token is not None:
            params["next"] = next_token

        path = f"/v2/accounts/{address}/transactions"
        while True:
            # Rounds up to a fixed upper bound are final, so that page can never change.
            page = await self._client.get_json(path, params, immutable=max_round is not None)
            transactions = _transactions_of(page, path)
            if not transactions:
                return
            token = page.get("next-token")
            following = token if isinstance(token, str) and token else None
            yield transactions, following
            if following is None:
                return
            params["next"] = following

    async def aclose(self) -> None:
        await self._client.aclose()


def _transactions_of(page: JsonObject, path: str) -> list[JsonObject]:
    transactions = page.get("transactions", [])
    if not isinstance(transactions, list):
        raise MalformedResponse(f"{path}: transactions is not a list")
    if not all(isinstance(item, dict) for item in transactions):
        raise MalformedResponse(f"{path}: a transaction is not an object")
    return [item for item in transactions if isinstance(item, dict)]
