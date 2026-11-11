"""Sync one EVM testnet account and print a summary of its activity.

python examples/evm/account_summary.py [CHAIN] [ADDRESS]

History needs the chain's history service key in the environment or in .env, for example
BLOCKSCOUT_API_KEY for Ethereum's Sepolia, the default here.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from multichain_indexer import Indexer

DEFAULT_CHAIN = "ethereum"
# A small Sepolia account: 20 transactions on 2026-11-09.
DEFAULT_ADDRESS = "0xAA37651031A4b641962550415dB2C43E8d962232"
WINDOW_DAYS = 3650


def summarise(chain: str, address: str, database: Path) -> None:
    with Indexer(chain, database=database) as indexer:
        account = indexer.account(address)
        result = indexer.sync(address)
        report = indexer.metrics(days=WINDOW_DAYS, account=result.account)
        symbol = indexer.profile.native_symbol.lower()

        print(f"Balance: {indexer.profile.format_native(account.balance)}")
        print(f"Nonce: {account.native.nonce}")
        print(f"Stored {result.records_fetched} transactions in {result.requests_made} requests")
        print(f"Transactions in the last {WINDOW_DAYS} days: {report.transactions}")
        print(f"Fees paid: {report.native[f'fees_{symbol}']} {indexer.profile.native_symbol}")
        print(f"Fee split in wei: {report.native['fees_wei']}")
        print(f"Transaction types: {report.native['transaction_types']}")
        print(f"Most called contracts: {report.native['top_contracts_by_calls'][:3]}")


if __name__ == "__main__":
    chain = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CHAIN
    address = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_ADDRESS
    with tempfile.TemporaryDirectory() as folder:
        summarise(chain, address, Path(folder) / "example.db")
