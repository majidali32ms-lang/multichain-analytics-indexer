# Changelog

## 0.6.0

Adds Bitcoin.

- `--chain bitcoin`, testnet4 by default (BIP-94), `--network signet`, `--network mainnet` on
  request, and `--network localnet` for a regtest node's Esplora server.
- Data comes from an Esplora REST API: mempool.space's public servers by default, at one
  request a second with retries, or your own server with `BITCOIN_<NETWORK>_ESPLORA_SERVER`.
  `BITCOIN_<NETWORK>_HISTORY_SOURCE` chooses the source; a Bitcoin Core node is planned as a
  second. Every lookup first checks the server's genesis block; a server of another network
  stops with `WrongNetwork` and names the setting to fix, never the server.
- Addresses of all five standard types (P2PKH, P2SH, P2WPKH, P2WSH, P2TR) and P2PK public keys
  in hex, read with embit and checked against the network before any request: a mainnet
  address on testnet4 is refused.
- `index address` shows the balance in BTC, confirmed UTXOs, the mempool's effect and recent
  txids; `index tx <txid>` shows one transaction. `Indexer.transaction()` reads one
  transaction by its ID on any adapter that can.
- Records keep every input, with the output it spends, and every output, with address,
  script type and value in sats; size, weight, virtual size, fee in sats, fee rate in sat/vB
  as mempool.space shows it, RBF signalling, SegWit, Taproot, coinbase, block and
  confirmations. The fee is inputs minus outputs and must equal what the server reports. The
  shared sender, receiver and amount follow a documented rule.
- A transaction is final from six confirmations. A pending one whose block left the best
  chain, or that left the mempool, becomes `orphaned`.
- Metrics: fees in sats and BTC, average and median fee rate, output types and their share,
  SegWit, Taproot and RBF shares, coinbase, OP_RETURN and unconfirmed counts.
- Quantum-exposed outputs, per address as of query time from Esplora's confirmed totals: P2PK
  and P2TR are exposed by script type, P2PKH and P2WPKH once the address has spent, P2SH and
  P2WSH show "script revealed" once spent. The rule and its limits are in the README and docs.
- The Bitcoin adapter imports only `core/`, so it can later ship on its own.
- `core/http.py` adds `ApiClient.get_json_value`, which returns any JSON value (Esplora
  answers with arrays and plain-text numbers) through the same retries, rate limiter and
  cache; `get_json` and `post_json` are unchanged.
- New dependency: `embit` 0.8.0, for Bitcoin addresses and public keys.
- Post-quantum status of Bitcoin re-checked on 2026-11-13: BIP-360 and BIP-361 are drafts,
  nothing is live.

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
