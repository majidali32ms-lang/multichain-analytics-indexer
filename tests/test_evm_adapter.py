from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from pydantic import SecretStr

from multichain_indexer import (
    ConfigError,
    Indexer,
    InvalidAddress,
    InvalidInput,
    MalformedResponse,
    NetworkError,
    NotFound,
    RateLimited,
    WrongNetwork,
)
from multichain_indexer.adapters.evm import EvmIndexer
from multichain_indexer.adapters.evm.adapter import indexer_factory
from multichain_indexer.adapters.evm.encoding import parse_data, parse_quantity
from multichain_indexer.adapters.evm.models import EvmAccount, EvmContract
from multichain_indexer.adapters.evm.parser import validated_address
from multichain_indexer.adapters.evm.profiles import (
    BASE,
    BNB,
    ETHEREUM,
    EVM_CHAINS,
    LOCALNET_RPC_SERVER,
    PUBLIC_NETWORKS_BY_CHAIN_ID,
    EvmChain,
)
from multichain_indexer.cli import EVM_CHAINS as CLI_EVM_CHAINS
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.registry import PLANNED_CHAINS, create_indexer
from tests.conftest import FakeNode, Reply

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
WETH = json.loads((FIXTURES / "base_sepolia_weth.json").read_text(encoding="utf-8"))
WETH_ADDRESS = WETH["address"]
WETH_BLOCK = int(WETH["replies"]["eth_blockNumber"]["result"], 16)
WETH_CODE = bytes.fromhex(WETH["replies"]["eth_getCode"]["result"][2:])

# The EIP-55 examples.
CHECKSUMMED = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
WRONG_CHECKSUM = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"
EOA = "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359"
DELEGATE = "0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B"

BASE_SEPOLIA = 84532
BNB_TESTNET = 97
BNB_MAINNET = 56
SEPOLIA = 11155111
ANVIL = 31337


def rpc_ok(result: object) -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "result": result})


def rpc_error(code: int, message: str = "refused") -> Reply:
    return Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


def finalized_block(number: int = WETH_BLOCK - 20) -> Reply:
    return rpc_ok({"number": hex(number), "hash": "0x" + "11" * 32})


def serve_weth(node: FakeNode) -> None:
    for method, reply in WETH["replies"].items():
        node.routes[f"rpc:{method}"] = Reply(reply)
    node.routes["rpc:eth_getBlockByNumber"] = finalized_block()


def serve_eoa(node: FakeNode, code: str = "0x") -> None:
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(BASE_SEPOLIA))
    node.routes["rpc:eth_blockNumber"] = rpc_ok(hex(WETH_BLOCK))
    node.routes["rpc:eth_getBalance"] = rpc_ok(hex(1_500_000_000_000_000_000))
    node.routes["rpc:eth_getTransactionCount"] = rpc_ok("0x7")
    node.routes["rpc:eth_getCode"] = rpc_ok(code)
    node.routes["rpc:eth_getBlockByNumber"] = finalized_block()


def settings_for(node: FakeNode, tmp_path: Path, chain: EvmChain, network: str) -> Settings:
    return Settings(
        database=tmp_path / "evm.db",
        requests_per_second=1000,
        request_timeout=5,
        max_retries=0,
        env={f"{chain.name}_{network}_RPC_SERVER".upper(): SecretStr(node.url)},
    )


def adapter_for(node: FakeNode, tmp_path: Path, chain: EvmChain, network: str) -> EvmIndexer:
    settings = settings_for(node, tmp_path, chain, network)
    return indexer_factory(chain)(network, settings, None)  # type: ignore[return-value]


@pytest.fixture
async def base(node: FakeNode, tmp_path: Path) -> AsyncIterator[EvmIndexer]:
    adapter = adapter_for(node, tmp_path, BASE, "testnet")
    yield adapter
    await adapter.aclose()


def test_every_planned_evm_chain_has_a_profile_and_is_registered() -> None:
    names = tuple(chain.name for chain in EVM_CHAINS)
    assert names == CLI_EVM_CHAINS
    assert set(names) <= set(PLANNED_CHAINS)
    for name in names:
        adapter = create_indexer(name, None, Settings())
        assert isinstance(adapter, EvmIndexer)
        assert adapter.profile.name == name
        assert adapter.network == "testnet"


