"""Holder figures for one ERC-20 or BEP-20 token over a block range, from its ``Transfer`` logs.

Nothing is stored and nothing claims to cover the whole chain: the report names its block
range and the time it covers, and counts only the addresses that moved the token in it. Their
balances are read at the range's last block (``balances.py``), so a range is refused when more
than ``MAX_ADDRESSES`` addresses moved the token in it, and the state of that block must still
be kept by the endpoint that reads it.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from multichain_indexer.adapters.evm.balances import read_balances, read_total_supply
from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.encoding import parse_quantity
from multichain_indexer.adapters.evm.logs import iter_logs
from multichain_indexer.adapters.evm.models import EvmHolder, EvmTokenHolders
from multichain_indexer.adapters.evm.transactions import TRANSFER_TOPIC, token_transfer
from multichain_indexer.core.errors import InvalidInput, NetworkError
from multichain_indexer.core.http import JsonObject

MAX_ADDRESSES = 2_000
TOP_COUNT = 10
ZERO_ADDRESS = "0x" + "0" * 40
MILLISECONDS_PER_SECOND = 1000
# What geth-based nodes answer for state they have pruned.
STATE_NOT_KEPT = "missing trie node"


@dataclass(frozen=True)
class HolderQuery:
    """What a holder report reads: one token on one network, over a block range."""

    chain: str
    network: str
    token: str
    standard: str
    from_block: int
    to_block: int


@dataclass(frozen=True)
class _Move:
    block: int
    sender: str
    receiver: str
    value: int


@dataclass(frozen=True)
class HolderSources:
    """Where a holder report reads: blocks, state (balances) and Transfer logs.

    ``state_hint`` says what to do when ``state`` no longer keeps the range's last block.
    """

    blocks: EvmRpc
    state: EvmRpc
    logs: EvmRpc
    state_hint: str


async def token_holders(sources: HolderSources, query: HolderQuery) -> EvmTokenHolders:
    """Build the holder report.

    Raises:
        InvalidInput: The range is empty or too many addresses moved the token in it, the
            contract has no ``totalSupply`` or ``balanceOf``, or the state endpoint no longer
            keeps the range's last block.
    """
    if query.from_block > query.to_block:
        raise InvalidInput(f"--from-block {query.from_block} is after --to-block {query.to_block}")
    moves, active = await _moves_and_parties(sources.logs, query)
    balances, supply = await _state_at_end(sources, query, active)
    starts = _starting_balances(balances, moves)
    rpc = sources.blocks
    first, last = await _block_time(rpc, query.from_block), await _block_time(rpc, query.to_block)
    days = await _transfers_per_day(rpc, query, moves, first, last)
    return EvmTokenHolders(
        chain=query.chain,
        network=query.network,
        token=query.token,
        standard=query.standard,
        from_block=query.from_block,
        to_block=query.to_block,
        from_time=_time(first),
        to_time=_time(last),
        transfers=len(moves),
        transfers_per_day=days,
        active_addresses=len(active),
        holders_at_end=sum(1 for balance in balances.values() if balance > 0),
        new_holders=sum(1 for a in active if starts[a] == 0 and balances[a] > 0),
        exited_holders=sum(1 for a in active if starts[a] > 0 and balances[a] == 0),
        inconsistent_addresses=sum(1 for start in starts.values() if start < 0),
        total_supply=supply,
        top_holders=_top(balances, supply),
        top_share_of_supply=_top_share(balances, supply),
        observed_at=datetime.now(UTC),
    )


async def _moves_and_parties(logs: EvmRpc, query: HolderQuery) -> tuple[list[_Move], list[str]]:
    """Read the range's transfers and the addresses they name, stopping once too many."""
    moves: list[_Move] = []
    parties: set[str] = set()
    windows = iter_logs(logs, query.from_block, query.to_block, [TRANSFER_TOPIC], query.token)
    async for window in windows:
        for move in _moves(window):
            moves.append(move)
            parties.update((move.sender, move.receiver))
        parties.discard(ZERO_ADDRESS)
        if len(parties) > MAX_ADDRESSES:
            raise InvalidInput(
                f"more than {MAX_ADDRESSES} addresses moved {query.token} in blocks "
                f"{query.from_block} to {query.to_block}; a holder report reads at most "
                f"{MAX_ADDRESSES} balances, so choose a shorter range"
            )
    return moves, sorted(parties)


