# BNB Chain: BNB Smart Chain and opBNB

BNB Smart Chain (`--chain bnb`) and opBNB (`--chain opbnb`), the OP Stack layer 2 that settles
on it, run on the shared [EVM adapter](evm.md) with their own profiles. This page covers what
is particular to them. Facts were checked on BNB Chain's own docs, blog, BEPs and client
releases, and on the chains themselves, in November 2026.

## Networks

| `--chain` | `--network testnet` (default) | `--network mainnet` | Gas token | Tokens | Block time |
|---|---|---|---|---|---|
| `bnb` | BNB Smart Chain testnet (Chapel), chain ID 97 | BNB Smart Chain mainnet, 56 | BNB | BEP-20 | 0.45 s |
| `opbnb` | opBNB testnet, 5611 | opBNB mainnet, 204 | BNB | BEP-20 | 0.25 s |

The default endpoints are the public ones the BNB Chain docs list:
`bsc-dataseed.bnbchain.org`, `bsc-testnet-dataseed.bnbchain.org`,
`opbnb-mainnet-rpc.bnbchain.org` and `opbnb-testnet-rpc.bnbchain.org`. The block times were
measured over 20,000 blocks: 0.4502 s on BNB Smart Chain mainnet, 0.4518 s on its testnet,
0.25 s on both opBNB networks. BNB Smart Chain has made 0.45-second blocks since the Fermi hard
fork (BEP-619, mainnet 14 January 2026). Both chains' block headers carry a `milliTimestamp`,
which the finality readout and the holder reports use.

opBNB receipts report the L1 data fee paid to BNB Smart Chain (`l1Fee`, `l1GasUsed`,
`l1GasPrice`, `l1BaseFeeScalar`, `l1BlobBaseFee`, `l1BlobBaseFeeScalar`); the fee is the
execution fee plus `l1Fee`, as on the other OP Stack chains. On two saved opBNB transactions it
equals opBNBScan's "Txn Fee" to the wei. Deposits from BNB Smart Chain are type 126 and pay
nothing on L2.

## Finality

BNB Smart Chain finalizes a block through fast finality (BEP-126): validators vote, and a block
is final once it and its child are justified, normally two blocks later. When fast finality
stalls, the chain falls back to probabilistic finality. The node's own
`eth_getFinalizedHeader(verifiedValidatorNum)` answers with the higher of the two: the
fast-finalized block, or the highest block that the given number of validators have built on.

On `--chain bnb` the adapter asks `eth_getFinalizedHeader(-2)`, two thirds of the validators,
and uses the standard `finalized` block tag only when the endpoint does not serve that method
(answers "method not found", as non-BSC nodes do) or names no block. Whatever the source, a
sync never moves its checkpoint past the newest block, the finalized block or what the history
service has indexed. No fixed confirmation count is used.

On opBNB, `finalized` is an L2 block whose batch sits in a finalized BNB Smart Chain block, which
takes longer; withdrawals to BNB Smart Chain wait a 7-day challenge period, which is not
finality of the L2 block itself.

`index finality` shows the lag now, in blocks and milliseconds:

```console
$ multichain-indexer --chain bnb --network mainnet index finality
Newest block     134547807 (2026-11-19 18:02:18.150 UTC)
Finalized block  134547805 (2026-11-19 18:02:17.250 UTC)
Lag              2 blocks, 900 ms
Source           eth_getFinalizedHeader
Observed at      2026-11-19 18:02:19.053 UTC

$ multichain-indexer --chain opbnb --network mainnet index finality
Newest block     207977653 (2026-11-19 18:02:24.000 UTC)
Finalized block  207977599 (2026-11-19 18:02:10.500 UTC)
Lag              54 blocks, 13500 ms
Source           finalized tag
Observed at      2026-11-19 18:02:25.970 UTC
```

