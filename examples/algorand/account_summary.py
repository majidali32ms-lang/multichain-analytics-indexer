"""Sync one Algorand testnet account and print a summary of its activity.

python examples/algorand/account_summary.py [ADDRESS]
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from multichain_indexer import Indexer

DEFAULT_ACCOUNT = "JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM"
WINDOW_DAYS = 365


def summarise(address: str, database: Path) -> None:
    with Indexer("algorand", database=database) as indexer:
        account = indexer.account(address)
        result = indexer.sync(address)
        report = indexer.metrics(days=WINDOW_DAYS, account=address)

        print(f"Balance: {indexer.profile.format_native(account.balance)}")
        print(f"Stored {result.records_fetched} transactions in {result.requests_made} requests")
        print(f"Transactions in the last {WINDOW_DAYS} days: {report.transactions}")
        print(f"Fees paid: {indexer.profile.format_native(report.total_fees)}")
        print(f"Most transferred ASAs: {report.native['top_asas_by_transfers']}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        summarise(
            sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ACCOUNT, Path(folder) / "example.db"
        )
