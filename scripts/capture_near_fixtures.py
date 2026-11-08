"""Save real NEAR responses as test fixtures.

Reads the history of a few busy accounts until one example of each transaction shape has
been seen. Function call arguments, logs, return values and contract panic messages are
scrubbed, because each can hold anything a sender wrote. Their structure is kept.

    python scripts/capture_near_fixtures.py
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HISTORY = {
    "testnet": "https://tx.test.fastnear.com",
    "mainnet": "https://tx.main.fastnear.com",
}
RPC = {
    "testnet": "https://near-testnet.drpc.org",
    "mainnet": "https://near.drpc.org",
}
SCAN_ACCOUNTS = (
    ("testnet", "guest-book.testnet"),
    ("testnet", "testnet"),
    ("testnet", "relay.testnet"),
    ("testnet", "hello.near-examples.testnet"),
    ("mainnet", "relay.aurora"),
    ("mainnet", "wrap.near"),
    ("mainnet", "near"),
)
WANTED = (
    "transfer",
    "function_call",
    "many_receipts",
    "failed",
    "failed_receipt_in_successful_transaction",
    "create_account",
    "add_key",
    "deploy_contract",
    "delegate",
    "batch",
    "mainnet",
)
DEPLOYMENT_HASH = "HZhEukSxthqJjVG3Y6TSL3RxiicCMDWeGgag7KoH6CZB"
OUTPUT = Path(__file__).parent.parent / "tests" / "fixtures" / "near"
PAUSE_SECONDS = 4.0
RATE_LIMIT_PAUSE_SECONDS = 30
PAGE_SIZE = 20
PAGES_PER_ACCOUNT = 6
MANY_RECEIPTS = 5
MAX_FIXTURE_BYTES = 30_000
PAGINATION_MIN_ROWS = 21
PAGINATION_MAX_ROWS = 40
MAX_ACCOUNTS_TO_INSPECT = 40
IMPLICIT = re.compile(r"[0-9a-f]{64}")


def post(url: str, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "fixture-capture"}
    while True:
        time.sleep(PAUSE_SECONDS)
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429:
                raise
            time.sleep(RATE_LIMIT_PAUSE_SECONDS)


def rows_page(network: str, account: str, cursor: str | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"account_id": account, "limit": PAGE_SIZE, "desc": True}
    if cursor:
        request["resume_token"] = cursor
    return post(f"{HISTORY[network]}/v0/account", request)


def transactions(network: str, hashes: list[str]) -> list[dict[str, Any]]:
    return post(f"{HISTORY[network]}/v0/transactions", {"tx_hashes": hashes})["transactions"]


def rpc(network: str, method: str, params: object) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    return post(RPC[network], payload)


def query(network: str, request_type: str, account: str) -> dict[str, Any]:
    params = {"request_type": request_type, "finality": "final", "account_id": account}
    return rpc(network, "query", params)


def scrubbed_args(encoded: str) -> str:
    try:
        decoded = json.loads(base64.b64decode(encoded))
    except ValueError:
        return base64.b64encode(b"\x00\x01not json").decode()
    return base64.b64encode(json.dumps(scrubbed_values(decoded)).encode()).decode()


def scrubbed_values(value: object) -> object:
    if isinstance(value, dict):
        return {key: scrubbed_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrubbed_values(item) for item in value]
    return "redacted" if isinstance(value, str) else value


def scrub(item: object) -> object:
    if isinstance(item, list):
        return [scrub(entry) for entry in item]
    if not isinstance(item, dict):
        return item
    cleaned: dict[str, object] = {}
    for key, value in item.items():
        if key in {"signature", "metadata", "data_receipts"}:
            continue
        if key == "args" and isinstance(value, str):
            cleaned[key] = scrubbed_args(value)
        elif key == "logs" and isinstance(value, list):
            cleaned[key] = ["redacted log" for _ in value]
        elif key == "SuccessValue":
            cleaned[key] = ""
        elif key == "ExecutionError":
            cleaned[key] = "redacted panic"
        else:
            cleaned[key] = scrub(value)
    return cleaned


def action_kinds(actions: list[Any]) -> list[str]:
    return [action if isinstance(action, str) else next(iter(action)) for action in actions]


def status_key(status: object) -> str:
    return status if isinstance(status, str) else next(iter(status))  # type: ignore[call-overload]


def real_receipts(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in item["receipts"] if r["receipt"]["predecessor_id"] != "system"]


def shapes_of(network: str, item: dict[str, Any], is_success: bool) -> set[str]:
    kinds = action_kinds(item["transaction"]["actions"])
    receipts = real_receipts(item)
    failed = any(
        status_key(r["execution_outcome"]["outcome"]["status"]) == "Failure" for r in receipts
    )
    found: set[str] = set()
    if kinds == ["Transfer"]:
        found.add("transfer")
    if kinds == ["FunctionCall"] and is_success and len(receipts) <= 2:
        found.add("function_call")
    if len(receipts) >= MANY_RECEIPTS:
        found.add("many_receipts")
    if not is_success:
        found.add("failed")
    if is_success and failed:
        found.add("failed_receipt_in_successful_transaction")
    if "CreateAccount" in kinds:
        found.add("create_account")
    if "AddKey" in kinds:
        found.add("add_key")
    if "DeployContract" in kinds:
        found.add("deploy_contract")
    if "Delegate" in kinds or "DelegateV2" in kinds:
        found.add("delegate")
    if len(kinds) > 1:
        found.add("batch")
    if network == "mainnet":
        found.add("mainnet")
    return found


def save(name: str, content: object) -> None:
    text = json.dumps(content, indent=1, sort_keys=True)
    (OUTPUT / f"{name}.json").write_text(text + "\n", encoding="utf-8")


def collect_transactions() -> set[str]:
    """Save one transaction of each wanted shape and return the signers seen."""
    missing = set(WANTED)
    signers: set[str] = set()
    for network, account in SCAN_ACCOUNTS:
        cursor = None
        for _ in range(PAGES_PER_ACCOUNT):
            if not missing:
                return signers
            page = rows_page(network, account, cursor)
            rows = {row["transaction_hash"]: row for row in page["account_txs"]}
            if not rows:
                break
            for item in transactions(network, list(rows)):
                hash_ = item["transaction"]["hash"]
                signers.add(f"{network}:{item['transaction']['signer_id']}")
                is_success = rows[hash_]["is_success"]
                cleaned = scrub(item)
                if len(json.dumps(cleaned)) > MAX_FIXTURE_BYTES:
                    continue
                for shape in sorted(shapes_of(network, item, is_success) & missing):
                    save(f"tx_{shape}", {"is_success": is_success, "transaction": cleaned})
                    missing.discard(shape)
                    print("saved", shape, hash_, "on", network, flush=True)
            cursor = page.get("resume_token")
            if not cursor:
                break
    print("not found:", sorted(missing), flush=True)
    return signers


def collect_deployment() -> None:
    """Save a known deployment: deploying is too rare for a scan to meet one."""
    item = transactions("testnet", [DEPLOYMENT_HASH])[0]
    request = {
        "account_id": item["transaction"]["signer_id"],
        "limit": PAGE_SIZE,
        "from_tx_block_height": item["execution_outcome"]["block_height"],
        "to_tx_block_height": item["execution_outcome"]["block_height"],
    }
    rows = post(f"{HISTORY['testnet']}/v0/account", request)["account_txs"]
    is_success = next(r["is_success"] for r in rows if r["transaction_hash"] == DEPLOYMENT_HASH)
    save("tx_deploy_contract", {"is_success": is_success, "transaction": scrub(item)})
    print("saved deploy_contract", DEPLOYMENT_HASH, flush=True)


def collect_pagination(signers: set[str]) -> None:
    """Save a short account's whole history, which spans two pages."""
    for entry in sorted(signers):
        network, account = entry.split(":", 1)
        if network != "testnet":
            continue
        first = rows_page(network, account)
        if not PAGINATION_MIN_ROWS <= first["txs_count"] <= PAGINATION_MAX_ROWS:
            continue
        last = rows_page(network, account, first["resume_token"])
        save(
            "history_pages",
            {
                "account": account,
                "pages": [
                    {
                        "rows": scrub(page),
                        "transactions": scrub(
                            transactions(
                                network, [row["transaction_hash"] for row in page["account_txs"]]
                            )
                        ),
                    }
                    for page in (first, last)
                ],
            },
        )
        print("saved history_pages for", account, first["txs_count"], flush=True)
        return
    print("no account with a two-page history was found", flush=True)


