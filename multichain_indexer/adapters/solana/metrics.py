"""Metrics that only make sense on Solana."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from statistics import fmean, median

from pydantic import JsonValue

from multichain_indexer.adapters.solana.models import SolanaTransaction
from multichain_indexer.core.models import TransactionRecord

TOP_COUNT = 10


def solana_metrics(records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
    """Return fee, compute-unit, program and SPL token figures for ``records``."""
    natives = [r.native for r in records if isinstance(r.native, SolanaTransaction)]
    return {
        "fees_lamports": sum(record.fee for record in records),
        "transaction_versions": dict(Counter(native.version for native in natives)),
        "compute_units": _compute_unit_usage(natives),
        "priority_fees_lamports": _priority_fee_stats(natives),
        "program_calls": _ranked(
            ("program_id", "calls"),
            Counter(i.program_id for native in natives for i in native.instructions),
        ),
        "top_spl_tokens_by_transfers": _ranked(
            ("mint", "transfers"),
            Counter(
                transfer.mint
                for native in natives
                for transfer in native.token_transfers
                if transfer.mint is not None
            ),
        ),
    }


def _compute_unit_usage(natives: Sequence[SolanaTransaction]) -> dict[str, JsonValue]:
    consumed = [n.compute_units_consumed for n in natives if n.compute_units_consumed is not None]
    return {"total": sum(consumed), "mean": fmean(consumed) if consumed else None}


def _priority_fee_stats(natives: Sequence[SolanaTransaction]) -> dict[str, JsonValue]:
    fees = [native.priority_fee_lamports for native in natives]
    return {
        "total": sum(fees),
        "mean": fmean(fees) if fees else None,
        "median": median(fees) if fees else None,
        "max": max(fees, default=None),
        "transactions_paying": sum(1 for fee in fees if fee > 0),
    }


def _ranked(labels: tuple[str, str], counts: Counter[str]) -> list[JsonValue]:
    name, count = labels
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_COUNT]
    return [{name: key, count: value} for key, value in ranked]
