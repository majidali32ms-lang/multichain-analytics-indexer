"""BNB Smart Chain's own labels: system contracts, system transactions, validators."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from eth_abi.abi import encode
from eth_utils.address import to_checksum_address
from pydantic import SecretStr

from multichain_indexer import AsyncIndexer, MalformedResponse, UnsupportedChain
from multichain_indexer.adapters.evm.adapter import indexer_class
from multichain_indexer.adapters.evm.bsc import (
    SYSTEM_CONTRACTS,
    SYSTEM_TRANSACTION_TARGETS,
    bnb_labels,
    cas20_variant,
    read_validators,
    system_contract_name,
)
from multichain_indexer.adapters.evm.client import EvmRpc
from multichain_indexer.adapters.evm.metrics import evm_metrics
from multichain_indexer.adapters.evm.models import EvmTransaction, EvmValidatorSet
from multichain_indexer.adapters.evm.profiles import BNB, ETHEREUM
from multichain_indexer.adapters.evm.transactions import RawTransaction, parse_transaction
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.http import ApiClient
from multichain_indexer.core.models import TransactionRecord
from tests.conftest import FakeNode, Reply

FIXTURES = Path(__file__).parent / "fixtures" / "evm"


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"tx_{name}.json").read_text("utf-8"))
    return loaded


def parse(raw: dict[str, Any], chain: str = "bnb") -> TransactionRecord:
    return parse_transaction(
        RawTransaction(raw["transaction"], raw["receipt"], raw["block"]),
        chain=chain,
        network="mainnet",
        final=True,
    )


def native(record: TransactionRecord) -> EvmTransaction:
    assert isinstance(record.native, EvmTransaction)
    return record.native


def test_the_list_is_system_sol_and_parlia_leaves_three_out() -> None:
    assert len(SYSTEM_CONTRACTS) == 17
    assert len(SYSTEM_TRANSACTION_TARGETS) == 14
    assert system_contract_name("0x0000000000000000000000000000000000002002") == "StakeHub"
    assert system_contract_name(None) is None
    assert system_contract_name("0x" + "11" * 20) is None


def test_a_system_transaction_is_labelled_with_its_contract() -> None:
    raw = fixture("bsc_mainnet_system")
    labels = native(parse(raw)).bnb
    assert labels is not None
    assert labels.system_transaction
    assert labels.system_contract == "BSCValidatorSet"
    assert labels.validator.lower() == raw["block"]["miner"] == raw["transaction"]["from"]
    assert raw["transaction"]["input"].startswith("0xf340fa01")


def test_a_zero_fee_transaction_from_someone_else_is_not_a_system_transaction() -> None:
    raw = fixture("bsc_mainnet_zero_gas")
    assert raw["receipt"]["effectiveGasPrice"] == "0x0"
    labels = native(parse(raw)).bnb
    assert labels is not None
    assert not labels.system_transaction
    assert labels.system_contract is None


@pytest.mark.parametrize(
    ("receiver", "price", "sender"),
    [
        ("0x0000000000000000000000000000000000002003", 0, "coinbase"),
        ("0x0000000000000000000000000000000000001000", 1, "coinbase"),
        ("0x0000000000000000000000000000000000001000", 0, "other"),
        (None, 0, "coinbase"),
    ],
)
def test_every_parlia_condition_is_needed(receiver: str | None, price: int, sender: str) -> None:
    coinbase = "0x" + "ab" * 20
    labels = bnb_labels(
        sender=coinbase if sender == "coinbase" else "0x" + "cd" * 20,
        receiver=receiver,
        effective_gas_price=price,
        coinbase=coinbase,
    )
    assert labels is not None
    assert not labels.system_transaction


def test_other_chains_and_headers_without_a_coinbase_get_no_labels() -> None:
    raw = fixture("bsc_mainnet_system")
    assert native(parse(raw, chain="ethereum")).bnb is None
    headless = {**raw, "block": {k: v for k, v in raw["block"].items() if k != "miner"}}
    assert native(parse(headless)).bnb is None


def test_describe_and_metrics_show_the_labels() -> None:
    adapter = indexer_class(BNB)("mainnet", rpc=None, settings=None)  # type: ignore[arg-type]
    system = parse(fixture("bsc_mainnet_system"))
    fields = adapter.describe(system)
    assert fields["System transaction"] == "yes, to BSCValidatorSet"
    assert fields["Validator"] == native(system).bnb.validator  # type: ignore[union-attr]
    ordinary = parse(fixture("bsc_mainnet_zero_gas"))
    assert "System transaction" not in adapter.describe(ordinary)
    report = evm_metrics([system, ordinary], native_symbol="BNB", token_standard="BEP-20")
    assert report["bnb_system_transactions"] == 1
    assert report["top_validators_by_transactions"] == [
        {"validator": native(system).bnb.validator, "count": 2}  # type: ignore[union-attr]
    ]
    other = evm_metrics(
        [parse(fixture("sepolia_native"), chain=ETHEREUM.name)],
        native_symbol="ETH",
        token_standard="ERC-20",
    )
    assert "bnb_system_transactions" not in other


VALIDATORS = json.loads((FIXTURES / "validators_bsc_testnet.json").read_text("utf-8"))


def replay_validators(node: FakeNode) -> None:
    replies = {(e["method"], json.dumps(e["params"])): e["reply"] for e in VALIDATORS["exchanges"]}

    def answer(method: str) -> Any:
        return lambda params: Reply(replies[(method, json.dumps(params))])

    for method, _ in replies:
        node.handlers[f"rpc:{method}"] = answer(method)


def validator_settings(node: FakeNode, tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "v.db",
        requests_per_second=1000,
        max_retries=0,
        env={"BNB_TESTNET_RPC_SERVER": SecretStr(node.url)},
    )


async def test_validator_set_matches_the_saved_read(node: FakeNode, tmp_path: Path) -> None:
    replay_validators(node)
    async with AsyncIndexer("bnb", settings=validator_settings(node, tmp_path)) as indexer:
        validator_set = await indexer.validators()
    expected = {**VALIDATORS["report"], "observed_at": validator_set.observed_at}
    assert validator_set == EvmValidatorSet.model_validate(expected)
    assert [v.moniker for v in validator_set.validators][:3] == ["Ararat", "Kita", "Fuji"]
    assert len(node.rpc_params("eth_call")) == 3


async def test_validators_are_for_bnb_smart_chain_only(tmp_path: Path) -> None:
    settings = Settings(database=tmp_path / "v.db")
    async with AsyncIndexer("opbnb", settings=settings) as indexer:
        with pytest.raises(UnsupportedChain, match="has no BNB Smart Chain validator set"):
            await indexer.validators()
    adapter = indexer_class(ETHEREUM)("testnet", rpc=None, settings=None)  # type: ignore[arg-type]
    with pytest.raises(UnsupportedChain, match="Ethereum has no BNB Smart Chain validator set"):
        await adapter.fetch_validators()


async def test_odd_system_contract_replies(node: FakeNode) -> None:
    rpc = EvmRpc(ApiClient(SecretStr(node.url), max_retries=0))
    consensus = ["0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "66" * 20]
    first, second = "0x" + "44" * 20, "0x" + "55" * 20
    operators = [first, second, "0x" + "0" * 40]
    replies = iter(
        [
            encode(["address[]"], [consensus]),
            encode(
                ["(bool,bytes)[]"],
                [
                    [(True, encode(["address"], [operator])) for operator in operators]
                    + [(False, b"")]
                ],
            ),
            encode(["(bool,bytes)[]"], [[(True, b""), (False, b"")]]),
        ]
    )

    def answer(params: list[Any]) -> Reply:
        return Reply({"jsonrpc": "2.0", "id": 1, "result": "0x" + next(replies).hex()})

    node.handlers["rpc:eth_call"] = answer
    try:
        validators = await read_validators(rpc, 1)
    finally:
        await rpc.aclose()
    assert [v.operator_address for v in validators] == [
        to_checksum_address(first),
        to_checksum_address(second),
        None,
        None,
    ]
    assert all(v.moniker is None for v in validators)


async def test_a_broken_validator_list_is_malformed(node: FakeNode) -> None:
    rpc = EvmRpc(ApiClient(SecretStr(node.url), max_retries=0))
    node.routes["rpc:eth_call"] = Reply({"jsonrpc": "2.0", "id": 1, "result": "0x1234"})
    try:
        with pytest.raises(MalformedResponse, match="getValidators"):
            await read_validators(rpc, 1)
    finally:
        await rpc.aclose()


def test_cli_lists_validators_with_their_names(node: FakeNode, tmp_path: Path) -> None:
    replay_validators(node)
    env = {
        "BNB_TESTNET_RPC_SERVER": node.url,
        "MCI_REQUESTS_PER_SECOND": "1000",
        "MCI_MAX_RETRIES": "0",
    }
    result = CliRunner(env=env).invoke(
        cli,
        [
            "--chain",
            "bnb",
            "--db",
            str(tmp_path / "c.db"),
            "--env-file",
            str(tmp_path / "n.env"),
            "index",
            "validators",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith(
        f"10 validators at block {int(VALIDATORS['exchanges'][1]['reply']['result'], 16)}"
    )
    assert "  Ararat" in result.output.splitlines()[1]
    nameless = CliRunner().invoke(
        cli, ["--chain", "opbnb", "--db", str(tmp_path / "d.db"), "index", "validators"]
    )
    assert nameless.exit_code == 1
    assert "index validators is for bnb, not opbnb" in nameless.output


CAS20 = json.loads((FIXTURES / "cas20_token_bsc_testnet.json").read_text("utf-8"))


@pytest.mark.parametrize(
    ("address", "code", "variant"),
    [
        ("0xca52000000000000000000805c65560851843659", b"\xef", "asset"),
        ("0xca52000000000000000001a96a592e8b818886d1", b"\xef", "stablecoin"),
        ("0xca52000000000000000002a96a592e8b818886d1", b"\xef", "variant 2"),
        ("0xca52000000000000000001a96a592e8b818886d1", b"", None),
        ("0xca52000000000000000001a96a592e8b818886d1", b"\xef\x00", None),
        ("0x0000000000000000000000000000000000001000", b"\xef", None),
    ],
)
def test_cas20_tokens_are_known_by_address_and_sentinel(
    address: str, code: bytes, variant: str | None
) -> None:
    assert cas20_variant(address, code) == variant


async def test_a_real_cas20_token_is_read_and_labelled(node: FakeNode, tmp_path: Path) -> None:
    replies = {(e["method"], json.dumps(e["params"])): e["reply"] for e in CAS20["exchanges"]}
    for method, _ in replies:
        node.handlers[f"rpc:{method}"] = (
            lambda m: lambda params: Reply(replies[(m, json.dumps(params))])
        )(method)
    token = "0xca52000000000000000001a96a592e8b818886d1"
    async with AsyncIndexer("bnb", settings=validator_settings(node, tmp_path)) as indexer:
        record = await indexer.asset(token)
        fields = indexer.describe(record)
    assert (record.name, record.unit, record.decimals) == ("48club Chapel Test", "T48", 6)
    assert fields["Standard"] == "BEP-20 (CAS20 stablecoin, BEP-702)"


def test_index_token_on_an_address_without_bytecode_is_a_clear_error(
    node: FakeNode, tmp_path: Path
) -> None:
    node.routes["rpc:eth_chainId"] = Reply({"jsonrpc": "2.0", "id": 1, "result": "0x61"})
    node.routes["rpc:eth_blockNumber"] = Reply({"jsonrpc": "2.0", "id": 1, "result": "0x10"})
    node.routes["rpc:eth_getCode"] = Reply({"jsonrpc": "2.0", "id": 1, "result": "0x"})
    env = {"BNB_TESTNET_RPC_SERVER": node.url, "MCI_MAX_RETRIES": "0"}
    dead = "0x000000000000000000000000000000000000dEaD"
    options = ["--chain", "bnb", "--db", str(tmp_path / "t.db"), "--env-file", str(tmp_path / "n")]
    result = CliRunner(env=env).invoke(cli, [*options, "index", "token", dead])
    assert result.exit_code == 1
    assert result.output == (
        f"Error: no contract is deployed at {dead} on BNB Smart Chain testnet\n"
    )


async def test_validators_on_a_chain_without_an_evm_adapter(tmp_path: Path) -> None:
    async with AsyncIndexer("algorand", settings=Settings(database=tmp_path / "a.db")) as indexer:
        with pytest.raises(UnsupportedChain, match="algorand has no BNB Smart Chain validator"):
            await indexer.validators()
