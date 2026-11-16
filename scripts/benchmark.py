"""Measure a first full sync of one small public account per chain, one chain at a time.

Each run starts from an empty database, so it reads the account's whole history. The result
is records per second and requests per record at each source's default rate; on public
endpoints the rate limit, not this code, sets the pace. Keys come from .env as usual.

    python scripts/benchmark.py
    python scripts/benchmark.py --output docs/benchmark-results.md --runs 3
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from multichain_indexer import Indexer, IndexerError
from multichain_indexer.core.config import Settings

# Small public accounts the live tests also use, so every run reads the same history.
ACCOUNTS = (
    ("algorand", "testnet", "JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM"),
    ("solana", "devnet", "FwcsNmTLbf8VVqxuBMjEDTh39gXR87n3vAnAwvAiPtRy"),
    ("near", "testnet", "f781edbfe07287152e3912cb698af4155234df3c2d9c23bf625f675b8f6de5fa"),
    ("ethereum", "testnet", "0xAA37651031A4b641962550415dB2C43E8d962232"),
    ("base", "testnet", "0xc9aefa97e362db77b379d1097af6fc047bc9a49e"),
    ("bitcoin", "testnet4", "mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu"),
    ("bitcoin", "testnet4", "tb1qlj64u6fqutr0xue85kl55fx0gt4m4urun25p7q"),
)
HEADER = (
    "| Chain | Network | Records | Requests | Seconds | Records per second "
    "| Requests per record |\n|---|---|---:|---:|---:|---:|---:|"
)


@dataclass(frozen=True)
class Measurement:
    chain: str
    network: str
    records: int
    requests: int
    seconds: float

    def row(self) -> str:
        per_second = self.records / self.seconds if self.seconds else 0.0
        per_record = self.requests / self.records if self.records else 0.0
        return (
            f"| {self.chain} | {self.network} | {self.records} | {self.requests} "
            f"| {self.seconds:.1f} | {per_second:.1f} | {per_record:.2f} |"
        )


def measure(chain: str, network: str, account: str, settings: Settings) -> Measurement:
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / "benchmark.db"
        with Indexer(chain, network, settings=settings, database=database) as indexer:
            result = indexer.sync(account)
    return Measurement(
        chain, network, result.records_fetched, result.requests_made, result.elapsed_seconds
    )


def median_run(
    chain: str, network: str, account: str, settings: Settings, runs: int
) -> Measurement:
    measured = [measure(chain, network, account, settings) for _ in range(runs)]
    return sorted(measured, key=lambda item: item.seconds)[len(measured) // 2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, help="Markdown file for the results table.")
    parser.add_argument("--runs", type=int, default=1, help="Runs per account; the median is kept.")
    options = parser.parse_args()
    settings = Settings.from_env()
    rows: list[str] = []
    for chain, network, account in ACCOUNTS:
        try:
            measured = median_run(chain, network, account, settings, options.runs)
        except IndexerError as exc:
            print(f"{chain} {network}: {exc}", file=sys.stderr)
            continue
        rows.append(measured.row())
        print(rows[-1], flush=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    table = "\n".join([HEADER, *rows])
    if options.output is not None:
        options.output.write_text(
            f"Measured {stamp}, {options.runs} run(s) per account, median kept.\n\n{table}\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
