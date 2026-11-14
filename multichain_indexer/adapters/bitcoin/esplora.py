"""Bitcoin data from an Esplora REST API, by default mempool.space's.

Esplora lists a script's history by its script hash: up to 50 unconfirmed transactions,
then confirmed ones 25 to a page, each page after the last transaction seen. The server is
``BITCOIN_<NETWORK>_ESPLORA_SERVER``, so a self-hosted Esplora or mempool server can stand
in for the public one.
"""

from __future__ import annotations

import re

from pydantic import JsonValue

from multichain_indexer.adapters.bitcoin.history import (
    BitcoinSource,
    BlockRef,
    HistoryPage,
    OutputTotals,
    ScriptActivity,
    SourceInput,
    SourceOutput,
    SourceTransaction,
)
from multichain_indexer.adapters.bitcoin.networks import BitcoinNetwork
from multichain_indexer.adapters.bitcoin.scripts import P2PK, P2TR, P2WPKH, P2WSH, Locator
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import InvalidInput, MalformedResponse, NotFound
from multichain_indexer.core.http import ApiClient, JsonObject, ResponseStore
from multichain_indexer.core.ratelimit import RateLimiter

CHAIN = "bitcoin"
SOURCE_NAME = "esplora"
SERVER_SETTING = "ESPLORA_SERVER"
# Esplora's documented page size for confirmed transactions; a shorter page is the last.
CHAIN_PAGE_SIZE = 25
# 30 requests at 1 a second drew no refusal from mempool.space on 2026-11-13; at 0.5, 2, 5
# and 10 a second about 4 in 30 failed, so failures there are not purely rate-based and
# the client's retries cover them. MCI_REQUESTS_PER_SECOND overrides this.
DEFAULT_REQUESTS_PER_SECOND = 1.0
# Esplora names segwit output types by witness version; records use the plain names.
SCRIPT_TYPES = {"v0_p2wpkh": P2WPKH, "v0_p2wsh": P2WSH, "v1_p2tr": P2TR}
TXID = re.compile(r"[0-9a-f]{64}")


class EsploraSource:
    """Reads one network through one Esplora server."""

    name = SOURCE_NAME

    def __init__(self, client: ApiClient) -> None:
        self._client = client

    @property
    def requests_made(self) -> int:
        return self._client.requests_made

    async def genesis_hash(self) -> str:
        blocks = await self._client.get_json_value("/blocks/0", immutable=True)
        first = blocks[0] if isinstance(blocks, list) and blocks else None
        if not isinstance(first, dict) or first.get("height") != 0:
            raise MalformedResponse("/blocks/0: expected the genesis block")
        return _text(first, "id", "/blocks/0")

    async def tip_height(self) -> int:
        height = await self._client.get_json_value("/blocks/tip/height")
        if not isinstance(height, int) or isinstance(height, bool) or height < 0:
            raise MalformedResponse("/blocks/tip/height: expected a block height")
        return height

    async def script_activity(self, locator: Locator) -> ScriptActivity:
        path = f"/scripthash/{locator.script_hash}"
        reply = await self._client.get_json(path)
        return ScriptActivity(
            confirmed=_totals(reply.get("chain_stats"), path),
            mempool=_totals(reply.get("mempool_stats"), path),
        )

    async def history_page(
        self, locator: Locator, *, cursor: str | None, include_mempool: bool
    ) -> HistoryPage:
        base = f"/scripthash/{locator.script_hash}/txs"
        unconfirmed = (
            await self._transactions(f"{base}/mempool")
            if include_mempool and cursor is None
            else ()
        )
        confirmed = await self._transactions(f"{base}/chain/{_cursor(cursor)}".rstrip("/"))
        last = confirmed[-1].txid if len(confirmed) >= CHAIN_PAGE_SIZE else None
        return HistoryPage(transactions=unconfirmed + confirmed, next_cursor=last)

    async def transaction(self, txid: str) -> SourceTransaction:
        path = f"/tx/{txid}"
        return parse_esplora_transaction(await self._client.get_json(path), path)

    async def block_in_best_chain(self, block_hash: str) -> bool:
        path = f"/block/{block_hash}/status"
        try:
            reply = await self._client.get_json(path)
        except NotFound:
            return False
        in_best_chain = reply.get("in_best_chain")
        if not isinstance(in_best_chain, bool):
            raise MalformedResponse(f"{path}: expected in_best_chain")
        return in_best_chain

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _transactions(self, path: str) -> tuple[SourceTransaction, ...]:
        reply = await self._client.get_json_value(path)
        if not isinstance(reply, list):
            raise MalformedResponse(f"{path}: expected a list of transactions")
        return tuple(parse_esplora_transaction(item, path) for item in reply)


def create_esplora_source(
    network: BitcoinNetwork, settings: Settings, cache: ResponseStore | None = None
) -> BitcoinSource:
    """Build the source for ``network``, on its configured or default server."""
    client = ApiClient(
        settings.endpoint(CHAIN, network.name, SERVER_SETTING, network.esplora_server),
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        limiter=RateLimiter(_requests_per_second(settings)),
        cache=cache,
    )
    return EsploraSource(client)


