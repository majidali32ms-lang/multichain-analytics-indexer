"""Account history from the FastNEAR Transactions API.

``/v0/account`` lists an account's transactions as index rows; ``/v0/transactions`` returns
up to 20 of them with their receipts and outcomes, in one request. A page is therefore one
of each, and is stored atomically by the sync.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue, SecretStr, ValidationError

from multichain_indexer.adapters.near.history import (
    ExecutedReceipt,
    ExecutedTransaction,
    ExecutionOutcome,
    HistoryPage,
    HistorySource,
    UnavailableHistory,
)
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import MalformedResponse
from multichain_indexer.core.http import ApiClient, JsonObject, ResponseStore
from multichain_indexer.core.ratelimit import RateLimiter

CHAIN = "near"
DEFAULT_SERVERS = {
    "mainnet": "https://tx.main.fastnear.com",
    "testnet": "https://tx.test.fastnear.com",
}
TRANSACTIONS_PER_REQUEST = 20
ACCOUNT_PATH = "/v0/account"
TRANSACTIONS_PATH = "/v0/transactions"
BLOCKS_PATH = "/v0/blocks"
# FastNEAR publishes no free limit. Measured without a key, one network at a time: no HTTP 429
# at 1 or 0.5 requests per second, and one in 40 at 0.25. An earlier run with other tests in
# parallel drew some at 2 and at 1. So there is no safe rate to promise, and retries remain the
# real protection. MCI_REQUESTS_PER_SECOND overrides this.
DEFAULT_REQUESTS_PER_SECOND = 0.5

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _RawBlock(_Raw):
    block_height: int


class _RawBlocks(_Raw):
    blocks: list[_RawBlock]


class _RawRow(_Raw):
    transaction_hash: str
    tx_block_height: int


class _RawRows(_Raw):
    account_txs: list[_RawRow]
    resume_token: str | None = None


class _RawOutcomeBody(_Raw):
    executor_id: str
    gas_burnt: int
    tokens_burnt: int
    status: JsonValue
    receipt_ids: list[str] = []
    logs: list[str] = []


class _RawOutcome(_Raw):
    id: str
    block_height: int
    block_timestamp: int
    outcome: _RawOutcomeBody


class _RawActionBody(_Raw):
    actions: list[JsonValue] = []


class _RawReceiptBody(_Raw):
    Action: _RawActionBody | None = None


class _RawReceipt(_Raw):
    receipt_id: str
    predecessor_id: str
    receiver_id: str
    receipt: _RawReceiptBody


class _RawExecutedReceipt(_Raw):
    receipt: _RawReceipt
    execution_outcome: _RawOutcome | None = None


class _RawTransactionBody(_Raw):
    hash: str
    signer_id: str
    receiver_id: str
    public_key: str
    nonce: int
    actions: list[JsonValue]


class _RawTransaction(_Raw):
    transaction: _RawTransactionBody
    execution_outcome: _RawOutcome
    receipts: list[_RawExecutedReceipt] = []


class _RawTransactions(_Raw):
    transactions: list[_RawTransaction]


class FastNearHistory:
    """Reads one network from the FastNEAR Transactions API."""

    def __init__(self, client: ApiClient) -> None:
        self._client = client

    @property
    def requests_made(self) -> int:
        return self._client.requests_made

    async def indexed_height(self) -> int:
        reply = await self._client.post_json(BLOCKS_PATH, {"limit": 1, "desc": True})
        blocks = _validate(_RawBlocks, reply, BLOCKS_PATH).blocks
        if not blocks:
            raise MalformedResponse(f"{BLOCKS_PATH}: the service reported no indexed block")
        return blocks[0].block_height

    async def account_page(
        self,
        account: str,
        *,
        from_height: int | None,
        to_height: int | None,
        cursor: str | None,
        immutable_up_to: int | None,
    ) -> HistoryPage:
        request = _rows_request(account, from_height, to_height, cursor)
        reply = await self._client.post_json(ACCOUNT_PATH, request)
        rows = _validate(_RawRows, reply, ACCOUNT_PATH)
        if not rows.account_txs:
            return HistoryPage((), None)
        immutable = immutable_up_to is not None and all(
            row.tx_block_height <= immutable_up_to for row in rows.account_txs
        )
        transactions = await self._transactions(rows.account_txs, immutable=immutable)
        return HistoryPage(transactions, rows.resume_token)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _transactions(
        self, rows: list[_RawRow], *, immutable: bool
    ) -> tuple[ExecutedTransaction, ...]:
        hashes: list[JsonValue] = list(dict.fromkeys(row.transaction_hash for row in rows))
        payload: JsonObject = {"tx_hashes": hashes}
        reply = await self._client.post_json(TRANSACTIONS_PATH, payload, immutable=immutable)
        found = {
            raw.transaction.hash: raw
            for raw in _validate(_RawTransactions, reply, TRANSACTIONS_PATH).transactions
        }
        return tuple(_executed_transaction(_require_found(found, str(h))) for h in hashes)


def create_fastnear_history(
    network: str, settings: Settings, cache: ResponseStore | None
) -> HistorySource:
    """Build the source for ``network``, with an API key if one is configured."""
    configured = settings.chain_setting(CHAIN, network, "HISTORY_SERVER")
    default = DEFAULT_SERVERS.get(network)
    if configured is None and default is None:
        setting = f"{CHAIN}_{network}_HISTORY_SERVER".upper()
        return UnavailableHistory(
            f"NEAR {network} has no public history service; "
            f"set {setting} to read an account's transactions"
        )
    client = ApiClient(
        settings.endpoint(CHAIN, network, "HISTORY_SERVER", default or ""),
        headers=_auth_headers(settings, network),
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        limiter=RateLimiter(_requests_per_second(settings)),
        cache=cache,
    )
    return FastNearHistory(client)


def _auth_headers(settings: Settings, network: str) -> dict[str, SecretStr]:
    key = settings.chain_setting(CHAIN, network, "HISTORY_API_KEY")
    if key is None:
        return {}
    return {"Authorization": SecretStr(f"Bearer {key.get_secret_value()}")}


def _requests_per_second(settings: Settings) -> float:
    if "requests_per_second" in settings.model_fields_set:
        return settings.requests_per_second
    return DEFAULT_REQUESTS_PER_SECOND


def _rows_request(
    account: str, from_height: int | None, to_height: int | None, cursor: str | None
) -> JsonObject:
    request: JsonObject = {
        "account_id": account,
        "limit": TRANSACTIONS_PER_REQUEST,
        "desc": True,
    }
    if from_height is not None:
        request["from_tx_block_height"] = from_height
    if to_height is not None:
        request["to_tx_block_height"] = to_height
    if cursor is not None:
        request["resume_token"] = cursor
    return request


def _require_found(found: dict[str, _RawTransaction], tx_hash: str) -> _RawTransaction:
    # The service leaves out a hash it cannot return. Carrying on would move the sync's
    # cursor past that transaction and lose it for good.
    raw = found.get(tx_hash)
    if raw is None:
        raise MalformedResponse(f"{TRANSACTIONS_PATH}: transaction {tx_hash} was not returned")
    return raw


def _executed_transaction(raw: _RawTransaction) -> ExecutedTransaction:
    body = raw.transaction
    return ExecutedTransaction(
        hash=body.hash,
        signer_id=body.signer_id,
        receiver_id=body.receiver_id,
        public_key=body.public_key,
        nonce=body.nonce,
        actions=tuple(body.actions),
        outcome=_outcome(raw.execution_outcome),
        receipts=tuple(
            _executed_receipt(receipt, receipt.execution_outcome)
            for receipt in raw.receipts
            if receipt.execution_outcome is not None
        ),
    )


def _executed_receipt(raw: _RawExecutedReceipt, outcome: _RawOutcome) -> ExecutedReceipt:
    action_body = raw.receipt.receipt.Action
    return ExecutedReceipt(
        receipt_id=raw.receipt.receipt_id,
        predecessor_id=raw.receipt.predecessor_id,
        receiver_id=raw.receipt.receiver_id,
        actions=tuple(action_body.actions) if action_body else (),
        outcome=_outcome(outcome),
    )


def _outcome(raw: _RawOutcome) -> ExecutionOutcome:
    return ExecutionOutcome(
        id=raw.id,
        executor_id=raw.outcome.executor_id,
        gas_burnt=raw.outcome.gas_burnt,
        tokens_burnt=raw.outcome.tokens_burnt,
        status=raw.outcome.status,
        receipt_ids=tuple(raw.outcome.receipt_ids),
        logs=tuple(raw.outcome.logs),
        block_height=raw.block_height,
        block_timestamp_ns=raw.block_timestamp,
    )


def _validate(model: type[_ModelT], data: object, what: str) -> _ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise MalformedResponse(f"{what}: unexpected fields: {', '.join(fields)}") from None