@pytest.mark.parametrize(
    ("chain", "mainnet", "testnet", "symbol"),
    [
        ("ethereum", 1, SEPOLIA, "ETH"),
        ("arbitrum", 42161, 421614, "ETH"),
        ("optimism", 10, 11155420, "ETH"),
        ("base", 8453, BASE_SEPOLIA, "ETH"),
        ("polygon", 137, 80002, "POL"),
        ("bnb", BNB_MAINNET, BNB_TESTNET, "BNB"),
    ],
)
def test_profiles_hold_each_chains_documented_ids_and_gas_token(
    chain: str, mainnet: int, testnet: int, symbol: str
) -> None:
    profile = next(candidate for candidate in EVM_CHAINS if candidate.name == chain)
    assert profile.networks["mainnet"].chain_id == mainnet
    assert profile.networks["testnet"].chain_id == testnet
    assert profile.native_symbol == symbol
    adapter = create_indexer(chain, None, Settings())
    assert adapter.profile.native_decimals == 18
    assert adapter.profile.base_unit == "wei"


def test_no_two_public_networks_share_a_chain_id() -> None:
    networks = [network for chain in EVM_CHAINS for network in chain.networks.values()]
    assert len(PUBLIC_NETWORKS_BY_CHAIN_ID) == len(networks)


def test_defaults_are_the_testnet_and_https_endpoints() -> None:
    for chain in EVM_CHAINS:
        for network in chain.networks.values():
            assert network.rpc_server.startswith("https://")
    adapter = create_indexer("base", None, Settings())
    assert adapter._rpc._client._base_url.get_secret_value() == "https://sepolia.base.org"
    local = create_indexer("base", "localnet", Settings())
    assert local._rpc._client._base_url.get_secret_value() == LOCALNET_RPC_SERVER


def test_unknown_network_is_refused() -> None:
    with pytest.raises(InvalidInput, match="unknown Base network 'devnet'"):
        create_indexer("base", "devnet", Settings())


def test_unencrypted_remote_endpoint_is_refused() -> None:
    env = {"BASE_TESTNET_RPC_SERVER": SecretStr("http://node.example")}
    with pytest.raises(ConfigError, match="BASE_TESTNET_RPC_SERVER"):
        create_indexer("base", None, Settings(env=env))


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (CHECKSUMMED, CHECKSUMMED),
        (CHECKSUMMED.lower(), CHECKSUMMED),
        ("0x" + CHECKSUMMED[2:].upper(), CHECKSUMMED),
    ],
    ids=["checksummed", "lowercase", "uppercase"],
)
def test_addresses_come_back_checksummed(given: str, expected: str) -> None:
    assert validated_address(given) == expected


def test_mixed_case_with_a_wrong_checksum_is_refused() -> None:
    with pytest.raises(InvalidAddress, match=r"checksum .* is wrong"):
        validated_address(WRONG_CHECKSUM)


@pytest.mark.parametrize(
    "address",
    ["", "0x", CHECKSUMMED[2:], CHECKSUMMED[:-1], CHECKSUMMED + "00", "0x" + "g" * 40, "alice"],
)
def test_malformed_addresses_are_refused(address: str) -> None:
    with pytest.raises(InvalidAddress, match="not a valid EVM address"):
        validated_address(address)


async def test_bad_addresses_never_reach_the_network(node: FakeNode, base: EvmIndexer) -> None:
    for lookup in (base.fetch_account, base.fetch_contract):
        with pytest.raises(InvalidAddress):
            await lookup(WRONG_CHECKSUM)
    assert node.rpc_calls == []


async def test_contract_lookup_reads_code_at_one_block(node: FakeNode, base: EvmIndexer) -> None:
    serve_weth(node)
    record = await base.fetch_contract(WETH_ADDRESS.lower())
    native = record.native
    assert isinstance(native, EvmContract)
    assert record.id == WETH_ADDRESS
    assert (record.chain, record.network) == ("base", "testnet")
    assert native.code_size == len(WETH_CODE)
    assert native.code_hash.startswith("0x")
    assert len(native.code_hash) == 66
    assert native.block_number == WETH_BLOCK
    assert node.rpc_params("eth_getCode") == [[WETH_ADDRESS, hex(WETH_BLOCK)]]


