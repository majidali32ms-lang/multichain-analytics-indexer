"""Metrics that only make sense on Algorand."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pydantic import JsonValue

from multichain_indexer.adapters.algorand.models import AlgorandTransaction, AsaAction
from multichain_indexer.core.models import TransactionRecord

TOP_ASA_COUNT = 10


def algorand_metrics(records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
    """Return ASA, application, group and inner-transaction figures for ``records``."""
    natives = [r.native for r in records if isinstance(r.native, AlgorandTransaction)]
    asa_transfers = Counter(
        native.asa_id
        for native in natives
        if native.asa_action is AsaAction.TRANSFER and native.asa_id is not None
    )
    grouped = sum(1 for native in natives if native.group)
    return {
        "fees_microalgos": sum(record.fee for record in records),
        "app_calls": sum(1 for native in natives if native.app_id is not None),
        "asa_opt_ins": sum(1 for native in natives if native.asa_action is AsaAction.OPT_IN),
        "group_transaction_share": grouped / len(natives) if natives else None,
        "inner_transactions": sum(native.inner_count for native in natives),
        "top_asas_by_transfers": _top_asas(asa_transfers),
    }


def _top_asas(transfers: Counter[int]) -> list[JsonValue]:
    ranked = sorted(transfers.items(), key=_most_transfers_first)[:TOP_ASA_COUNT]
    return [{"asa_id": asa_id, "transfers": count} for asa_id, count in ranked]


def _most_transfers_first(item: tuple[int, int]) -> tuple[int, int]:
    asa_id, count = item
    return -count, asa_id
