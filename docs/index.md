# multichain-analytics-indexer

A Python library and command line for indexing and analysing blockchain accounts and
transactions, in each chain's own terms.

This version supports [Algorand](chains/algorand.md) and [Solana](chains/solana.md).

## Install

```bash
pip install multichain-analytics-indexer
```

Python 3.11 or newer.

## Use it

```python
from multichain_indexer import Indexer

with Indexer("algorand") as indexer:
    indexer.sync("JCN7TCSQEG6YHAQ7F6BS73J2UDXI2L7S7WJU35LYMIK22IL264ZRJO55QM")
    print(indexer.metrics(days=30))
```

The [Python API](api.md) page lists every method. The command line calls the same API.
