# Changelog

## Unreleased

- Solana: checks the endpoint's `getGenesisHash` before the first request and raises
  `WrongNetwork` if it serves a different cluster than `--network`. Each run makes one more
  request, which takes about 2 seconds more at the default rate.

## 0.2.1

The first release on PyPI.

- Fixes the command line when its output pipe closes early, as in `multichain-indexer metrics
  | head -1`. It used to print an error from Python and exit with code 120 on Windows. It
  now exits quietly with code 1 on Windows and Linux.

## 0.2.0

Published to TestPyPI only.

Adds Solana.

- Look up accounts with their SPL token accounts, SPL Token and Token-2022 mints, and
  programs with their upgrade authority.
- Sync an account's history with `getSignaturesForAddress` and `getTransaction`. An
  interrupted sync resumes from its signature cursor. Records stay pending until finalized.
- Reads legacy, v0 and v1 transactions. Priority fees are in lamports for every version,
  from `transactionConfig` on v1 and from Compute Budget instructions on legacy and v0.
- Metrics: fees in lamports, compute units, priority fees, program calls, top SPL tokens,
  and a count per transaction version.
- Devnet by default; mainnet, testnet and localnet with `--network`. Commitment is
  `confirmed` or `finalized`.

Changes to existing behaviour:

- `AssetRecord.creator` and `ContractRecord.creator` are now optional, because a Solana
  mint or program records no creator.
- The Solana request rate defaults to 1 every 2 seconds, the fastest measured rate that
  drew no HTTP 429 from the public endpoints. `MCI_REQUESTS_PER_SECOND` overrides it.

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
