"""Whether a quantum computer could reach an address's unspent outputs, by one stated rule.

Reading a public key is enough for Shor's algorithm; a hash of a key is not. So an output is
exposed when its public key is on chain:

- P2PK and P2TR: exposed by script type. A P2PK output holds the public key, and a P2TR output
  key is a public key that can spend by the key path (BIP-341).
- P2PKH and P2WPKH: exposed once the address has spent in a confirmed block, because every
  spend shows the key, and all of an address's outputs share it.
- P2SH and P2WSH: "script revealed" once the address has spent in a confirmed block. The spend
  shows the script, which may hold public keys or only hashes; the script is not read, so
  these are not called exposed.

Only the address's confirmed outputs are counted, from the source's totals when asked: a
spend still in the mempool has already shown the key, but it counts once it is confirmed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from multichain_indexer.adapters.bitcoin.history import OutputTotals
from multichain_indexer.adapters.bitcoin.models import QuantumExposure
from multichain_indexer.adapters.bitcoin.scripts import P2PK, P2PKH, P2SH, P2TR, P2WPKH, P2WSH

EXPOSED = "exposed"
SCRIPT_REVEALED = "script revealed"
NOT_EXPOSED = "not exposed"

KEY_IN_OUTPUT = frozenset({P2PK, P2TR})
KEY_HASHED = frozenset({P2PKH, P2WPKH})
SCRIPT_HASHED = frozenset({P2SH, P2WSH})


def quantum_exposure(
    address_type: str, confirmed: OutputTotals, *, as_of_height: int, source: str
) -> QuantumExposure:
    """Apply the rule to an address's confirmed totals, read at ``as_of_height``."""
    status, reason = _status(address_type, has_spent=confirmed.spent_count > 0)
    return QuantumExposure(
        status=status,
        reason=reason,
        utxo_count=confirmed.funded_count - confirmed.spent_count,
        utxo_value=confirmed.funded_sum - confirmed.spent_sum,
        as_of_height=as_of_height,
        observed_at=datetime.now(UTC),
        source=source,
    )


def _status(address_type: str, *, has_spent: bool) -> tuple[str, str]:
    if address_type in KEY_IN_OUTPUT:
        return EXPOSED, f"the public key is in every {address_type.upper()} output"
    if address_type in KEY_HASHED and has_spent:
        return EXPOSED, "a confirmed spend revealed the public key"
    if address_type in SCRIPT_HASHED and has_spent:
        return SCRIPT_REVEALED, "a confirmed spend revealed the script"
    return NOT_EXPOSED, "only a hash is on chain: no confirmed spend yet"
