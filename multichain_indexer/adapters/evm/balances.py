"""Token balances of many addresses at one block, read in a few calls through Multicall3.

Public BNB Smart Chain and opBNB endpoints keep state for about the newest 100 blocks only
(checked on 2026-11-19: a balance 1,000 blocks back answers "missing trie node"), and opBNB
makes four blocks a second, so reading two thousand balances one call at a time would outlive
the state it reads. Multicall3 (``multicall.py``) runs many ``balanceOf`` calls in one
``eth_call`` at one block. Where it is not deployed, balances are read one call each.
"""

from __future__ import annotations

from collections.abc import Sequence

from eth_abi.abi import encode

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.multicall import aggregate, has_multicall
from multichain_indexer.adapters.evm.tokens import selector
from multichain_indexer.core.errors import InvalidInput

WORD_BYTES = 32


async def read_total_supply(rpc: EvmRpc, token: str, block: int) -> int:
    """Return ``totalSupply()`` at ``block``.

    Raises:
        InvalidInput: The contract does not answer it.
    """
    return _uint(await rpc.call(token, selector("totalSupply()"), block), token, "totalSupply")


async def read_balances(
    rpc: EvmRpc, token: str, addresses: Sequence[str], block: int
) -> dict[str, int]:
    """Return ``balanceOf`` of every address at ``block``.

    Raises:
        InvalidInput: The contract does not answer ``balanceOf`` for an address.
        MalformedResponse: Multicall3 answered with something it never returns.
    """
    if not addresses:
        return {}
    calls = [(token, _balance_call(address)) for address in addresses]
    if await has_multicall(rpc, block):
        replies = await aggregate(rpc, calls, block)
    else:
        replies = [await rpc.call(token, data, block) for _, data in calls]
    return {
        address: _uint(reply, token, "balanceOf")
        for address, reply in zip(addresses, replies, strict=True)
    }


def _balance_call(address: str) -> bytes:
    return selector("balanceOf(address)") + encode(["address"], [address])


def _uint(data: bytes | None, token: str, function: str) -> int:
    if data is None or len(data) != WORD_BYTES:
        raise InvalidInput(f"{token} did not answer {function}() at the range's last block")
    return int.from_bytes(data)
