"""Save real responses that show post-quantum signatures, and ones that must not count.

Algorand: native Falcon-1024 (pqsig, scheme f1) transactions and their accounts on mainnet and
testnet, and a state proof transaction, which never counts. NEAR: the access keys of a
testnet account holding an ML-DSA-65 key, which the RPC lists by their hash.

    python scripts/capture_pq_fixtures.py
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

ALGORAND_TRANSACTIONS = {
    "mainnet": "U6NDFMOVRJSJB4S7ZUAQAK4FYJ36PGVBTJ5NW4JE2DGNEA34Q7IA",
    "testnet": "RCESIXC2TQP6VNK5GLIB6CL3P2H4VWIZHG4NIIMMOTMJYSACRHXA",
}
# Signatures that are not post-quantum: an Ed25519 payment and a LogicSig transaction.
ALGORAND_NOT_PQ = {
    "tx_sig": ("testnet", "QRUMYVNOD5XSBNFN5RMW6SI7WKKJT3PGCANZXJGOOQV6KNGPP4XQ"),
    "tx_lsig": ("mainnet", "WYZVGOAAIMRP4O3PSJZ2Y3QHUN4EFY2IDTRLRRWJKVFGAAY7DTEA"),
}
# Searching every round for a type times out on the public testnet Indexer.
STATE_PROOF_SEARCH_ROUNDS = 200_000
NEAR_ML_DSA_ACCOUNT = "zf-b6bc410f46.testnet"
NEAR_RPC = "https://near-testnet.drpc.org"
OUTPUT = Path(__file__).parent.parent / "tests" / "fixtures"
PAUSE_SECONDS = 1.5


def fetch(url: str, payload: dict[str, Any] | None = None) -> Any:
    time.sleep(PAUSE_SECONDS)
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": "fixture-capture", "Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=90) as reply:
        return json.load(reply)


def save(folder: str, name: str, content: object) -> None:
    path = OUTPUT / folder / f"{name}.json"
    path.write_text(json.dumps(content, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("saved", path.relative_to(OUTPUT.parent.parent), flush=True)


def algorand() -> None:
    for network, tx_id in ALGORAND_TRANSACTIONS.items():
        indexer = f"https://{network}-idx.4160.nodely.dev"
        transaction = fetch(f"{indexer}/v2/transactions/{tx_id}")["transaction"]
        save("algorand", f"tx_pqsig_{network}", transaction)
        account = fetch(f"{indexer}/v2/accounts/{transaction['sender']}?exclude=all")
        save("algorand", f"account_pqsig_{network}", account)
    for name, (network, tx_id) in ALGORAND_NOT_PQ.items():
        indexer = f"https://{network}-idx.4160.nodely.dev"
        save("algorand", name, fetch(f"{indexer}/v2/transactions/{tx_id}")["transaction"])
    indexer = "https://testnet-idx.4160.nodely.dev"
    head = fetch("https://testnet-api.4160.nodely.dev/v2/status")["last-round"]
    since = head - STATE_PROOF_SEARCH_ROUNDS
    search = f"{indexer}/v2/transactions?tx-type=stpf&limit=1&min-round={since}"
    save("algorand", "tx_stpf", fetch(search)["transactions"][0])


def near() -> None:
    params = {
        "request_type": "view_access_key_list",
        "finality": "final",
        "account_id": NEAR_ML_DSA_ACCOUNT,
    }
    keys = fetch(NEAR_RPC, {"jsonrpc": "2.0", "id": "1", "method": "query", "params": params})
    save("near", "access_keys_ml_dsa", keys)
    params = {
        "request_type": "view_account",
        "finality": "final",
        "account_id": NEAR_ML_DSA_ACCOUNT,
    }
    state = fetch(NEAR_RPC, {"jsonrpc": "2.0", "id": "1", "method": "query", "params": params})
    save("near", "account_ml_dsa", state)


if __name__ == "__main__":
    algorand()
    near()
