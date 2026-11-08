from __future__ import annotations

from datetime import UTC, datetime

from multichain_indexer.core.models import NativeData, Status, TransactionRecord
from multichain_indexer.core.registry import PLANNED_CHAINS
from multichain_indexer.pq import PQ_STATUS, PqSignature, PqState, first_match, pq_share

FALCON = PqSignature(scheme="falcon-1024", evidence="pqsig f1")
ML_DSA = PqSignature(scheme="ml-dsa-65", evidence="ml-dsa-65 key")


class _WithPq(NativeData):
    pq: PqSignature | None = None


class _WithoutPq(NativeData):
    pass


def record(native: NativeData, number: int) -> TransactionRecord:
    return TransactionRecord(
        id=f"tx{number}",
        time=datetime(2026, 11, 8, tzinfo=UTC),
        sender="a",
        amount=0,
        fee=0,
        status=Status.FINAL,
        chain="test",
        network="testnet",
        native=native,
    )


def test_first_match_returns_the_first_detector_that_recognises_the_item() -> None:
    def never(item: str) -> PqSignature | None:
        return None

    def falcon(item: str) -> PqSignature | None:
        return FALCON if item == "f1" else None

    def ml_dsa(item: str) -> PqSignature | None:
        return ML_DSA

    assert first_match([never, falcon, ml_dsa], "f1") == FALCON
    assert first_match([never, falcon, ml_dsa], "other") == ML_DSA
    assert first_match([never, falcon], "other") is None
    assert first_match([], "f1") is None


def test_pq_share_counts_per_scheme() -> None:
    records = [
        record(_WithPq(pq=FALCON), 1),
        record(_WithPq(pq=FALCON), 2),
        record(_WithPq(pq=ML_DSA), 3),
        record(_WithPq(), 4),
        record(_WithoutPq(), 5),
    ]
    assert pq_share(records) == {
        "transactions": 3,
        "share": 3 / 5,
        "schemes": {"falcon-1024": 2, "ml-dsa-65": 1},
    }


def test_pq_share_of_nothing() -> None:
    assert pq_share([]) == {"transactions": 0, "share": None, "schemes": {}}
    assert pq_share([record(_WithoutPq(), 1)])["share"] == 0


def test_status_covers_every_planned_chain_with_a_source_and_a_date() -> None:
    assert {row.chain for row in PQ_STATUS} == set(PLANNED_CHAINS)
    for row in PQ_STATUS:
        assert row.source.startswith("https://")
        assert datetime.strptime(row.checked_on, "%Y-%m-%d")
        assert row.note


def test_only_chains_with_a_detector_claim_anything() -> None:
    for row in PQ_STATUS:
        if row.state is PqState.NONE_LIVE:
            assert row.scheme is None
            assert not row.detected
        else:
            assert row.scheme
            assert row.detected
            assert row.since


def test_no_chain_without_an_adapter_is_called_live() -> None:
    adapters = {"algorand", "solana", "near"}
    assert {row.chain for row in PQ_STATUS if row.state is not PqState.NONE_LIVE} <= adapters
