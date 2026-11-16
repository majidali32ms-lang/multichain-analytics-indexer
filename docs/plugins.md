# Add a chain

Another package can add a chain without changing this one. It registers an adapter under
the entry point group `multichain_indexer.adapters`; the entry point's name is the chain's name
and its value the factory that builds the adapter. Once that package is installed, the chain
works everywhere: the Python API, every command, `--chain all`, the report and the HTTP API.

`examples/plugin-template` in the repository is a complete, tested plugin to copy. Its tests
install it the way pip would and use it through the Python API and the command line.

## 1. Declare the entry point

```toml
[project]
name = "examplechain-indexer"
dependencies = ["multichain-analytics-indexer>=0.7,<0.8"]

[project.entry-points."multichain_indexer.adapters"]
examplechain = "examplechain_indexer:create_indexer"
```

The name cannot be one of the built-in chains; a plugin that tries is refused with an error
naming it. A plugin is imported only when its chain is used.

## 2. Describe the chain

A `ChainProfile` says what shared code needs: the native token's symbol and decimals, the base
unit, the networks, the default network, and the model for the chain's `native` block.

```python
PROFILE = ChainProfile(
    name="examplechain",
    native_symbol="EXC",
    base_unit="drop",
    native_decimals=6,
    networks=("testnet",),
    default_network="testnet",
    transaction_model=ExampleTransaction,
)
```

Put what is specific to the chain in `NativeData` models. Never squeeze it into the shared
fields.

## 3. Implement BaseIndexer

| Method | Does |
|---|---|
| `validate_address` | refuse a malformed address before any request |
| `fetch_account`, `fetch_asset`, `fetch_contract` | read one item; raise `InvalidInput` for what the chain does not have |
| `chain_head` | the newest height the endpoint serves |
| `iter_account_transactions` | yield pages newest first, honouring `from_height`, `to_height` and `cursor` |
| `chain_metrics` | metrics only this chain has |
| `describe` | label and value pairs in the chain's own terms |
| `requests_made`, `aclose` | request count and cleanup |
| `final_height`, `recheck_pending` | override when the chain has no instant finality |

Send requests through `ApiClient` from `multichain_indexer.core.http`, so timeouts, retries,
`Retry-After` and the response cache come for free, and take the limiter from
`shared_limiter(host_scope(server), rate)`, or `key_scope` when a key is shared with other
chains. Measure the public endpoint's limit through `ApiClient` before choosing the rate.

## 4. Write the factory

```python
def create_indexer(network, settings, cache=None):
    chosen = network or PROFILE.default_network
    server = settings.endpoint("examplechain", chosen, "API_SERVER", DEFAULT_SERVERS[chosen])
    client = ApiClient(
        server,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        limiter=shared_limiter(host_scope(server), REQUESTS_PER_SECOND),
        cache=cache,
    )
    return ExampleIndexer(chosen, client)
```

`settings.endpoint` reads `EXAMPLECHAIN_TESTNET_API_SERVER` from `.env`, the environment or
`[chains.examplechain.testnet]` in `config.toml`, and refuses plain `http` to remote hosts.

## 5. Test it

Test the parser on saved real responses, the adapter against a local fake server, and the
plugin as installed: `tests/test_plugins.py` shows how, with a `.dist-info` folder holding
`entry_points.txt` on the import path.
