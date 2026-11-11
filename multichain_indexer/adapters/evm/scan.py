"""Account history by reading every block over the RPC, for local networks such as Anvil.

No service is needed, but every block in the range costs a request, so this suits a local
node or a short range; on a public network it is slow and the endpoint may rate limit it.
A cursor is the number of the next block to read, walking down.
"""

from __future__ import annotations

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.history import (
    SCOPE_ALL,
    HistoryPage,
    HistorySource,
    SourceContext,
)
from multichain_indexer.core.errors import ConfigError, InvalidInput, MalformedResponse
from multichain_indexer.core.http import JsonObject

BLOCKS_PER_PAGE = 25


class RpcScanHistory:
    def __init__(self, rpc: EvmRpc) -> None:
        self._rpc = rpc

    @property
    def requests_made(self) -> int:
        # The adapter counts the RPC it shares with this source.
        return 0

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
        top = _parse_cursor(cursor) if cursor else to_height
        if top is None:
            top = await self._rpc.block_number()
        bottom = max(from_height or 0, top - BLOCKS_PER_PAGE + 1)
        hashes: list[str] = []
        for number in range(top, bottom - 1, -1):
            hashes.extend(_involving(await self._rpc.block_transactions(number), address))
        return HistoryPage(tuple(hashes), str(bottom - 1) if bottom > (from_height or 0) else None)

    async def aclose(self) -> None:
        """The RPC belongs to the adapter, which closes it."""


def create_rpc_scan_history(context: SourceContext) -> HistorySource:
    """Build the block scan; it lists only the transactions an account sent or received.

    Raises:
        ConfigError: The ``all`` scope was asked for.
    """
    if context.scope == SCOPE_ALL:
        setting = f"{context.chain.name}_{context.network}_HISTORY_SCOPE".upper()
        raise ConfigError(f"rpc-scan lists direct transactions only; set {setting} to direct")
    return RpcScanHistory(context.rpc)


def _parse_cursor(cursor: str) -> int:
    if not cursor.isdigit():
        raise InvalidInput(f"not a block scan cursor: {cursor!r}")
    return int(cursor)


def _involving(transactions: list[JsonObject], address: str) -> list[str]:
    wanted = address.lower()
    hashes = []
    for transaction in reversed(transactions):
        tx_hash = transaction.get("hash")
        if not isinstance(tx_hash, str):
            raise MalformedResponse("eth_getBlockByNumber: expected transaction hashes")
        parties = {str(transaction.get("from")).lower(), str(transaction.get("to")).lower()}
        if wanted in parties:
            hashes.append(tx_hash)
    return hashes
