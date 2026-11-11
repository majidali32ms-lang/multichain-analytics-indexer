# EVM chains

Ethereum, Arbitrum, OP Mainnet, Base, Polygon PoS and BNB Smart Chain share one adapter, with
a profile per chain in `multichain_indexer/adapters/evm/profiles.py`. A new EVM chain is one
more profile. Requests go through the project's own HTTP client, with web3.py's helpers for
addresses, Keccak-256 and ABI decoding.

## Networks

| `--chain` | `--network testnet` (default) | `--network mainnet` | Gas token | Tokens |
|---|---|---|---|---|
| `ethereum` | Sepolia, chain ID 11155111 | chain ID 1 | ETH | ERC-20 |
| `arbitrum` | Arbitrum Sepolia, 421614 | Arbitrum One, 42161 | ETH | ERC-20 |
| `optimism` | OP Sepolia, 11155420 | OP Mainnet, 10 | ETH | ERC-20 |
| `base` | Base Sepolia, 84532 | Base Mainnet, 8453 | ETH | ERC-20 |
| `polygon` | Amoy, 80002 | Polygon PoS mainnet, 137 | POL | ERC-20 |
| `bnb` | BNB Smart Chain testnet, 97 | BNB Smart Chain mainnet, 56 | BNB | BEP-20 |

`--network localnet` reads a local node at `http://localhost:8545`, Anvil's default.

The default RPC endpoints are the ones each chain's docs list. Ethereum's docs list none, so
its mainnet default is publicnode's and its Sepolia default is Tenderly's public gateway:
publicnode's Sepolia endpoint returned no receipts for two-day-old transactions on
2026-11-11. An endpoint that drops old receipts cannot serve history, because every record
is built from its receipt. Set your own with `<CHAIN>_<NETWORK>_RPC_SERVER`:

```text
BASE_TESTNET_RPC_SERVER=https://your-base-sepolia-rpc.example
```

Before its first request, the adapter asks the endpoint for `eth_chainId` and stops if it
belongs to another network:

```text
Error: the RPC endpoint serves BNB Smart Chain testnet (chain ID 97), not Base Sepolia (chain ID 84532). Check BASE_TESTNET_RPC_SERVER.
```

`localnet` accepts any chain ID that no public network in the table uses. An Anvil fork keeps
the forked chain's ID unless you start it with `--chain-id`.

## Addresses

Addresses are returned checksummed (EIP-55). An all-lowercase or all-uppercase address has no
checksum to check; a mixed-case one with a wrong checksum is refused before any request,
since it usually means a typo.

## Transactions

Transactions, receipts and blocks come from the chain's own RPC, so each chain's receipt
fields are read as the chain reports them:

| Field in `native` | From | Chains |
|---|---|---|
| `kind` | the transaction type: `legacy`, `eip-2930`, `eip-1559`, `eip-4844`, `eip-7702`, `deposit` (OP Stack, type 126), or `type N` | all |
| `base_fee_per_gas`, `priority_fee_per_gas`, `effective_gas_price`, `max_fee_per_gas`, `max_priority_fee_per_gas` | block and receipt | all |
| `method_selector` | the first 4 bytes of the call data | all |
| `token_transfers` | ERC-20 and ERC-721 `Transfer` events in the logs | all |
| `blob` | `blobGasUsed`, `blobGasPrice` | Ethereum and BNB Smart Chain type 3 |
| `l1_data` | `l1Fee`, `l1GasUsed`, `l1GasPrice`, `l1BlobBaseFee` and scalars | OP Mainnet, Base |
| `arbitrum` | `gasUsedForL1`, `l1BlockNumber`, `timeboosted` | Arbitrum |
| `history_source` | the service that listed the transaction | all |

- `fee` is `gas_used * effective_gas_price` (`native.execution_fee`), plus the blob fee and
  the L1 data fee where the chain charges them. On the saved Base and OP Sepolia
  transactions it equals Blockscout's fee to the wei. For a blob transaction Blockscout's
  fee is the execution fee only; ours also counts the blob gas.
