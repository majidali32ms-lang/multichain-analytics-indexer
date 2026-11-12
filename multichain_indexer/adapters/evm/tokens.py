"""Reads an ERC-20 token's metadata with ``eth_call``, decoded by web3.py's ``eth_abi``.

EIP-20 makes ``name``, ``symbol`` and ``decimals`` optional and ``totalSupply`` required, so a
contract without ``totalSupply`` is not a token, while a missing name or symbol is recorded as
missing. Some early tokens return ``bytes32`` instead of ``string`` for the name and symbol;
both are read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from eth_abi.abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils.abi import function_signature_to_4byte_selector

from multichain_indexer.adapters.evm.models import EvmToken
from multichain_indexer.core.errors import InvalidInput
from multichain_indexer.core.models import AssetRecord

WORD_BYTES = 32
UINT8_MAX = 255

Call = Callable[[bytes], Awaitable[bytes | None]]


def selector(signature: str) -> bytes:
    return function_signature_to_4byte_selector(signature)


async def read_token(
    call: Call, *, address: str, block_number: int, standard: str, chain: str, network: str
) -> AssetRecord:
    """Build the token record from ``call``, which runs ``eth_call`` at one block.

    Raises:
        InvalidInput: The contract has no ``totalSupply``, so it is not a token.
    """
    total_supply = _uint(await call(selector("totalSupply()")))
    if total_supply is None:
        raise InvalidInput(f"{address} is not an {standard} token: it has no totalSupply()")
    decimals = _uint(await call(selector("decimals()")))
    if decimals is not None and decimals > UINT8_MAX:
        decimals = None
    native = EvmToken(
        standard=standard, decimals_declared=decimals is not None, block_number=block_number
    )
    return AssetRecord(
        id=address,
        name=_text(await call(selector("name()"))),
        unit=_text(await call(selector("symbol()"))),
        decimals=decimals or 0,
        total=total_supply,
        chain=chain,
        network=network,
        native=native,
    )


def _uint(data: bytes | None) -> int | None:
    if data is None or len(data) != WORD_BYTES:
        return None
    return int.from_bytes(data)


def _text(data: bytes | None) -> str | None:
    if not data:
        return None
    if len(data) == WORD_BYTES:
        return data.rstrip(b"\0").decode("utf-8", errors="replace") or None
    try:
        (text,) = decode(["string"], data)
    except (DecodingError, OverflowError, UnicodeDecodeError):
        # A contract can return anything; eth_abi raises OverflowError for a length past
        # what the reply holds.
        return None
    return str(text) or None
