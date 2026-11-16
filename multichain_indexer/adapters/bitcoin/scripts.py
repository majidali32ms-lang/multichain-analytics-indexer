"""What a lookup names: an address of one of the five standard types, or a P2PK public key.

Addresses are decoded with embit. embit reads an address without asking which network it
is for, so the address is encoded again with the requested network's prefixes and must come
back the same: a mainnet address on testnet4 is refused here, before any request. testnet4
and signet share their prefixes, so their addresses cannot be told apart.

Esplora finds every script by its script hash, the SHA-256 of the output script, so a P2PK
output, which has no address, is looked up the same way as an address.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import cast

from embit.base import EmbitError
from embit.ec import PublicKey
from embit.networks import NETWORKS as EMBIT_NETWORKS
from embit.script import Script, address_to_scriptpubkey

from multichain_indexer.adapters.bitcoin.networks import NETWORKS, BitcoinNetwork
from multichain_indexer.core.errors import InvalidAddress

P2PKH = "p2pkh"
P2SH = "p2sh"
P2WPKH = "p2wpkh"
P2WSH = "p2wsh"
P2TR = "p2tr"
P2PK = "p2pk"
BECH32_TYPES = frozenset({P2WPKH, P2WSH, P2TR})
OP_CHECKSIG = 0xAC
COMPRESSED_KEY = re.compile(r"0[23][0-9a-fA-F]{64}")
UNCOMPRESSED_KEY = re.compile(r"04[0-9a-fA-F]{128}")


@dataclass(frozen=True)
class Locator:
    """An address or P2PK public key, as written in records, and its output script."""

    text: str
    script_type: str
    script_pubkey: bytes

    @property
    def script_hash(self) -> str:
        return hashlib.sha256(self.script_pubkey).hexdigest()


def parse_locator(text: str, network: BitcoinNetwork) -> Locator:
    """Return the locator for an address on ``network``, or for a P2PK public key in hex.

    Raises:
        InvalidAddress: Not an address of a standard type, an address of another network,
            or a public key that is not a point on the curve.
    """
    if COMPRESSED_KEY.fullmatch(text) or UNCOMPRESSED_KEY.fullmatch(text):
        return _public_key_locator(text)
    return _address_locator(text, network)


def _public_key_locator(text: str) -> Locator:
    key = bytes.fromhex(text)
    try:
        PublicKey.parse(key)
    except EmbitError:
        raise InvalidAddress(f"{text} is not a valid public key") from None
    return Locator(text.lower(), P2PK, bytes([len(key)]) + key + bytes([OP_CHECKSIG]))


def _address_locator(text: str, network: BitcoinNetwork) -> Locator:
    script = _decoded_script(text)
    script_type = cast(str | None, script.script_type()) if script else None
    if script is None or script_type is None:
        raise InvalidAddress(
            f"{text!r} is not a Bitcoin address (P2PKH, P2SH, P2WPKH, P2WSH or P2TR) "
            "or a P2PK public key in hex"
        )
    given = text.lower() if script_type in BECH32_TYPES else text
    if _encoded(script, network) != given:
        raise InvalidAddress(_other_network_message(text, script, network))
    return Locator(given, script_type, cast(bytes, script.data))


def _decoded_script(text: str) -> Script | None:
    script = _embit_script(text)
    if script is not None or not text.isupper():
        return script
    # BIP-173 allows a bech32 address in capitals, but embit compares the human-readable
    # part case-sensitively, so it is decoded in lower case; only a bech32 result counts.
    lowered = _embit_script(text.lower())
    return lowered if lowered and lowered.script_type() in BECH32_TYPES else None


def _embit_script(text: str) -> Script | None:
    try:
        return cast(Script | None, address_to_scriptpubkey(text))
    except EmbitError:
        return None


def _encoded(script: Script, network: BitcoinNetwork) -> str:
    return cast(str, script.address(EMBIT_NETWORKS[network.embit_name]))


def _other_network_message(text: str, script: Script, network: BitcoinNetwork) -> str:
    given = text.lower()
    owners = [
        other.name
        for other in NETWORKS.values()
        if other is not network and _encoded(script, other).lower() == given
    ]
    if not owners:
        return f"{text} is not an address on Bitcoin {network.name}"
    return f"{text} is an address on Bitcoin {' or '.join(owners)}, not {network.name}"
