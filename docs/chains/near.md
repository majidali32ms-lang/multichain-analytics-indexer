# NEAR

NEAR's RPC cannot list an account's transactions, so two sources are used. Account state,
access keys and the chain head come from NEAR's standard JSON-RPC. History comes from an
indexing service, chosen by configuration.

## Networks

| `--network` | RPC | History |
|---|---|---|
| `testnet` (default) | `https://near-testnet.drpc.org` | `https://tx.test.fastnear.com` |
| `mainnet` | `https://near.drpc.org` | `https://tx.main.fastnear.com` |
| `localnet` | `http://localhost:3030` | none unless you set a server |

```text
NEAR_TESTNET_RPC_SERVER=https://your-rpc.example
NEAR_TESTNET_HISTORY_SOURCE=fastnear
NEAR_TESTNET_HISTORY_SERVER=https://your-history-service.example
NEAR_TESTNET_HISTORY_API_KEY=your-key
NEAR_TESTNET_HISTORY_SCOPE=direct
```

An RPC endpoint whose chain ID belongs to another network raises `WrongNetwork`.

## Which transactions count

A history service links far more transactions to an account than an explorer shows on its
account page. `NEAR_<NETWORK>_HISTORY_SCOPE` chooses which ones a sync keeps:

| Scope | Keeps | Matches |
|---|---|---|
| `direct` (default) | Transactions the account signed or received | NearBlocks' account page |
| `all` | Also those where the account only received a receipt, or appears in an action's arguments or an event log | What 0.3.0 kept before this setting existed |

Metrics, `index account` and exports all follow the scope, because it decides what is
stored. It applies to syncs made after it is set, so sync into a new database (`--db`) after
changing it: records already stored stay as they are.

## Adding a history source

Write a class that meets the `HistorySource` protocol in `adapters/near/history.py` and add
its factory to `HISTORY_SOURCES` in `adapters/near/adapter.py`. The parser and `core/` do not
change.

## Limits

- **No key is a convenience, not a promise.** FastNEAR answers without a key today, but its
  docs ask for one on every request and publish no free limit. Its site lists plans from $69
  a month; it does not say whether they cover this API.
- **Completeness is sampled, not proven.** `scripts/crosscheck_near_history.py` compared 12
  samples with NearBlocks on 2026-11-08, 6 on testnet and 6 on mainnet, reaching back to
  2021. FastNEAR missed none of the 287 transactions NearBlocks listed, and signer, receiver,
  block height, status and fee matched on all of them. In an earlier run on 2026-11-07, one
  status differed: a transaction among the newest blocks that was still `pending` here.
  FastNEAR's docs do not state how far back history goes.
- **FastNEAR lists more than NearBlocks.** In the same block ranges it linked 2,687 further
  transactions to the sampled accounts, 2,547 of them to `root.near`. In all 12 samples, the
  transactions the account signed or received were exactly the ones NearBlocks listed, which
  is why `direct` is the default.
- **Long histories take time.** A page is 2 requests for 20 transactions, so at most 5
  transactions a second at the default rate. `direct` saves no requests: transactions
  outside it are fetched before they are left out.
- **Shards are on accounts only.** FastNEAR's transaction data has no shard. An account's
  shard is worked out from the current layout, so it says nothing about past transactions.
- **The network check covers the RPC only.** Nothing confirms which network a configured
  history server serves.
- **A record can stay pending.** Transactions in the last 300 blocks are fetched again on the
  next sync. One whose receipts take longer than that stays `pending`.
- **Not tested on a real sandbox.** `localnet` was only tested against a local fake server.
- **No token lookups.** A NEP-141 token is a contract, so `index contract` shows it, but its
  metadata is not read.

## Rate limits

None of these services publishes a free limit. Measured on 2026-11-07 without a key and with
no retries, one run at each rate. "Rate reached" is lower than the rate asked when the
service slowed its replies or answered HTTP 429.

FastNEAR on its own, one network at a time, alternating `/v0/account` and
`/v0/transactions`:

| Rate asked | Network | Rate reached | Requests | HTTP 429 | Other failures |
|---|---|---|---|---|---|
| 1 per second | testnet | 1.01 per second | 30 | 0 | none |
| 1 every 2 seconds | testnet | 0.51 per second | 30 | 0 | none |
| 1 every 4 seconds | testnet | 0.26 per second | 20 | 0 | none |
| 1 per second | mainnet | 1.01 per second | 30 | 0 | none |
| 1 every 2 seconds | mainnet | 0.51 per second | 30 | 0 | none |
| 1 every 4 seconds | mainnet | 0.26 per second | 20 | 1 | none |

An earlier run, with both networks and other tests running at the same time. Connections that
stalled on the test machine lowered the rate reached, so this run is less clean, but it is
the only one that tried 2 per second:

