# Solana

Reads the Solana JSON-RPC API over `aiohttp`, the same way Algorand reads its Indexer, so
both get the same retries, rate limiting and `Retry-After` handling. `solders` validates
public keys. Transactions are requested with `jsonParsed` encoding, so the node decodes
system and SPL token instructions.

## Clusters

| `--network` | Endpoint |
|---|---|
| `devnet` (default) | `https://api.devnet.solana.com` |
| `testnet` | `https://api.testnet.solana.com` |
| `mainnet` | `https://api.mainnet.solana.com` |
| `localnet` | `http://localhost:8899` (`solana-test-validator`) |

To use your own endpoint, set these in `.env`, replacing `DEVNET` as needed:

```text
SOLANA_DEVNET_RPC_SERVER=https://your-rpc.example
SOLANA_DEVNET_COMMITMENT=finalized
```

Commitment is `confirmed` (the default) or `finalized`. The RPC methods used here refuse
`processed`.

Before its first request, the adapter asks the endpoint for `getGenesisHash` and stops with
`WrongNetwork` if the answer belongs to a different cluster than `--network`. A hash that
is not a public cluster's counts as `localnet`, because every local validator creates its
own. So a `devnet` setting that points at mainnet fails, and so does a `localnet` setting
that points at any public cluster. This takes one request per run.

## Rate limits

The [cluster docs](https://solana.com/docs/references/clusters) state 40 requests per 10
seconds for a single RPC method, and warn that limits change without notice. In practice
the public endpoints are stricter. In November 2026 they reported these limits per method,
in their `x-ratelimit-method-limit` header:

| Method | Reported limit |
|---|---|
| `getSlot` | 150 |
| `getAccountInfo` | 50 |
| `getTransaction` | 10 |
| `getSignaturesForAddress` | 10 |
| `getTokenAccountsByOwner` | 10 |
| `getBlock` | 6 |

A sync is mostly `getTransaction`, so that is the one that matters. Measured against it:

| Rate | Requests | HTTP 429 |
|---|---|---|
| 2 per second (devnet) | 30 | 19 |
| 1 per second (devnet) | 30 | 2 |
| 1 every 2 seconds (devnet) | 20 | 0 |
| 1 every 2 seconds (mainnet) | 20 | 0 |

So requests default to 1 every 2 seconds. A 429 is retried after the wait the node asks
for. On a dedicated endpoint, raise the rate with `MCI_REQUESTS_PER_SECOND`.

A sync makes one `getTransaction` for each signature, so a long history takes time on a
public endpoint. A node that no longer holds an old transaction stops the sync with a
clear error; an archival endpoint is needed for that history.

## Commands

```bash
multichain-indexer --chain solana index account <address>
multichain-indexer --chain solana index token <mint>
multichain-indexer --chain solana index program <program-id>
multichain-indexer --chain solana sync <address>
multichain-indexer --chain solana metrics --days 30 --account <address>
multichain-indexer --chain solana export --format csv --output transactions.csv
```

Amounts are held in lamports and shown in SOL. A token amount is in the token's base units.

## Transaction versions

`maxSupportedTransactionVersion` is sent as the integer `1`. It is a ceiling, so legacy
and v0 transactions still come back. All three are read:

| | Compute unit limit | Priority fee |
|---|---|---|
| v1 | `message.transactionConfig.computeUnitLimit` | `transactionConfig.priorityFee`, the total in lamports |
| v0, legacy | Compute Budget instruction | Compute Budget price in micro-lamports per unit, times the limit (capped at 1,400,000), rounded up |

`priority_fee_lamports` is in lamports for every version, so metrics compare correctly. A
legacy or v0 transaction that sets a price but no limit is charged against a default the
node works out from its instructions, so its priority fee is read back from the fee paid.

The base fee is 5000 lamports per signature, and that includes signatures verified by the
Ed25519, secp256k1 and secp256r1 precompile programs, which are not signers. So:

```text
meta.fee = 5000 * (signers + precompile signatures) + priority fee
```

Both counts are recorded on every transaction. This formula was checked on 15,751 mainnet
and over 1,000 devnet transactions, of all three versions. The one exception it first
showed was a limit above the 1,400,000 cap, which is now applied.

## Status

| `status` | Meaning |
|---|---|
| `pending` | Confirmed, not yet finalized. Fetched again on the next sync. |
| `final` | Finalized and succeeded. |
| `failed` | Finalized and failed. The error is in `native.error`. |

`getSignaturesForAddress` has no slot bounds, so a sync applies its slot range to the rows.
The checkpoint stops 150 slots behind the newest slot when the commitment is `confirmed`,
so records still pending are fetched again.

## The shared fields

- `sender` is the fee payer.
- `receiver` and `amount` come from the first transfer in the transaction. A Solana
  transaction can hold any number of transfers; all of them are in `native`.
- `amount` is in lamports, or in a token's base units when `native.amount_mint` is set.
- A mint or program records no creator, so `creator` is empty. Their authorities are in
  `native`.

## The native block

::: multichain_indexer.adapters.solana.models.SolanaTransaction

::: multichain_indexer.adapters.solana.models.SolanaInstruction

::: multichain_indexer.adapters.solana.models.SolTransfer

::: multichain_indexer.adapters.solana.models.SplTokenTransfer

::: multichain_indexer.adapters.solana.models.SolanaAccount

::: multichain_indexer.adapters.solana.models.SplTokenAccount

::: multichain_indexer.adapters.solana.models.SplMint

::: multichain_indexer.adapters.solana.models.SolanaProgram

## Metrics

`metrics` adds these to the shared figures, in its `native` block:

| Field | Meaning |
|---|---|
| `fees_lamports` | Fees paid |
| `transaction_versions` | Transactions per version |
| `compute_units` | Total and mean compute units consumed |
| `priority_fees_lamports` | Total, mean, median, maximum, and how many paid one |
| `program_calls` | The ten most called programs, inner instructions included |
| `top_spl_tokens_by_transfers` | The ten most transferred mints |

## Example

`examples/solana/` has a script and a notebook.
