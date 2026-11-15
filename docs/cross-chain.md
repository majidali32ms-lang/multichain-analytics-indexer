# Cross-chain

Every chain keeps its own terms, but they all land in one database with the same shared
fields: id, time, sender, receiver, amount, fee, status, chain and network. That is what lets
one command look at all of them at once.

## Metrics side by side

`--chain all` reads every chain and network that has stored transactions:

```bash
multichain-indexer --chain all metrics --days 365
```

```text
Chain             Transactions  Per day  Active                      Fees  Fees (USD)  Failed
algorand testnet           108      0.3       6                0.108 ALGO         n/a    0.0%
bitcoin testnet4             4     0.01       4            0.00001472 BTC         n/a    0.0%
ethereum testnet            19     0.05       1  0.026567220971150023 ETH         n/a    0.0%
near testnet                20     0.05       2   0.0221832130514401 NEAR         n/a   25.0%
solana devnet                6     0.02       2               0.00003 SOL         n/a    0.0%
Last 365 days. US dollars only for mainnet, at the price in config.toml.
```

This came from a database that version 0.6.0 wrote while syncing one small account on each
test network. `--json` prints the same rows as JSON, with each chain's transactions per day.

A chain that cannot be read, for example because the plugin that added it is no longer
installed, shows its error on its own row while the others are still reported.

## Fees in US dollars

Fees are always shown in each chain's own unit. A US dollar figure appears only for mainnet
records, and only when you give a price, with its date, in `config.toml`:

```toml
[prices.bitcoin]
usd = 98000.5
date = 2026-11-15
```

Nothing is fetched from a price service, so the same database and the same file always give
the same report. Test network tokens have no market price, so test networks show `n/a`.

## The report

```bash
multichain-indexer --chain all report --days 365 --output report.html
```

writes one HTML file with a summary table and charts of transactions, active accounts, fees
in US dollars when priced, and transactions per day for each chain. It has no scripts and
loads nothing, so it opens offline and can be attached to an email. `--format csv` and
`--format json` write the same rows as data.
[An example report](examples/cross-chain-report.html) was made from the database above.

## One export for every chain

```bash
multichain-indexer --chain all export --format parquet --output all.parquet
```

writes every chain's transactions into one table: the shared fields as columns, amounts and
fees as text so values past 64 bits stay exact, and each chain's `native` block as a JSON
column. It loads into one pandas DataFrame:

```python
import pandas as pd

frame = pd.read_parquet("all.parquet")
frame.groupby(["chain", "network"]).size()
```

An export never leaves out a chain silently: if one cannot be read, the export stops and
names it.

## Syncing many accounts

List the accounts in `config.toml`, by chain and network:

```toml
[accounts.bitcoin]
testnet4 = ["mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu"]

[accounts.algorand]
testnet = ["JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM"]
```

```bash
multichain-indexer --chain all sync
```

Chains are synced in parallel and the accounts of one chain one after another. Mainnet
accounts are synced only with `--network mainnet`. One failing account never stops the others;
each is reported, and the command exits with 1 if any failed.

Free API plans limit a key, not a chain: Blockscout serves Ethereum, Arbitrum and OP Mainnet
on one key. So every chain that uses one key, for Blockscout, Etherscan, NodeReal or Alchemy,
shares one rate limiter, and every keyless endpoint is limited per host. With one limiter per
chain, three chains on one Blockscout key at 4 requests a second each drew HTTP 429 on 26 to 47
of 90 requests; with one shared limiter, 0 or 1.

## Following an account

```bash
multichain-indexer --chain bitcoin sync mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu --follow
```

runs the ordinary incremental sync again and again, storing new transactions as they arrive,
until Ctrl-C, which prints a summary. A round that fails on the network is tried again on the
next round. The pause between rounds suits each chain's block time and the cost of asking a
public endpoint for its head; `--interval` changes it.

| Chain | Default pause | Requests to read the head |
|---|---:|---:|
| Algorand | 10 s | 1 |
| EVM chains | 15 s | 2 |
| Solana | 30 s | 1, at 1 request every 2 seconds |
| NEAR | 30 s | 2 |
| Bitcoin | 60 s | 1 |
| Plugin chains | 30 s | |

## config.toml

`config.toml` holds endpoints, default networks, accounts and prices. It is read from the
current folder, or from `--config`. It sits below `.env` and the environment, which win.

```toml
[settings]
requests_per_second = 2

[chains.bitcoin]
network = "signet"

[chains.bitcoin.signet]
esplora_server = "https://my-esplora.example/signet/api"
```

`[chains.<chain>.<network>]` entries become the same settings `.env` takes, here
`BITCOIN_SIGNET_ESPLORA_SERVER`. A chain's `network` becomes its default, but mainnet is
always chosen with `--network mainnet`. The file is meant to be shared, so it never holds
secrets: a key ending in `_API_KEY` or `_TOKEN` is refused with an error that names the key
and never shows its value. Keys, and a database URL with a password, belong in `.env`.

## From Python

```python
from pathlib import Path

from multichain_indexer import CrossChain

with CrossChain() as everything:
    for outcome in everything.sync():
        print(outcome.chain, outcome.account, outcome.error or outcome.result.records_fetched)
    report = everything.metrics(days=30)
    for row in report.rows:
        print(row.chain, row.network, row.transactions, row.total_fees_display)
    everything.export("parquet", Path("all.parquet"))
```

`AsyncCrossChain` has the same methods as coroutines. `Indexer.follow` and
`AsyncIndexer.follow` follow one account.
