"""Sync one Solana devnet account and print a summary of its activity.

python examples/solana/account_summary.py [ADDRESS]
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from multichain_indexer import Indexer

DEFAULT_ACCOUNT = "FwcsNmTLbf8VVqxuBMjEDTh39gXR87n3vAnAwvAiPtRy"
WINDOW_DAYS = 365


def summarise(address: str, database: Path) -> None:
    with Indexer("solana", database=database) as indexer:
        account = indexer.account(address)
        result = indexer.sync(address)
        report = indexer.metrics(days=WINDOW_DAYS, account=address)

        print(f"Balance: {indexer.profile.format_native(account.balance)}")
        print(f"Stored {result.records_fetched} transactions in {result.requests_made} requests")
        print(f"Transactions in the last {WINDOW_DAYS} days: {report.transactions}")
        print(f"Fees paid: {report.total_fees} lamports")
        print(f"Priority fees: {report.native['priority_fees_lamports']}")
        print(f"Transaction versions: {report.native['transaction_versions']}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        summarise(
            sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ACCOUNT, Path(folder) / "example.db"
        )