def parse_esplora_transaction(item: JsonValue, path: str) -> SourceTransaction:
    """Read one transaction in Esplora's JSON form.

    Raises:
        MalformedResponse: A field is missing or has the wrong type.
    """
    if not isinstance(item, dict):
        raise MalformedResponse(f"{path}: expected a transaction object")
    return SourceTransaction(
        txid=_txid(item, "txid", path),
        version=_integer(item, "version", path),
        locktime=_integer(item, "locktime", path),
        size=_integer(item, "size", path),
        weight=_integer(item, "weight", path),
        inputs=tuple(_input(entry, path) for entry in _list(item, "vin", path)),
        outputs=tuple(
            _output(index, entry, path) for index, entry in enumerate(_list(item, "vout", path))
        ),
        block=_block(item.get("status"), path),
        reported_fee=_optional_integer(item, "fee", path),
    )


def _input(entry: JsonValue, path: str) -> SourceInput:
    if not isinstance(entry, dict):
        raise MalformedResponse(f"{path}: expected an input object")
    witness = entry.get("witness") or []
    if not isinstance(witness, list):
        raise MalformedResponse(f"{path}: expected an input's witness list")
    sequence = _integer(entry, "sequence", path)
    if entry.get("is_coinbase") is True:
        return SourceInput(None, None, None, None, 0, sequence, len(witness), coinbase=True)
    spent = entry.get("prevout")
    if not isinstance(spent, dict):
        raise MalformedResponse(f"{path}: expected the output an input spends")
    output = _output(_integer(entry, "vout", path), spent, path)
    return SourceInput(
        txid=_txid(entry, "txid", path),
        vout=output.index,
        address=output.address,
        script_type=output.script_type,
        value=output.value,
        sequence=sequence,
        witness_items=len(witness),
        coinbase=False,
    )


def _output(index: int, entry: JsonValue, path: str) -> SourceOutput:
    if not isinstance(entry, dict):
        raise MalformedResponse(f"{path}: expected an output object")
    reported = _text(entry, "scriptpubkey_type", path)
    script_type = SCRIPT_TYPES.get(reported, reported)
    script = _text(entry, "scriptpubkey", path)
    address = entry.get("scriptpubkey_address")
    if address is not None and not isinstance(address, str):
        raise MalformedResponse(f"{path}: expected an output address")
    return SourceOutput(
        index=index,
        address=_p2pk_key(script) if script_type == P2PK else address,
        script_type=script_type,
        script_pubkey=script,
        value=_integer(entry, "value", path),
    )


def _p2pk_key(script: str) -> str:
    # A P2PK script is the key pushed with its length byte, then OP_CHECKSIG.
    return script[2:-2]


def _block(status: JsonValue, path: str) -> BlockRef | None:
    if not isinstance(status, dict) or not isinstance(status.get("confirmed"), bool):
        raise MalformedResponse(f"{path}: expected a transaction status")
    if not status["confirmed"]:
        return None
    return BlockRef(
        height=_integer(status, "block_height", path),
        hash=_txid(status, "block_hash", path),
        time=_integer(status, "block_time", path),
    )


def _totals(stats: JsonValue, path: str) -> OutputTotals:
    if not isinstance(stats, dict):
        raise MalformedResponse(f"{path}: expected chain_stats and mempool_stats")
    return OutputTotals(
        funded_count=_integer(stats, "funded_txo_count", path),
        funded_sum=_integer(stats, "funded_txo_sum", path),
        spent_count=_integer(stats, "spent_txo_count", path),
        spent_sum=_integer(stats, "spent_txo_sum", path),
        tx_count=_integer(stats, "tx_count", path),
    )


def _cursor(cursor: str | None) -> str:
    if cursor is None:
        return ""
    if not TXID.fullmatch(cursor):
        raise InvalidInput(f"not an Esplora history cursor: {cursor!r}")
    return cursor


def _list(item: JsonObject, key: str, path: str) -> list[JsonValue]:
    value = item.get(key)
    if not isinstance(value, list):
        raise MalformedResponse(f"{path}: expected {key} to be a list")
    return value


def _text(item: JsonObject, key: str, path: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise MalformedResponse(f"{path}: expected {key} to be text")
    return value


def _txid(item: JsonObject, key: str, path: str) -> str:
    value = _text(item, key, path)
    if not TXID.fullmatch(value):
        raise MalformedResponse(f"{path}: expected {key} to be 64 hex digits")
    return value


def _integer(item: JsonObject, key: str, path: str) -> int:
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MalformedResponse(f"{path}: expected {key} to be a whole number")
    return value


def _optional_integer(item: JsonObject, key: str, path: str) -> int | None:
    return None if item.get(key) is None else _integer(item, key, path)


def _requests_per_second(settings: Settings) -> float:
    if "requests_per_second" in settings.model_fields_set:
        return settings.requests_per_second
    return DEFAULT_REQUESTS_PER_SECOND