The finalized block is read first and the newest one after it, so the lag is an upper bound.
Sampled 20 times a second apart on 16 November 2026: BNB Smart Chain mainnet 2 to 3 blocks,
opBNB mainnet 92 to 128 blocks. The public BNB Smart Chain testnet endpoint sits behind a load
balancer whose nodes are not always in step: in 8 of 20 samples it reported a finalized block
above its newest block. The readout then shows the finalized block at the newest block and says
so; the sync guard does the same. From Python: `Indexer("bnb").finality()`.

## Account history

| Network | Source | Key | Notes |
|---|---|---|---|
| BNB Smart Chain mainnet | NodeReal `nr_getAssetTransfers` | `NODEREAL_API_KEY` (free) | windows of 1,000,000 blocks; NodeReal indexes a few blocks behind the chain |
| BNB Smart Chain testnet | `rpc-scan`: every block over the RPC | none for direct transactions | at most 20,000 blocks without a start block, see below |
| opBNB mainnet and testnet | NodeReal `nr_getAssetTransfers` | `NODEREAL_API_KEY` (free) | NodeReal's method page says "BSC and ETH mainnet only", its opBNB page lists both opBNB networks; both answered with a free key on 16 November 2026 |

No free service lists BNB Smart Chain testnet history: NodeReal answers "the method
nr_getAssetTransfers does not exist/is not available" there, and Etherscan's API serves chain 97
on paid plans only. So the testnet reads blocks. Each block is one request, and the testnet makes
about 192,000 blocks a day, so a day of its history is about 10.7 hours of requests at the
default 5 a second. A range longer than 20,000 blocks (about 2.5 hours of chain) is refused unless
`BNB_TESTNET_SCAN_FROM_BLOCK` names the first block to read:

```console
$ multichain-indexer --chain bnb sync 0xbcf0e3b8e7508b771ae71d9bb41c81369601dcaf
Error: rpc-scan reads BNB Smart Chain testnet one block per request, and this range is 143683348 blocks (at most 20000 without a start block). Set BNB_TESTNET_SCAN_FROM_BLOCK to the first block to read; older blocks are then not read.
```

With the start block set, the guard is lifted and blocks before it are never read. The `all`
scope also lists BEP-20 transfers inside other accounts' calls, from the tokens' `Transfer` logs.
BNB Smart Chain's public endpoints refuse `eth_getLogs` ("limit exceeded" even for 10 blocks), so
those logs come from NodeReal's `eth_getLogs`, which needs the free `NODEREAL_API_KEY`.

## BEP-20 holder reports

`index holders` reports one token over a block range, from its `Transfer` logs. Nothing is
stored, and no figure claims to cover the whole chain: the report names its block range and
the time it covers, and counts only addresses that sent or received the token in that range.

```console
$ multichain-indexer --chain bnb index holders 0xca52000000000000000001a96a592e8b818886d1 --from-block 141677813 --to-block 141727812
Token               0xCa52000000000000000001a96a592e8b818886d1 (BEP-20)
Blocks              141677813 to 141727812
Time covered        2026-11-09 06:31:36.400 UTC to 2026-11-09 12:46:36.200 UTC
Transfers           102
Addresses active    42
Holders at the end  42 of the addresses active in the range
New holders         42
Holders that left   0
Total supply        1000000000000000 base units
Top-ten share       100.00% of total supply (at least)

Largest balances at the last block (base units, share of total supply):
  0x6bCEa29b20e7FAD8b099Bb00317546D7f7eab251  998999999959660  99.90%
  0xAf0Da7427ce0c81Ff3C04C372aB9C32E43742F47  999999999900  0.10%
  0x01586e5db5B3DbC9a6b31386f83F03Cc5edB2C06  2015  0.00%
...
Transfers per day (UTC):
  2026-11-09  102
```

| Figure | Meaning |
|---|---|
| Transfers | fungible `Transfer` logs of the token in the range |
| Addresses active | distinct senders and receivers, without the zero address |
| Holders at the end | active addresses with a balance at the last block (`balanceOf`) |
| New holders / Holders that left | active addresses with no balance before the range and one after it, or the other way round |
| Top-ten share | the ten largest of those balances over `totalSupply()`: at least the token's real top-ten share, since holders that moved nothing in the range are not seen |
| Transfers per day | by UTC day of block time; the first block of each day is found by bisection |