async def test_contract_lookup_on_an_address_without_code(node: FakeNode, base: EvmIndexer) -> None:
    serve_eoa(node)
    with pytest.raises(NotFound, match=f"no contract is deployed at {EOA} on Base Sepolia"):
        await base.fetch_contract(EOA)


async def test_account_of_a_contract(node: FakeNode, base: EvmIndexer) -> None:
    serve_weth(node)
    record = await base.fetch_account(WETH_ADDRESS)
    native = record.native
    assert isinstance(native, EvmAccount)
    assert record.balance == int(WETH["replies"]["eth_getBalance"]["result"], 16)
    assert native.is_contract
    assert native.delegated_to is None
    for method in ("eth_getBalance", "eth_getTransactionCount", "eth_getCode"):
        assert node.rpc_params(method) == [[WETH_ADDRESS, hex(WETH_BLOCK)]]


async def test_account_of_an_externally_owned_account(node: FakeNode, base: EvmIndexer) -> None:
    serve_eoa(node)
    record = await base.fetch_account(EOA)
    assert base.describe(record) == {
        "Address": EOA,
        "Kind": "externally owned account",
        "Balance": "1.5 ETH",
        "Nonce": "7",
        "As of block": str(WETH_BLOCK),
    }


async def test_account_delegated_under_eip_7702(node: FakeNode, base: EvmIndexer) -> None:
    serve_eoa(node, code="0xef0100" + DELEGATE[2:].lower())
    record = await base.fetch_account(EOA)
    native = record.native
    assert isinstance(native, EvmAccount)
    assert not native.is_contract
    assert native.delegated_to == DELEGATE
    assert base.describe(record)["Kind"] == (
        f"externally owned account, delegated to {DELEGATE} (EIP-7702)"
    )
    with pytest.raises(NotFound):
        await base.fetch_contract(EOA)


async def test_contract_description(node: FakeNode, base: EvmIndexer) -> None:
    serve_weth(node)
    fields = base.describe(await base.fetch_contract(WETH_ADDRESS))
    assert fields["Contract"] == WETH_ADDRESS
    assert fields["Code size"] == f"{len(WETH_CODE)} bytes"
    assert fields["As of block"] == str(WETH_BLOCK)


async def test_head_is_the_latest_block_and_final_height_the_finalized_one(
    node: FakeNode, base: EvmIndexer
) -> None:
    serve_weth(node)
    assert base.final_height(WETH_BLOCK) == 0
    assert await base.chain_head() == WETH_BLOCK
    assert base.final_height(WETH_BLOCK) == WETH_BLOCK - 20
    assert base.final_height(WETH_BLOCK - 30) == WETH_BLOCK - 30


@pytest.mark.parametrize(
    ("chain", "network", "chain_id"),
    [
        (BASE, "testnet", BASE_SEPOLIA),
        (BNB, "mainnet", BNB_MAINNET),
        (ETHEREUM, "testnet", SEPOLIA),
        (BASE, "localnet", ANVIL),
        (BNB, "localnet", 1337),
    ],
)
async def test_endpoint_serving_the_requested_network_is_accepted(
    node: FakeNode, tmp_path: Path, chain: EvmChain, network: str, chain_id: int
) -> None:
    serve_weth(node)
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(chain_id))
    adapter = adapter_for(node, tmp_path, chain, network)
    try:
        assert await adapter.chain_head() == WETH_BLOCK
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("chain", "network", "chain_id", "message"),
    [
        (
            BASE,
            "testnet",
            BNB_TESTNET,
            "the RPC endpoint serves BNB Smart Chain testnet (chain ID 97), "
            "not Base Sepolia (chain ID 84532). Check BASE_TESTNET_RPC_SERVER.",
        ),
        (
            BASE,
            "mainnet",
            BASE_SEPOLIA,
            "the RPC endpoint serves Base Sepolia (chain ID 84532), "
            "not Base Mainnet (chain ID 8453). Check BASE_MAINNET_RPC_SERVER.",
        ),
        (
            ETHEREUM,
            "testnet",
            ANVIL,
            "the RPC endpoint serves an unknown network, chain ID 31337, "
            "not Sepolia (chain ID 11155111). Check ETHEREUM_TESTNET_RPC_SERVER.",
        ),
        (
            BNB,
            "localnet",
            BNB_MAINNET,
            "the RPC endpoint serves BNB Smart Chain mainnet (chain ID 56), "
            "not a local network. Check BNB_LOCALNET_RPC_SERVER.",
        ),
    ],
)
async def test_endpoint_serving_another_network_is_refused_before_any_other_call(
    node: FakeNode, tmp_path: Path, chain: EvmChain, network: str, chain_id: int, message: str
) -> None:
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(chain_id))
    adapter = adapter_for(node, tmp_path, chain, network)
    try:
        with pytest.raises(WrongNetwork) as caught:
            await adapter.chain_head()
    finally:
        await adapter.aclose()
    assert str(caught.value) == message
    assert [method for method, _ in node.rpc_calls] == ["eth_chainId"]


