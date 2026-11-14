from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from multichain_indexer import InvalidAddress
from multichain_indexer.adapters.bitcoin.esplora import SCRIPT_TYPES
from multichain_indexer.adapters.bitcoin.networks import (
    LOCALNET,
    MAINNET,
    NETWORKS,
    SIGNET,
    TESTNET4,
)
from multichain_indexer.adapters.bitcoin.scripts import parse_locator

FIXTURES = Path(__file__).parent / "fixtures" / "bitcoin"
TESTNET_FIXTURES = sorted(
    path for path in FIXTURES.glob("tx_*.json") if path.name != "tx_mainnet_p2pk.json"
)
# The key mainnet block 9 paid; Esplora answered /scripthash/<this> with its six outputs.
BLOCK_9_KEY = (
    "0411db93e1dcdb8a016b49840f8c53bc1eb68a382e97b1482ecad7b148a6909a5c"
    "b2e0eaddfb84ccf9744464f82e160bfa9b8b64f9d4c03f999b8643f656b412a3"
)
BLOCK_9_SCRIPT_HASH = "786929a9e558952ce72efc809ef12043c96978534ca2ccb7dda62d9b1be33181"
# Mainnet addresses: the genesis coinbase's P2PKH, a well-known P2SH, and the BIP-173 and
# BIP-350 examples for P2WPKH and P2TR.
MAINNET_ADDRESSES = (
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
    "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
    "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",
)


def addressed_outputs() -> list[tuple[str, str, str]]:
    """Every address in the testnet4 fixtures, with Esplora's script type and script."""
    found = {}
    for path in TESTNET_FIXTURES:
        tx: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        spent = [entry["prevout"] for entry in tx["vin"] if entry.get("prevout")]
        for output in [*spent, *tx["vout"]]:
            if "scriptpubkey_address" in output:
                found[output["scriptpubkey_address"]] = (
                    output["scriptpubkey_type"],
                    output["scriptpubkey"],
                )
    return [(address, kind, script) for address, (kind, script) in sorted(found.items())]


def test_fixtures_hold_all_five_address_types() -> None:
    kinds = {SCRIPT_TYPES.get(kind, kind) for _, kind, _ in addressed_outputs()}
    assert kinds == {"p2pkh", "p2sh", "p2wpkh", "p2wsh", "p2tr"}


@pytest.mark.parametrize(("address", "kind", "script"), addressed_outputs())
def test_every_real_address_decodes_to_the_script_esplora_reports(
    address: str, kind: str, script: str
) -> None:
    locator = parse_locator(address, NETWORKS[TESTNET4])
    assert locator.text == address
    assert locator.script_type == SCRIPT_TYPES.get(kind, kind)
    assert locator.script_pubkey.hex() == script


def test_bech32_in_capitals_is_accepted_and_written_in_lower_case() -> None:
    for address, _, _ in addressed_outputs():
        if address.startswith("tb1"):
            assert parse_locator(address.upper(), NETWORKS[TESTNET4]).text == address


def test_base58_in_capitals_is_not_read_as_bech32() -> None:
    with pytest.raises(InvalidAddress, match="not a Bitcoin address"):
        parse_locator("1A1ZP1EP5QGEFI2DMPTFTL5SLMV7DIVFNA", NETWORKS[MAINNET])


def test_testnet4_and_signet_share_their_addresses() -> None:
    for address, _, _ in addressed_outputs():
        assert parse_locator(address, NETWORKS[SIGNET]).text == address


@pytest.mark.parametrize("address", MAINNET_ADDRESSES)
def test_a_mainnet_address_is_refused_on_testnet4_before_any_request(address: str) -> None:
    with pytest.raises(InvalidAddress, match="on Bitcoin mainnet, not testnet4"):
        parse_locator(address, NETWORKS[TESTNET4])
    expected = address if address[0] in "13" else address.lower()
    assert parse_locator(address, NETWORKS[MAINNET]).text == expected


def test_a_testnet_address_is_refused_on_mainnet() -> None:
    address = addressed_outputs()[0][0]
    with pytest.raises(InvalidAddress, match="not mainnet"):
        parse_locator(address, NETWORKS[MAINNET])


def test_a_regtest_address_is_only_valid_on_localnet() -> None:
    locator = parse_locator("bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080", NETWORKS[LOCALNET])
    assert locator.script_type == "p2wpkh"
    with pytest.raises(InvalidAddress, match="localnet, not testnet4"):
        parse_locator(locator.text, NETWORKS[TESTNET4])


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not-an-address",
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb",
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5",
        "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd",
        "BC1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        "tb1zw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
    ],
)
def test_malformed_addresses_and_bad_checksums_are_refused(text: str) -> None:
    with pytest.raises(InvalidAddress, match="not a Bitcoin address"):
        parse_locator(text, NETWORKS[MAINNET])


def test_an_address_of_another_coin_names_no_bitcoin_network() -> None:
    with pytest.raises(InvalidAddress, match="not an address on Bitcoin mainnet"):
        parse_locator("ltc1qw508d6qejxtdg4y5r3zarvary0c5xw7kgmn4n9", NETWORKS[MAINNET])


def test_a_p2pk_public_key_is_looked_up_by_the_hash_of_its_script() -> None:
    locator = parse_locator(BLOCK_9_KEY.upper().replace("0X", ""), NETWORKS[MAINNET])
    assert locator.text == BLOCK_9_KEY
    assert locator.script_type == "p2pk"
    assert locator.script_hash == BLOCK_9_SCRIPT_HASH


def test_a_compressed_public_key_is_accepted_on_any_network() -> None:
    key = "02" + "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    locator = parse_locator(key, NETWORKS[TESTNET4])
    assert locator.script_pubkey.hex() == f"21{key}ac"


def test_a_public_key_off_the_curve_is_refused() -> None:
    with pytest.raises(InvalidAddress, match="not a valid public key"):
        parse_locator("02" + "ff" * 32, NETWORKS[MAINNET])
