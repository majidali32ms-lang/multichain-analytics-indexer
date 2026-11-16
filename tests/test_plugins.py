"""Third-party chains arrive through the ``multichain_indexer.adapters`` entry point group.

Each test installs the template plugin from ``examples/plugin-template`` the way pip would
leave it: a ``.dist-info`` folder with ``entry_points.txt`` on the import path.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from pydantic import SecretStr

from multichain_indexer import Indexer, UnsupportedChain
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import ConfigError
from multichain_indexer.core.registry import available_chains, create_indexer
from tests.conftest import FakeNode, Reply

TEMPLATE = Path(__file__).parent.parent / "examples" / "plugin-template" / "src"
ADDRESS = "ex1qpzry9x8gf2t"
TRANSACTIONS = [
    {
        "id": "tx2",
        "time": 1_763_200_000,
        "from": ADDRESS,
        "to": "ex1receiver01",
        "amount": 2_500_000,
        "fee": 1_000,
        "height": 118,
        "memo": "rent",
    },
    {
        "id": "tx1",
        "time": 1_763_100_000,
        "from": "ex1sender0001",
        "to": ADDRESS,
        "amount": 7_000_000,
        "fee": 1_000,
        "height": 100,
    },
]

Install = Callable[..., None]


@pytest.fixture
def install_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Install]:
    def install(
        chain: str = "examplechain", target: str = "examplechain_indexer:create_indexer"
    ) -> None:
        dist_info = tmp_path / "site" / "examplechain_indexer-0.1.0.dist-info"
        dist_info.mkdir(parents=True, exist_ok=True)
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: examplechain-indexer\nVersion: 0.1.0\n", encoding="utf-8"
        )
        (dist_info / "entry_points.txt").write_text(
            f"[multichain_indexer.adapters]\n{chain} = {target}\n", encoding="utf-8"
        )
        monkeypatch.syspath_prepend(str(tmp_path / "site"))
        monkeypatch.syspath_prepend(str(TEMPLATE))

    yield install
    for name in [name for name in sys.modules if name.startswith("examplechain_indexer")]:
        del sys.modules[name]


@pytest.fixture
def plugin_node(node: FakeNode) -> FakeNode:
    node.routes["/head"] = Reply({"height": 120})
    node.routes[f"/accounts/{ADDRESS}"] = Reply({"balance": 5_000_000})
    node.routes[f"/accounts/{ADDRESS}/transactions"] = Reply({"transactions": TRANSACTIONS})
    return node


def plugin_settings(node: FakeNode, tmp_path: Path) -> Settings:
    return Settings(
        database=tmp_path / "plugin.db",
        max_retries=0,
        env={"EXAMPLECHAIN_TESTNET_API_SERVER": SecretStr(node.url)},
    )


def test_an_installed_plugin_is_listed_after_the_built_in_chains(install_plugin: Install) -> None:
    assert "examplechain" not in available_chains()
    install_plugin()
    assert available_chains()[-1] == "examplechain"


def test_a_plugin_is_imported_only_when_its_chain_is_used(install_plugin: Install) -> None:
    install_plugin()
    available_chains()
    assert "examplechain_indexer" not in sys.modules


def test_a_plugin_chain_syncs_and_reports_through_the_python_api(
    install_plugin: Install, plugin_node: FakeNode, tmp_path: Path
) -> None:
    install_plugin()
    with Indexer("examplechain", settings=plugin_settings(plugin_node, tmp_path)) as indexer:
        assert indexer.account(ADDRESS).balance == 5_000_000
        assert indexer.sync(ADDRESS).records_fetched == 2
        report = indexer.metrics(days=36500)
    assert (report.chain, report.transactions, report.total_fees) == ("examplechain", 2, 2_000)
    assert report.native == {"memos": 1}


def test_the_command_line_picks_up_a_plugin_chain(
    install_plugin: Install,
    plugin_node: FakeNode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_plugin()
    monkeypatch.setenv("EXAMPLECHAIN_TESTNET_API_SERVER", plugin_node.url)

    def run(*arguments: str) -> Result:
        options = ["--chain", "examplechain", "--db", str(tmp_path / "cli.db")]
        options += ["--env-file", str(tmp_path / "none.env")]
        return CliRunner().invoke(cli, [*options, *arguments])

    account = run("index", "account", ADDRESS)
    assert account.exit_code == 0, account.output
    assert "5 EXC" in account.stdout
    assert run("sync", ADDRESS).exit_code == 0
    assert json.loads(run("metrics", "--days", "36500").stdout)["transactions"] == 2


def test_a_plugin_cannot_take_a_built_in_chain_name(
    install_plugin: Install, tmp_path: Path
) -> None:
    install_plugin(chain="bitcoin")
    with pytest.raises(ConfigError, match="examplechain-indexer registers chain 'bitcoin'"):
        available_chains()
    with pytest.raises(ConfigError, match="built in"):
        create_indexer("bitcoin", None, Settings(database=tmp_path / "db"))
    result = CliRunner().invoke(cli, ["--chain", "bitcoin", "pq-status"])
    assert result.exit_code == 2
    assert "registers chain 'bitcoin', which is built in" in result.output
    assert "Traceback" not in result.output


def test_a_factory_that_returns_no_adapter_is_refused(
    install_plugin: Install, tmp_path: Path
) -> None:
    install_plugin(target="builtins:slice")
    with pytest.raises(ConfigError, match="did not return a BaseIndexer"):
        create_indexer("examplechain", None, Settings(database=tmp_path / "db"))


def test_an_unknown_chain_names_the_plugin_chains_too(install_plugin: Install) -> None:
    install_plugin()
    with pytest.raises(UnsupportedChain, match="bitcoin, examplechain"):
        create_indexer("dogecoin", None, Settings())
