"""Settings, read once from ``config.toml``, ``.env`` and the process environment.

The environment wins over ``.env``, which wins over ``config.toml``. Secrets belong only in
the first two; ``config_file`` explains why.

Endpoints are held as secrets because hosted providers often put the API key in the
URL. ``.env`` is parsed with ``dotenv_values`` rather than loaded, so its secrets are
never copied into ``os.environ`` where child processes could read them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from multichain_indexer.core.config_file import FileConfig, Price, read_config_file
from multichain_indexer.core.errors import ConfigError

ENV_PREFIX = "MCI_"
DEFAULT_DATABASE = Path.home() / ".multichain-indexer" / "indexer.db"
DEFAULT_CONFIG_FILE = "config.toml"
LOCAL_URL_PREFIXES = ("http://localhost", "http://127.0.0.1")


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    database: Path = DEFAULT_DATABASE
    cache_path: Path | None = None
    requests_per_second: float = Field(default=10.0, gt=0, le=1000)
    request_timeout: float = Field(default=20.0, gt=0, le=300)
    max_retries: int = Field(default=4, ge=0, le=10)
    env: Mapping[str, SecretStr] = Field(default_factory=dict, repr=False)
    default_networks: Mapping[str, str] = Field(default_factory=dict)
    accounts: Mapping[str, Mapping[str, tuple[str, ...]]] = Field(default_factory=dict)
    prices: Mapping[str, Price] = Field(default_factory=dict)

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = ".env",
        environ: Mapping[str, str] | None = None,
        config_file: str | Path | None = DEFAULT_CONFIG_FILE,
    ) -> Settings:
        """Merge ``config_file``, ``env_file`` and the environment; the environment wins.

        Either file is skipped when it does not exist.

        Raises:
            ConfigError: A setting has an invalid value, or ``config_file`` holds a secret
                or is malformed. The message names the setting and never repeats its value.
        """
        from_file = read_config_file(Path(config_file)) if config_file else FileConfig()
        merged = {**from_file.env, **_merged_environment(env_file, environ)}
        fields = {
            key.removeprefix(ENV_PREFIX).lower(): value
            for key, value in merged.items()
            if key.startswith(ENV_PREFIX)
        }
        secrets = {key: SecretStr(value) for key, value in merged.items()}
        try:
            return cls.model_validate(
                {
                    **fields,
                    "env": secrets,
                    "default_networks": from_file.default_networks,
                    "accounts": from_file.accounts,
                    "prices": from_file.prices,
                }
            )
        except ValidationError as exc:
            names = sorted({f"{ENV_PREFIX}{str(e['loc'][0]).upper()}" for e in exc.errors()})
            raise ConfigError(f"invalid setting: {', '.join(names)}") from None

    def chain_setting(self, chain: str, network: str, name: str) -> SecretStr | None:
        """Return a per-network value such as ``ALGORAND_TESTNET_INDEXER_TOKEN``."""
        return self.env.get(f"{chain}_{network}_{name}".upper())

    def endpoint(self, chain: str, network: str, name: str, default: str) -> SecretStr:
        """Return a per-network URL, or ``default`` when none is configured.

        Raises:
            ConfigError: The URL would send requests unencrypted to a remote host.
        """
        configured = self.chain_setting(chain, network, name)
        url = default if configured is None else configured.get_secret_value()
        if not url.startswith(("https://", *LOCAL_URL_PREFIXES)):
            setting = f"{chain}_{network}_{name}".upper()
            raise ConfigError(f"{setting} must use https (plain http only for localhost)")
        return SecretStr(url)

    def secret_values(self) -> tuple[str, ...]:
        """Return every value the log redaction filter must mask."""
        chain_settings = [
            secret.get_secret_value()
            for key, secret in self.env.items()
            if key.endswith(("_SERVER", "_TOKEN", "_API_KEY"))
        ]
        return tuple(value for value in chain_settings if value)


def _merged_environment(
    env_file: str | Path | None, environ: Mapping[str, str] | None
) -> dict[str, str]:
    from_file = dotenv_values(env_file) if env_file and Path(env_file).is_file() else {}
    merged = {key: value for key, value in from_file.items() if value}
    source = os.environ if environ is None else environ
    merged.update({key: value for key, value in source.items() if value})
    return merged
