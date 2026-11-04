# Changelog

## 0.1.0

First release, with Algorand support.

- Python API (`Indexer`, `AsyncIndexer`) and a command line built on it.
- Look up accounts, ASAs and applications on the Algorand Indexer.
- Incremental `sync` with a checkpoint per account; re-running adds no duplicates, and
  an interrupted sync resumes from where it stopped with no gaps.
- Native support for ASA actions, app calls, inner transactions, atomic groups, rekeyed
  accounts, minimum balance and participation status.
- Metrics over stored transactions, and export to CSV, JSON and Parquet.
- Mainnet, testnet and AlgoKit LocalNet; testnet by default.
- SQLite storage, per-endpoint rate limiting, retries with backoff, an optional cache
  for data that can never change, and structured logs with secrets masked.
