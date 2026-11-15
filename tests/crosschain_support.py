"""Real records from two chains, stored the way a sync stores them, for cross-chain tests."""

from __future__ import annotations

from pathlib import Path

from multichain_indexer.adapters.algorand.parser import parse_transaction as parse_algorand
from multichain_indexer.adapters.bitcoin.esplora import parse_esplora_transaction
from multichain_indexer.adapters.bitcoin.parser import parse_transaction as parse_bitcoin
from multichain_indexer.core.models import TransactionRecord
from multichain_indexer.core.storage import SQLiteStorage
from tests.bitcoin_support import fixture
from tests.conftest import load_fixture

ALGO_ACCOUNT = "ALGORANDACCOUNT"


def algorand_records() -> list[TransactionRecord]:
    return [parse_algorand(load_fixture(name), "testnet") for name in ("tx_pay", "tx_axfer")]


def bitcoin_mainnet_record() -> TransactionRecord:
    source = parse_esplora_transaction(fixture("tx_mainnet_p2pk"), "/tx/p2pk")
    return parse_bitcoin(source, "mainnet", tip_height=930_000, history_source="esplora")


def write_two_chains(path: Path) -> Path:
    """Store Algorand testnet and Bitcoin mainnet records in a database at ``path``."""
    storage = SQLiteStorage(path)
    try:
        storage.save_transactions("algorand", "testnet", ALGO_ACCOUNT, algorand_records())
        storage.save_transactions("bitcoin", "mainnet", "key", [bitcoin_mainnet_record()])
    finally:
        storage.close()
    return path
