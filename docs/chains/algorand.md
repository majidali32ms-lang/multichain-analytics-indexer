# Algorand

Reads the official Algorand Indexer REST API over `aiohttp`, which gives async requests
and the HTTP status needed to recognise rate limits. `py-algorand-sdk` (`algosdk`) handles
address validation and encoding.

## Networks

| `--network` | Endpoint |
|---|---|
| `testnet` (default) | `https://testnet-idx.4160.nodely.dev` |
| `mainnet` | `https://mainnet-idx.4160.nodely.dev` |
| `localnet` | `http://localhost:8980` (AlgoKit LocalNet) |

To use your own endpoint, set these in `.env`, replacing `TESTNET` as needed:

```text
ALGORAND_TESTNET_INDEXER_SERVER=https://your-indexer.example
ALGORAND_TESTNET_INDEXER_TOKEN=your-token
```

## Commands

```bash
multichain-indexer index account <address>
multichain-indexer index asset <asa-id>
multichain-indexer index app <app-id>
multichain-indexer sync <address>
multichain-indexer metrics --days 30 --account <address>
multichain-indexer export --format csv --output transactions.csv
```

`sync` can be stopped at any point and resumes from the next page it had not stored.

Amounts are held in microAlgos and shown in Algo. An asset transfer's amount is in the
ASA's base units.

## The native block

::: multichain_indexer.adapters.algorand.models.AlgorandTransaction

::: multichain_indexer.adapters.algorand.models.AlgorandInnerTransaction

::: multichain_indexer.adapters.algorand.models.AlgorandAccount

::: multichain_indexer.adapters.algorand.models.AlgorandAsset

::: multichain_indexer.adapters.algorand.models.AlgorandApplication

## Metrics

`metrics` adds these to the shared figures, in its `native` block:

| Field | Meaning |
|---|---|
| `fees_microalgos` | Fees paid, including inner transactions |
| `app_calls` | Top-level application calls |
| `asa_opt_ins` | ASA opt-ins |
| `group_transaction_share` | Share of transactions in an atomic group |
| `inner_transactions` | Inner transactions at every depth |
| `top_asas_by_transfers` | The ten most transferred ASAs |

## Example

`examples/algorand/` has a script and a notebook.
