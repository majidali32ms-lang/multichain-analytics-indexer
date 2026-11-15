# Storage, PostgreSQL and Docker

## SQLite, the default

Records are kept in a SQLite file, `~/.multichain-indexer/indexer.db` unless `--db`,
`MCI_DATABASE` or the `database` argument says otherwise. Every table is keyed by chain and
network as well as by transaction, so a testnet sync never touches a mainnet one.

## Migrations

Every database is brought to the current schema with Alembic before anything reads it. A
database written by 0.6.0 or earlier has the schema these versions all used, and no version
table; it is stamped at that first revision without touching a row. The tests rebuild a
database the released 0.6.0 package wrote, from its full SQL dump, migrate it, and check every
row is still there and that it syncs on from its checkpoints.

## PostgreSQL

```bash
pip install 'multichain-analytics-indexer[postgres]'
```

```bash
multichain-indexer --db postgresql://indexer@localhost:5432/indexer --chain bitcoin sync tb1q...
```

or set `MCI_DATABASE_URL` in `.env`, which is where a URL with a password belongs. The URL's
password never appears in logs or error messages, and a connection gives up after 10
seconds. PostgreSQL keeps the same tables in its own types, so they can be queried directly:
amounts and fees are `NUMERIC(78, 0)`, exact for any 256-bit value; times are `TIMESTAMPTZ`;
each chain's `native` block is `JSONB`.

```sql
SELECT chain, network, count(*), sum(fee)
FROM transactions
WHERE native->>'segwit' = 'true'
GROUP BY chain, network;
```

The same storage tests run on SQLite and, in CI, on PostgreSQL 18.7, along with a sync whose
records and metrics must equal SQLite's.

## Docker

The image holds the command line with the `postgres` and `serve` extras, on
`python:3.14-slim` pinned by digest, and runs as an unprivileged user. `docker-compose.yml`
adds PostgreSQL 18.7:

```bash
echo "POSTGRES_PASSWORD=choose-one" >> .env
docker compose up -d
docker compose run --rm indexer --chain bitcoin sync mgdKMwnT4S5gbx55Uw3tRJchUSrHwAFjzu
curl http://127.0.0.1:8000/chains
```

The HTTP API is published on this machine's `127.0.0.1:8000` only. Because `serve` never
migrates, the compose file runs an ordinary command first, which brings the schema up to date.
API keys in `.env` reach the indexer container; the database password comes from
`POSTGRES_PASSWORD`. A CI job builds
the image, starts both services, and checks the API, a cross-chain command and the migrated
schema.
