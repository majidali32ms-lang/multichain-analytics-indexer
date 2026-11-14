# multichain-analytics-indexer

[![CI](https://github.com/majidali32ms-lang/multichain-analytics-indexer/actions/workflows/ci.yml/badge.svg)](https://github.com/majidali32ms-lang/multichain-analytics-indexer/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)
[![PyPI](https://img.shields.io/pypi/v/multichain-analytics-indexer)](https://pypi.org/project/multichain-analytics-indexer/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

A Python library and command line for indexing and analysing blockchain accounts and
transactions, in each chain's own terms. It keeps what it reads in a local database,
syncs incrementally, and produces metrics and exports for analysis.

This version supports **Algorand**, **Solana**, **NEAR**, the **EVM chains** (Ethereum,
Arbitrum, OP Mainnet, Base, Polygon PoS, BNB Smart Chain) and **Bitcoin**. See the
[roadmap](#roadmap) for what comes next.

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

## EVM chains

Ethereum, Arbitrum, OP Mainnet, Base, Polygon PoS and BNB Smart Chain share one adapter, with
a profile per chain. Every lookup first checks the RPC endpoint's `eth_chainId`; an endpoint
of another network stops with `WrongNetwork` and names the setting to fix.

| `--chain` | Testnet (default) | Gas token | Tokens |
|---|---|---|---|
| `ethereum` | Sepolia | ETH | ERC-20 |
| `arbitrum` | Arbitrum Sepolia | ETH | ERC-20 |
| `optimism` | OP Sepolia | ETH | ERC-20 |
| `base` | Base Sepolia | ETH | ERC-20 |
| `polygon` | Amoy | POL | ERC-20 |
| `bnb` | BNB Smart Chain testnet | BNB | BEP-20 |

`--network mainnet` must be asked for explicitly; `--network localnet` reads a local node such
as Anvil at `http://localhost:8545`. Set `<CHAIN>_<NETWORK>_RPC_SERVER` in `.env` to use your
own endpoint. Fees include the L1 data fee on OP Mainnet and Base and the blob fee on blob
transactions; [the EVM page](https://majidali32ms-lang.github.io/multichain-analytics-indexer/chains/evm/)
lists every field.

### Commands

```bash
multichain-indexer --chain ethereum index account <address>
```

```bash
multichain-indexer --chain ethereum index token <contract>
```

```bash
multichain-indexer --chain base index contract <address>
```

```bash
multichain-indexer --chain ethereum sync <address>
```

```bash
multichain-indexer --chain ethereum metrics --days 30
```

```bash
multichain-indexer --chain ethereum export --format csv --output transactions.csv
```

```text
Address      0xAA37651031A4b641962550415dB2C43E8d962232
Kind         externally owned account
Balance      0.000000000001 ETH
Nonce        20
As of block  12113523

2026-11-09 17:11  0xa2a58cf2e34c2ec06ba20beeb01120204124fc117e856569d8c5f9475fcd44ec  legacy  final  0.038050303691589778 ETH  0.00000021 ETH  21000  1 ERC-20
2026-03-01 08:06  0xbef676788756bdd8774837d63ef4b7cb2c350fdf8bc6b9a329400da1497d09bf  eip-1559  final  0 ETH  0.000000541183870018 ETH  498707  1 ERC-20
```

```text
Token         0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14
Standard      ERC-20
Name          Wrapped Ether
Symbol        WETH
Decimals      18
Total supply  193104088539324295320660 base units
As of block   12113524
```

```text
Synced 0xAA37651031A4b641962550415dB2C43E8d962232 on ethereum testnet: 20 transactions up to height 12113525, 65 requests in 13.9s
Synced 0xAA37651031A4b641962550415dB2C43E8d962232 on ethereum testnet: 0 transactions up to height 12113526, 5 requests in 2.9s
```

### Account history

Standard JSON-RPC cannot list an address's transactions, so a history service lists them.
The transactions, receipts and blocks themselves always come from the chain's own RPC, so
fields such as `l1Fee` (OP Mainnet, Base), `gasUsedForL1` (Arbitrum) and blob gas
(EIP-4844) are read as the chain reports them.

| Chain | Mainnet | Testnet |
|---|---|---|
| Ethereum | Blockscout | Blockscout (Sepolia) |
| Arbitrum | Blockscout | Blockscout (Arbitrum Sepolia) |
| OP Mainnet | Blockscout | Blockscout (OP Sepolia) |
| Polygon PoS | Etherscan | Etherscan (Amoy) |
| Base | Alchemy | Alchemy (Base Sepolia) |
| BNB Smart Chain | NodeReal | none: NodeReal serves mainnet only, so transactions are read by hash |
| any chain, `--network localnet` | reads every block over the RPC (Anvil) | |

`<CHAIN>_<NETWORK>_HISTORY_SOURCE` switches the service (`blockscout`, `etherscan`,
`nodereal`, `alchemy` or `rpc-scan`) without a code change. Account and contract lookups need no key; history needs
the chosen service's free key:

| Setting | Where to get it | Free plan, as its provider states it |
|---|---|---|
| `BLOCKSCOUT_API_KEY` | dev.blockscout.com, no card needed | 5 requests a second and 100,000 credits a day (a history page costs 20, so about 5,000 pages); requests fail once the day's credits are used. Base, Polygon and zkSync left the free plan on 1 October 2026. |
| `ETHERSCAN_API_KEY` | etherscan.io | 3 requests a second, 100,000 a day, at most 1,000 records a request since 1 July 2026. Selected chains only. |
| `ALCHEMY_API_KEY` | dashboard.alchemy.com, no card asked on sign-up | 30 million compute units a month and 300 a second (the dashboard shows 25 requests a second). `alchemy_getAssetTransfers` costs 120, so about 250,000 calls a month; this project sends two a second, and 30 calls at 2, 4 and 8 a second drew no refusal. Each call covers at most 1,000,000 blocks, sent and received transactions are separate calls, and internal transfers are not listed on Base. |
| `NODEREAL_API_KEY` | dashboard.nodereal.io, no card asked on sign-up | 10 million compute units a month and 150 a second per key. `nr_getAssetTransfers` costs 250, so about 40,000 calls a month; this project sends one every 2 seconds. Each call covers at most 1,000,000 blocks (the service refuses 2,000,000 or more), and sent and received transactions are separate calls, so a first sync of a long history takes many calls. |

What each free plan answered on 11 November 2026, with one request per network:

| Network | Blockscout | Etherscan |
|---|---|---|
| Ethereum, Sepolia | yes | yes |
| Arbitrum One, Arbitrum Sepolia | yes | yes |
| OP Mainnet | yes | no, paid plan |
| OP Sepolia | yes | yes |
| Base Mainnet | no, HTTP 402 | no, paid plan |
| Base Sepolia | no, HTTP 402 | yes, although Etherscan lists it as paid |
| Polygon PoS mainnet | no, HTTP 402 | yes |
| Amoy | not listed | yes |
| BNB Smart Chain, mainnet and testnet | not listed | no, paid plan |

Free plans change: a source that answers today may need a paid plan later. Each provider's
terms apply to your key, not this project's:

- **Etherscan** ([API terms](https://etherscan.io/apiterms)): the free plan is for personal
  and research use only; anywhere you display or publish data obtained through it, show
  "Powered by Etherscan.io APIs" with a link to etherscan.io (not needed for purely private
  use); do not bulk-download, mirror or archive its data, pass it on to third parties through
  a proxy, or put your key in public code. `index account` prints that line under
  transactions Etherscan listed, and every such record carries
  `native.history_source = "etherscan"`, also in CSV, JSON and Parquet exports.
- **Blockscout** ([terms](https://eaas.blockscout.com/terms-and-conditions)): no
  API-specific clause on attribution or storing results was found. The licence is revocable,
  and access or limits can change without notice.
- **NodeReal** ([terms](https://nodereal.io/terms)): no clause on attribution, caching or
  redistribution was found. The key sits in the endpoint URL; this project keeps it out of
  messages and logs, but treat any URL you copy from your dashboard as a secret.
- **Alchemy** ([terms](https://legal.alchemy.com/), version 2.0 of 27 June 2025), for Base
  history: use is "solely for your internal business purposes" (2.1); you may not resell,
  mirror or let others use the service (2.2(v)), scrape it or use crawlers (2.2(viii), (xi)),
  or spread use across accounts to get past limits (2.2(x)); the free tier is "as is" and can
  end at any time (2.11); on termination, Alchemy's confidential information must be destroyed
  (11.6), which may include data you stored from it. No attribution or caching clause was
  found. Each user's own key, fetching history for that user into a local database, appears
  to fit these terms; read them yourself before relying on that.

A history service is a third party: it can be behind the chain, list a transaction the RPC
does not have yet, or change its limits. A listed transaction the RPC does not have is left
out and fetched on a later sync.

### Which transactions count

`<CHAIN>_<NETWORK>_HISTORY_SCOPE` chooses them:

| Scope | Keeps | Extra cost per history page |
|---|---|---|
| `direct` (default) | transactions the address sent or received | none |
| `all` | also those that moved an ERC-20 (BEP-20) token to or from the address inside someone else's contract call | Blockscout and Etherscan: a second call, `tokentx`, so twice the calls (Blockscout charges 20 credits each, 40 a page); NodeReal and Alchemy: none, the same calls ask for BEP-20 or ERC-20 transfers too (250 and 120 compute units each) |

Every extra transaction also costs three RPC reads (transaction, receipt, block). The block
scan used on `localnet` lists direct transactions only. NFT transfers inside other calls are
not added. Sync into a new database after changing the scope: stored records stay as they are.

## Bitcoin

Bitcoin is read from an Esplora REST API: mempool.space's public servers by default, at one
request a second, or your own Esplora or mempool server. Addresses of the five standard
types (P2PKH, P2SH, P2WPKH, P2WSH, P2TR) and P2PK public keys are read with embit and
checked against the network before any request; the server's genesis block is checked
before the first one.

| `--network` | Esplora server |
|---|---|
| `testnet4` | The default: `https://mempool.space/testnet4/api` |
| `signet` | `https://mempool.space/signet/api` |
| `mainnet` | `https://mempool.space/api`, only when asked for |
| `localnet` | A regtest node's Esplora at `http://localhost:3002` |

Set `BITCOIN_<NETWORK>_ESPLORA_SERVER` in `.env` to use your own server.
`BITCOIN_<NETWORK>_HISTORY_SOURCE` chooses the source; Esplora is the only one so far, and a
Bitcoin Core node is planned as the second. mempool.space states no request limit and warns
of bans for heavy use, so run your own server for more than a few addresses.
[The Bitcoin page](https://majidali32ms-lang.github.io/multichain-analytics-indexer/chains/bitcoin/)
has the measured rates, every field and the limits.

### Commands

```bash
multichain-indexer --chain bitcoin index address <address-or-public-key>
```

```bash
multichain-indexer --chain bitcoin index tx <txid>
```

```bash
multichain-indexer --chain bitcoin sync <address>
```

```bash
multichain-indexer --chain bitcoin metrics --days 30
```

```bash
multichain-indexer --chain bitcoin export --format csv --output transactions.csv
```

```text
Address           mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu
Type              P2PKH
Balance           0.03571478 BTC
Unconfirmed       +0 sats
UTXOs             2
Transactions      4
Quantum exposure  exposed: 2 confirmed UTXOs, 0.03571478 BTC; a confirmed spend revealed the public key (as of query time, from Esplora, height 161120)

2026-11-13 16:56  c6f15b762eb44dbe10f2625bb4ddaec9ce1f11aa324aca2df9a530ac11a150a0  24 confirmations, block 161097  0.03561478 BTC  522 sats  1.01 sat/vB  519 vB, 2076 WU  3 in, 2 out  no
2026-11-13 15:12  48b9226d71084b95bd93baac5f33fc1bd4da2d3e4a93c92ca5c4a19901cac7ff  33 confirmations, block 161088  49.820251 BTC  294 sats  2.01 sat/vB  147 vB, 585 WU  1 in, 2 out  yes
```

```text
Txid                23246681e815c1cc7f70a04f2feed025a9c3d6335aa3a4bcb8ba89692355296b
Time                2026-11-13 16:02 UTC
Sender              tb1pqhrrvztxyz09zcj89pxvu9jg3ltcfw3qs54hjpjc74gm6k38k4uqrpm3x5
Receiver            tb1p93kgv2ye3axwqph8l8s5nxm9sccn28vffmm9vs44utcuuztljfus2v8e5q
Status              19 confirmations, block 161098
Amount              87.30535694 BTC
Fee                 310 sats
Fee rate            2.01 sat/vB
Size                154 vB, 616 WU
Inputs and outputs  1 in, 2 out
RBF                 yes
```

```text
Synced mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu on bitcoin testnet4: 4 transactions up to height 161116, 5 requests in 4.1s
```

### What is recorded

The `native` block holds every input, with the output it spends, and every output, each with
its address, script type and value in sats, plus version, locktime, size, weight, virtual
size, RBF signalling, SegWit and Taproot use, coinbase, block and confirmations.

- `fee` is inputs minus outputs, in sats, and must equal what the server reports.
- `fee_rate` is in sat/vB over the weight divided by four, the rate mempool.space shows.
  The fee and fee rate of every saved test transaction match mempool.space's page for it.
- `sender` is the address of the input spending the most sats, `coinbase` for a coinbase.
- `receiver` and `amount` are the largest output paying an address no input spends from, so
  change back to the sender is skipped; with only change, the largest output.
- A transaction is `final` from six confirmations. A pending one whose block leaves the best
  chain, or that leaves the mempool, becomes `orphaned`.
- A P2PK output has no address: give its public key in hex, which is also how such inputs
  and outputs are shown.

### Quantum-exposed outputs

`index address`, and `Indexer.account()` as `native.quantum_exposure`, say whether the
address's confirmed unspent outputs show a public key on chain, which is what Shor's
algorithm needs; a hash of a key is not enough. It is per address, as of query time, from
Esplora's totals for the address, and it names the height it was read at.

| Address type | Status | When |
|---|---|---|
| P2PK | exposed | Always: the output holds the public key. |
| P2TR | exposed | Always: the output key is a public key that can spend by the key path (BIP-341). |
| P2PKH, P2WPKH | exposed | Once the address has spent in a confirmed block (`chain_stats.spent_txo_count` above 0): the spend showed the key, and every output of the address shares it. |
| P2PKH, P2WPKH | not exposed | No confirmed spend yet: only a hash of the key is on chain. |
| P2SH, P2WSH | script revealed | Once the address has spent in a confirmed block: the spend showed the script. Whether that script holds public keys is not read, so it is not called exposed. |
| P2SH, P2WSH | not exposed | No confirmed spend yet. |

Limits:

- It is per address, as of query time, from Esplora: the answer can change with the next
  block, and it is only as right as the server.
- Only confirmed outputs count. A spend still in the mempool has already shown the key, but
  it counts once confirmed; the mempool's effect on the balance is shown apart.
- It covers the address you ask about, not the whole UTXO set, and not other addresses that
  share a key with it (the same key behind a P2PKH and a P2WPKH address, for example).
- A key shown anywhere else, such as in a P2SH script, an xpub or a message signature, is not
  seen.
- "script revealed" does not say whether the script's keys can be attacked; read the script.
- It reports exposure by this rule only. It is not a statement about when a quantum computer
  could break a key, and nothing on Bitcoin is post-quantum yet (`pq-status`).

## Post-quantum signatures

Algorand transactions signed by native Falcon-1024 accounts (`pqsig`, scheme `f1`) and NEAR
keys and transactions using ML-DSA-65 carry a `pq` field in their `native` block, and metrics
report their share. Nothing else is live on any planned chain yet. For Bitcoin, `index address`
reports whether an address's outputs are [quantum-exposed](#quantum-exposed-outputs).

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
  adapters/evm/       one adapter, a profile per chain, and pluggable history sources
  adapters/bitcoin/   Esplora source, parser, metrics; imports only core/
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
| 0.8 | A local dashboard over your own database, on localhost only |
| 0.9 | Wallet utilities: pre-flight checks, simulation, signing on test networks first |
| 1.0 | Stable release on PyPI, with a docs page and demo per chain |

## License

[Apache-2.0](LICENSE)