The balance before the range is the balance at its end minus the range's net transfers, so no
older state is needed. An address whose balance does not follow from its transfers (a
fee-on-transfer or rebasing token) is counted as "Balances not matching transfers". A live test
checks the new and leaving holders against balances read directly before and after a CAKE range.

Balances are read through Multicall3 (`0xcA11bde05977b3631167028862bE2a173976CA11`, deployed on
all four networks), up to 200 in one call, all at the same block. Every public BNB Smart Chain
and opBNB endpoint keeps state for only about the newest 100 blocks (a balance 1,000 blocks back
answers "missing trie node"); NodeReal's free key serves state at any block. So:

| `NODEREAL_API_KEY` | Logs | Balances | `--to-block` default |
|---|---|---|---|
| set | NodeReal on BNB Smart Chain; the public RPC on opBNB | NodeReal, at any block | the newest finalized block |
| not set | refused on BNB Smart Chain; the public RPC on opBNB | the public RPC, recent blocks only | the newest block |

At most 2,000 addresses may move the token in the range; the report stops reading logs as soon
as more do. `--nodereal-count` adds NodeReal's own full-chain holder count (`nr_getTokenHolderCount`,
50 compute units on the free plan), labelled "according to NodeReal"; it is NodeReal's figure, not
one this tool computed. `--json` prints the whole report. From Python:
`Indexer("bnb", "mainnet").token_holders(token, from_block=..., to_block=...)`.

NodeReal's cost for a token's logs, measured on 19 November 2026 over blocks 133,900,000 to
133,909,999 with this tool's reader (windows of 5,000 blocks, halved when NodeReal refuses one
holding more than 50,000 logs; 50 compute units a call):

| Token | Logs | Calls | Time | Compute units | Per day of chain data |
|---|---|---|---|---|---|
| CAKE | 1,466 | 2 | 5.8 s | 100 | about 1,900 |
| USDT | 788,806 | 33, and 4 refused as too large | 299.5 s | 1,650 (1,850 if refused calls cost too) | about 32,000 to 35,500 |

The free plan's 10 million compute units a month cover about 280 days of USDT's chain data at
this rate. For the busiest tokens, time is the limit: USDT took about 30 seconds of requests per
1,000 blocks. One 10,000-block sample per token, at November 2026 activity.

## BNB Smart Chain labels

Every BNB Smart Chain transaction carries `native.bnb`:

| Field | Meaning |
|---|---|
| `validator` | the block's coinbase, the validator that produced it |
| `system_transaction` | true for the block's own system calls |
| `system_contract` | the system contract the transaction calls, if any |

A system transaction is what the Parlia engine's `IsSystemTransaction` accepts: sent to a system
contract, at a gas price of zero, by the block's coinbase. A zero gas price alone is not enough;
ordinary zero-fee transactions exist too (a saved one is in the tests). The system contracts are
those in `contracts/System.sol` of bnb-chain/bsc-genesis-contract:

| Address | Contract |
|---|---|
| `0x…1000` | BSCValidatorSet |
| `0x…1001` | SlashIndicator |
| `0x…1002` | SystemReward |
| `0x…1003` | TendermintLightClient |
| `0x…1004` | TokenHub |
| `0x…1005` | RelayerIncentivize |
| `0x…1006` | RelayerHub |
| `0x…1007` | GovHub |
| `0x…1008` | TokenManager |
| `0x…2000` | CrossChain |
| `0x…2001` | Staking |
| `0x…2002` | StakeHub |
| `0x…2003` | StakeCredit (a template; each validator has its own copy) |
| `0x…2004` | BSCGovernor |
| `0x…2005` | GovToken |
| `0x…2006` | BSCTimelock |
| `0x…3000` | TokenRecoverPortal |

Parlia's list leaves out TokenManager, Staking and StakeCredit, so calls to those are never
system transactions. `Indexer.describe()` shows the validator and the system call for a
transaction; `metrics` adds `bnb_system_transactions` and `top_validators_by_transactions`.

