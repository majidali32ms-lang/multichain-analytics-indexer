# HTTP API

```bash
pip install 'multichain-analytics-indexer[serve]'
multichain-indexer serve
```

serves the local database read-only on `http://127.0.0.1:8000`, with the OpenAPI schema at
`/openapi.json` and interactive docs at `/docs` (the docs page loads its scripts from a CDN;
the schema does not need one).

| Route | Gives |
|---|---|
| `GET /health` | status and version |
| `GET /chains` | chains this install can read, and the chains and networks stored |
| `GET /metrics?days=30` | every stored chain side by side, as `metrics --chain all --json` |
| `GET /chains/{chain}/{network}/metrics?days=30&account=` | one chain's metrics, as `metrics` |
| `GET /chains/{chain}/{network}/transactions?account=&days=&offset=0&limit=50` | stored transactions, newest first, in pages of up to 1,000 |
| `GET /chains/{chain}/{network}/accounts/{address}` | the account's state as `index account` last stored it |

```bash
curl -s "http://127.0.0.1:8000/chains/bitcoin/testnet4/transactions?limit=1"
```

```text
{
  "total": 4,
  "offset": 0,
  "limit": 1,
  "next_offset": 1,
  "items": [
    {
      "id": "c6f15b762eb44dbe10f2625bb4ddaec9ce1f11aa324aca2df9a530ac11a150a0",
      "time": "2026-11-13T16:56:45Z",
      "sender": "mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu",
      "receiver": "mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu",
      "amount": "3561478",
      "fee": "522",
      "status": "final",
      "chain": "bitcoin",
      "network": "testnet4",
      "native": {"version": 1, "vsize": 519, "fee_rate": 1.0057803468208093, ...}
    }
  ]
}
```

Amounts and fees in transactions are text, as in exports, because JSON readers such as
JavaScript lose integers past 2**53. Metrics are the exact JSON `metrics` prints.

## Read-only

By default no request reaches the network or writes to the database. An account is answered
from the state `index account` last stored for it; an account never looked up gets HTTP 404
with the command to run first:

```bash
multichain-indexer --chain bitcoin --network testnet4 index account mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu
```

`serve --live-accounts`, off by default, lets account lookups read the chain and keep the new
state, exactly as `index account` does. That is the only way a request can reach a public
endpoint or write to the database. Account lookups read a SQLite database through a read-only
connection. When `serve` starts it brings the database schema up to date once, as every command
does; a database already at the current schema is not changed.

## Safety

- Every route is a GET.
- It listens on 127.0.0.1. `--host` changes that, and the command then warns that anyone who can
  reach the address can read the database.
- Each client may send 120 requests a minute (`--requests-per-minute`); more get HTTP 429 with
  `Retry-After`. With `--live-accounts`, account lookups reach public endpoints, which have
  limits of their own.
- Errors carry the same one-line messages as the command line: 400 for bad input, 404 for an
  unknown chain or record, 502 when a public endpoint fails.
