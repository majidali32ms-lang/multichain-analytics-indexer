"""The template chain's own fields, carried in each record's ``native`` block."""

from __future__ import annotations

from pydantic import Field

from multichain_indexer.core.models import NativeData


class ExampleTransaction(NativeData):
    height: int = Field(ge=0)
    memo: str | None = None


class ExampleAccount(NativeData):
    """Nothing beyond the shared fields; a real chain adds its own here."""
