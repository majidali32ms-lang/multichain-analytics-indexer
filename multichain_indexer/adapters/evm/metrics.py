"""Metrics that only make sense on EVM chains. Fees are in wei and in the chain's gas token."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal

from pydantic import JsonValue

from multichain_indexer.adapters.evm.models import EvmTransaction
from multichain_indexer.core.models import TransactionRecord
from multichain_indexer.pq import pq_share

TOP_COUNT = 10
WEI_DECIMALS = 18


def evm_metrics(
    records: Sequence[TransactionRecord], *, native_symbol: str, token_standard: str
) -> dict[str, JsonValue]:
    """Return gas, fee, token and contract figures for ``records``.

    The fee split shows what the L2 execution cost and what the L1 data fee added (OP
    Mainnet, Base), and the blob fee paid for EIP-4844 blobs; on Arbitrum the L1 part is
    already inside the gas used, and its share of the gas is reported instead.
    """
    natives = [record.native for record in records if isinstance(record.native, EvmTransaction)]
    fees = sum(record.fee for record in records)
    l1_fees = sum(native.l1_data.l1_fee for native in natives if native.l1_data)
    blob_fees = sum(
        native.blob.blob_gas_used * native.blob.blob_gas_price for native in natives if native.blob
    )
    gas_used = sum(native.gas_used for native in natives)
    return {
        "gas_used": gas_used,
        f"fees_{native_symbol.lower()}": _whole(fees),
        "fees_wei": {
            "total": fees,
            "execution": sum(native.execution_fee for native in natives),
            "l1_data": l1_fees,
            "blob": blob_fees,
        },
        "arbitrum_gas_used_for_l1": _arbitrum_l1_gas(natives, gas_used),
        "transaction_types": dict(sorted(Counter(native.kind for native in natives).items())),
        f"top_{_key(token_standard)}_tokens_by_transfers": _ranked(
            "token",
            Counter(
                transfer.token
                for native in natives
                for transfer in native.token_transfers
                if transfer.kind == "fungible"
            ),
        ),
        "top_contracts_by_calls": _ranked(
            "contract",
            Counter(
                record.receiver
                for record in records
                if isinstance(record.native, EvmTransaction)
                and record.native.method_selector
                and record.receiver
            ),
        ),
        "post_quantum": pq_share(records),
    }


def _arbitrum_l1_gas(natives: Sequence[EvmTransaction], gas_used: int) -> JsonValue:
    measured = [native.arbitrum.gas_used_for_l1 for native in natives if native.arbitrum]
    if not measured:
        return None
    total = sum(measured)
    return {"gas": total, "share_of_gas_used": total / gas_used if gas_used else None}


def _whole(wei: int) -> str:
    text = format(Decimal(wei).scaleb(-WEI_DECIMALS), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _key(token_standard: str) -> str:
    return token_standard.lower().replace("-", "")


def _ranked(name: str, counts: Counter[str]) -> list[JsonValue]:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_COUNT]
    return [{name: key, "count": value} for key, value in ranked]
