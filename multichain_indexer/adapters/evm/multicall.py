"""Many read-only calls at one block in one ``eth_call``, through Multicall3.

Multicall3 is deployed at the same address on BNB Smart Chain, opBNB and most EVM chains (its
code was found at this address on BSC mainnet and testnet and opBNB mainnet and testnet on
2026-11-19). Its ``aggregate3`` runs a list of calls and returns each one's success and data, so
a call that reverts does not fail the others.
"""

from __future__ import annotations

from collections.abc import Sequence

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.tokens import selector
from multichain_indexer.core.errors import MalformedResponse

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CALLS_PER_BATCH = 200


async def has_multicall(rpc: EvmRpc, block: int) -> bool:
    return bool(await rpc.code(MULTICALL3, block))


async def aggregate(
    rpc: EvmRpc, calls: Sequence[tuple[str, bytes]], block: int
) -> list[bytes | None]:
    """Run ``(contract, calldata)`` calls at ``block``, ``CALLS_PER_BATCH`` per ``eth_call``.

    Returns each call's data, or None for a call that reverted.

    Raises:
        MalformedResponse: Multicall3 answered with something it never returns.
    """
    results: list[bytes | None] = []
    for start in range(0, len(calls), CALLS_PER_BATCH):
        results.extend(await _batch(rpc, calls[start : start + CALLS_PER_BATCH], block))
    return results


async def _batch(rpc: EvmRpc, calls: Sequence[tuple[str, bytes]], block: int) -> list[bytes | None]:
    wrapped = [(contract, True, data) for contract, data in calls]
    data = selector("aggregate3((address,bool,bytes)[])") + encode(
        ["(address,bool,bytes)[]"], [wrapped]
    )
    reply = await rpc.call(MULTICALL3, data, block)
    try:
        (results,) = decode(["(bool,bytes)[]"], reply or b"")
    except (DecodingError, OverflowError) as broken:
        raise MalformedResponse(f"Multicall3 aggregate3: {broken}") from None
    if len(results) != len(calls):
        raise MalformedResponse("Multicall3 aggregate3: one result per call expected")
    return [bytes(returned) if succeeded else None for succeeded, returned in results]
