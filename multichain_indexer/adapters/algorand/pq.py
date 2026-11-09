"""Post-quantum signatures on Algorand.

Only native post-quantum accounts count: a transaction authorised by ``pqsig`` names its
scheme, and ``f1`` is Falcon-1024 (consensus v42, ``EnablePQSchemeFalcon1024``). LogicSigs
that call ``falcon_verify`` are possible but cannot be told apart from the program bytes
without parsing TEAL, so they are not identified. State proof transactions are protocol
messages, never an account's own transaction, and never count.
"""

from __future__ import annotations

from dataclasses import dataclass

from multichain_indexer.pq import Detector, PqSignature, first_match

STATE_PROOF = "stpf"
PQ_SIGNATURE_TYPE = "pqsig"
# Schemes the network has enabled. A code not listed here is reported as it is, never as a
# scheme the network is known to run.
SCHEMES = {"f1": "falcon-1024"}


@dataclass(frozen=True)
class SignedTransaction:
    """What a detector needs from a transaction: its type and its pqsig scheme, if any."""

    tx_type: str
    pq_scheme: str | None


def native_falcon(transaction: SignedTransaction) -> PqSignature | None:
    if transaction.tx_type == STATE_PROOF or transaction.pq_scheme is None:
        return None
    code = transaction.pq_scheme
    return PqSignature(
        scheme=SCHEMES.get(code, f"pqsig scheme {code}"),
        evidence=f"signed with pqsig, scheme {code}",
    )


TRANSACTION_DETECTORS: tuple[Detector[SignedTransaction], ...] = (native_falcon,)


def transaction_pq(tx_type: str, pq_scheme: str | None) -> PqSignature | None:
    """Return the post-quantum scheme that authorised a transaction, if any."""
    return first_match(TRANSACTION_DETECTORS, SignedTransaction(tx_type, pq_scheme))


def account_pq(signature_type: str | None) -> PqSignature | None:
    """Return what the Indexer says about an account's signature, if it is post-quantum.

    The account record names the kind, ``pqsig``, but not the scheme; its transactions do.
    """
    if signature_type != PQ_SIGNATURE_TYPE:
        return None
    return PqSignature(scheme=PQ_SIGNATURE_TYPE, evidence="the Indexer lists sig-type pqsig")
