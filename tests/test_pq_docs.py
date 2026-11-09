"""The docs page's status table must say exactly what ``pq/status.py`` says."""

from __future__ import annotations

from pathlib import Path

from multichain_indexer.pq import PQ_STATUS

PAGE = Path(__file__).parent.parent / "docs" / "post-quantum.md"


def status_table() -> str:
    lines = [
        "| Chain | Network | Status | Scheme | Since | Note | Source | Checked on |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in PQ_STATUS:
        cells = (
            row.chain,
            row.network,
            row.state.value,
            row.scheme or "",
            row.since,
            row.note,
            f"[source]({row.source})",
            row.checked_on,
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def test_docs_page_holds_the_status_table_as_it_is() -> None:
    assert status_table() in PAGE.read_text(encoding="utf-8")
