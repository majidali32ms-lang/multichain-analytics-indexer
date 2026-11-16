"""Reads ``config.toml``: endpoints, networks, accounts and prices, but never secrets.

The file is meant to be shared and committed, so keys and tokens stay in ``.env`` or the
environment. A secret found here is refused with an error that names the key, never its
value. Every other value becomes the setting it names, at the lowest precedence:

    [settings]                  MCI_<NAME>, e.g. requests_per_second = 2
    [chains.<chain>]            network = "signet", the chain's default network
    [chains.<chain>.<network>]  <CHAIN>_<NETWORK>_<NAME>, e.g. esplora_server = "https://..."
    [accounts.<chain>]          <network> = ["address", ...], for ``sync --chain all``
    [prices.<chain>]            usd = 98000.5, date = 2026-11-15
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from multichain_indexer.core.errors import ConfigError

SECRET_SUFFIXES = ("_API_KEY", "_TOKEN")
SECRET_NAMES = ("API_KEY", "TOKEN")
MAINNET = "mainnet"
SECTIONS = ("settings", "chains", "accounts", "prices")

Table = dict[str, object]


@dataclass(frozen=True)
class Price:
    """The USD price of a chain's native token, as the user gave it, and its date."""

    usd: Decimal
    date: date


@dataclass(frozen=True)
class FileConfig:
    """What ``config.toml`` holds, split by use."""

    env: dict[str, str] = field(default_factory=dict)
    default_networks: dict[str, str] = field(default_factory=dict)
    accounts: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    prices: dict[str, Price] = field(default_factory=dict)


def read_config_file(path: Path) -> FileConfig:
    """Read ``path``; a file that does not exist is an empty configuration.

    Raises:
        ConfigError: The file is not valid TOML, holds a secret, or holds a value of the
            wrong shape. The message names the key and never repeats a value.
    """
    if not path.is_file():
        return FileConfig()
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML: {exc}") from None
    _refuse_secrets(document, ())
    _refuse_unknown_sections(document)
    env, default_networks = _chain_settings(_table(document, "chains"))
    return FileConfig(
        env={**_global_settings(_table(document, "settings")), **env},
        default_networks=default_networks,
        accounts=_accounts(_table(document, "accounts")),
        prices=_prices(_table(document, "prices")),
    )


def _refuse_secrets(table: Table, path: tuple[str, ...]) -> None:
    for key, value in table.items():
        name = key.upper()
        if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES):
            dotted = ".".join((*path, key))
            raise ConfigError(
                f"config.toml must not hold secrets: move {dotted} to .env or the environment"
            )
        if isinstance(value, dict):
            _refuse_secrets(value, (*path, key))


def _refuse_unknown_sections(document: Table) -> None:
    unknown = sorted(set(document) - set(SECTIONS))
    if unknown:
        raise ConfigError(
            f"unknown config.toml section {unknown[0]!r}; known sections: {', '.join(SECTIONS)}"
        )


def _global_settings(table: Table) -> dict[str, str]:
    return {f"MCI_{key.upper()}": _scalar(value, f"settings.{key}") for key, value in table.items()}


def _chain_settings(chains: Table) -> tuple[dict[str, str], dict[str, str]]:
    env: dict[str, str] = {}
    default_networks: dict[str, str] = {}
    for chain, entries in chains.items():
        for key, value in _as_table(entries, f"chains.{chain}").items():
            if key == "network":
                default_networks[chain] = _default_network(value, chain)
                continue
            for name, setting in _as_table(value, f"chains.{chain}.{key}").items():
                where = f"chains.{chain}.{key}.{name}"
                env[f"{chain}_{key}_{name}".upper()] = _scalar(setting, where)
    return env, default_networks


def _default_network(value: object, chain: str) -> str:
    network = _scalar(value, f"chains.{chain}.network")
    if network == MAINNET:
        raise ConfigError(
            f"chains.{chain}.network cannot be mainnet in config.toml; "
            "pass --network mainnet on the command line to read mainnet"
        )
    return network


def _accounts(table: Table) -> dict[str, dict[str, tuple[str, ...]]]:
    accounts: dict[str, dict[str, tuple[str, ...]]] = {}
    for chain, networks in table.items():
        per_network: dict[str, tuple[str, ...]] = {}
        for network, listed in _as_table(networks, f"accounts.{chain}").items():
            where = f"accounts.{chain}.{network}"
            if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
                raise ConfigError(f"{where} must be a list of addresses")
            per_network[network] = tuple(str(item) for item in listed)
        accounts[chain] = per_network
    return accounts


def _prices(table: Table) -> dict[str, Price]:
    prices: dict[str, Price] = {}
    for chain, entry in table.items():
        values = _as_table(entry, f"prices.{chain}")
        usd, day = values.get("usd"), values.get("date")
        if isinstance(usd, bool) or not isinstance(usd, int | float) or usd < 0:
            raise ConfigError(f"prices.{chain}.usd must be a number of US dollars, 0 or more")
        if not isinstance(day, date):
            raise ConfigError(f"prices.{chain}.date must be a date such as 2026-11-15")
        prices[chain] = Price(usd=Decimal(str(usd)), date=day)
    return prices


def _table(document: Table, name: str) -> Table:
    return _as_table(document.get(name, {}), name)


def _as_table(value: object, where: str) -> Table:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table")
    return value


def _scalar(value: object, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    raise ConfigError(f"{where} must be a string or a number")