def collect_accounts(signers: set[str]) -> None:
    """Save an implicit account and an account holding function-call access keys."""
    implicit_saved = function_call_saved = False
    for entry in sorted(signers)[:MAX_ACCOUNTS_TO_INSPECT]:
        network, account = entry.split(":", 1)
        if implicit_saved and function_call_saved:
            return
        if not implicit_saved and IMPLICIT.fullmatch(account):
            save("account_implicit", query(network, "view_account", account))
            save("access_keys_implicit", query(network, "view_access_key_list", account))
            implicit_saved = True
            print("saved implicit account", account, flush=True)
        if not function_call_saved and not IMPLICIT.fullmatch(account):
            keys = query(network, "view_access_key_list", account)
            if "result" not in keys:
                continue
            permissions = [k["access_key"]["permission"] for k in keys["result"]["keys"]]
            if any(isinstance(p, dict) for p in permissions) and len(permissions) < 12:
                save("access_keys_function_call", keys)
                save("account_named", query(network, "view_account", account))
                function_call_saved = True
                print("saved function-call keys of", account, flush=True)
    print("implicit:", implicit_saved, "function-call keys:", function_call_saved, flush=True)


def collect_chain_views() -> None:
    save(
        "rpc_status_testnet",
        {"result": {"chain_id": rpc("testnet", "status", [])["result"]["chain_id"]}},
    )
    save(
        "rpc_status_mainnet",
        {"result": {"chain_id": rpc("mainnet", "status", [])["result"]["chain_id"]}},
    )
    block = rpc("testnet", "block", {"finality": "final"})
    save("rpc_block", {"result": {"header": {"height": block["result"]["header"]["height"]}}})
    config = rpc("testnet", "EXPERIMENTAL_protocol_config", {"finality": "final"})
    save("rpc_shard_layout", {"result": {"shard_layout": config["result"]["shard_layout"]}})
    save("account_contract", query("testnet", "view_account", "guest-book.testnet"))
    save("account_unknown", query("testnet", "view_account", "no-such-account-xyz.testnet"))
    blocks = post(f"{HISTORY['testnet']}/v0/blocks", {"limit": 1, "desc": True})
    save("indexed_blocks", {"blocks": [{"block_height": blocks["blocks"][0]["block_height"]}]})


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    signers = collect_transactions()
    collect_deployment()
    collect_pagination(signers)
    collect_accounts(signers)
    collect_chain_views()


if __name__ == "__main__":
    main()