@pytest.mark.parametrize(
    "lookup",
    [
        lambda adapter: adapter.fetch_account(EOA),
        lambda adapter: adapter.fetch_contract(WETH_ADDRESS),
        lambda adapter: adapter.fetch_asset(WETH_ADDRESS),
        lambda adapter: adapter.chain_head(),
    ],
    ids=["account", "contract", "token", "head"],
)
async def test_every_lookup_checks_the_chain_id_first(
    node: FakeNode, base: EvmIndexer, lookup: Callable[[EvmIndexer], Awaitable[object]]
) -> None:
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(BNB_TESTNET))
    with pytest.raises(WrongNetwork, match="serves BNB Smart Chain testnet"):
        await lookup(base)
    assert [method for method, _ in node.rpc_calls] == ["eth_chainId"]


async def test_chain_id_is_confirmed_once(node: FakeNode, base: EvmIndexer) -> None:
    serve_weth(node)
    await base.chain_head()
    await base.fetch_contract(WETH_ADDRESS)
    await base.fetch_account(WETH_ADDRESS)
    assert len(node.rpc_params("eth_chainId")) == 1


async def test_chain_id_check_that_could_not_run_is_tried_again(
    node: FakeNode, base: EvmIndexer
) -> None:
    serve_weth(node)
    node.routes["rpc:eth_chainId"] = [
        Reply({"message": "restarting"}, status=503),
        Reply(WETH["replies"]["eth_chainId"]),
    ]
    with pytest.raises(NetworkError):
        await base.chain_head()
    assert await base.chain_head() == WETH_BLOCK


def test_metrics_of_no_records(base: EvmIndexer) -> None:
    assert base.chain_metrics([])["fees_eth"] == "0"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0x0", 0), ("0x1a", 26), ("0xAa36A7", SEPOLIA)],
)
def test_quantities_are_read_as_hex(value: str, expected: int) -> None:
    assert parse_quantity(value, "eth_test") == expected


@pytest.mark.parametrize("value", ["0x", "1a", "0x1g", "0x 1", "0x1_0", 26, None])
def test_malformed_quantities_are_refused(value: object) -> None:
    with pytest.raises(MalformedResponse, match="eth_test: expected a hex quantity"):
        parse_quantity(value, "eth_test")  # type: ignore[arg-type]


def test_data_must_be_whole_bytes() -> None:
    assert parse_data("0x", "eth_test") == b""
    assert parse_data("0x6080", "eth_test") == bytes.fromhex("6080")
    with pytest.raises(MalformedResponse, match="whole bytes"):
        parse_data("0x608", "eth_test")
    with pytest.raises(MalformedResponse, match="hex data"):
        parse_data("6080", "eth_test")


async def test_malformed_chain_id_is_reported(node: FakeNode, base: EvmIndexer) -> None:
    node.routes["rpc:eth_chainId"] = rpc_ok(84532)
    with pytest.raises(MalformedResponse, match="eth_chainId"):
        await base.chain_head()


