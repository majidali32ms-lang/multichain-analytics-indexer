"""The record a post-quantum detector returns."""

from __future__ import annotations

from pydantic import Field

from multichain_indexer.core.models import NativeData


class PqSignature(NativeData):
    """A post-quantum signature scheme found on a transaction or an account.

    ``scheme`` names the scheme in the usual way, such as ``falcon-1024`` or ``ml-dsa-65``;
    ``evidence`` says what in the chain's own data showed it.
    """

    scheme: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
