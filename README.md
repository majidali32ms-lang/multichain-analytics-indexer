# multichain-analytics-indexer

[![CI](https://github.com/majidali32ms-lang/multichain-analytics-indexer/actions/workflows/ci.yml/badge.svg)](https://github.com/majidali32ms-lang/multichain-analytics-indexer/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

A Python library and command line for indexing and analysing blockchain accounts and
transactions, in each chain's own terms. It keeps what it reads in a local database,
syncs incrementally, and produces metrics and exports for analysis.

This version supports **Algorand** and **Solana**. See the [roadmap](#roadmap) for what
comes next.

## Install

```bash
pip install .
```

Python 3.11 or newer. The package is not on PyPI yet, so install it from a checkout of
this repository.

## Quick start

From Python:

```python
from multichain_indexer import Indexer

with Indexer("algorand") as indexer:  # testnet by default
    address = "JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM"
    print(indexer.account(address).balance)  # microAlgos
    print(indexer.sync(address).records_fetched)
    print(indexer.metrics(days=30, account=address).transactions)
```

`AsyncIndexer` has the same methods as coroutines.

From the command line:

```bash
multichain-indexer index account JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM
```

```text
Address        JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM
Balance        0.482 ALGO
Min balance    0.3 ALGO
Participation  Offline
Rekeyed to     not rekeyed
ASAs held      2
Apps opted in  0
ASAs created   0
Apps created   0
As of round    68936216

2026-11-03 10:52  5KIYIOY7WTMLMIWC6BVGWBUTQCMXKQDHC7H6Y3CY4CG5ZU2ILBDA  transfer  1000 of ASA 106414664  0.001 ALGO
```

## Algorand

Reads the official [Algorand Indexer REST API](https://dev.algorand.co/reference/rest-api/indexer/)
over `aiohttp`, which gives async requests and the HTTP status needed to recognise rate
limits. `py-algorand-sdk` (`algosdk`) handles address validation and encoding, before
any request is sent.

| Option | Meaning |
|---|---|
| `--network testnet` | The default. Uses Nodely's free public endpoint. |
| `--network mainnet` | Must be asked for explicitly. |
| `--network localnet` | AlgoKit LocalNet, at `http://localhost:8980`. |

Set `ALGORAND_<NETWORK>_INDEXER_SERVER` and `ALGORAND_<NETWORK>_INDEXER_TOKEN` in `.env`
to use your own endpoint. See [`.env.example`](.env.example).

### Commands

```bash
multichain-indexer index account <address>
```

```bash
multichain-indexer index asset <asa-id>
```

```bash
multichain-indexer index app <app-id>
```

```bash
multichain-indexer sync <address>
```

```bash
multichain-indexer metrics --days 30
```

```bash
multichain-indexer export --format parquet --output transactions.parquet
```

`sync` stores an account's history, then only fetches rounds it has not seen:

```text
Synced JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM on algorand testnet: 108 transactions up to height 68936400, 3 requests in 1.3s
Synced JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM on algorand testnet: 0 transactions up to height 68936403, 2 requests in 1.0s
```

`metrics` works on what has been stored:

```json
{
  "chain": "algorand",
  "network": "testnet",
  "days": 30,
  "account": null,
  "transactions": 24,
  "transactions_per_day": {
    "2026-10-08": 13,
    "2026-10-09": 4,
    "2026-10-12": 4,
    "2026-11-03": 3
  },
  "active_accounts": 3,
  "total_fees": 24000,
  "failure_rate": 0.0,
  "native": {
    "fees_microalgos": 24000,
    "app_calls": 0,
    "asa_opt_ins": 0,
    "group_transaction_share": 0.0,
    "inner_transactions": 0,
    "top_asas_by_transfers": [
      {
        "asa_id": 106414664,
        "transfers": 24
      }
    ]
  }
}
```

### What is recorded

Every transaction has shared fields (`id`, `time`, `sender`, `receiver`, `amount`, `fee`,
`status`, `chain`, `network`) and a `native` block with Algorand's own concepts:

- **ASAs:** create, reconfigure, destroy, opt-in, opt-out, transfer, clawback, freeze and
  unfreeze.
- **Applications:** app ID, on-completion, and logs decoded as ARC-4 return values, text
  or raw bytes.
- **Inner transactions:** kept as a tree under their parent.
- **Atomic groups:** the group ID.
- **Rekeying:** `rekey_to` on the transaction, and `auth_addr` on transactions and
  accounts.
- **Accounts:** minimum balance, participation status, ASA holdings.

A few things worth knowing when reading the numbers:

- `amount` is in microAlgos for a payment and in the ASA's base units for an asset
  transfer.
- `fee` is the transaction's fee plus those of its inner transactions.
- Active accounts counts distinct senders.
- Every record is `final`: Algorand has instant finality, and a transaction that fails
  never reaches a block.

## Solana

Reads the Solana JSON-RPC API over `aiohttp`. `solders` validates public keys, before any
request is sent. A mint or program records no creator on-chain, so `creator` is empty and
the mint and upgrade authorities are in `native`.

| Option | Meaning |
|---|---|
| `--network devnet` | The default. |
| `--network testnet` | The public testnet endpoint. |
| `--network mainnet` | Must be asked for explicitly. |
| `--network localnet` | `solana-test-validator`, at `http://localhost:8899`. |

Set `SOLANA_<NETWORK>_RPC_SERVER` in `.env` to use your own endpoint, and
`SOLANA_<NETWORK>_COMMITMENT` to choose `confirmed` (the default) or `finalized`.

A sync makes one `getTransaction` per signature, and public endpoints limit that method
tightly: at 1 request per second they still answered HTTP 429, and at 1 every 2 seconds
they did not, so that is the default. Raise it with `MCI_REQUESTS_PER_SECOND` on a
dedicated endpoint. If a public endpoint keeps answering 429, the error says so and
suggests one. The [Solana page](https://majidali32ms-lang.github.io/multichain-analytics-indexer/chains/solana/)
has the measurements.

### Commands

```bash
multichain-indexer --chain solana index account <address>
```

```bash
multichain-indexer --chain solana index token <mint>
```

```bash
multichain-indexer --chain solana index program <program-id>
```

```bash
multichain-indexer --chain solana sync <address>
```

```bash
multichain-indexer --chain solana metrics --days 30
```

```bash
multichain-indexer --chain solana export --format csv --output transactions.csv
```

```text
Synced FwcsNmTLbf8VVqxuBMjEDTh39gXR87n3vAnAwvAiPtRy on solana devnet: 6 transactions up to height 519016039, 8 requests in 15.2s
Synced FwcsNmTLbf8VVqxuBMjEDTh39gXR87n3vAnAwvAiPtRy on solana devnet: 0 transactions up to height 519016125, 2 requests in 2.8s
```

### Transaction versions

Legacy, v0 and v1 transactions are all read. Priority fees are in lamports for every
version, so metrics compare correctly:

| | Compute unit limit | Priority fee |
|---|---|---|
| v1 | `transactionConfig.computeUnitLimit` | `transactionConfig.priorityFee`, the total |
| v0, legacy | Compute Budget instruction | Compute Budget price per unit, times the limit |

### What is recorded

The `native` block holds the slot, transaction version, compute units, priority fee,
address lookup tables, every instruction including inner ones, and SOL, SPL Token and
Token-2022 transfers. Accounts include their SPL token accounts.

- `amount` is the first transfer in the transaction: in lamports, or in a token's base
  units when `native.amount_mint` is set. All transfers are in `native`.
- `sender` is the fee payer.
- `status` is `pending` until finalized, then `final` or `failed`. Pending records are
  fetched again on the next sync.

## How it is built

```text
multichain_indexer/
  api.py            Indexer and AsyncIndexer, the public API
  cli.py            the command line, which only calls api.py
  core/             shared code that knows nothing about a specific chain
    base_indexer.py   the contract every adapter implements
    registry.py       chain name to adapter
    pipeline.py       fetch, store, summarise; incremental sync
    storage.py        SQLite behind a storage interface
    metrics.py        pandas metrics any chain can use
    export.py         CSV, JSON and Parquet
    http.py           timeouts, retries, Retry-After
    ratelimit.py      per-endpoint request pacing
    cache.py          optional cache for data that can never change
    config.py         settings from .env and the environment
    observability.py  structured logs with secrets masked
  adapters/algorand/  client, parser, metrics
  adapters/solana/    client, parser, metrics
```

A sync can be stopped at any point and picks up where it left off. It walks a fixed
range of rounds, and each page's records and the cursor for the next page are saved in
one database transaction, so the two can never disagree. Writes are upserts, so nothing
is stored twice, and the checkpoint only moves once the whole range is stored. While a
sync runs it shows a running count, because the total is not known in advance.

## Security

- This version reads public data only. It does not handle private keys and cannot sign or
  send anything.
- Endpoint URLs and tokens come from `.env` or the environment. They are masked in logs
  and left out of error messages.
- `.env` is parsed, not loaded, so its values are never copied into the process
  environment.
- Plain `http` endpoints are refused, except on localhost.
- Addresses and IDs are validated before any request is sent.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Development

```bash
uv sync
```

```bash
uv run pytest
```

Tests use saved testnet responses and need no network. `uv run pytest -m live --no-cov`
runs the ones that talk to testnet. [CONTRIBUTING.md](CONTRIBUTING.md) has the rest.

## Roadmap

| Version | Adds |
|---|---|
| 0.1 | Core and Algorand |
| 0.2 | Solana: SPL tokens, programs, compute units, priority fees |
| 0.3 | NEAR: named accounts, receipts, function calls, access keys |
| 0.4 | EVM chains: Ethereum, Arbitrum, Optimism, Base, Polygon PoS, BNB Smart Chain |
| 0.5 | Bitcoin: UTXOs, address types, fee rates |
| 0.6 | Cross-chain metrics and reports, PostgreSQL, a read-only HTTP API, follow mode, plugins, Docker |
| 0.7 | Wallet utilities: pre-flight checks, simulation, signing on test networks first |
| 1.0 | Stable release on PyPI, with a docs page and demo per chain |

## License

[Apache-2.0](LICENSE)
