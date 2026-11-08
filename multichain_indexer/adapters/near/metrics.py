"""Metrics that only make sense on NEAR."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal

from pydantic import JsonValue

from multichain_indexer.adapters.near.models import NearReceipt, NearTransaction
from multichain_indexer.adapters.near.parser import FUNCTION_CALL, STATUS_FAILURE, STATUS_SUCCESS
from multichain_indexer.core.models import TransactionRecord
from multichain_indexer.pq import pq_share

TOP_COUNT = 10
YOCTO_DECIMALS = 24
GAS_PER_TGAS = 1_000_000_000_000


def near_metrics(records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
    """Return fee, gas, function call and receipt figures for ``records``.

    Function calls come from receipts, so cross-contract calls count. Refund receipts are
    left out of the receipt figures: the protocol sends them, and nearly every transaction
    has one, so they would hide how often real work fails.
    """
    natives = [r.native for r in records if isinstance(r.native, NearTransaction)]
    receipts = [
        receipt for native in natives for receipt in native.receipts if not receipt.is_refund
    ]
    calls = [
        (receipt.receiver_id, action.method_name)
        for receipt in receipts
        for action in receipt.actions
        if action.kind == FUNCTION_CALL and action.method_name is not None
    ]
    fees = sum(record.fee for record in records)
    gas = sum(native.gas_burnt for native in natives)
    return {
        "fees_yoctonear": fees,
        "fees_near": _whole_near(fees),
        "gas_burnt": {"gas": gas, "tgas": gas / GAS_PER_TGAS},
        "top_contracts_by_calls": _ranked(
            ("contract", "calls"), Counter(contract for contract, _ in calls)
        ),
        "function_calls_by_method": _ranked(
            ("method", "calls"), Counter(method for _, method in calls)
        ),
        "receipts": _receipt_outcomes(receipts),
        "post_quantum": pq_share(records),
    }


def _whole_near(yoctonear: int) -> str:
    text = format(Decimal(yoctonear).scaleb(-YOCTO_DECIMALS), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _receipt_outcomes(receipts: Sequence[NearReceipt]) -> dict[str, JsonValue]:
    succeeded = sum(1 for receipt in receipts if receipt.status == STATUS_SUCCESS)
    failed = sum(1 for receipt in receipts if receipt.status == STATUS_FAILURE)
    finished = succeeded + failed
    return {
        "total": len(receipts),
        "succeeded": succeeded,
        "failed": failed,
        "success_rate": succeeded / finished if finished else None,
        "failure_rate": failed / finished if finished else None,
    }


def _ranked(labels: tuple[str, str], counts: Counter[str]) -> list[JsonValue]:
    name, count = labels
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_COUNT]
    return [{name: key, count: value} for key, value in ranked]
