from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from algosdk import account as algosdk_account
from pydantic import SecretStr

from multichain_indexer.adapters.algorand import AlgorandIndexer, create_indexer
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import InvalidAddress, InvalidInput, MalformedResponse, NotFound
from multichain_indexer.core.models import TransactionRecord
from tests.conftest import FakeNode, Reply, load_fixture

ADDRESS = algosdk_account.generate_account()[1]
TRANSACTIONS_PATH = f"/v2/accounts/{ADDRESS}/transactions"
LOOKUP_PATH = f"/v2/accounts/{ADDRESS}"


@pytest.fixture
async def indexer(settings: Settings) -> AsyncIterator[AlgorandIndexer]:
    adapter = create_indexer(None, settings)
    yield adapter
    await adapter.aclose()


async def all_pages(adapter: AlgorandIndexer, **bounds: int) -> list[list[TransactionRecord]]:
    return [
        list(page.records) async for page in adapter.iter_account_transactions(ADDRESS, **bounds)
    ]


def page_of(fixture: str, next_token: str | None = None) -> Reply:
    body: dict[str, object] = {"transactions": [load_fixture(fixture)]}
    if next_token:
        body["next-token"] = next_token
    return Reply(body)


async def test_defaults_to_testnet(indexer: AlgorandIndexer) -> None:
    assert indexer.network == "testnet"


async def test_pagination_follows_next_token_and_stops_on_the_last_page(
    node: FakeNode, indexer: AlgorandIndexer
) -> None:
    node.routes[TRANSACTIONS_PATH] = [page_of("tx_pay", "T1"), page_of("tx_axfer")]
    pages = await all_pages(indexer)
    assert [len(page) for page in pages] == [1, 1]
    queries = node.requests_to(TRANSACTIONS_PATH)
    assert "next" not in queries[0]
    assert queries[1]["next"] == "T1"


async def test_pagination_stops_on_an_empty_page_even_with_a_token(
    node: FakeNode, indexer: AlgorandIndexer
) -> None:
    empty_with_token = Reply({"transactions": [], "next-token": "T2"})
    node.routes[TRANSACTIONS_PATH] = [page_of("tx_pay", "T1"), empty_with_token]
    assert len(await all_pages(indexer)) == 1
    assert len(node.requests_to(TRANSACTIONS_PATH)) == 2


async def test_real_pages_parse(node: FakeNode, indexer: AlgorandIndexer) -> None:
    first = load_fixture("page_first")
    last = load_fixture("page_last")
    node.routes[TRANSACTIONS_PATH] = [Reply(first), Reply(last), Reply(load_fixture("page_empty"))]
    pages = await all_pages(indexer)
    assert sum(len(page) for page in pages) == len(first["transactions"]) + len(
        last["transactions"]
    )


async def test_empty_account_yields_nothing(node: FakeNode, indexer: AlgorandIndexer) -> None:
    node.routes[TRANSACTIONS_PATH] = Reply({"transactions": []})
    assert await all_pages(indexer) == []


async def test_round_bounds_are_sent(node: FakeNode, indexer: AlgorandIndexer) -> None:
    node.routes[TRANSACTIONS_PATH] = Reply({"transactions": []})
    await all_pages(indexer, from_height=5, to_height=9)
    query = node.requests_to(TRANSACTIONS_PATH)[0]
    assert (query["min-round"], query["max-round"]) == ("5", "9")


@pytest.mark.parametrize("body", [{"transactions": "nope"}, {"transactions": ["nope"]}])
async def test_malformed_page_is_refused(
    node: FakeNode, indexer: AlgorandIndexer, body: dict[str, object]
) -> None:
    node.routes[TRANSACTIONS_PATH] = Reply(body)
    with pytest.raises(MalformedResponse):
        await all_pages(indexer)


async def test_chain_head(node: FakeNode, indexer: AlgorandIndexer) -> None:
    node.routes["/health"] = Reply(load_fixture("health"))
    assert await indexer.chain_head() == load_fixture("health")["round"]
    assert indexer.final_height(5) == 5


async def test_health_without_a_round_is_refused(node: FakeNode, indexer: AlgorandIndexer) -> None:
    node.routes["/health"] = Reply({"message": "ok"})
    with pytest.raises(MalformedResponse, match="no round"):
        await indexer.chain_head()


