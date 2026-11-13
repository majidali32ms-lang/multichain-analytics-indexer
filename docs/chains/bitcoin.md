# Bitcoin

Bitcoin is read from an [Esplora](https://github.com/Blockstream/esplora/blob/master/API.md)
REST API: mempool.space's public servers by default, or your own Esplora or mempool server.
An account is an address of one of the five standard types, or a public key for P2PK
outputs, and its history is every transaction that pays or spends its output script.
Amounts are held in sats and shown in BTC.

## Networks

| `--network` | Esplora server |
|---|---|
| `testnet4` (default) | `https://mempool.space/testnet4/api` |
| `signet` | `https://mempool.space/signet/api` |
| `mainnet` | `https://mempool.space/api` |
| `localnet` | `http://localhost:3002`, electrs' port for regtest |

testnet4 ([BIP-94](https://github.com/bitcoin/bips/blob/master/bip-0094.mediawiki)) replaces
testnet3, which Bitcoin Core plans to phase out; testnet3 is not offered.

```text
BITCOIN_TESTNET4_ESPLORA_SERVER=https://your-esplora.example/api
BITCOIN_TESTNET4_HISTORY_SOURCE=esplora
```

Before its first request, the adapter reads the server's genesis block (`/blocks/0`) and stops
if it belongs to another network:

```text
Error: the esplora server serves Bitcoin signet, not Bitcoin testnet4. Check BITCOIN_TESTNET4_ESPLORA_SERVER.
```

`BITCOIN_<NETWORK>_HISTORY_SOURCE` chooses where data comes from. Esplora is the only source
today; a Bitcoin Core node over JSON-RPC is planned as the second. A source is one class that
meets the `BitcoinSource` protocol in `adapters/bitcoin/history.py`, with no change to the
parser or to `core/`.

**`localnet` was not tested against a real regtest node**, only against a local fake server.

### The public server

mempool.space publishes no request limit. Its terms (10 July 2024) offer the service as is,
without warranty, and its API documentation warns of HTTP 429 and possible bans for heavy
use. Measured on 13 November 2026 through this project's HTTP client, 30 requests each, one
test at a time: none was refused at 1 request a second, and about 4 in 30 failed at 0.5, 2,
5 and 10 a second, so those failures are not simply a matter of rate. The default is 1 a
second, `MCI_REQUESTS_PER_SECOND` changes it, and failed requests are retried with backoff.
For anything larger than a few addresses, run your own server.

blockstream.info also serves Esplora, but it is not the default: the "Explorer API" it
documents is a paid plan, and its keyless endpoint states no terms.

## Addresses

P2PKH, P2SH, P2WPKH, P2WSH and P2TR addresses are read with
[embit](https://github.com/diybitcoinhardware/embit), pinned at 0.8.0, and encoded again for
the requested network, which must give back the same address. So a mainnet address on
testnet4 is refused before any request:

```text
Error: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa is an address on Bitcoin mainnet, not testnet4
```

testnet4 and signet share their prefixes (`tb1`, `m`, `n`, `2`), so an address cannot tell
them apart; the genesis check covers the server. Bech32 addresses in capitals are accepted
and written in lower case. Witness versions 2 and above have no standard address type yet
and are refused.

A P2PK output has no address. Give its public key in hex (33 or 65 bytes) instead; Esplora
finds every script by the SHA-256 of its output script, so a key is looked up like an
address.

## Transactions

| Field | Meaning |
|---|---|
| `fee` | Inputs minus outputs, in sats; 0 for a coinbase. It must equal the fee the server reports, or the reply is refused as malformed. |
| `native.vsize` | Weight over four, rounded up (BIP-141). |
| `native.fee_rate` | Fee over weight divided by four, not rounded, in sat/vB. This is the rate mempool.space shows, which divides by 146.25 vB for a weight of 585 where the rounded size would be 147. |
| `native.rbf` | An input's sequence is below `0xfffffffe` (BIP-125). |
| `native.segwit` | An input other than a coinbase carries witness data, so P2SH-wrapped SegWit counts. |
| `native.taproot` | An input spends a P2TR output. |
| `native.confirmations` | Counted when the record was read: 1 in the block holding it, 0 in the mempool. |
| `native.inputs`, `native.outputs` | Every input with the output it spends (address, script type, value, sequence), and every output with its script. |

Script types are `p2pkh`, `p2sh`, `p2wpkh`, `p2wsh`, `p2tr`, `p2pk`, `op_return`, and the
server's names for anything else, such as `multisig` for bare multisig. For P2PK, `address`
holds the public key in hex.

Fees and fee rates in the test fixtures were compared with mempool.space's pages for the
same ten transactions on testnet4 and signet, one of each kind: legacy, P2SH-wrapped SegWit,
P2WPKH, P2WSH, Taproot, seven inputs and seven outputs, OP_RETURN, coinbase, and two in the
mempool. All ten match to the three digits mempool.space shows.

## Status and the shared fields

A transaction is `final` from six confirmations and `pending` before; one in the mempool is
`pending` with no block, and its `time` is when it was read. A sync fetches the newest five
blocks again every time. A pending record the history no longer lists is read again: if its
block has left the best chain, which proves a reorg, it becomes `orphaned`, and so does a
transaction that has left the mempool, as a replacement or eviction would; if it confirms
after all, a later sync stores it again. One whose block is still on the best chain is left
as it is.

A Bitcoin transaction has many inputs and outputs, so the shared fields follow one rule:

- `sender`: the address of the input spending the most sats (the first on a tie), the public
  key for a P2PK input, `coinbase` for a coinbase, or the first spent output as `txid:vout`
  when no input has either.
- `receiver` and `amount`: the largest output paying an address no input spends from, so
  change back to a sender is skipped. When every output pays a sender, the largest output
  with an address. With no such output, `receiver` is empty and `amount` 0.

Metrics count distinct senders as active addresses, so this rule decides that figure.

## Metrics

```bash
multichain-indexer --chain bitcoin metrics --days 30
```

The `native` block adds:

| Key | Meaning |
|---|---|
| `fees_sats`, `fees_btc` | Fees of the stored transactions. |
| `fee_rate_sat_per_vb` | Average and median fee rate, coinbase left out. |
| `address_types` | Outputs by script type, with each type's share of all outputs. |
| `segwit_share`, `taproot_share`, `rbf_share` | Share of non-coinbase transactions with SegWit inputs, spending P2TR, signalling RBF. |
| `coinbase_transactions`, `op_return_outputs`, `unconfirmed_transactions` | Counts. |

`index address` shows the UTXO count and balance from the server's totals for the address:
outputs received minus outputs spent, confirmed only, with the mempool's effect shown apart.

## Limits

- Esplora lists at most 50 unconfirmed transactions for a script.
- A busy address costs one request per 25 confirmed transactions on its first sync.
- The SegWit share counts witness data, so a SegWit output that has not been spent is not
  counted until a transaction spends it.
- Script types and output addresses are as the server decodes them. The adapter checks that
  inputs minus outputs equal the reported fee, but it does not decode scripts itself.

## Commands

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
multichain-indexer --chain bitcoin export --format csv --output transactions.csv
```
