"""Metrics that only make sense on Bitcoin. Fees are in sats, fee rates in sat/vB."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from decimal import Decimal
from statistics import fmean, median

from pydantic import JsonValue

from multichain_indexer.adapters.bitcoin.models import BitcoinTransaction
from multichain_indexer.core.models import TransactionRecord

SATS_DECIMALS = 8
OP_RETURN = "op_return"


def bitcoin_metrics(records: Sequence[TransactionRecord]) -> dict[str, JsonValue]:
    """Return fee, fee rate, output type and adoption figures for ``records``.

    Fee rates and the SegWit, Taproot and RBF shares leave coinbase transactions out: they
    pay no fee and spend no earlier output.
    """
    natives = [r.native for r in records if isinstance(r.native, BitcoinTransaction)]
    spending = [native for native in natives if not native.coinbase]
    fees = sum(record.fee for record in records)
    return {
        "fees_sats": fees,
        "fees_btc": _whole_btc(fees),
        "fee_rate_sat_per_vb": _fee_rates(spending),
        "address_types": _output_types(natives),
        "segwit_share": _share(spending, lambda native: native.segwit),
        "taproot_share": _share(spending, lambda native: native.taproot),
        "rbf_share": _share(spending, lambda native: native.rbf),
        "coinbase_transactions": len(natives) - len(spending),
        "op_return_outputs": sum(
            1 for native in natives for out in native.outputs if out.script_type == OP_RETURN
        ),
        "unconfirmed_transactions": sum(1 for native in natives if not native.confirmed),
    }


def _whole_btc(sats: int) -> str:
    text = format(Decimal(sats).scaleb(-SATS_DECIMALS), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fee_rates(natives: Sequence[BitcoinTransaction]) -> dict[str, JsonValue]:
    rates = [native.fee_rate for native in natives]
    return {
        "average": fmean(rates) if rates else None,
        "median": median(rates) if rates else None,
    }


def _output_types(natives: Sequence[BitcoinTransaction]) -> dict[str, JsonValue]:
    """Count outputs by script type, with each type's share of all outputs."""
    counts = Counter(out.script_type for native in natives for out in native.outputs)
    total = sum(counts.values())
    return {
        script_type: {"outputs": count, "share": count / total}
        for script_type, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    }


def _share(
    natives: Sequence[BitcoinTransaction], test: Callable[[BitcoinTransaction], bool]
) -> float | None:
    return sum(1 for native in natives if test(native)) / len(natives) if natives else None
