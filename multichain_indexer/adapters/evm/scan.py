"""Account history by reading every block over the RPC: local networks such as Anvil, and BNB
Smart Chain testnet, which no free history service lists.

No service is needed, but every block in the range costs a request. BNB Smart Chain testnet
makes about 192,000 blocks a day (0.45 s each), so a day of its history is about 10.7 hours of
requests at 5 a second. On a public network a scan therefore refuses a range of more than
``MAX_SCAN_BLOCKS`` unless ``<CHAIN>_<NETWORK>_SCAN_FROM_BLOCK`` names the first block wanted;
older blocks are then never read. A cursor is the number of the next block to read, walking
down.

The ``all`` scope also lists transactions that moved a token to or from the account inside
another account's call, from the token contracts' ``Transfer`` logs. BNB Smart Chain's public
endpoints refuse ``eth_getLogs``, so on its testnet the logs come from NodeReal with the free
``NODEREAL_API_KEY``; elsewhere from the chain's own RPC.
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import JsonValue

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.encoding import parse_quantity
from multichain_indexer.adapters.evm.history import (
    SCOPE_ALL,
    HistoryPage,
    HistorySource,
    SourceContext,
)
from multichain_indexer.adapters.evm.logs import logs_rpc
from multichain_indexer.adapters.evm.profiles import LOCALNET
from multichain_indexer.adapters.evm.transactions import TRANSFER_TOPIC
from multichain_indexer.core.errors import ConfigError, InvalidInput, MalformedResponse
from multichain_indexer.core.http import JsonObject

BLOCKS_PER_PAGE = 25
# About 2.5 hours of BNB Smart Chain testnet, about 67 minutes of requests at 5 a second.
MAX_SCAN_BLOCKS = 20_000
TOPIC_HEX_DIGITS = 64


class _Position(NamedTuple):
    block: int
    transaction_index: int
    tx_hash: str


class RpcScanHistory:
    def __init__(
        self,
        rpc: EvmRpc,
        *,
        guarded: bool = False,
        start_block: int | None = None,
        guard_message: str = "",
        logs: EvmRpc | None = None,
    ) -> None:
        self._rpc = rpc
        self._guarded = guarded
        self._start_block = start_block
        self._guard_message = guard_message
        self._logs = logs

    @property
    def requests_made(self) -> int:
        # The adapter counts the RPC it shares with this source; a separate logs server is
        # counted here.
        if self._logs is None or self._logs is self._rpc:
            return 0
        return self._logs.requests_made

    @property
    def indexed_up_to(self) -> int | None:
        return None

    async def account_page(
        self,
        address: str,
        *,
        from_height: int | None,
        to_height: int | None,
        cursor: str | None,
    ) -> HistoryPage:
        lowest = max(from_height or 0, self._start_block or 0)
        top = _parse_cursor(cursor) if cursor else to_height
        if top is None:
            top = await self._rpc.block_number()
        if cursor is None:
            self._check_range(lowest, top)
        bottom = max(lowest, top - BLOCKS_PER_PAGE + 1)
        found: list[_Position] = []
        for number in range(top, bottom - 1, -1):
            transactions = await self._rpc.block_transactions(number)
            found.extend(_involving(transactions, address, number))
        if self._logs is not None:
            found.extend(_logged(await _token_transfers(self._logs, address, bottom, top)))
        return HistoryPage(_newest_first(found), str(bottom - 1) if bottom > lowest else None)

    async def aclose(self) -> None:
        """The adapter's RPC is closed by the adapter; a separate logs server is closed here."""
        if self._logs is not None and self._logs is not self._rpc:
            await self._logs.aclose()

    def _check_range(self, lowest: int, top: int) -> None:
        if self._guarded and self._start_block is None and top - lowest + 1 > MAX_SCAN_BLOCKS:
            raise ConfigError(self._guard_message.format(blocks=top - lowest + 1))


