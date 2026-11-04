"""Optional on-disk cache for responses that can never change, such as finalized history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, body TEXT NOT NULL)"


class ResponseCache:
    """Stores response bodies by request key in a SQLite file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(_SCHEMA)

    def get(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT body FROM responses WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def put(self, key: str, body: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO responses (key, body) VALUES (?, ?)", (key, body)
            )

    def close(self) -> None:
        self._connection.close()
