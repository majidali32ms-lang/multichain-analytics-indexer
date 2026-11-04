"""Structured logging with secrets masked on the handler.

Masking lives on the handler rather than at call sites because the dangerous log line
is the one somebody forgot to scrub.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

MASK = "***"
MINIMUM_SECRET_LENGTH = 4

_STANDARD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_URL = re.compile(r"https?://[^\s'\"<>]+")


def redact_url(url: str) -> str:
    """Keep the scheme and host; drop credentials, path, query and fragment."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    has_detail = parts.path.strip("/") or parts.query or parts.fragment
    return urlunsplit((parts.scheme, host, "", "", "")) + (f"/{MASK}" if has_detail else "")


class RedactingFilter(logging.Filter):
    """Masks known secrets and URL details in the message and extra fields."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        known = {secret for secret in secrets if len(secret) >= MINIMUM_SECRET_LENGTH}
        self._secrets = sorted(known, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.getMessage())
        record.args = None
        for key, value in _extra_fields(record).items():
            if isinstance(value, str):
                setattr(record, key, self._scrub(value))
        return True

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return _URL.sub(lambda match: redact_url(match.group(0)), text)


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Records the exception type, never the frames."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **_extra_fields(record),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["error"] = record.exc_info[0].__name__
        return json.dumps(entry, default=str)


def configure_logging(
    *, verbose: bool = False, quiet: bool = False, secrets: Iterable[str] = ()
) -> None:
    """Send structured logs to stderr, replacing any handlers already installed."""
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter(secrets))
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(_level(verbose=verbose, quiet=quiet))


def _level(*, verbose: bool, quiet: bool) -> int:
    if quiet:
        return logging.ERROR
    return logging.INFO if verbose else logging.WARNING


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRIBUTES}