async def test_reply_without_result_or_error_is_reported(node: FakeNode, base: EvmIndexer) -> None:
    node.routes["rpc:eth_chainId"] = Reply({"jsonrpc": "2.0", "id": 1})
    with pytest.raises(MalformedResponse, match="neither a result nor an error"):
        await base.chain_head()


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (rpc_error(-32602, "invalid argument 0"), InvalidInput),
        (rpc_error(429, "too many requests"), RateLimited),
        (rpc_error(-32000, "header not found"), NetworkError),
        (Reply({"jsonrpc": "2.0", "id": 1, "error": "down"}), NetworkError),
    ],
)
async def test_rpc_errors_map_to_library_errors(
    node: FakeNode, base: EvmIndexer, reply: Reply, error: type[Exception]
) -> None:
    serve_weth(node)
    node.routes["rpc:eth_getCode"] = reply
    with pytest.raises(error, match="eth_getCode"):
        await base.fetch_contract(WETH_ADDRESS)


async def test_requests_are_counted(node: FakeNode, base: EvmIndexer) -> None:
    serve_weth(node)
    await base.fetch_contract(WETH_ADDRESS)
    assert base.requests_made == 3


def test_library_reads_a_contract(node: FakeNode, tmp_path: Path) -> None:
    serve_weth(node)
    with Indexer("base", settings=settings_for(node, tmp_path, BASE, "testnet")) as indexer:
        record = indexer.contract(WETH_ADDRESS)
    assert record.id == WETH_ADDRESS


@pytest.fixture
def invoke(node: FakeNode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    serve_weth(node)
    monkeypatch.setenv("BASE_TESTNET_RPC_SERVER", node.url)
    monkeypatch.setenv("MCI_MAX_RETRIES", "0")
    monkeypatch.setenv("MCI_REQUESTS_PER_SECOND", "1000")

    def run(*arguments: str) -> Result:
        options = ["--db", str(tmp_path / "cli.db"), "--env-file", str(tmp_path / "none.env")]
        return CliRunner().invoke(cli, [*options, *arguments])

    return run


def test_cli_index_contract_on_an_evm_chain(invoke: Any) -> None:
    result = invoke("--chain", "base", "index", "contract", WETH_ADDRESS)
    assert result.exit_code == 0, result.output
    assert WETH_ADDRESS in result.stdout
    assert "Code hash" in result.stdout
    assert f"As of block  {WETH_BLOCK}" in result.stdout


def test_cli_reports_the_wrong_chain_without_the_endpoint_or_a_traceback(
    node: FakeNode, invoke: Any
) -> None:
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(BNB_TESTNET))
    result = invoke("--chain", "base", "index", "contract", WETH_ADDRESS)
    assert result.exit_code == 1
    assert "serves BNB Smart Chain testnet (chain ID 97), not Base Sepolia" in result.output
    assert "BASE_TESTNET_RPC_SERVER" in result.output
    assert node.url not in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_cli_reports_a_wrong_checksum_in_one_line(node: FakeNode, invoke: Any) -> None:
    result = invoke("--chain", "base", "index", "contract", WRONG_CHECKSUM)
    assert result.exit_code == 1
    assert "checksum" in result.output
    assert "Traceback" not in result.output
    assert node.rpc_calls == []


async def test_code_the_size_of_a_delegation_without_its_prefix_is_a_contract(
    node: FakeNode, base: EvmIndexer
) -> None:
    serve_eoa(node, code="0x" + "60" * 23)
    record = await base.fetch_account(EOA)
    assert base.describe(record)["Kind"] == "contract"
    assert (await base.fetch_contract(EOA)).native.code_size == 23


async def test_localnet_names_itself_in_messages(node: FakeNode, tmp_path: Path) -> None:
    serve_eoa(node)
    node.routes["rpc:eth_chainId"] = rpc_ok(hex(ANVIL))
    adapter = adapter_for(node, tmp_path, ETHEREUM, "localnet")
    try:
        with pytest.raises(NotFound, match=f"no contract is deployed at {EOA} on a local network"):
            await adapter.fetch_contract(EOA)
    finally:
        await adapter.aclose()


def test_default_rpc_rate_is_measured_and_configurable() -> None:
    adapter = create_indexer("base", None, Settings())
    assert adapter._rpc._client._limiter._interval == pytest.approx(0.2)
    configured = create_indexer(
        "base", None, Settings.from_env(None, environ={"MCI_REQUESTS_PER_SECOND": "50"})
    )
    assert configured._rpc._client._limiter._interval == pytest.approx(0.02)
