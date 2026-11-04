# Contributing

## Setup

The project uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Checks

All of these must pass before a pull request is merged:

```bash
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Tests use saved responses and need no network. To run the tests that talk to a public
test network:

```bash
uv run pytest -m live --no-cov
```

## Code

- One function does one thing, and its name says what that is.
- Full type hints. `mypy --strict` passes on the package.
- Comments explain why, not what.
- Use each chain's own SDK for addresses and encoding. Do not re-implement them.
- Test the failure paths too: bad input, timeouts, rate limits, malformed responses.

To build the docs site:

```bash
uv sync --group docs
uv run mkdocs build --strict
```

## Adding fixtures

Fixtures in `tests/fixtures/` are real responses from a test network, with notes and
signatures removed. `scripts/capture_algorand_fixtures.py` regenerates Algorand's.

## Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
