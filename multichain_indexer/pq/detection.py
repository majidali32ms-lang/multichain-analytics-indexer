"""The shape of a post-quantum detector, and how a chain runs its own."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from multichain_indexer.pq.models import PqSignature

_ItemT = TypeVar("_ItemT")

Detector = Callable[[_ItemT], PqSignature | None]


def first_match(detectors: Iterable[Detector[_ItemT]], item: _ItemT) -> PqSignature | None:
    """Return what the first detector to recognise ``item`` found, or None."""
    for detector in detectors:
        found = detector(item)
        if found is not None:
            return found
    return None
