"""Token balances of many addresses at one block, read in a few calls through Multicall3.

Public BNB Smart Chain and opBNB endpoints keep state for about the newest 100 blocks only
(checked on 2026-11-19: a balance 1,000 blocks back answers "missing trie node"), and opBNB
makes four blocks a second, so reading two thousand balances one call at a time would outlive
the state it reads. Multicall3, deployed at the same address on BNB Smart Chain, opBNB and most
EVM chains, runs up to ``CALLS_PER_BATCH`` ``balanceOf`` calls in one ``eth_call`` at one block.
Where it is not deployed, balances are read one call each.
"""

from __future__ import annotations

from collections.abc import Sequence

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.tokens import selector
from multichain_indexer.core.errors import InvalidInput, MalformedResponse

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CALLS_PER_BATCH = 200
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
    if not await rpc.code(MULTICALL3, block):
        return {address: await _one_balance(rpc, token, address, block) for address in addresses}
    balances: dict[str, int] = {}
    for start in range(0, len(addresses), CALLS_PER_BATCH):
        batch = addresses[start : start + CALLS_PER_BATCH]
        balances.update(zip(batch, await _batch_balances(rpc, token, batch, block), strict=True))
    return balances


async def _one_balance(rpc: EvmRpc, token: str, address: str, block: int) -> int:
    return _uint(await rpc.call(token, _balance_call(address), block), token, "balanceOf")


async def _batch_balances(
    rpc: EvmRpc, token: str, addresses: Sequence[str], block: int
) -> list[int]:
    calls = [(token, True, _balance_call(address)) for address in addresses]
    data = selector("aggregate3((address,bool,bytes)[])") + encode(
        ["(address,bool,bytes)[]"], [calls]
    )
    reply = await rpc.call(MULTICALL3, data, block)
    try:
        (results,) = decode(["(bool,bytes)[]"], reply or b"")
    except (DecodingError, OverflowError) as broken:
        raise MalformedResponse(f"Multicall3 aggregate3: {broken}") from None
    if len(results) != len(addresses):
        raise MalformedResponse("Multicall3 aggregate3: one result per call expected")
    return [
        _uint(returned if succeeded else None, token, "balanceOf")
        for succeeded, returned in results
    ]


def _balance_call(address: str) -> bytes:
    return selector("balanceOf(address)") + encode(["address"], [address])


def _uint(data: bytes | None, token: str, function: str) -> int:
    if data is None or len(data) != WORD_BYTES:
        raise InvalidInput(f"{token} did not answer {function}() at the range's last block")
    return int.from_bytes(data)
