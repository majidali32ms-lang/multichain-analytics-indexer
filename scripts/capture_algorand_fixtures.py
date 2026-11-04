"""Save real Algorand testnet responses as test fixtures.

Unfiltered Indexer searches time out on testnet, so each search walks back through
windows of recent rounds until it finds a match. Notes and signatures are removed: a
note can hold anything a sender wrote, and neither is read by the parser.

    python scripts/capture_algorand_fixtures.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

INDEXER = "https://testnet-idx.4160.nodely.dev"
OUTPUT = Path(__file__).parent.parent / "tests" / "fixtures" / "algorand"
WINDOW_ROUNDS = 40_000
APP_CALL_WINDOW_ROUNDS = 2_000
MAX_WINDOWS = 60
PAUSE_SECONDS = 0.2
STRIPPED_FIELDS = ("note", "signature", "global-state-delta", "local-state-delta")

SEARCHES = {
    "pay": {"tx-type": "pay"},
    "axfer": {"tx-type": "axfer"},
    "acfg": {"tx-type": "acfg"},
    "afrz": {"tx-type": "afrz"},
    "appl": {"tx-type": "appl"},
    "keyreg": {"tx-type": "keyreg"},
    "rekey": {"rekey-to": "true"},
}


def get(path: str, **params: object) -> dict[str, object]:
    time.sleep(PAUSE_SECONDS)
    url = f"{INDEXER}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def strip_private_fields(item: object) -> object:
    if isinstance(item, list):
        return [strip_private_fields(entry) for entry in item]
    if not isinstance(item, dict):
        return item
    return {k: strip_private_fields(v) for k, v in item.items() if k not in STRIPPED_FIELDS}


def save(name: str, body: object) -> None:
    (OUTPUT / f"{name}.json").write_text(
        json.dumps(strip_private_fields(body), indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"saved {name}")


def search_recent(head: int, **filters: object) -> list[dict[str, object]]:
    """Return matches from the newest window that has any."""
    for window in range(MAX_WINDOWS):
        upper = head - window * WINDOW_ROUNDS
        try:
            page = get(
                "/v2/transactions",
                limit=1000,
                **{"min-round": upper - WINDOW_ROUNDS, "max-round": upper},
                **filters,
            )
        except urllib.error.HTTPError:
            continue
        found = [tx for tx in page.get("transactions", []) if _is_top_level_match(tx, filters)]
        if found:
            return found
    return []


def _is_top_level_match(transaction: dict[str, object], filters: dict[str, object]) -> bool:
    # The Indexer also matches on inner transactions and returns their parent app call.
    wanted = filters.get("tx-type")
    return wanted is None or transaction.get("tx-type") == wanted


def find_app_call_with(head: int, field: str) -> dict[str, object] | None:
    """Walk back through app calls until one carries ``field``."""
    for window in range(MAX_WINDOWS):
        upper = head - window * APP_CALL_WINDOW_ROUNDS
        try:
            page = get(
                "/v2/transactions",
                limit=1000,
                **{
                    "tx-type": "appl",
                    "min-round": upper - APP_CALL_WINDOW_ROUNDS,
                    "max-round": upper,
                },
            )
        except urllib.error.HTTPError:
            continue
        for transaction in page.get("transactions", []):
            if transaction.get(field):
                return transaction
    return None


def capture_transactions(head: int) -> dict[str, list[dict[str, object]]]:
    found: dict[str, list[dict[str, object]]] = {}
    for name, filters in SEARCHES.items():
        found[name] = search_recent(head, **filters)
        if found[name]:
            save(f"tx_{name}", found[name][0])
        else:
            print(f"nothing found for {name}", file=sys.stderr)
    for name, field in (("group", "group"), ("inner", "inner-txns"), ("logs", "logs")):
        match = find_app_call_with(head, field)
        if match:
            save(f"tx_{name}", match)
        else:
            print(f"no app call with {field} found", file=sys.stderr)
    return found


def capture_lookups(found: dict[str, list[dict[str, object]]]) -> None:
    asa = found["axfer"][0]["asset-transfer-transaction"]
    save("asset", get(f"/v2/assets/{asa['asset-id']}"))

    application = found["appl"][0]["application-transaction"]
    save("application", get(f"/v2/applications/{application['application-id']}"))

    save("account", get(f"/v2/accounts/{found['axfer'][0]['sender']}"))
    if found["rekey"]:
        save("account_rekeyed", get(f"/v2/accounts/{found['rekey'][0]['sender']}"))


def capture_pages(found: dict[str, list[dict[str, object]]]) -> None:
    address = found["keyreg"][0]["sender"] if found["keyreg"] else found["pay"][0]["sender"]
    path = f"/v2/accounts/{address}/transactions"
    first = get(path, limit=2)
    save("page_first", first)
    save("page_last", get(path, limit=2, next=first["next-token"]))
    save("page_empty", {"current-round": first["current-round"], "transactions": []})


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    health = get("/health")
    save("health", health)
    found = capture_transactions(int(str(health["round"])))
    capture_lookups(found)
    capture_pages(found)


if __name__ == "__main__":
    main()