def _moves(logs: Sequence[JsonObject]) -> list[_Move]:
    moves = []
    for log in logs:
        transfer = token_transfer(log)
        if transfer is None or transfer.kind != "fungible" or transfer.value is None:
            continue
        block = parse_quantity(log.get("blockNumber"), "eth_getLogs")
        moves.append(_Move(block, transfer.sender, transfer.receiver, transfer.value))
    return moves


async def _state_at_end(
    sources: HolderSources, query: HolderQuery, active: list[str]
) -> tuple[dict[str, int], int]:
    try:
        balances = await read_balances(sources.state, query.token, active, query.to_block)
        supply = await read_total_supply(sources.state, query.token, query.to_block)
    except NetworkError as failed:
        if STATE_NOT_KEPT not in str(failed):
            raise
        raise InvalidInput(
            f"the endpoint no longer keeps the state of block {query.to_block}; "
            f"{sources.state_hint}"
        ) from None
    return balances, supply


def _starting_balances(balances: dict[str, int], moves: Sequence[_Move]) -> dict[str, int]:
    net: Counter[str] = Counter()
    for move in moves:
        net[move.receiver] += move.value
        net[move.sender] -= move.value
    return {address: balance - net[address] for address, balance in balances.items()}


def _top(balances: dict[str, int], supply: int) -> tuple[EvmHolder, ...]:
    ranked = sorted(
        ((address, balance) for address, balance in balances.items() if balance > 0),
        key=lambda item: (-item[1], item[0]),
    )[:TOP_COUNT]
    return tuple(
        EvmHolder(address=address, balance=balance, share_of_supply=_share(balance, supply))
        for address, balance in ranked
    )


def _top_share(balances: dict[str, int], supply: int) -> float | None:
    top = sorted(balances.values(), reverse=True)[:TOP_COUNT]
    return _share(sum(top), supply)


def _share(amount: int, supply: int) -> float | None:
    return amount / supply if supply else None


async def _block_time(rpc: EvmRpc, number: int) -> int:
    """Return a block's time in milliseconds, from ``milliTimestamp`` where the chain has it."""
    header = await rpc.block_header(number, immutable=True)
    if "milliTimestamp" in header:
        return parse_quantity(header.get("milliTimestamp"), "eth_getBlockByNumber")
    seconds = parse_quantity(header.get("timestamp"), "eth_getBlockByNumber")
    return seconds * MILLISECONDS_PER_SECOND


async def _transfers_per_day(
    rpc: EvmRpc, query: HolderQuery, moves: Sequence[_Move], first: int, last: int
) -> dict[str, int]:
    """Count transfers per UTC day; the first block of each day is found by bisection."""
    day = _time(first).date()
    starts = [query.from_block]
    labels = [day.isoformat()]
    while (midnight := _midnight_after(day)) <= last:
        starts.append(await _first_block_at(rpc, midnight, starts[-1], query.to_block))
        day = day + timedelta(days=1)
        labels.append(day.isoformat())
    counts = Counter(labels[bisect_right(starts, move.block) - 1] for move in moves)
    return {label: counts[label] for label in labels}


def _midnight_after(day: date) -> int:
    moment = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
    return int(moment.timestamp() * MILLISECONDS_PER_SECOND)


async def _first_block_at(rpc: EvmRpc, moment: int, low: int, high: int) -> int:
    """Return the first block in ``low..high`` whose time is at or after ``moment``."""
    while low < high:
        middle = (low + high) // 2
        if await _block_time(rpc, middle) < moment:
            low = middle + 1
        else:
            high = middle
    return low


def _time(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / MILLISECONDS_PER_SECOND, tz=UTC)
