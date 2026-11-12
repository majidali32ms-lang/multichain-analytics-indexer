# Changelog

## 0.5.0

Adds the EVM chains: Ethereum, Arbitrum, OP Mainnet, Base, Polygon PoS and BNB Smart Chain,
through one adapter with a profile per chain.

- `--chain ethereum|arbitrum|optimism|base|polygon|bnb`, testnet by default (Sepolia, Arbitrum
  Sepolia, OP Sepolia, Base Sepolia, Amoy, BSC testnet), `--network mainnet` on request, and
  `--network localnet` for a local node such as Anvil.
- Every lookup first checks the RPC endpoint's `eth_chainId`; an endpoint of another network
  stops with `WrongNetwork` and names the setting to fix, never the endpoint.
- `index account`, `index contract` and `index token` (ERC-20, BEP-20 on BNB Smart Chain),
  read at one block. A mixed-case address with a wrong EIP-55 checksum is refused.
- Transactions and receipts come from the chain's own RPC: type, EIP-1559 fees, method
  selector, ERC-20 and ERC-721 transfers, EIP-4844 blob gas, `l1Fee` and its parts on OP
  Mainnet and Base, `gasUsedForL1` on Arbitrum. `fee` adds the blob fee and the L1 data fee.
- Account history from a service chosen per chain and network with
  `<CHAIN>_<NETWORK>_HISTORY_SOURCE`: Blockscout, Etherscan, NodeReal (BNB Smart Chain
  mainnet), Alchemy (Base), or reading every block on `localnet`. Each needs its own free key
  (`BLOCKSCOUT_API_KEY`, `ETHERSCAN_API_KEY`, `NODEREAL_API_KEY`, `ALCHEMY_API_KEY`); lookups
  need none. BNB Smart Chain testnet has no free history service.
- `<CHAIN>_<NETWORK>_HISTORY_SCOPE=all` also lists ERC-20 transfers to or from the address
  inside other accounts' calls; `direct`, the default, lists what it sent or received.
- `index account` prints "Powered by Etherscan.io APIs" under transactions Etherscan listed,
  and records name their history service in `native.history_source`.
- Metrics: gas, fees in the gas token and in wei split into execution, L1 data and blob fees,
  Arbitrum's L1 share of gas, transaction types, top tokens and contracts.
- Records up to the endpoint's finalized block are final. A sync never moves its checkpoint
  past that block, nor past what NodeReal has indexed.
- After each sync, stored pending records the history did not list again are checked again.
  `BaseIndexer.recheck_pending` is new, a change to `core/`; by default it changes nothing, so
  Algorand, Solana and NEAR behave as before. On EVM chains a record a reorg dropped becomes
  `orphaned`; nothing is deleted. Databases from 0.4.0 load unchanged.
- `load_transactions` takes an optional status filter.
- New dependency: `web3`, for its address, hashing and ABI helpers.
- Post-quantum status of the six EVM chains re-checked on 2026-11-09: still nothing live.
  Optimism has a roadmap; BNB Smart Chain a research report.

## 0.4.0

Adds post-quantum readiness.

- Algorand: transactions signed by native Falcon-1024 accounts (`pqsig`, scheme `f1`) and
  accounts the Indexer lists as `pqsig` carry a `pq` field in their `native` block. State
  proof transactions never count. LogicSigs that call `falcon_verify` are not identified.
- NEAR: transactions signed with an ML-DSA-65 key, ML-DSA-65 access keys (as `ml-dsa-65:` or
  `ml-dsa-65-hash:`) and accounts holding one carry a `pq` field.
- Metrics on both chains add `post_quantum`: the number and share of post-quantum
  transactions, per scheme. `index account` shows a "Post-quantum" line.
- `multichain-indexer pq-status` shows where each of the 10 planned chains stands, with a
  source and the date it was checked. Only Algorand and NEAR have anything live.
- A new `multichain_indexer.pq` package holds what every chain's detectors share; adding a
  scheme needs no change to it or to `core`.

## 0.3.1

The first 0.3 release on PyPI.

- Fixes the command line when the reader of its output is gone before the first write, as in
  `multichain-indexer export --format json | true` with an export larger than the output
  buffer. It printed an error from Python. It now exits quietly with code 1, as it already
  did when the reader left later, on Windows and Linux. 0.2.1 had the same fault.

## 0.3.0

Published to TestPyPI only.

Adds NEAR.

- Look up accounts with their kind, access keys and shard, and contracts with their code
  hash. These come from NEAR's standard JSON-RPC.
- Sync an account's history, with every receipt and its outcome. NEAR's RPC cannot list an
  account's transactions, so this comes from an indexing service, chosen by
  `NEAR_<NETWORK>_HISTORY_SOURCE`. The FastNEAR Transactions API is the default and works
  without a key; `NEAR_<NETWORK>_HISTORY_API_KEY` sets one.
- A sync keeps the transactions an account signed or received, the set NearBlocks lists for
  it. `NEAR_<NETWORK>_HISTORY_SCOPE=all` also keeps those where the account only received a
  receipt or is named in an action's arguments or an event log.
- A sync never moves its checkpoint past what the history service has indexed, so a service
  that falls behind cannot leave a gap.
- Metrics: fees in yoctoNEAR and NEAR, gas burnt, top contracts by calls, function calls by
  method, and receipt success and failure rates.
- Testnet by default; mainnet and a local sandbox with `--network`. An RPC endpoint serving a
  different network than `--network` raises `WrongNetwork`.

Other changes:

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
