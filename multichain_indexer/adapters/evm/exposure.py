"""Whether an EVM account has revealed its public key, as of the block it was read at.

An externally owned account's address is a hash of its secp256k1 public key; the key itself
appears on chain with the account's first signature, which every transaction it sends carries.
A nonce above zero proves at least one sent transaction (an EIP-7702 authorization also raises
the signer's nonce), so the key is public. The rule is a lower bound: a permit or other EIP-712
signature that someone else submits reveals the key while the nonce is still zero. A contract
account has no private key; an EIP-7702 delegated account signed its delegation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

from multichain_indexer.adapters.evm.models import EvmAccount, EvmExposure, EvmExposureReport

REVEALED = "public key revealed (nonce > 0)"
DELEGATED = "public key revealed (EIP-7702 delegated account)"
CONTRACT = "contract account (no private key)"
NOT_SEEN = "no transaction sent yet (nonce 0)"
STATUSES = (REVEALED, DELEGATED, CONTRACT, NOT_SEEN)
LOWER_BOUND_NOTE = (
    "A lower bound: a permit or other EIP-712 signature used on chain by someone else reveals "
    "a key while the nonce is still 0."
)


def key_exposure(*, nonce: int, is_contract: bool, delegated_to: str | None) -> str:
    if delegated_to is not None:
        return DELEGATED
    if is_contract:
        return CONTRACT
    return REVEALED if nonce > 0 else NOT_SEEN


def exposure_report(
    *, chain: str, network: str, accounts: Sequence[EvmAccount], addresses: Sequence[str]
) -> EvmExposureReport:
    """Summarise accounts read at query time; the share counts accounts that can sign."""
    rows = tuple(
        EvmExposure(
            address=address,
            status=key_exposure(
                nonce=account.nonce,
                is_contract=account.is_contract,
                delegated_to=account.delegated_to,
            ),
            block_number=account.block_number,
        )
        for address, account in zip(addresses, accounts, strict=True)
    )
    counts = Counter(row.status for row in rows)
    signers = len(rows) - counts[CONTRACT]
    revealed = counts[REVEALED] + counts[DELEGATED]
    blocks = [row.block_number for row in rows]
    return EvmExposureReport(
        chain=chain,
        network=network,
        from_block=min(blocks, default=0),
        to_block=max(blocks, default=0),
        observed_at=datetime.now(UTC),
        accounts=rows,
        counts={status: counts[status] for status in STATUSES},
        revealed_share=revealed / signers if signers else None,
        note=LOWER_BOUND_NOTE,
    )
