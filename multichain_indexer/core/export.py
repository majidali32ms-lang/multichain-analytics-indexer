"""Writes stored transactions as CSV, JSON or Parquet."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from multichain_indexer.core.errors import InvalidInput
from multichain_indexer.core.metrics import transactions_frame
from multichain_indexer.core.models import TransactionRecord

EXPORT_FORMATS = ("csv", "json", "parquet")


def export_transactions(
    records: Sequence[TransactionRecord], export_format: str, output: Path | None = None
) -> str | None:
    """Write ``records`` to ``output``, or return the text if ``output`` is None.

    Returns:
        The exported text when no path is given, otherwise None.

    Raises:
        InvalidInput: The format is unknown, or Parquet was requested without a path.
    """
    if export_format not in EXPORT_FORMATS:
        raise InvalidInput(f"unknown export format {export_format!r}")
    if export_format == "parquet" and output is None:
        raise InvalidInput("Parquet is a binary format; choose a file with --output")

    frame = _flat_frame(records)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
    return _write(frame, export_format, output)


def _write(frame: pd.DataFrame, export_format: str, output: Path | None) -> str | None:
    if export_format == "parquet":
        frame.to_parquet(output, index=False)
        return None
    if export_format == "csv":
        return _deliver(frame.to_csv(index=False), output)
    return _deliver(frame.to_json(orient="records", indent=2, date_format="iso"), output)


def _deliver(text: str, output: Path | None) -> str | None:
    if output is None:
        return text
    output.write_text(text, encoding="utf-8")
    return None


def _flat_frame(records: Sequence[TransactionRecord]) -> pd.DataFrame:
    # Text columns keep amounts past 64 bits exact in every format and every reader.
    frame = transactions_frame(records)
    frame["amount"] = frame["amount"].map(str)
    frame["fee"] = frame["fee"].map(str)
    frame["native"] = frame["native"].map(json.dumps)
    return frame
