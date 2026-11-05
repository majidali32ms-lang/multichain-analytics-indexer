"""Save real Solana devnet responses as test fixtures.

Walks recent blocks until one example of each transaction shape has been seen. Program
logs and memos are removed: both can hold anything a sender wrote, and neither is read
by the parser.

    python scripts/capture_solana_fixtures.py
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

RPC = "https://api.devnet.solana.com"
MAINNET_RPC = "https://api.mainnet.solana.com"
# A real mainnet transaction asking for more than the 1,400,000 compute unit cap.
LIMIT_ABOVE_CAP_SIGNATURE = (
    "59fx4xQLC19CNk9gB6uGpcFELzho2ju9rJrY4yrWAkYhKbNnhEf3RhF95XyaFMUAMLgFxJTbwYvgZ3Efm4MxK2V6"
)
OUTPUT = Path(__file__).parent.parent / "tests" / "fixtures" / "solana"
PAUSE_SECONDS = 0.4
RATE_LIMIT_PAUSE_SECONDS = 15
MAX_BLOCKS = 400
SLOTS_BETWEEN_BLOCKS = 2
MAX_BLOCK_TRANSACTIONS = 40
MAX_HISTORY_FOR_EXAMPLE_ACCOUNT = 25
MIN_HISTORY_FOR_EXAMPLE_ACCOUNT = 5

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
PRECOMPILES = {
    "Ed25519SigVerify111111111111111111111111111",
    "KeccakSecp256k11111111111111111111111111111",
    "Secp256r1SigVerify1111111111111111111111111",
}
TOKEN_TRANSFER_TYPES = {"transfer", "transferChecked", "transferCheckedWithFee"}

TRANSACTION_OPTIONS = {
    "encoding": "jsonParsed",
    "maxSupportedTransactionVersion": 1,
    "commitment": "finalized",
}
BLOCK_OPTIONS = {**TRANSACTION_OPTIONS, "transactionDetails": "full", "rewards": False}


def rpc(method: str, params: list[object], url: str = RPC) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "fixture-capture"}
    while True:
        time.sleep(PAUSE_SECONDS)
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429:
                raise
            time.sleep(RATE_LIMIT_PAUSE_SECONDS)


def scrub(item: object) -> object:
    if isinstance(item, list):
        return [scrub(entry) for entry in item]
    if not isinstance(item, dict):
        return item
    cleaned = {key: scrub(value) for key, value in item.items() if key != "logMessages"}
    if "memo" in cleaned:
        cleaned["memo"] = None
    if cleaned.get("program") == "spl-memo":
        cleaned["parsed"] = ""
    return cleaned


def save(name: str, body: object) -> None:
    (OUTPUT / f"{name}.json").write_text(
        json.dumps(scrub(body), indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"saved {name}", flush=True)


def top_level_programs(entry: dict) -> list[str]:
    return [ix.get("programId", "") for ix in entry["transaction"]["message"]["instructions"]]


def all_instructions(entry: dict) -> list[dict]:
    inner = entry["meta"].get("innerInstructions") or []
    nested = [ix for group in inner for ix in group["instructions"]]
    return [*entry["transaction"]["message"]["instructions"], *nested]


def is_token_transfer(instruction: dict, program: str) -> bool:
    parsed = instruction.get("parsed")
    return (
        instruction.get("programId") == program
        and isinstance(parsed, dict)
        and parsed.get("type") in TOKEN_TRANSFER_TYPES
    )


def is_sol_transfer(entry: dict) -> bool:
    instructions = entry["transaction"]["message"]["instructions"]
    return any(
        ix.get("programId") == SYSTEM_PROGRAM
        and isinstance(ix.get("parsed"), dict)
        and ix["parsed"].get("type") == "transfer"
        for ix in instructions
    )


def budget_instruction_count(entry: dict) -> int:
    return sum(1 for program in top_level_programs(entry) if program == COMPUTE_BUDGET_PROGRAM)


def version_of(entry: dict) -> str:
    return str(entry.get("version"))


SHAPES = {
    "tx_sol_transfer": is_sol_transfer,
    "tx_spl_transfer": lambda e: any(
        is_token_transfer(i, TOKEN_PROGRAM) for i in all_instructions(e)
    ),
    "tx_token2022_transfer": lambda e: any(
        is_token_transfer(i, TOKEN_2022_PROGRAM) for i in all_instructions(e)
    ),
    "tx_inner_instructions": lambda e: bool(e["meta"].get("innerInstructions")),
    "tx_failed": lambda e: e["meta"].get("err") is not None,
    "tx_legacy": lambda e: version_of(e) == "legacy",
    "tx_v0": lambda e: (
        version_of(e) == "0" and bool(e["transaction"]["message"].get("addressTableLookups"))
    ),
    "tx_v1": lambda e: version_of(e) == "1",
    "tx_v1_priority_fee": lambda e: (
        version_of(e) == "1"
        and (e["transaction"]["message"]["transactionConfig"].get("priorityFee") or 0) > 0
    ),
    "tx_legacy_compute_budget": lambda e: (
        version_of(e) == "legacy" and budget_instruction_count(e) >= 2
    ),
    "tx_v0_compute_budget": lambda e: version_of(e) == "0" and budget_instruction_count(e) >= 2,
    "tx_price_without_limit": lambda e: (
        budget_instruction_count(e) == 1
        and e["meta"]["fee"] > 5000 * len(e["transaction"]["signatures"])
        and not set(top_level_programs(e)) & PRECOMPILES
    ),
    "tx_precompile_legacy": lambda e: (
        version_of(e) == "legacy" and bool(set(top_level_programs(e)) & PRECOMPILES)
    ),
    "tx_precompile_v0": lambda e: (
        version_of(e) == "0" and bool(set(top_level_programs(e)) & PRECOMPILES)
    ),
    "tx_precompile_v1": lambda e: (
        version_of(e) == "1" and bool(set(top_level_programs(e)) & PRECOMPILES)
    ),
}


def full_transaction(entry: dict) -> dict:
    """A block entry has no slot or time, so fetch the transaction as getTransaction gives it."""
    signature = entry["transaction"]["signatures"][0]
    return rpc("getTransaction", [signature, TRANSACTION_OPTIONS])["result"]


def capture_shapes(head: int) -> dict[str, dict]:
    found: dict[str, dict] = {}
    block_saved = False
    for number in range(MAX_BLOCKS):
        slot = head - number * SLOTS_BETWEEN_BLOCKS
        block = rpc("getBlock", [slot, BLOCK_OPTIONS]).get("result")
        if not block:
            continue
        entries = block["transactions"]
        if (
            not block_saved
            and len(entries) <= MAX_BLOCK_TRANSACTIONS
            and {"legacy", "0", "1"} <= {version_of(e) for e in entries}
        ):
            save("block_with_v1", {"slot": slot, "block": block})
            block_saved = True
        for entry in entries:
            for name, matches in SHAPES.items():
                if name not in found and matches(entry):
                    found[name] = full_transaction(entry)
                    save(name, found[name])
        if block_saved and len(found) == len(SHAPES):
            return found
    print("not found:", sorted(set(SHAPES) - set(found)), flush=True)
    return found


def signature_rows(address: str, **options: object) -> list[dict]:
    return rpc("getSignaturesForAddress", [address, {"commitment": "finalized", **options}])[
        "result"
    ]


def capture_example_account(found: dict[str, dict]) -> None:
    """Find a wallet with a short history, so a real sync of it stays small."""
    for address in candidate_accounts(found):
        rows = signature_rows(address, limit=MAX_HISTORY_FOR_EXAMPLE_ACCOUNT + 1)
        if MIN_HISTORY_FOR_EXAMPLE_ACCOUNT <= len(rows) <= MAX_HISTORY_FOR_EXAMPLE_ACCOUNT:
            save("signatures_first_page", {"address": address, "rows": rows[:2]})
            save("signatures_next_page", {"address": address, "rows": rows[2:4]})
            for number, row in enumerate(rows[:4]):
                signature = row["signature"]
                detail = rpc("getTransaction", [signature, TRANSACTION_OPTIONS])["result"]
                save(f"history_{number}", detail)
            capture_account(address)
            return
    print("no account with a short history in this sample")


def candidate_accounts(found: dict[str, dict]) -> Iterator[str]:
    seen: set[str] = set()
    for transaction in found.values():
        for key in transaction["transaction"]["message"]["accountKeys"]:
            address = key["pubkey"]
            if address not in seen and not key["signer"] and key["writable"]:
                seen.add(address)
                yield address


def capture_account(address: str) -> None:
    save("account", rpc("getAccountInfo", [address, {"encoding": "jsonParsed"}])["result"])
    for name, program in (
        ("token_accounts", TOKEN_PROGRAM),
        ("token2022_accounts", TOKEN_2022_PROGRAM),
    ):
        params = [address, {"programId": program}, {"encoding": "jsonParsed"}]
        save(name, rpc("getTokenAccountsByOwner", params)["result"])


def account_info(address: str, **options: object) -> dict:
    return rpc("getAccountInfo", [address, {"encoding": "jsonParsed", **options}])["result"]


def mint_of(transaction: dict) -> str | None:
    balances = transaction["meta"].get("postTokenBalances") or []
    return balances[0]["mint"] if balances else None


def capture_lookups(found: dict[str, dict]) -> None:
    for name, shape in (("mint", "tx_spl_transfer"), ("mint_token2022", "tx_token2022_transfer")):
        mint = mint_of(found[shape]) if shape in found else None
        if mint:
            save(name, account_info(mint))

    program = account_info(TOKEN_PROGRAM)
    save("program", program)
    program_data = program["value"]["data"]["parsed"]["info"]["programData"]
    header = {"encoding": "base64", "dataSlice": {"offset": 0, "length": 45}}
    save("program_data", rpc("getAccountInfo", [program_data, header])["result"])
    save("not_a_mint", account_info(SYSTEM_PROGRAM))


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    head = rpc("getSlot", [{"commitment": "finalized"}])["result"]
    found = capture_shapes(head)
    capture_example_account(found)
    capture_lookups(found)
    params = [LIMIT_ABOVE_CAP_SIGNATURE, TRANSACTION_OPTIONS]
    save("tx_limit_above_cap", rpc("getTransaction", params, MAINNET_RPC)["result"])


if __name__ == "__main__":
    main()
