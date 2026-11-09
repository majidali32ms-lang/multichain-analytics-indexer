"""Post-quantum signatures on NEAR.

ML-DSA-65 (FIPS 204) is a third key and signature scheme beside ed25519 and secp256k1 since
nearcore 2.13.0. A transaction carries the full key as ``ml-dsa-65:``; ``view_access_key_list``
lists such a key by its hash, as ``ml-dsa-65-hash:``, because only the hash is stored.
"""

from __future__ import annotations

from collections.abc import Sequence

from multichain_indexer.pq import Detector, PqSignature, first_match

ML_DSA_65 = "ml-dsa-65"
ML_DSA_65_KEY = "ml-dsa-65:"
ML_DSA_65_KEY_HASH = "ml-dsa-65-hash:"


def ml_dsa_65(public_key: str) -> PqSignature | None:
    if public_key.startswith(ML_DSA_65_KEY_HASH):
        return PqSignature(scheme=ML_DSA_65, evidence="key listed by its ml-dsa-65 hash")
    if public_key.startswith(ML_DSA_65_KEY):
        return PqSignature(scheme=ML_DSA_65, evidence="ml-dsa-65 public key")
    return None


KEY_DETECTORS: tuple[Detector[str], ...] = (ml_dsa_65,)


def key_pq(public_key: str) -> PqSignature | None:
    """Return the post-quantum scheme of an access key, if it uses one."""
    return first_match(KEY_DETECTORS, public_key)


def transaction_pq(public_key: str) -> PqSignature | None:
    """Return the post-quantum scheme of the key that signed a transaction, if any."""
    found = key_pq(public_key)
    if found is None:
        return None
    return PqSignature(scheme=found.scheme, evidence=f"signed with an {found.scheme} key")


def account_pq(keys: Sequence[PqSignature | None]) -> PqSignature | None:
    """Summarise an account's access keys: post-quantum if any key is."""
    found = [key for key in keys if key is not None]
    if not found:
        return None
    schemes = sorted({key.scheme for key in found})
    return PqSignature(
        scheme=", ".join(schemes),
        evidence=f"{len(found)} of {len(keys)} access keys use {', '.join(schemes)}",
    )
