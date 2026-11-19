"""What only BNB Smart Chain has: system contracts, system transactions and the validator of
each block.

The addresses are the constants of ``contracts/System.sol`` in bnb-chain/bsc-genesis-contract
(last changed in commit 4c03f8222f, 2024-09-23). A block's validator is its coinbase: the Parlia
engine's ``Author`` returns ``header.Coinbase`` and the seal's signer must equal it. A system
transaction is what Parlia's ``IsSystemTransaction`` accepts: sent to one of the contracts in
its ``systemContracts`` map, at a gas price of zero, by the block's coinbase. A zero gas price
alone is not enough: ordinary zero-fee transactions exist on BNB Smart Chain too. The current
validators come from BSCValidatorSet (0x...1000) and their names from StakeHub (0x...2002).
"""

from __future__ import annotations

from collections.abc import Mapping

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils.address import to_checksum_address

from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.models import EvmBnbLabels, EvmValidator
from multichain_indexer.adapters.evm.multicall import aggregate
from multichain_indexer.adapters.evm.tokens import selector
from multichain_indexer.core.errors import MalformedResponse

SYSTEM_CONTRACTS: Mapping[str, str] = {
    "0x0000000000000000000000000000000000001000": "BSCValidatorSet",
    "0x0000000000000000000000000000000000001001": "SlashIndicator",
    "0x0000000000000000000000000000000000001002": "SystemReward",
    "0x0000000000000000000000000000000000001003": "TendermintLightClient",
    "0x0000000000000000000000000000000000001004": "TokenHub",
    "0x0000000000000000000000000000000000001005": "RelayerIncentivize",
    "0x0000000000000000000000000000000000001006": "RelayerHub",
    "0x0000000000000000000000000000000000001007": "GovHub",
    "0x0000000000000000000000000000000000001008": "TokenManager",
    "0x0000000000000000000000000000000000002000": "CrossChain",
    "0x0000000000000000000000000000000000002001": "Staking",
    "0x0000000000000000000000000000000000002002": "StakeHub",
    "0x0000000000000000000000000000000000002003": "StakeCredit",
    "0x0000000000000000000000000000000000002004": "BSCGovernor",
    "0x0000000000000000000000000000000000002005": "GovToken",
    "0x0000000000000000000000000000000000002006": "BSCTimelock",
    "0x0000000000000000000000000000000000003000": "TokenRecoverPortal",
}
VALIDATOR_SET = "0x0000000000000000000000000000000000001000"
STAKE_HUB = "0x0000000000000000000000000000000000002002"
# StakeHub.sol: a public mapping and a view returning Description(moniker, identity, website,
# details); both answered for every mainnet validator on 2026-11-19.
CONSENSUS_TO_OPERATOR = "consensusToOperator(address)"
DESCRIPTION = "getValidatorDescription(address)"
WORD_BYTES = 32
# Parlia's systemContracts map leaves out TokenManager, Staking and StakeCredit.
SYSTEM_TRANSACTION_TARGETS = frozenset(SYSTEM_CONTRACTS) - {
    "0x0000000000000000000000000000000000001008",
    "0x0000000000000000000000000000000000002001",
    "0x0000000000000000000000000000000000002003",
}


def system_contract_name(address: str | None) -> str | None:
    return SYSTEM_CONTRACTS.get(address.lower()) if address else None


def bnb_labels(
    *, sender: str, receiver: str | None, effective_gas_price: int, coinbase: str | None
) -> EvmBnbLabels | None:
    """Label one BNB Smart Chain transaction; None when the block header has no coinbase."""
    if coinbase is None:
        return None
    system = (
        receiver is not None
        and receiver.lower() in SYSTEM_TRANSACTION_TARGETS
        and effective_gas_price == 0
        and sender.lower() == coinbase.lower()
    )
    return EvmBnbLabels(
        validator=to_checksum_address(coinbase),
        system_transaction=system,
        system_contract=system_contract_name(receiver),
    )


async def read_validators(rpc: EvmRpc, block: int) -> tuple[EvmValidator, ...]:
    """Return the current validator set at ``block`` with the names StakeHub holds.

    Raises:
        MalformedResponse: A system contract answered with something it never returns.
    """
    consensus = _addresses(await rpc.call(VALIDATOR_SET, selector("getValidators()"), block))
    operator_calls = [(STAKE_HUB, _with_address(CONSENSUS_TO_OPERATOR, a)) for a in consensus]
    operators = [_operator(reply) for reply in await aggregate(rpc, operator_calls, block)]
    known = [operator for operator in operators if operator]
    description_calls = [(STAKE_HUB, _with_address(DESCRIPTION, a)) for a in known]
    replies = await aggregate(rpc, description_calls, block)
    names = dict(zip(known, (_description(reply) for reply in replies), strict=True))
    return tuple(
        _validator(address, operator, names)
        for address, operator in zip(consensus, operators, strict=True)
    )


def _validator(
    consensus: str, operator: str | None, names: dict[str, tuple[str | None, str | None]]
) -> EvmValidator:
    moniker, website = names.get(operator, (None, None)) if operator else (None, None)
    return EvmValidator(
        consensus_address=to_checksum_address(consensus),
        operator_address=operator,
        moniker=moniker,
        website=website,
    )


def _with_address(signature: str, address: str) -> bytes:
    return selector(signature) + encode(["address"], [address])


def _addresses(reply: bytes | None) -> list[str]:
    try:
        (addresses,) = decode(["address[]"], reply or b"")
    except (DecodingError, OverflowError) as broken:
        raise MalformedResponse(f"getValidators: {broken}") from None
    return [str(address) for address in addresses]


def _operator(reply: bytes | None) -> str | None:
    if reply is None or len(reply) != WORD_BYTES:
        return None
    (operator,) = decode(["address"], reply)
    return None if int(operator, 16) == 0 else to_checksum_address(operator)


def _description(reply: bytes | None) -> tuple[str | None, str | None]:
    if reply is None:
        return None, None
    try:
        ((moniker, _identity, website, _details),) = decode(
            ["(string,string,string,string)"], reply
        )
    except (DecodingError, OverflowError, UnicodeDecodeError):
        return None, None
    return moniker or None, website or None
