"""Post-quantum signatures: what each chain's detectors found, and where each chain stands.

Each chain adapter has its own detectors, which return a ``PqSignature`` when a transaction or
account uses a post-quantum scheme. Adding a scheme means adding a detector to that adapter
and a row to ``status.py``; nothing in ``core/`` changes.
"""

from multichain_indexer.pq.detection import Detector, first_match
from multichain_indexer.pq.metrics import pq_share
from multichain_indexer.pq.models import PqSignature
from multichain_indexer.pq.status import PQ_STATUS, ChainPqStatus, PqState

__all__ = [
    "PQ_STATUS",
    "ChainPqStatus",
    "Detector",
    "PqSignature",
    "PqState",
    "first_match",
    "pq_share",
]
