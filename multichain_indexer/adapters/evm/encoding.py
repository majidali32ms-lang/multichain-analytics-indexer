"""Reads the JSON-RPC wire format: hex quantities and hex data."""

from __future__ import annotations

import re

from pydantic import JsonValue

from multichain_indexer.core.errors import MalformedResponse

HEX_PREFIX = "0x"
HEX_DIGITS = re.compile(r"[0-9a-fA-F]*")


def parse_quantity(value: JsonValue, method: str) -> int:
    """Read a JSON-RPC quantity, a hex string such as ``0x1a``.

    Raises:
        MalformedResponse: ``value`` is not a hex quantity.
    """
    digits = _hex_digits(value, method, "a hex quantity")
    if not digits:
        raise MalformedResponse(f"{method}: expected a hex quantity, got {value!r}")
    return int(digits, 16)


def parse_data(value: JsonValue, method: str) -> bytes:
    """Read JSON-RPC data, a hex string of whole bytes such as ``0x60806040``.

    Raises:
        MalformedResponse: ``value`` is not hex data.
    """
    digits = _hex_digits(value, method, "hex data")
    if len(digits) % 2:
        raise MalformedResponse(f"{method}: expected whole bytes of hex data")
    return bytes.fromhex(digits)


def _hex_digits(value: JsonValue, method: str, expected: str) -> str:
    if not isinstance(value, str) or not value.startswith(HEX_PREFIX):
        raise MalformedResponse(f"{method}: expected {expected}, got {value!r}")
    digits = value.removeprefix(HEX_PREFIX)
    if not HEX_DIGITS.fullmatch(digits):
        raise MalformedResponse(f"{method}: expected {expected}, got {value!r}")
    return digits
