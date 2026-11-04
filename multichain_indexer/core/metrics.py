"""Chain-agnostic metrics. Adapters add their own through ``BaseIndexer.chain_metrics``."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd
from pydantic import JsonValue

from multichain_indexer.core.models import MetricsReport, Status, TransactionRecord

SHARED_COLUMNS = ("id", "time", "sender", "receiver", "amount", "fee", "status", "chain")


def transactions_frame(records: Sequence[TransactionRecord]) -> pd.DataFrame:
    """Return one row per transaction: the shared fields plus ``native`` as a dict.

    ``amount`` and ``fee`` keep Python integers (object dtype) so values past 64 bits
    stay exact.
    """
    rows = [
        {**record.model_dump(include=set(SHARED_COLUMNS)), "native": record.native.model_dump()}
        for record in records
    ]
    frame = pd.DataFrame(rows, columns=[*SHARED_COLUMNS, "native"])
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    frame["amount"] = frame["amount"].astype(object)
    frame["fee"] = frame["fee"].astype(object)
    return frame


def build_report(
    records: Sequence[TransactionRecord],
    *,
    chain: str,
    network: str,
    days: int,
    account: str | None,
    native: dict[str, JsonValue],
) -> MetricsReport:
    """Summarise ``records``, which the caller has already limited to the window.

    Active accounts counts distinct senders: an account that only receives has not
    done anything on the chain.
    """
    frame = transactions_frame(records)
    return MetricsReport(
        chain=chain,
        network=network,
        days=days,
        account=account,
        transactions=len(frame),
        transactions_per_day=_transactions_per_day(frame),
        active_accounts=int(frame["sender"].nunique()),
        total_fees=sum(record.fee for record in records),
        failure_rate=_failure_rate(frame),
        native=native,
    )


def _transactions_per_day(frame: pd.DataFrame) -> dict[str, int]:
    days = frame["time"].dt.strftime("%Y-%m-%d")
    return {str(day): int(count) for day, count in days.value_counts().sort_index().items()}


def _failure_rate(frame: pd.DataFrame) -> float | None:
    if frame.empty:
        return None
    return float((frame["status"] == Status.FAILED).mean())
