"""Compare NEAR history from FastNEAR with the same history from NearBlocks.

For a sample of accounts and block ranges, it reports the transactions only one service
lists and, for those both list, any difference in signer, receiver, block height, status or
fee. The fee and status are the ones this project's parser works out from FastNEAR's
receipts; NearBlocks states its own. NearBlocks' free tier allows 6 calls a minute, so this
is slow, and it is a sample, not proof of completeness.

    python scripts/crosscheck_near_history.py
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue, SecretStr

from multichain_indexer.adapters.near.fastnear import DEFAULT_SERVERS, create_fastnear_history
from multichain_indexer.adapters.near.parser import parse_transaction
from multichain_indexer.core.config import Settings
from multichain_indexer.core.http import ApiClient
from multichain_indexer.core.models import Status, TransactionRecord
from multichain_indexer.core.ratelimit import RateLimiter

ROLE_ROWS_PER_PAGE = 200
ROLE_REQUESTS_PER_SECOND = 0.5

NEARBLOCKS = {
    "testnet": "https://api-testnet.nearblocks.io",
    "mainnet": "https://api.nearblocks.io",
}
ROWS_PER_SAMPLE = 25
NEARBLOCKS_PAUSE_SECONDS = 12
RATE_LIMIT_PAUSE_SECONDS = 70
# NearBlocks answered HTTP 503 now and then on 2026-11-08; retrying a few times got through.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 8
# Nanoseconds: 2024-01-01 and 2022-01-01, to look well behind the newest transactions.
SINCE_2024 = "1704067200000000000"
SINCE_2022 = "1640995200000000000"


@dataclass(frozen=True)
class Sample:
    network: str
    account: str
    before_ts: str | None = None


SAMPLES = (
    Sample("testnet", "guest-book.testnet"),
    Sample("testnet", "guest-book.testnet", SINCE_2024),
    Sample("testnet", "guest-book.testnet", SINCE_2022),
    Sample("testnet", "calmhare3618.testnet"),
    Sample("testnet", "wrap.testnet", SINCE_2024),
    Sample("testnet", "testnet"),
    Sample("mainnet", "root.near"),
    Sample("mainnet", "root.near", SINCE_2022),
    Sample("mainnet", "near", SINCE_2024),
    Sample("mainnet", "wrap.near"),
    Sample("mainnet", "wrap.near", SINCE_2022),
    Sample("mainnet", "aurora", SINCE_2024),
)


def nearblocks_rows(sample: Sample) -> list[dict[str, Any]]:
    url = (
        f"{NEARBLOCKS[sample.network]}/v3/accounts/{sample.account}/txns?limit={ROWS_PER_SAMPLE}"
        + (f"&before_ts={sample.before_ts}" if sample.before_ts else "")
    )
    headers = {"User-Agent": "history-crosscheck", "Accept": "application/json"}
    for _ in range(MAX_ATTEMPTS):
        time.sleep(NEARBLOCKS_PAUSE_SECONDS)
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=90
            ) as response:
                data: list[dict[str, Any]] = json.load(response)["data"]
                return data
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUS:
                raise
            print(f"  NearBlocks answered HTTP {error.code}, trying again", flush=True)
            time.sleep(RATE_LIMIT_PAUSE_SECONDS if error.code == 429 else NEARBLOCKS_PAUSE_SECONDS)
    raise RuntimeError(f"NearBlocks kept failing for {sample}")


async def fastnear_records(sample: Sample, low: int, high: int) -> dict[str, TransactionRecord]:
    source = create_fastnear_history(sample.network, Settings(), None)
    records: dict[str, TransactionRecord] = {}
    cursor = None
    try:
        while True:
            page = await source.account_page(
                sample.account,
                from_height=low,
                to_height=high,
                cursor=cursor,
                immutable_up_to=None,
            )
            for executed in page.transactions:
                records[executed.hash] = parse_transaction(executed, sample.network)
            cursor = page.next_cursor
            if cursor is None:
                return records
    finally:
        await source.aclose()


async def extra_roles(sample: Sample, low: int, high: int, extra: set[str]) -> dict[str, int]:
    """Count the role flags FastNEAR gives the account in the transactions only it lists."""
    client = ApiClient(
        SecretStr(DEFAULT_SERVERS[sample.network]),
        max_retries=4,
        limiter=RateLimiter(ROLE_REQUESTS_PER_SECOND),
    )
    roles: Counter[str] = Counter()
    request: dict[str, JsonValue] = {
        "account_id": sample.account,
        "limit": ROLE_ROWS_PER_PAGE,
        "desc": True,
        "from_tx_block_height": low,
        "to_tx_block_height": high,
    }
    try:
        while True:
            reply = await client.post_json("/v0/account", request)
            rows = reply.get("account_txs")
            assert isinstance(rows, list)
            for row in rows:
                assert isinstance(row, dict)
                if row["transaction_hash"] in extra:
                    flags = [name for name, value in row.items() if value is True]
                    roles[",".join(sorted(flags)) or "none"] += 1
            token = reply.get("resume_token")
            if not token:
                return dict(roles.most_common())
            request["resume_token"] = token
    finally:
        await client.aclose()


def differences(row: dict[str, Any], record: TransactionRecord) -> list[str]:
    status_true = row["outcomes"]["status"]
    expected_status = {True: Status.FINAL, False: Status.FAILED}.get(status_true)
    fields = (
        ("signer", row["signer_account_id"], record.sender),
        ("receiver", row["receiver_account_id"], record.receiver),
        ("height", int(row["block"]["block_height"]), record.native.block_height),
        ("status", expected_status, record.status),
        ("fee", int(row["outcomes_agg"]["transaction_fee"]), record.fee),
    )
    return [f"{name}: {theirs} vs {ours}" for name, theirs, ours in fields if theirs != ours]


async def check(sample: Sample) -> dict[str, int]:
    rows = nearblocks_rows(sample)
    label = f"{sample.network} {sample.account} before={sample.before_ts or 'now'}"
    if not rows:
        print(f"{label}: NearBlocks lists nothing here", flush=True)
        return {}
    heights = [int(row["block"]["block_height"]) for row in rows]
    # NearBlocks' last row may share a block with rows it cut off, so that block is left out.
    low, high = min(heights) + 1, max(heights)
    in_range = [row for row in rows if low <= int(row["block"]["block_height"]) <= high]
    records = await fastnear_records(sample, low, high)
    theirs = {row["transaction_hash"] for row in in_range}

    missing = sorted(theirs - set(records))
    extra = sorted(set(records) - theirs)
    direct = {h for h, r in records.items() if sample.account in (r.sender, r.receiver)}
    their_direct = {
        row["transaction_hash"]
        for row in in_range
        if sample.account in (row["signer_account_id"], row["receiver_account_id"])
    }
    print(
        f"  signer or receiver: NearBlocks {len(their_direct)} of {len(theirs)}, "
        f"FastNEAR {len(direct)} of {len(records)}, "
        f"same set: {direct == their_direct}, "
        f"extras that are signer or receiver: {len(direct - theirs)}",
        flush=True,
    )
    if extra:
        print(f"  roles of the extras: {await extra_roles(sample, low, high, set(extra))}")
    mismatched = 0
    for row in in_range:
        record = records.get(row["transaction_hash"])
        found = differences(row, record) if record else []
        if found:
            mismatched += 1
            print(f"  {row['transaction_hash']}: {'; '.join(found)}", flush=True)
    print(
        f"{label}: blocks {low}..{high}, NearBlocks {len(theirs)}, FastNEAR {len(records)}, "
        f"only NearBlocks {len(missing)}, only FastNEAR {len(extra)}, differing {mismatched}",
        flush=True,
    )
    for tx_hash in missing[:5]:
        print(f"  only NearBlocks: {tx_hash}", flush=True)
    for tx_hash in extra[:3]:
        print(f"  only FastNEAR: {tx_hash}", flush=True)
    return {
        "compared": len(theirs) - len(missing),
        "only_nearblocks": len(missing),
        "only_fastnear": len(extra),
        "differing": mismatched,
    }


async def main() -> None:
    assert set(DEFAULT_SERVERS) == set(NEARBLOCKS)
    totals: dict[str, int] = {}
    for sample in SAMPLES:
        for name, count in (await check(sample)).items():
            totals[name] = totals.get(name, 0) + count
    print("TOTAL", totals, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