async def _token_transfers(logs: EvmRpc, address: str, bottom: int, top: int) -> list[JsonObject]:
    party = "0x" + address.lower().removeprefix("0x").rjust(TOPIC_HEX_DIGITS, "0")
    sent: list[JsonValue] = [TRANSFER_TOPIC, party]
    received: list[JsonValue] = [TRANSFER_TOPIC, None, party]
    return [*await logs.logs(bottom, top, sent), *await logs.logs(bottom, top, received)]


def create_rpc_scan_history(context: SourceContext) -> HistorySource:
    """Build the block scan for the context's network.

    Raises:
        ConfigError: ``SCAN_FROM_BLOCK`` is not a block number, or the ``all`` scope needs a
            logs key that is not set.
    """
    public = context.network != LOCALNET
    return RpcScanHistory(
        context.rpc,
        guarded=public,
        start_block=_start_block(context),
        guard_message=_guard_message(context),
        logs=_logs_rpc(context) if context.scope == SCOPE_ALL else None,
    )


def _start_block(context: SourceContext) -> int | None:
    configured = context.settings.chain_setting(
        context.chain.name, context.network, "SCAN_FROM_BLOCK"
    )
    if configured is None:
        return None
    value = configured.get_secret_value()
    if not value.isdigit():
        raise ConfigError(f"{_setting(context, 'SCAN_FROM_BLOCK')} must be a block number")
    return int(value)


def _guard_message(context: SourceContext) -> str:
    title = context.chain.networks[context.network].title if context.network != LOCALNET else ""
    return (
        f"rpc-scan reads {title or 'this network'} one block per request, and this range is "
        f"{{blocks}} blocks (at most {MAX_SCAN_BLOCKS} without a start block). Set "
        f"{_setting(context, 'SCAN_FROM_BLOCK')} to the first block to read; older blocks are "
        "then not read."
    )


def _logs_rpc(context: SourceContext) -> EvmRpc:
    try:
        return logs_rpc(
            context.chain, context.network, context.settings, context.rpc, "token transfers"
        )
    except ConfigError as missing:
        scope = _setting(context, "HISTORY_SCOPE")
        raise ConfigError(f"{missing}, or set {scope} to direct") from None


def _setting(context: SourceContext, name: str) -> str:
    return f"{context.chain.name}_{context.network}_{name}".upper()


def _parse_cursor(cursor: str) -> int:
    if not cursor.isdigit():
        raise InvalidInput(f"not a block scan cursor: {cursor!r}")
    return int(cursor)


def _involving(transactions: list[JsonObject], address: str, block: int) -> list[_Position]:
    wanted = address.lower()
    found = []
    for index, transaction in enumerate(transactions):
        tx_hash = transaction.get("hash")
        if not isinstance(tx_hash, str):
            raise MalformedResponse("eth_getBlockByNumber: expected transaction hashes")
        parties = {str(transaction.get("from")).lower(), str(transaction.get("to")).lower()}
        if wanted in parties:
            found.append(_Position(block, index, tx_hash))
    return found


def _logged(logs: list[JsonObject]) -> list[_Position]:
    found = []
    for log in logs:
        tx_hash = log.get("transactionHash")
        block, index = log.get("blockNumber"), log.get("transactionIndex")
        if not (isinstance(tx_hash, str) and isinstance(block, str) and isinstance(index, str)):
            raise MalformedResponse("eth_getLogs: expected transaction hashes and positions")
        found.append(
            _Position(
                parse_quantity(block, "eth_getLogs"), parse_quantity(index, "eth_getLogs"), tx_hash
            )
        )
    return found


def _newest_first(positions: list[_Position]) -> tuple[str, ...]:
    # A transaction with several transfers, or one both direct and logged, is listed once.
    unique = {position.tx_hash: position for position in positions}
    return tuple(position.tx_hash for position in sorted(unique.values(), reverse=True))
