# examplechain-indexer: a template plugin

A complete third-party chain for multichain-analytics-indexer, to copy when adding your own.
It reads a small JSON API, described at the top of `src/examplechain_indexer/adapter.py`;
replace those three requests with your chain's API.

```bash
pip install -e .
multichain-indexer --chain examplechain index account ex1...
```

The guide is in the docs: "Add a chain". The repository's `tests/test_plugins.py` installs
this template and checks it through the Python API and the command line.