- OP Stack receipts report a `blobGasUsed` on every transaction, with no blob gas price: the
  data-availability footprint, not a blob fee. Only a receipt with both pays a blob fee.
- On Arbitrum the L1 part is already inside `gasUsed`, so it is not added again.
- `status` is `failed` when the receipt says so; otherwise `final` up to the endpoint's
  `finalized` block and `pending` after it. A sync never moves its checkpoint past the
  finalized block, so pending records are read again.
- A receipt whose block hash is not the chain's block at that height came from a block a
  reorg replaced. It is not stored; the next sync finds the transaction where it landed.

## Account history

Standard JSON-RPC cannot list an address's transactions, so a history service lists their
hashes. `<CHAIN>_<NETWORK>_HISTORY_SOURCE` chooses it:

| Source | Lists | Key | Default on |
|---|---|---|---|
| `blockscout` | Blockscout's Etherscan-compatible `txlist` | `BLOCKSCOUT_API_KEY` | Ethereum, Arbitrum, OP Mainnet and their testnets |
| `etherscan` | Etherscan's `txlist` | `ETHERSCAN_API_KEY` | Polygon PoS, Amoy |
| `nodereal` | NodeReal's `nr_getAssetTransfers`, external transfers | `NODEREAL_API_KEY` | BNB Smart Chain mainnet |
| `rpc-scan` | every block over the RPC | none | `localnet` |

Base and BNB Smart Chain testnet have no free default. On BNB Smart Chain testnet a
transaction can still be read by hash from Python, with `fetch_transaction`.

The free plans, their limits and their terms are in the
[README](https://github.com/majidali32ms-lang/multichain-analytics-indexer#account-history).
`index account` prints "Powered by Etherscan.io APIs" under transactions Etherscan listed.

Rates were measured on 2026-11-11 through this project's client, one test at a time:

| Endpoint | Rate sent | Refused |
|---|---|---|
| Blockscout `txlist` | 4 and 5 a second | 0 of 30 each |
| Blockscout `txlist` | 8 a second | 1 of 30 |
| Etherscan `txlist` | 2 a second | 0 of 30 |
| Etherscan `txlist` | 3 and 5 a second | 3 and 6 of 30 |
| NodeReal `nr_getAssetTransfers` | 0.5 and 1 a second | 0 of 20 each |
| the twelve default RPC endpoints | 5 a second | 0 of 30 each |
| Sepolia, Arbitrum Sepolia, Base Sepolia RPC | 10 a second | 1, 3 and 1 of 30 |

The defaults are Blockscout 4, Etherscan 2, NodeReal 0.5 and RPC 5 requests a second.
Polygon's documented Amoy endpoint, dRPC, timed out on 2 and 3 of 30 requests in the first
test and failed 29 of 30 later the same day, so Amoy's default is Tenderly's gateway, which
Polygon's docs list as a provider: 0 of 30 failed.

History holds the transactions an address sent or received. A token transfer to the address
inside someone else's contract call is not included yet.

## Metrics

`metrics` adds these to the shared figures, in its `native` block:

| Field | Meaning |
|---|---|
| `gas_used` | gas used by the stored transactions |
| `fees_<token>` | fees in the gas token, for example `fees_eth` or `fees_bnb` |
| `fees_wei` | `total`, `execution`, `l1_data` and `blob` fees in wei |
| `arbitrum_gas_used_for_l1` | on Arbitrum, the L1 part of the gas and its share |
| `transaction_types` | count per `kind` |
| `top_erc20_tokens_by_transfers` | `top_bep20_...` on BNB Smart Chain |
| `top_contracts_by_calls` | receivers of transactions with call data |
| `post_quantum` | nothing is live on any EVM chain; see [Post-quantum readiness](../post-quantum.md) |

## Limits

- `localnet` was tested against a local fake server, not a real Anvil node.
- A record that a reorg dropped for good stays `pending`; marking it `orphaned` needs the
  sync pipeline to look stored records up again, a change to `core/` not made yet.
- History services are third parties with their own limits, which change: Blockscout's free
  plan dropped Base and Polygon on 1 October 2026.
