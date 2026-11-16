"""A clean PostgreSQL database for each test, when ``MCI_TEST_DATABASE_URL`` names one.

CI runs these tests against a ``postgres:18.7`` service; without the setting they skip.
"""

from __future__ import annotations

import os

import pytest

TEST_DATABASE_URL = "MCI_TEST_DATABASE_URL"
TABLES = ("account_transactions", "snapshots", "sync_state", "transactions", "alembic_version")


def postgres_url() -> str:
    """Return the test database's URL with every table dropped, or skip the test."""
    url = os.environ.get(TEST_DATABASE_URL)
    if not url:
        pytest.skip(f"set {TEST_DATABASE_URL} to run the PostgreSQL tests")
    import psycopg

    with psycopg.connect(url, autocommit=True) as connection:
        for table in TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
    return url
