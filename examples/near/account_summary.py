"""Sync one NEAR testnet account and print a summary of its activity.

python examples/near/account_summary.py [ACCOUNT_ID]
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from multichain_indexer import Indexer

DEFAULT_ACCOUNT = "f781edbfe07287152e3912cb698af4155234df3c2d9c23bf625f675b8f6de5fa"
WINDOW_DAYS = 365


def summarise(account_id: str, database: Path) -> None:
    with Indexer("near", database=database) as indexer:
        account = indexer.account(account_id)
        result = indexer.sync(account_id)
        report = indexer.metrics(days=WINDOW_DAYS, account=account_id)

        print(f"Balance: {indexer.profile.format_native(account.balance)}")
        print(f"Access keys: {len(account.native.access_keys)}, shard {account.native.shard_id}")
        print(f"Stored {result.records_fetched} transactions in {result.requests_made} requests")
        print(f"Transactions in the last {WINDOW_DAYS} days: {report.transactions}")
        print(f"Fees paid: {report.native['fees_near']} NEAR")
        print(f"Gas burnt: {report.native['gas_burnt']['tgas']:.3f} Tgas")
        print(f"Receipts: {report.native['receipts']}")
        print(f"Most called methods: {report.native['function_calls_by_method'][:3]}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        summarise(
            sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ACCOUNT, Path(folder) / "example.db"
        )
