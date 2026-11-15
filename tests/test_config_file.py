"""config.toml: settings at the lowest precedence, and never a secret."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from multichain_indexer import AsyncIndexer
from multichain_indexer.cli import cli
from multichain_indexer.core.config import Settings
from multichain_indexer.core.config_file import Price, read_config_file
from multichain_indexer.core.errors import ConfigError

SECRET_VALUE = "not-a-real-secret"
FULL = """
[settings]
requests_per_second = 2.5
max_retries = 2

[chains.bitcoin]
network = "signet"

[chains.bitcoin.signet]
esplora_server = "https://esplora.example.org/signet/api"

[chains.ethereum.testnet]
history_source = "etherscan"

[accounts.bitcoin]
signet = ["tb1qexample0", "tb1qexample1"]
mainnet = ["bc1qexample2"]

[prices.bitcoin]
usd = 98000.5
date = 2026-11-15
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_every_section_becomes_the_setting_it_names(tmp_path: Path) -> None:
    settings = Settings.from_env(None, environ={}, config_file=write(tmp_path, FULL))
    assert settings.requests_per_second == 2.5
    assert settings.max_retries == 2
    assert settings.default_networks == {"bitcoin": "signet"}
    server = settings.chain_setting("bitcoin", "signet", "ESPLORA_SERVER")
    assert server is not None
    assert server.get_secret_value() == "https://esplora.example.org/signet/api"
    history = settings.chain_setting("ethereum", "testnet", "HISTORY_SOURCE")
    assert history is not None
    assert history.get_secret_value() == "etherscan"
    assert settings.accounts["bitcoin"] == {
        "signet": ("tb1qexample0", "tb1qexample1"),
        "mainnet": ("bc1qexample2",),
    }
    assert settings.prices == {"bitcoin": Price(usd=Decimal("98000.5"), date=date(2026, 11, 15))}


def test_the_environment_wins_over_env_file_which_wins_over_config_toml(tmp_path: Path) -> None:
    config = write(tmp_path, "[settings]\nmax_retries = 1\nrequest_timeout = 9\ncache_path = 'c'\n")
    env_file = tmp_path / ".env"
    env_file.write_text("MCI_MAX_RETRIES=2\nMCI_REQUEST_TIMEOUT=8\n", encoding="utf-8")
    settings = Settings.from_env(env_file, environ={"MCI_MAX_RETRIES": "3"}, config_file=config)
    assert (settings.max_retries, settings.request_timeout) == (3, 8)
    assert settings.cache_path == Path("c")


def test_a_missing_config_file_changes_nothing(tmp_path: Path) -> None:
    settings = Settings.from_env(None, environ={}, config_file=tmp_path / "absent.toml")
    assert settings == Settings.from_env(None, environ={}, config_file=None)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        (
            f'[chains.ethereum.testnet]\nrpc_api_key = "{SECRET_VALUE}"\n',
            "chains.ethereum.testnet.rpc_api_key",
        ),
        (f'[settings]\nblockscout_api_key = "{SECRET_VALUE}"\n', "settings.blockscout_api_key"),
        (
            f'[chains.algorand.testnet]\nindexer_token = "{SECRET_VALUE}"\n',
            "chains.algorand.testnet.indexer_token",
        ),
        (f'[chains.near.mainnet]\nTOKEN = "{SECRET_VALUE}"\n', "chains.near.mainnet.TOKEN"),
        (f'[settings.api_key]\nvalue = "{SECRET_VALUE}"\n', "settings.api_key"),
    ],
)
def test_a_secret_is_refused_and_only_its_key_is_named(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(ConfigError) as refused:
        read_config_file(write(tmp_path, text))
    assert key in str(refused.value)
    assert ".env" in str(refused.value)
    assert SECRET_VALUE not in str(refused.value)


def test_the_command_line_reports_a_secret_in_one_line_without_its_value(tmp_path: Path) -> None:
    config = write(tmp_path, f'[settings]\netherscan_api_key = "{SECRET_VALUE}"\n')
    result = CliRunner().invoke(
        cli, ["--config", str(config), "--env-file", str(tmp_path / "none.env"), "pq-status"]
    )
    assert result.exit_code == 1
    assert "settings.etherscan_api_key" in result.output
    assert SECRET_VALUE not in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[chains.bitcoin]\nnetwork = "mainnet"\n', "pass --network mainnet on the command line"),
        ("[wallets]\nx = 1\n", "unknown config.toml section 'wallets'"),
        ("[settings\n", "not valid TOML"),
        ("chains = 3\n", "chains must be a table"),
        ("[chains.bitcoin]\nsignet = 4\n", "chains.bitcoin.signet must be a table"),
        ("[settings]\nmax_retries = [1]\n", "settings.max_retries must be a string or a number"),
        ('[accounts.bitcoin]\nsignet = "tb1q"\n', "accounts.bitcoin.signet must be a list"),
        ('[prices.bitcoin]\nusd = "a lot"\ndate = 2026-11-15\n', "prices.bitcoin.usd"),
        ("[prices.bitcoin]\nusd = -1\ndate = 2026-11-15\n", "prices.bitcoin.usd"),
        ("[prices.bitcoin]\nusd = true\ndate = 2026-11-15\n", "prices.bitcoin.usd"),
        ('[prices.bitcoin]\nusd = 1\ndate = "yesterday"\n', "prices.bitcoin.date"),
    ],
)
def test_malformed_files_are_refused_by_key(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        read_config_file(write(tmp_path, text))


def test_booleans_become_settings_text(tmp_path: Path) -> None:
    config = read_config_file(
        write(tmp_path, "[chains.solana.devnet]\nflag = true\nother = false\n")
    )
    assert config.env == {"SOLANA_DEVNET_FLAG": "true", "SOLANA_DEVNET_OTHER": "false"}


async def test_the_chains_network_in_config_toml_is_its_default(tmp_path: Path) -> None:
    settings = Settings.from_env(None, environ={}, config_file=write(tmp_path, FULL))
    async with AsyncIndexer("bitcoin", settings=settings, database=tmp_path / "db") as indexer:
        assert indexer.network == "signet"
    async with AsyncIndexer(
        "bitcoin", "testnet4", settings=settings, database=tmp_path / "db"
    ) as indexer:
        assert indexer.network == "testnet4"