`index validators` lists the current validator set with the names validators registered in
StakeHub (`consensusToOperator`, then `getValidatorDescription`), read in two Multicall3 calls:

```console
$ multichain-indexer --chain bnb --network mainnet index validators
45 validators at block 134547837 (2026-11-19 18:02:32.477 UTC)
  0xbdcc079BBb23C1D9a6F36AA31309676C258aBAC7  Fuji
  0xD3b0d838cCCEAe7ebF1781D11D1bB741DB7Fe1A7  BNBEve
  0x460A252B4fEEFA821d3351731220627D7B7d1F3d  Defibit
  0x75B851a27D7101438F45fce31816501193239A83  Figment
  0x5009317FD4F6F8FeEa9dAe41E5F0a4737BB7A7D5  NodeReal
...
```

Blob transactions (BEP-336) keep their blob fee in `native.blob`; the blob data itself
(`eth_getBlobSidecars`) is not read.

## CAS20 tokens (BEP-702)

The Jenner hard fork (testnet 29 October 2026, mainnet 8 December 2026) adds CAS20, tokens the
chain runs natively at reserved addresses (`0xCA52` and eight zero bytes, then a variant byte)
whose code is the one-byte sentinel `0xEF`. Creating them also needs a governance switch. CAS20
tokens emit the standard `Transfer` events, so the holder report reads them like any BEP-20.
`index token` names them:

```console
$ multichain-indexer --chain bnb index token 0xca52000000000000000001a96a592e8b818886d1
Token         0xCa52000000000000000001a96a592e8b818886d1
Standard      BEP-20 (CAS20 stablecoin, BEP-702)
Name          48club Chapel Test
Symbol        T48
Decimals      6
Total supply  1000000000000000 base units
As of block   143683299
```

An address with no bytecode at all is refused with a plain message:
`Error: no contract is deployed at 0x…dEaD on BNB Smart Chain testnet`.

## Public key exposure

An externally owned account's address is a hash of its public key; the key itself is on chain
from the account's first signature, which every transaction it sends carries. A nonce above zero
proves at least one sent transaction, so its key is public. `index account` shows this for every
EVM chain, as of the block it read, and `index exposure` for a list:

```console
$ multichain-indexer --chain bnb --network mainnet index exposure 0x60d162eff2b7043c12174bd7da231a0996fd36e7 0x5009317FD4F6F8FeEa9dAe41E5F0a4737BB7A7D5 0x55d398326f99059fF775485246999027B3197955 0x000000000000000000000000000000000000dEaD
  0x60d162eff2b7043C12174BD7DA231A0996Fd36E7  public key revealed (EIP-7702 delegated account)
  0x5009317FD4F6F8FeEa9dAe41E5F0a4737BB7A7D5  public key revealed (nonce > 0)
  0x55d398326f99059fF775485246999027B3197955  contract account (no private key)
  0x000000000000000000000000000000000000dEaD  no transaction sent yet (nonce 0)

Accounts                                          4
Public key revealed (nonce > 0)                   1
Public key revealed (EIP-7702 delegated account)  1
Contract account (no private key)                 1
No transaction sent yet (nonce 0)                 1
Revealed share                                    66.67% of the accounts that can sign
As of                                             blocks 134547853 to 134547853, 2026-11-19 18:02:41.408 UTC
A lower bound: a permit or other EIP-712 signature used on chain by someone else reveals a key while the nonce is still 0.
```

| Status | Rule |
|---|---|
| public key revealed (nonce > 0) | an externally owned account that has sent a transaction (an EIP-7702 authorization also raises the nonce) |
| public key revealed (EIP-7702 delegated account) | its code is a delegation (`0xef0100` and an address), which it signed |
| contract account (no private key) | the address holds contract code |
| no transaction sent yet (nonce 0) | no transaction sent; the key may still be known, see below |

