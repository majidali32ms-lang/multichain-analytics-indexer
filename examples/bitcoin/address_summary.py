"""Sync one Bitcoin testnet4 address and print a summary of its activity.

python examples/bitcoin/address_summary.py [ADDRESS]
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from multichain_indexer import Indexer

DEFAULT_ADDRESS = "mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu"
WINDOW_DAYS = 365


def summarise(address: str, database: Path) -> None:
    with Indexer("bitcoin", database=database) as indexer:
        account = indexer.account(address)
        result = indexer.sync(address)
        report = indexer.metrics(days=WINDOW_DAYS, account=address)
        rates = report.native["fee_rate_sat_per_vb"]

        print(f"Balance: {indexer.profile.format_native(account.balance)}")
        print(f"Type: {account.native.address_type}, UTXOs: {account.native.utxo_count}")
        print(f"Quantum exposure: {account.native.quantum_exposure.status}")
        print(f"Stored {result.records_fetched} transactions in {result.requests_made} requests")
        print(f"Transactions in the last {WINDOW_DAYS} days: {report.transactions}")
        print(f"Fees: {report.native['fees_sats']} sats ({report.native['fees_btc']} BTC)")
        print(f"Fee rate: {rates}")
        print(f"Output types: {report.native['address_types']}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        summarise(
            sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ADDRESS, Path(folder) / "example.db"
        )
