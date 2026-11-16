"""The cross-chain comparison report as CSV, JSON or one offline HTML file.

The HTML file has no scripts and loads nothing: its charts are inline SVG drawn from the
same rows the CSV and JSON carry, so every number in it is one ``metrics --chain all``
prints.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path

import pandas as pd

from multichain_indexer.core.errors import InvalidInput
from multichain_indexer.crosschain import ChainMetrics, CrossChainReport

REPORT_FORMATS = ("html", "csv", "json")
CHART_WIDTH = 640
BAR_HEIGHT = 22
BAR_GAP = 8
LABEL_WIDTH = 190
VALUE_WIDTH = 90
DAILY_HEIGHT = 60
MAX_DAY_WIDTH = 24
CENT = Decimal("0.01")

_STYLE = """
:root { --ink: #1f2328; --muted: #59636e; --line: #d1d9e0; --bar: #3b6ea8; --bg: #ffffff;
        --error: #b42318; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #e6edf3; --muted: #9198a1; --line: #3d444d; --bar: #6ea8e0; --bg: #0d1117;
          --error: #f47067; }
}
body { font: 15px/1.5 system-ui, sans-serif; color: var(--ink); background: var(--bg);
       max-width: 960px; margin: 2rem auto; padding: 0 16px; }
h1 { font-size: 1.5rem; } h2 { font-size: 1.15rem; margin-top: 2rem; }
p.note { color: var(--muted); }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
td.error { color: var(--error); text-align: left; }
svg { max-width: 100%; height: auto; }
svg text { fill: var(--ink); font: 13px system-ui, sans-serif; }
svg rect.bar { fill: var(--bar); }
"""


def write_report(
    report: CrossChainReport, report_format: str, output: Path | None = None
) -> str | None:
    """Write ``report`` to ``output``, or return the text if ``output`` is None.

    Raises:
        InvalidInput: The format is unknown.
    """
    if report_format not in REPORT_FORMATS:
        raise InvalidInput(f"unknown report format {report_format!r}")
    text = _render(report, report_format)
    if output is None:
        return text
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return None


def metrics_table(report: CrossChainReport) -> list[str]:
    """Return the side-by-side table ``metrics --chain all`` prints, one line per row."""
    header = ("Chain", "Transactions", "Per day", "Active", "Fees", "Fees (USD)", "Failed")
    lines = [header, *(_table_cells(row) for row in report.rows)]
    # A failed row is its label and one error message, which must not widen the columns.
    full = [line for line in lines if len(line) == len(header)]
    widths = [max(len(line[column]) for line in full) for column in range(len(header))]
    widths[0] = max(len(line[0]) for line in lines)
    rendered = []
    for line in lines:
        if len(line) != len(header):
            rendered.append(f"{line[0].ljust(widths[0])}  {line[1]}")
            continue
        cells = [
            line[0].ljust(widths[0]),
            *(cell.rjust(width) for cell, width in zip(line[1:], widths[1:], strict=True)),
        ]
        rendered.append("  ".join(cells))
    rendered.append(
        f"Last {report.days} days. US dollars only for mainnet, at the price in config.toml."
    )
    return rendered


def _table_cells(row: ChainMetrics) -> tuple[str, ...]:
    if row.error is not None:
        return (_label(row), f"error: {row.error}")
    failure = "n/a" if row.failure_rate is None else f"{row.failure_rate:.1%}"
    return (
        _label(row),
        str(row.transactions),
        f"{row.transactions_per_day:g}",
        str(row.active_accounts),
        row.total_fees_display,
        _usd(row.fees_usd),
        failure,
    )


def report_frame(report: CrossChainReport) -> pd.DataFrame:
    """Return one row per chain and network; daily counts are a JSON column."""
    rows = [row.model_dump(mode="json") for row in report.rows]
    frame = pd.DataFrame(rows, columns=list(ChainMetrics.model_fields))
    frame["daily_transactions"] = frame["daily_transactions"].map(json.dumps)
    return frame


def render_html(report: CrossChainReport) -> str:
    """Return the report as one HTML page that needs no network to open."""
    working = [row for row in report.rows if row.error is None]
    sections = [
        f"<h1>Cross-chain report, last {report.days} days</h1>",
        f'<p class="note">Generated {report.generated_at:%Y-%m-%d %H:%M} UTC from the local '
        "database. Fees are in each chain's own unit; US dollars only for mainnet, at the "
        "price and date given in config.toml.</p>",
        "<h2>Summary</h2>",
        _summary_table(report.rows),
        "<h2>Transactions</h2>",
        _bar_chart([(_label(row), row.transactions, str(row.transactions)) for row in working]),
        "<h2>Active accounts</h2>",
        _bar_chart(
            [(_label(row), row.active_accounts, str(row.active_accounts)) for row in working]
        ),
    ]
    priced = [row for row in working if row.fees_usd is not None]
    if priced:
        sections += [
            "<h2>Fees in US dollars</h2>",
            _bar_chart([(_label(row), row.fees_usd or 0, _usd(row.fees_usd)) for row in priced]),
        ]
    sections.append("<h2>Transactions per day</h2>")
    sections += [_daily_chart(row) for row in working if row.daily_transactions]
    body = "\n".join(sections)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Cross-chain report</title><style>{_STYLE}</style></head>"
        f"<body>\n{body}\n</body></html>\n"
    )


def _render(report: CrossChainReport, report_format: str) -> str:
    if report_format == "html":
        return render_html(report)
    frame = report_frame(report)
    if report_format == "csv":
        return str(frame.to_csv(index=False))
    return json.dumps(report.model_dump(mode="json"), indent=2)


def _summary_table(rows: Sequence[ChainMetrics]) -> str:
    header = (
        "<tr><th>Chain</th><th>Transactions</th><th>Per day</th><th>Active accounts</th>"
        "<th>Fees</th><th>Fees (USD)</th><th>Failure rate</th></tr>"
    )
    lines = [_summary_line(row) for row in rows]
    return f"<table>\n{header}\n" + "\n".join(lines) + "\n</table>"


def _summary_line(row: ChainMetrics) -> str:
    if row.error is not None:
        error = f'<td class="error" colspan="6">{escape(row.error)}</td>'
        return f"<tr><td>{escape(_label(row))}</td>{error}</tr>"
    failure = "n/a" if row.failure_rate is None else f"{row.failure_rate:.1%}"
    usd = "n/a" if row.fees_usd is None else f"{_usd(row.fees_usd)} at {_price(row)}"
    cells = (
        _label(row),
        str(row.transactions),
        f"{row.transactions_per_day:g}",
        str(row.active_accounts),
        row.total_fees_display,
        usd,
        failure,
    )
    return "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in cells) + "</tr>"


def _bar_chart(items: Sequence[tuple[str, int | Decimal, str]]) -> str:
    if not items:
        return '<p class="note">No data.</p>'
    largest = max(float(value) for _, value, _ in items) or 1.0
    span = CHART_WIDTH - LABEL_WIDTH - VALUE_WIDTH
    height = len(items) * (BAR_HEIGHT + BAR_GAP)
    parts = []
    for position, (label, value, text) in enumerate(items):
        top = position * (BAR_HEIGHT + BAR_GAP)
        width = span * float(value) / largest
        middle = top + BAR_HEIGHT * 0.7
        parts.append(
            f'<text x="0" y="{middle:.1f}">{escape(label)}</text>'
            f'<rect class="bar" x="{LABEL_WIDTH}" y="{top}" width="{width:.1f}" '
            f'height="{BAR_HEIGHT}"><title>{escape(label)}: {escape(text)}</title></rect>'
            f'<text x="{LABEL_WIDTH + width + 6:.1f}" y="{middle:.1f}">{escape(text)}</text>'
        )
    return (
        f'<svg viewBox="0 0 {CHART_WIDTH} {height}" role="img" '
        f'aria-label="bar chart">{"".join(parts)}</svg>'
    )


def _daily_chart(row: ChainMetrics) -> str:
    days = _every_day(row.daily_transactions)
    largest = max(row.daily_transactions.values())
    step = min(CHART_WIDTH / len(days), MAX_DAY_WIDTH)
    bars = []
    for position, day in enumerate(days):
        count = row.daily_transactions.get(day, 0)
        height = DAILY_HEIGHT * count / largest
        bars.append(
            f'<rect class="bar" x="{position * step:.1f}" y="{DAILY_HEIGHT - height:.1f}" '
            f'width="{max(step - 1, 0.5):.1f}" height="{height:.1f}">'
            f"<title>{day}: {count}</title></rect>"
        )
    caption = f"{_label(row)}: {days[0]} to {days[-1]}, busiest day {largest}"
    return (
        f'<p class="note">{escape(caption)}</p>'
        f'<svg viewBox="0 0 {CHART_WIDTH} {DAILY_HEIGHT}" role="img" '
        f'aria-label="{escape(caption)}">{"".join(bars)}</svg>'
    )


def _every_day(daily: dict[str, int]) -> list[str]:
    first, last = date.fromisoformat(min(daily)), date.fromisoformat(max(daily))
    return [
        (first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
    ]


def _label(row: ChainMetrics) -> str:
    return f"{row.chain} {row.network}"


def _usd(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    if value and abs(value) < CENT:
        return f"${value:.6f}"
    return f"${value.quantize(CENT):,}"


def _price(row: ChainMetrics) -> str:
    return f"${row.price_usd} on {row.price_date}"