The rule is a lower bound. A permit (EIP-2612) or another EIP-712 signature that someone else
submits reveals the signer's key while its nonce is still zero, so "nonce 0" does not prove the
key is hidden. The share counts accounts that can sign, so contracts are left out of it. EIP-7702
is live on BNB Smart Chain since the Pascal hard fork (BEP-441, mainnet 20 March 2025). Public
endpoints keep about 100 blocks of state, so a long list reads the newest block again every 25
accounts; a revealed key stays revealed, so the counts still add up. At most 500 addresses a call.
From Python: `Indexer("bnb", "mainnet").key_exposure([...])`.

Post-quantum signatures themselves are not live on either chain; see
[Post-quantum readiness](../post-quantum.md).

## Limits

| What | Limit |
|---|---|
| BNB Smart Chain testnet history | block scan only; about 10.7 hours of requests per day of chain data; 20,000 blocks without `BNB_TESTNET_SCAN_FROM_BLOCK` |
| BNB Smart Chain logs (`all` scope, holder reports) | NodeReal only (`NODEREAL_API_KEY`); the public endpoints refuse `eth_getLogs` |
| NodeReal free plan | 10 million compute units a month, 150 a second per key; `nr_getAssetTransfers` 250, `eth_getLogs` 50 |
| Holder reports | addresses that moved the token in the range only; at most 2,000; balances at old blocks need NodeReal |
| opBNB without a key | holder balances at the newest block only (public state is about 100 blocks deep) |
| Public key exposure | a lower bound, as of query time; at most 500 accounts a call |
| Validator names | what each validator registered in StakeHub; none for a validator that registered none |
| Fast finality | `eth_getFinalizedHeader` is BNB Smart Chain's own method; other endpoints get the `finalized` tag |
| CAS20 | read on testnet; mainnet tokens only after Jenner (8 December 2026) and the governance switch |

## How it compares

Checked on each project's own pages, PyPI and GitHub on 16 to 19 November 2026. "Not found"
means the pages read did not say so, not that the tool cannot.

| Tool | Language, licence | Local database | Finality documented | One schema across chains | Public key or post-quantum view | Free without a paid plan | Latest release |
|---|---|---|---|---|---|---|---|
| multichain-analytics-indexer | Python, Apache-2.0 | SQLite or PostgreSQL | yes, per chain | yes | yes | yes (free keys for some sources) | this version |
| [bscscan-python](https://github.com/pcko1/bscscan-python) | Python, MIT | no | not found | no | not found | library yes; its BscScan API is now paid for BNB Smart Chain | 2.0.0, 2021-05-22 |
| [bsc-etl](https://pypi.org/project/bsc-etl/) | Python, MIT classifier | files | not found | no | not found | runs on your own node | 0.0.3, 2020-09-15 |
| [ethereum-etl](https://github.com/blockchain-etl/ethereum-etl) | Python, MIT | files, relational export | not found | no | not found | runs on your own node | 2.4.2, 2024-04-11 |
| [web3.py](https://github.com/ApeWorX/web3.py) | Python, MIT | no (a client library) | block tags only | not a pipeline | not found | yes | 8.0.0, 2026-08-31 |
| [SubQuery](https://github.com/subquery/subql) | TypeScript, GPL-3.0 | PostgreSQL | not found | one schema per project | not found | self-hosted yes | node 6.5.0, 2026-10-29 |
| [ankr-sdk](https://github.com/Ankr-network/ankr-python-sdk) | Python, MIT (hosted API) | no | not found | same methods per chain | not found | registration; some methods premium | 1.0.3, 2025-10-13 |
| [GoldRush (Covalent)](https://goldrush.dev/pricing) | hosted | no | not found | yes | not found | 14-day trial, then paid | hosted |
| [Bitquery](https://bitquery.io/pricing) | hosted GraphQL | no | not found | yes | not found | 7-day trial | hosted |
| [Dune](https://dune.com/pricing) | hosted SQL | no | not found | not found | not found | API needs a trial or paid plan | hosted |
| [NodeReal MegaNode](https://nodereal.io/pricing) | hosted RPC and APIs | no | the chain's own tags | no | not found | yes, 10M compute units a month | hosted |
