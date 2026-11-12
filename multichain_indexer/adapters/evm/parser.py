"""Turns EVM JSON-RPC results into the shared records.

Addresses are checked and checksummed with web3.py's own helpers (``eth_utils``), never by
hand. An all-lowercase or all-uppercase address has no checksum to check; a mixed-case one
must carry a valid EIP-55 checksum, since a wrong one usually means a mistyped address.
"""

from __future__ import annotations

from eth_utils.address import is_checksum_address, is_hex_address, to_checksum_address
from eth_utils.crypto import keccak

from multichain_indexer.adapters.evm.models import EvmAccount, EvmContract
from multichain_indexer.core.errors import InvalidAddress
from multichain_indexer.core.models import AccountRecord, ContractRecord

HEX_PREFIX = "0x"
# EIP-7702: the code of a delegated externally owned account is this prefix and an address.
DELEGATION_PREFIX = bytes.fromhex("ef0100")
ADDRESS_BYTES = 20


def validated_address(value: str) -> str:
    """Return ``value`` as a checksummed address.

    Raises:
        InvalidAddress: It is not a 20-byte hex address, or its mixed case is not a valid
            EIP-55 checksum.
    """
    if not is_hex_address(value) or not value.startswith(HEX_PREFIX):
        raise InvalidAddress(f"not a valid EVM address: {value!r}")
    digits = value.removeprefix(HEX_PREFIX)
    mixed_case = digits not in (digits.lower(), digits.upper())
    if mixed_case and not is_checksum_address(value):
        raise InvalidAddress(
            f"the checksum of {value!r} is wrong (EIP-55); check the address for a typo"
        )
    return to_checksum_address(value)


def parse_account(
    *,
    address: str,
    balance: int,
    nonce: int,
    code: bytes,
    block_number: int,
    chain: str,
    network: str,
) -> AccountRecord:
    native = EvmAccount(
        nonce=nonce,
        is_contract=is_contract_code(code),
        delegated_to=_delegation_target(code),
        block_number=block_number,
    )
    return AccountRecord(
        address=address, balance=balance, chain=chain, network=network, native=native
    )


def parse_contract(
    *, address: str, code: bytes, block_number: int, chain: str, network: str
) -> ContractRecord:
    native = EvmContract(
        code_size=len(code),
        code_hash=HEX_PREFIX + keccak(code).hex(),
        block_number=block_number,
    )
    return ContractRecord(id=address, chain=chain, network=network, native=native)


def is_contract_code(code: bytes) -> bool:
    return bool(code) and _delegation_target(code) is None


def _delegation_target(code: bytes) -> str | None:
    if len(code) != len(DELEGATION_PREFIX) + ADDRESS_BYTES:
        return None
    if not code.startswith(DELEGATION_PREFIX):
        return None
    return to_checksum_address(code.removeprefix(DELEGATION_PREFIX))