async def test_bad_address_never_reaches_the_network(
    node: FakeNode, indexer: AlgorandIndexer
) -> None:
    with pytest.raises(InvalidAddress):
        await indexer.fetch_account("not-an-address")
    with pytest.raises(InvalidAddress):
        await all_pages_of_bad_address(indexer)
    assert node.requests == []


async def all_pages_of_bad_address(adapter: AlgorandIndexer) -> None:
    async for _ in adapter.iter_account_transactions("bad"):
        pass


@pytest.mark.parametrize("value", ["abc", "0", "-1", str(2**64), "1.5", ""])
async def test_bad_ids_never_reach_the_network(
    node: FakeNode, indexer: AlgorandIndexer, value: str
) -> None:
    with pytest.raises(InvalidInput, match="ASA ID"):
        await indexer.fetch_asset(value)
    with pytest.raises(InvalidInput, match="app ID"):
        await indexer.fetch_contract(value)
    assert node.requests == []


async def test_lookups_use_the_documented_paths(node: FakeNode, indexer: AlgorandIndexer) -> None:
    node.routes[LOOKUP_PATH] = Reply(load_fixture("account"))
    node.routes["/v2/assets/7"] = Reply(load_fixture("asset"))
    node.routes["/v2/applications/8"] = Reply(load_fixture("application"))

    assert (await indexer.fetch_account(ADDRESS)).chain == "algorand"
    assert (await indexer.fetch_asset("7")).chain == "algorand"
    assert (await indexer.fetch_contract("8")).chain == "algorand"
    assert indexer.requests_made == 3


async def test_unknown_asa_is_not_found(node: FakeNode, indexer: AlgorandIndexer) -> None:
    node.routes["/v2/assets/7"] = Reply({"message": "no assets found"}, status=404)
    with pytest.raises(NotFound, match="no assets found"):
        await indexer.fetch_asset("7")


async def test_describe_uses_algorand_terms(node: FakeNode, indexer: AlgorandIndexer) -> None:
    node.routes[LOOKUP_PATH] = Reply(load_fixture("account"))
    node.routes["/v2/assets/7"] = Reply(load_fixture("asset"))
    node.routes["/v2/applications/8"] = Reply(load_fixture("application"))
    node.routes[TRANSACTIONS_PATH] = [page_of("tx_axfer", "T"), page_of("tx_pay")]

    assert "ALGO" in indexer.describe(await indexer.fetch_account(ADDRESS))["Balance"]
    assert "ASA ID" in indexer.describe(await indexer.fetch_asset("7"))
    assert "App ID" in indexer.describe(await indexer.fetch_contract("8"))
    asa_page, pay_page = await all_pages(indexer)
    assert "of ASA" in indexer.describe(asa_page[0])["Amount"]
    assert indexer.describe(pay_page[0])["Amount"].endswith("ALGO")


def test_unknown_network_is_refused(settings: Settings) -> None:
    with pytest.raises(InvalidInput, match="unknown Algorand network"):
        create_indexer("devnet", settings)


async def test_configured_token_is_sent(node: FakeNode, settings: Settings) -> None:
    env = {**settings.env, "ALGORAND_TESTNET_INDEXER_TOKEN": SecretStr("tok")}
    adapter = create_indexer(None, settings.model_copy(update={"env": env}))
    node.routes["/health"] = Reply({"round": 1})
    await adapter.chain_head()
    await adapter.aclose()
    assert node.received_headers[0]["X-Indexer-API-Token"] == "tok"


async def test_localnet_defaults_to_algokit_port_and_token(node: FakeNode) -> None:
    default = create_indexer("localnet", Settings())
    assert default._api._client._base_url.get_secret_value() == "http://localhost:8980"
    await default.aclose()

    env = {"ALGORAND_LOCALNET_INDEXER_SERVER": SecretStr(node.url)}
    adapter = create_indexer("localnet", Settings(env=env))
    node.routes["/health"] = Reply({"round": 1})
    await adapter.chain_head()
    await adapter.aclose()
    assert node.received_headers[0]["X-Indexer-API-Token"] == "a" * 64


def test_public_networks_default_to_nodely() -> None:
    testnet = create_indexer("testnet", Settings())
    url = testnet._api._client._base_url.get_secret_value()
    assert url == "https://testnet-idx.4160.nodely.dev"
    assert testnet._api._client._headers == {}
