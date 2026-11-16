# Post-quantum readiness

Which chains already carry post-quantum signatures, and which accounts and transactions use
them. Nothing is reported as live unless the chain's own sources say it is enabled and it was
seen on the chain; everything else says "none live yet", with its source and the date it was
checked.

## Status

| Chain | Network | Status | Scheme | Since | Note | Source | Checked on |
|---|---|---|---|---|---|---|---|
| algorand | mainnet | live | falcon-1024 | in use by 2026-08-22 | Native Falcon-1024 accounts: signature type pqsig, scheme f1 (consensus v42). LogicSigs using falcon_verify are possible but not identified. State proof transactions never count. | [source](https://github.com/algorand/go-algorand/blob/master/config/consensus.go) | 2026-11-08 |
| algorand | testnet | live | falcon-1024 | in use by 2026-09-07 | As on mainnet. | [source](https://github.com/algorand/go-algorand/blob/master/config/consensus.go) | 2026-11-08 |
| solana | mainnet | none live yet |  |  | The Winternitz vault program in Blueshift's repository (wntrRTssxbf1rz9RPJ4xNBbpXxfsidQJT177NN3MXhB) is not deployed on mainnet, devnet or testnet. | [source](https://github.com/blueshift-gg/solana-winternitz-vault) | 2026-11-08 |
| near | mainnet | enabled, no use seen | ml-dsa-65 | enabled after 2026-07-20 | ML-DSA-65 keys and signatures (nearcore 2.13.0). No use seen as of 2026-11-08. | [source](https://github.com/near/nearcore/releases/tag/2.13.0) | 2026-11-08 |
| near | testnet | live | ml-dsa-65 | 2026-07-01 (protocol 85) | ML-DSA-65 keys and signatures; NEP-645 is still a draft. | [source](https://github.com/near/NEPs/blob/master/neps/nep-0645.md) | 2026-11-08 |
| ethereum | mainnet | none live yet |  |  | EIP-8141 (frame transactions) is a draft, scheduled for the Hegota upgrade, which has no activation date. | [source](https://ethereum.org/roadmap/security/quantum-resistance/) | 2026-11-09 |
| arbitrum | mainnet | none live yet |  |  | No official post-quantum statement found in the Arbitrum docs or blog. | [source](https://docs.arbitrum.io/) | 2026-11-09 |
| optimism | mainnet | none live yet |  |  | A roadmap exists, nothing is live: the Post-Quantum Roadmap for the Superchain (2026-01-14) plans to retire ECDSA accounts by January 2036, subject to governance, and has not chosen a scheme. | [source](https://optimism.io/blog/a-post-quantum-roadmap-for-the-superchain) | 2026-11-09 |
| base | mainnet | none live yet |  |  | No official post-quantum statement found. Optimism's Superchain roadmap does not name Base, which has run its own stack since its Azul upgrade (2026); its P256VERIFY precompile is not post-quantum. | [source](https://base.org/blog/introducing-base-azul) | 2026-11-09 |
| polygon | mainnet | none live yet |  |  | No official post-quantum statement found in the Polygon PoS docs, blog or improvement proposals. | [source](https://docs.polygon.technology/pos/overview/) | 2026-11-09 |
| bnb | mainnet | none live yet |  |  | Research only: a migration report (2026-05-14) benchmarked ML-DSA-44 transaction signatures; its proof of concept was not merged, and no BEP enables them. | [source](https://www.bnbchain.org/en/blog/bsc-post-quantum-cryptography-migration-report) | 2026-11-09 |
| bitcoin | mainnet | none live yet |  |  | BIP-360 (P2MR, a witness v2 output with no key path) and BIP-361 (migration and legacy signature sunset) are drafts with no activation parameters; no post-quantum signature BIP exists, and Bitcoin Core 32.1 adds none. | [source](https://github.com/bitcoin/bips/blob/master/bip-0360.mediawiki) | 2026-11-13 |

`multichain-indexer pq-status` prints the same table. It comes from one file,
`multichain_indexer/pq/status.py`, where every row records when it was last checked.

## What is counted

**Algorand.** A transaction signed by a native post-quantum account: its signature is
`pqsig`, and scheme `f1` is Falcon-1024 (consensus v42, `EnablePQSchemeFalcon1024`). An
account counts when the Indexer lists its `sig-type` as `pqsig`; the account record names
the kind but not the scheme, so its `pq.scheme` is `pqsig`. A scheme code the network has not
enabled is shown as it is, never as a scheme the network runs.

- LogicSigs that call `falcon_verify` (AVM v12) are possible, but are **not identified**:
  telling them apart would mean parsing TEAL programs.
- State proof transactions (`stpf`) never count. They are protocol messages, not an
  account's own transactions.
- The address is not used. Choosing an address off the Ed25519 curve is a client convention,
  not a consensus rule.

**NEAR.** ML-DSA-65 (FIPS 204), a third key and signature scheme since nearcore 2.13.0. A
transaction counts when the key that signed it starts with `ml-dsa-65:`. An access key counts
in either form the network shows: `ml-dsa-65:` with the full key, or `ml-dsa-65-hash:` in
`view_access_key_list`, which stores only the hash. An account counts when any of its keys
does; its `pq.evidence` says how many.

**Solana.** Nothing yet: the Winternitz vault program in Blueshift's repository is not
deployed on any public cluster. There is no detector until a deployed official program
exists.

**EVM chains and Bitcoin.** Nothing is live. Bitcoin also reports, per address, whether its
confirmed unspent outputs are quantum-exposed: P2PK and P2TR by script type; P2PKH and P2WPKH
once the address has spent in a confirmed block; P2SH and P2WSH show "script revealed" once
spent. The [Bitcoin page](chains/bitcoin.md#quantum-exposed-outputs) has the rule and its
limits.

## Where it shows

| | |
|---|---|
| `native.pq` on transactions and accounts | Algorand and NEAR. `scheme` and `evidence`; empty when not post-quantum |
| `metrics` | `native.post_quantum`: how many transactions, their share, and the count per scheme |
| `index account` | A "Post-quantum" line for the account and each recent transaction |
| `pq-status` | The status table above |

Records stored by an earlier version load with `pq` empty. Sync them again to have them
checked.

## Limits

- **NEAR mainnet:** ML-DSA-65 is enabled, but no mainnet account or transaction using it was
  seen as of the "checked on" date. The detector would report one.
- **"Since":** for Algorand it is the earliest use the Indexer search returned, not the
  activation round. For NEAR mainnet it is bounded by the 2.13.0 release, whose protocol
  vote started on 20 July 2026; the exact epoch was not confirmed.
- The share counts stored transactions only, after the sync scope has been applied.

## Adding a scheme

Write a detector in the chain's adapter (`adapters/<chain>/pq.py`), add it to that module's
detector list, and add or change the chain's row in `pq/status.py` with its source and date.
The shared code in `pq/` and `core/` does not change.
