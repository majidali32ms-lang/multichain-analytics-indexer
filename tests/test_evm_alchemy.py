from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from multichain_indexer import ConfigError, NetworkError, RateLimited
from multichain_indexer.adapters.evm import alchemy as alchemy_module
from multichain_indexer.adapters.evm.adapter import indexer_factory
from multichain_indexer.adapters.evm.profiles import BASE, ETHEREUM, EvmChain
from multichain_indexer.adapters.evm.transfers import TransferHistory
from multichain_indexer.core.config import Settings
from tests.conftest import FakeNode, Reply

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "evm" / "history_base_sepolia_alchemy.json").read_text(
        "utf-8"
    )
)
ADDRESS = FIXTURE["address"]
TOP, BOTTOM = FIXTURE["to_block"], FIXTURE["from_block"]
KEY = "alchemy-test-key-0123456789abcdef"
METHOD = "rpc:alchemy_getAssetTransfers"


def rpc_error(code: int, message: str) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


def saved(direction: str) -> list[str]:
    return [
        transfer["hash"]
        for exchange in FIXTURE["exchanges"]
        if direction in exchange["params"]
        for transfer in exchange["reply"]["result"]["transfers"]
    ]


def replay(params: list[dict[str, Any]]) -> Reply:
    (asked,) = params
    for exchange in FIXTURE["exchanges"]:
        known = exchange["params"]
        same = all(asked.get(name) == known.get(name) for name in ("fromAddress", "toAddress"))
        if same and asked.get("pageKey") == known.get("pageKey"):
            return Reply(exchange["reply"])
    return Reply({"jsonrpc": "2.0", "id": 1, "result": {"transfers": []}})


def settings_for(node: FakeNode, tmp_path: Path, network: str = "testnet", **env: str) -> Settings:
    prefix = f"BASE_{network.upper()}"
    values = {
        f"{prefix}_RPC_SERVER": node.url,
        f"{prefix}_HISTORY_SERVER": node.url,
        "ALCHEMY_API_KEY": KEY,
        **env,
    }
    return Settings(
        database=tmp_path / "base.db",
        requests_per_second=1000,
        max_retries=0,
        env={name: SecretStr(value) for name, value in values.items() if value},
    )


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alchemy_module, "REQUESTS_PER_SECOND", 1000)


@pytest.fixture
def source_for() -> Iterator[Callable[[Settings, str], TransferHistory]]:
    adapters: list[Any] = []

    def build(settings: Settings, network: str = "testnet") -> TransferHistory:
        adapters.append(indexer_factory(BASE)(network, settings, None))
        history = adapters[-1]._history_source()
        assert isinstance(history, TransferHistory)
        return history

    yield build


def test_saved_replies_are_one_base_sepolia_account_paged_two_at_a_time() -> None:
    assert FIXTURE["network"] == "Base Sepolia"
    assert len(saved("fromAddress")) == 3
    assert len(saved("toAddress")) == 1
    assert KEY not in json.dumps(FIXTURE)
    assert all(exchange["params"]["excludeZeroValue"] is False for exchange in FIXTURE["exchanges"])


async def test_one_window_holds_both_directions_newest_first(
    node: FakeNode, tmp_path: Path, source_for: Any
) -> None:
    node.handlers[METHOD] = replay
    history = source_for(settings_for(node, tmp_path))
    try:
        page = await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()
    expected = {*saved("fromAddress"), *saved("toAddress")}
    assert set(page.hashes) == expected
    assert len(page.hashes) == len(expected)
    assert page.next_cursor == str(BOTTOM - 1)
    asked = [
        params[0] for method, params in node.rpc_calls if method == "alchemy_getAssetTransfers"
    ]
    assert all(call["excludeZeroValue"] is False for call in asked)
    assert all(call["category"] == ["external"] for call in asked)
    assert node.requests[0][0] == f"/v2/{KEY}/"


@pytest.mark.parametrize(
    ("network", "host"), [("testnet", "base-sepolia"), ("mainnet", "base-mainnet")]
)
def test_base_defaults_to_alchemy_on_both_networks(network: str, host: str) -> None:
    settings = Settings(env={"ALCHEMY_API_KEY": SecretStr(KEY)})
    adapter = indexer_factory(BASE)(network, settings, None)
    history = adapter._history_source()
    assert isinstance(history, TransferHistory)
    url = history._client._base_url.get_secret_value()
    assert url == f"https://{host}.g.alchemy.com/v2/{KEY}"


async def test_all_scope_adds_erc20_transfers(
    node: FakeNode, tmp_path: Path, source_for: Any
) -> None:
    node.handlers[METHOD] = replay
    history = source_for(settings_for(node, tmp_path, BASE_TESTNET_HISTORY_SCOPE="all"))
    try:
        await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()
    categories = {
        tuple(params[0]["category"])
        for method, params in node.rpc_calls
        if method == "alchemy_getAssetTransfers"
    }
    assert categories == {("external", "erc20")}


@pytest.mark.parametrize(
    ("reply", "error", "message"),
    [
        (
            rpc_error(429, "Your app has exceeded its compute units per second capacity"),
            RateLimited,
            "compute units",
        ),
        (
            rpc_error(-32602, "The 'internal' category is only supported for ETH and MATIC."),
            NetworkError,
            "internal",
        ),
    ],
)
async def test_errors_map_to_library_errors(
    node: FakeNode,
    tmp_path: Path,
    source_for: Any,
    reply: Reply,
    error: type[Exception],
    message: str,
) -> None:
    node.routes[METHOD] = reply
    history = source_for(settings_for(node, tmp_path))
    try:
        with pytest.raises(error, match=message):
            await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()


async def test_key_never_reaches_a_message(node: FakeNode, tmp_path: Path, source_for: Any) -> None:
    node.routes[METHOD] = Reply({"message": "down"}, status=500)
    history = source_for(settings_for(node, tmp_path))
    try:
        with pytest.raises(NetworkError) as caught:
            await history.account_page(ADDRESS, from_height=None, to_height=TOP, cursor=None)
    finally:
        await history.aclose()
    assert KEY not in str(caught.value)


def test_missing_key_names_the_setting(node: FakeNode, tmp_path: Path) -> None:
    adapter = indexer_factory(BASE)(
        "testnet", settings_for(node, tmp_path, ALCHEMY_API_KEY=""), None
    )
    with pytest.raises(ConfigError, match=r"dashboard\.alchemy\.com and set ALCHEMY_API_KEY"):
        adapter._history_source()


@pytest.mark.parametrize(("chain", "network"), [(ETHEREUM, "mainnet"), (BASE, "localnet")])
def test_other_networks_are_refused(chain: EvmChain, network: str) -> None:
    env = {
        f"{chain.name}_{network}_HISTORY_SOURCE".upper(): SecretStr("alchemy"),
        "ALCHEMY_API_KEY": SecretStr(KEY),
    }
    adapter = indexer_factory(chain)(network, Settings(env=env), None)
    with pytest.raises(ConfigError, match="Base Mainnet and Base Sepolia only"):
        adapter._history_source()


def test_key_is_masked_in_logs() -> None:
    assert KEY in Settings(env={"ALCHEMY_API_KEY": SecretStr(KEY)}).secret_values()
