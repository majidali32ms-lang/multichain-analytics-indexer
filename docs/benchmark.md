# Benchmark

`scripts/benchmark.py` syncs one small public account per chain into an empty database, so
each run reads the account's whole history, one chain at a time, at each source's default rate.
Requests include retries.

```bash
python scripts/benchmark.py --runs 3 --output results.md
```

Measured 2026-11-15 15:24 UTC from one home connection, 3 runs per account, median kept:

| Chain | Network | Records | Requests | Seconds | Records per second | Requests per record |
|---|---|---:|---:|---:|---:|---:|
| algorand | testnet | 108 | 3 | 1.2 | 89.8 | 0.03 |
| solana | devnet | 6 | 9 | 17.9 | 0.3 | 1.50 |
| near | testnet | 20 | 7 | 9.8 | 2.0 | 0.35 |
| ethereum | testnet | 20 | 65 | 14.8 | 1.4 | 3.25 |
| base | testnet | 7 | 145 | 65.8 | 0.1 | 20.71 |
| bitcoin | testnet4 | 4 | 5 | 4.7 | 0.9 | 1.25 |
| bitcoin | testnet4 | 1689 | 82 | 125.5 | 13.5 | 0.05 |

What sets the pace is the public endpoints' limits, not this code:

- Algorand's Indexer returns up to 1,000 transactions a page, at 10 requests a second.
- Solana's public RPC allows about one request every two seconds, and each transaction is
  read on its own.
- NEAR reads history from FastNEAR at one request every two seconds.
- Ethereum lists history from Blockscout, then reads each transaction and its receipt from the
  RPC, so requests grow with the records.
- Base lists history from Alchemy's transfers API in windows of a million blocks, with one
  request for what the account sent and one for what it received in each window, then reads
  each transaction and receipt; a quiet account on Base Sepolia's long chain costs requests in
  every window, which is why 7 records took 145.
- Bitcoin reads 25 transactions a page from mempool.space at one request a second. The larger
  account is a busy testnet4 miner; mempool.space refused or dropped some requests, which were
  retried (26 retries over its three runs).

A dedicated endpoint with a higher `MCI_REQUESTS_PER_SECOND` changes these numbers far more
than anything in the code does.