| Rate asked | Network | Path | Rate reached | Requests | HTTP 429 | Other failures |
|---|---|---|---|---|---|---|
| 2 per second | testnet | `/v0/account` | 0.83 per second | 30 | 0 | none |
| 2 per second | mainnet | `/v0/account` | 0.52 per second | 30 | 0 | none |
| 2 per second | testnet | `/v0/transactions` | 0.84 per second | 30 | 4 | none |
| 2 per second | mainnet | `/v0/transactions` | 1.99 per second | 30 | 5 | none |
| 1 per second | mainnet | `/v0/account` | 1.00 per second | 30 | 3 | none |
| 1 per second | testnet | `/v0/account` | 0.59 per second | 30 | 3 | none |
| 1 per second | mainnet | `/v0/transactions` | 0.33 per second | 30 | 6 | 1 connection closed |
| 1 per second | testnet | `/v0/transactions` | 0.26 per second | 30 | 0 | 1 connection closed |
| 1 every 2 seconds | mainnet | `/v0/account` | 0.25 per second | 20 | 0 | none |
| 1 every 2 seconds | testnet | `/v0/account` | 0.20 per second | 20 | 0 | 1 connection closed |
| 1 every 2 seconds | mainnet | `/v0/transactions` | 0.17 per second | 20 | 0 | 1 connection closed |
| 1 every 2 seconds | testnet | `/v0/transactions` | 0.25 per second | 20 | 0 | none |

No rate was free of HTTP 429 in every run, so the default of 1 every 2 seconds still relies on
retries.

Public RPC endpoints, alternating `status`, `block`, `view_account`, `view_access_key_list`
and `EXPERIMENTAL_protocol_config`. `near.org` is `rpc.<network>.near.org`, and
`near.org archival` is `archival-rpc.<network>.near.org`, measured in a separate run:

| Endpoint | Network | Rate asked | Rate reached | Requests | HTTP 429 | Other failures |
|---|---|---|---|---|---|---|
| dRPC | mainnet | 1 per second | 1.03 per second | 30 | 0 | none |
| dRPC | mainnet | 1 every 2 seconds | 0.52 per second | 30 | 0 | none |
| dRPC | testnet | 1 per second | 1.03 per second | 30 | 0 | none |
| dRPC | testnet | 1 every 2 seconds | 0.52 per second | 30 | 0 | none |
| FastNEAR | mainnet | 1 per second | 1.03 per second | 30 | 0 | none |
| FastNEAR | mainnet | 1 every 2 seconds | 0.39 per second | 30 | 0 | 1 connection error |
| FastNEAR | testnet | 1 per second | 1.03 per second | 30 | 0 | none |
| FastNEAR | testnet | 1 every 2 seconds | 0.52 per second | 30 | 0 | none |
| Intear | mainnet | 1 per second | 0.11 per second | 30 | 5 | none |
| Intear | mainnet | 1 every 2 seconds | 0.24 per second | 30 | 2 | none |
| Intear | testnet | 1 per second | 0.42 per second | 30 | 0 | none |
| Intear | testnet | 1 every 2 seconds | 0.50 per second | 30 | 0 | none |
| near.org | mainnet | 1 per second | 0.05 per second | 30 | 10 | none |
| near.org | mainnet | 1 every 2 seconds | 0.32 per second | 30 | 0 | none |
| near.org | testnet | 1 per second | 0.27 per second | 30 | 0 | 1 connection error |
| near.org | testnet | 1 every 2 seconds | 0.31 per second | 30 | 0 | none |
| near.org archival | testnet | 2 per second | 0.32 per second | 20 | 0 | none |
| near.org archival | testnet | 1 per second | 0.32 per second | 20 | 0 | none |
| near.org archival | testnet | 1 every 2 seconds | 0.05 per second | 15 | 6 | none |
| near.org archival | mainnet | 2 per second | 0.04 per second | 20 | 9 | none |
| near.org archival | mainnet | 1 per second | 0.03 per second | 20 | 12 | none |
| near.org archival | mainnet | 1 every 2 seconds | 0.04 per second | 15 | 9 | none |

dRPC is the default, at 1 per second: it was the only endpoint with no HTTP 429 and no failed
request on either network. It is a different company from the history service.
`MCI_REQUESTS_PER_SECOND` replaces both defaults.

## Commands

```bash
multichain-indexer --chain near index account <account-id>
multichain-indexer --chain near index contract <account-id>
multichain-indexer --chain near sync <account-id>
multichain-indexer --chain near metrics --days 30 --account <account-id>
multichain-indexer --chain near export --format csv --output transactions.csv
```

## Status and the shared fields

| `status` | Meaning |
|---|---|
| `pending` | A receipt that decides the result has not run yet. |
| `final` | Succeeded. |
| `failed` | Failed. The error is in `native.error`. |

Status starts at the first receipt and follows `SuccessReceiptId` until a value or a failure.
A failure in any other receipt does not fail the transaction. This agreed with FastNEAR's
`is_success` on every saved transaction.

- `sender` is the signer and `receiver` the transaction's receiver.
- `amount` is the yoctoNEAR attached by its actions, delegated ones included.
- `fee` is the tokens burnt by the transaction and all its receipts, in yoctoNEAR.
- Function call arguments that are not plain JSON, or are nested too deeply to store, stay in
  `args_base64`, so a crafted call cannot stop a sync.

## The native block

::: multichain_indexer.adapters.near.models.NearTransaction

::: multichain_indexer.adapters.near.models.NearReceipt

::: multichain_indexer.adapters.near.models.NearAction

::: multichain_indexer.adapters.near.models.NearAccount

::: multichain_indexer.adapters.near.models.NearAccessKey

::: multichain_indexer.adapters.near.models.NearContract

## Example

`examples/near/` has a script and a notebook.
