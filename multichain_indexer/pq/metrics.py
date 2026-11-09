"""The post-quantum share of a set of transactions, the same on every chain."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pydantic import JsonValue

from multichain_indexer.core.models import TransactionRecord
from multichain_indexer.pq.models import PqSignature


def pq_share(records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
    """Count the transactions signed with a post-quantum scheme, overall and per scheme.

    A chain's native block carries a ``pq`` field only if it has a detector; records from
    other chains, or stored before detectors existed, count as not post-quantum.
    """
    schemes = Counter(found.scheme for record in records if (found := _pq_of(record)))
    signed = sum(schemes.values())
    return {
        "transactions": signed,
        "share": signed / len(records) if records else None,
        "schemes": dict(sorted(schemes.items())),
    }


def _pq_of(record: TransactionRecord) -> PqSignature | None:
    found = getattr(record.native, "pq", None)
    return found if isinstance(found, PqSignature) else None
