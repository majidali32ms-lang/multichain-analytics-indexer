# multichain-analytics-indexer

[![CI](https://github.com/majidali32ms-lang/multichain-analytics-indexer/actions/workflows/ci.yml/badge.svg)](https://github.com/majidali32ms-lang/multichain-analytics-indexer/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)
[![PyPI](https://img.shields.io/pypi/v/multichain-analytics-indexer)](https://pypi.org/project/multichain-analytics-indexer/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

A Python library and command line for indexing and analysing blockchain accounts and
transactions, in each chain's own terms. It keeps what it reads in a local database,
syncs incrementally, and produces metrics and exports for analysis.

This version supports **Algorand**, **Solana** and **NEAR**. See the [roadmap](#roadmap) for
what comes next.

## Install

```bash
pip install multichain-analytics-indexer
```

Python 3.11 or newer.

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

## NEAR

NEAR's RPC cannot list an account's transactions, so two sources are used:

| Data | Source | Default |
|---|---|---|
| Account state, access keys, chain head | NEAR's standard JSON-RPC (`view_account`, `view_access_key_list`) | dRPC's public endpoints |
| Account history, with receipts and outcomes | An indexing service, chosen by configuration | The FastNEAR Transactions API |

No API key is needed to start. Read
[what this history can and cannot promise](https://majidali32ms-lang.github.io/multichain-analytics-indexer/chains/near/#limits)
before relying on it.

| Option | Meaning |
|---|---|
| `--network testnet` | The default. |
| `--network mainnet` | Must be asked for explicitly. |
| `--network localnet` | A NEAR sandbox at `http://localhost:3030`. Account and contract lookups only, unless you set a history server. |

Set `NEAR_<NETWORK>_RPC_SERVER`, `NEAR_<NETWORK>_HISTORY_SERVER`, and
`NEAR_<NETWORK>_HISTORY_API_KEY` in `.env`. `NEAR_<NETWORK>_HISTORY_SOURCE` chooses the
service; adding another needs one new class, with no change to the parser or to `core/`.

A history service links many more transactions to an account than an explorer shows. By
default a sync keeps the ones the account signed or received, which is the set NearBlocks
lists for it. `NEAR_<NETWORK>_HISTORY_SCOPE=all` also keeps those where the account only
received a receipt or is named in an action's arguments or an event log. Metrics count
what the syncs stored, so they follow this setting; sync into a new database after
changing it.

An RPC endpoint on the wrong network stops with `WrongNetwork`. The history server's
network cannot be checked, so set `NEAR_<NETWORK>_HISTORY_SERVER` with care.

### Commands

```bash
multichain-indexer --chain near index account <account-id>
```

```bash
multichain-indexer --chain near index contract <account-id>
```

```bash
multichain-indexer --chain near sync <account-id>
```

```bash
multichain-indexer --chain near metrics --days 30
```

```bash
multichain-indexer --chain near export --format csv --output transactions.csv
```

```text
Account ID    f781edbfe07287152e3912cb698af4155234df3c2d9c23bf625f675b8f6de5fa
Kind          near-implicit
Exists        yes
Balance       0.732016766023559899999994 NEAR
Staked        0 NEAR
Storage used  182 bytes
Contract      no
Access keys   1 (0 function-call)
Shard         9
As of block   276744703

2026-09-04 07:40  GEZx7KDqDdLbyViv4BvHhKmUSCBmTekLvmM2Q22bkidH  Batch  failed  0.000000000000000000000002 NEAR  0.0004703005168722 NEAR  19.753 Tgas  3
2026-09-04 07:36  HUnm1ZN6wKv3hXgsy3xnTkVwCFiAeoCJ9GH2KjHyaubS  FunctionCall  final  0.000000000000000000000001 NEAR  0.0002665409479373 NEAR  10.190 Tgas  2
```

```text
Synced f781edbfe07287152e3912cb698af4155234df3c2d9c23bf625f675b8f6de5fa on near testnet: 20 transactions up to height 276744722, 7 requests in 9.4s
Synced f781edbfe07287152e3912cb698af4155234df3c2d9c23bf625f675b8f6de5fa on near testnet: 0 transactions up to height 276744748, 4 requests in 3.5s
```

### What is recorded

The `native` block holds the block height, every action with its method, decoded arguments,
gas and attached deposit, and every receipt with its outcome, logs, gas burnt and tokens
burnt. Accounts include their kind, access keys with their permissions, and their shard.

- `amount` is the yoctoNEAR attached by the transaction's actions, shown in NEAR.
- `fee` is the tokens burnt by the transaction and all its receipts, in yoctoNEAR.
- `sender` is the signer. Gas is held in gas units and shown in Tgas.
- A failed receipt does not always fail its transaction, so both are recorded. `status`
  follows the transaction's first receipt, as the protocol does.
- `status` is `pending` until the receipts that decide it have run. Transactions in the last
  300 blocks are fetched again on the next sync.

## Post-quantum signatures

Algorand transactions signed by native Falcon-1024 accounts (`pqsig`, scheme `f1`) and NEAR
keys and transactions using ML-DSA-65 carry a `pq` field in their `native` block, and metrics
report their share. Nothing else is live on any planned chain yet.

```bash
multichain-indexer pq-status
```

Each row of its output has the chain's status, its source and the date it was checked. The
[post-quantum page](https://majidali32ms-lang.github.io/multichain-analytics-indexer/post-quantum/)
has the full table, what is counted, and its limits.

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
  adapters/near/      client, parser, metrics, and pluggable history sources
  pq/                 post-quantum detection shared by the adapters, and each chain's status
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
| 0.4 | Post-quantum readiness: Falcon-1024 on Algorand, ML-DSA-65 on NEAR, a dated status for every chain |
| 0.5 | EVM chains: Ethereum, Arbitrum, Optimism, Base, Polygon PoS, BNB Smart Chain |
| 0.6 | Bitcoin: UTXOs, address types, fee rates, quantum-exposed outputs |
| 0.7 | Cross-chain metrics and reports, PostgreSQL, a read-only HTTP API, follow mode, plugins, Docker |
| 0.8 | Wallet utilities: pre-flight checks, simulation, signing on test networks first |
| 1.0 | Stable release on PyPI, with a docs page and demo per chain |

## License

[Apache-2.0](LICENSE)
