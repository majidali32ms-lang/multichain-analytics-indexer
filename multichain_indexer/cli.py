"""The command line, a thin layer over the Python API."""

from __future__ import annotations

import functools
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Concatenate, ParamSpec, TypeVar

import click

from multichain_indexer.api import Indexer
from multichain_indexer.core.config import Settings
from multichain_indexer.core.errors import IndexerError
from multichain_indexer.core.export import EXPORT_FORMATS
from multichain_indexer.core.models import SyncResult, TransactionRecord
from multichain_indexer.core.observability import configure_logging
from multichain_indexer.core.registry import PLANNED_CHAINS

_P = ParamSpec("_P")
_T = TypeVar("_T")

DEFAULT_RECENT_TRANSACTIONS = 10


class _Options:
    """Options given before the command, resolved into an ``Indexer`` on demand."""

    def __init__(
        self,
        chain: str,
        network: str | None,
        database: Path | None,
        settings: Settings,
        quiet: bool,
    ) -> None:
        self.chain = chain
        self.network = network
        self.database = database
        self.settings = settings
        self.quiet = quiet

    def open_indexer(self) -> Indexer:
        return Indexer(self.chain, self.network, settings=self.settings, database=self.database)


def _with_indexer(
    command: Callable[Concatenate[Indexer, _P], _T],
) -> Callable[Concatenate[_Options, _P], _T]:
    """Open the indexer for a command, and turn expected failures into a one-line error."""

    @functools.wraps(command)
    def wrapper(options: _Options, /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        try:
            with options.open_indexer() as indexer:
                return command(indexer, *args, **kwargs)
        except IndexerError as exc:
            raise click.ClickException(str(exc)) from None

    return wrapper


@click.group()
@click.option(
    "--chain",
    type=click.Choice(PLANNED_CHAINS),
    default="algorand",
    show_default=True,
    help="Chain to work on. Chains not yet supported say so when used.",
)
@click.option(
    "--network",
    type=click.Choice(["mainnet", "testnet", "localnet"]),
    help="Network to read. Defaults to the chain's test network.",
)
@click.option("--db", "database", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--env-file", default=".env", show_default=True)
@click.option("-v", "--verbose", is_flag=True, help="Log progress as well as problems.")
@click.option("-q", "--quiet", is_flag=True, help="Log errors only and hide sync progress.")
@click.pass_context
def cli(
    context: click.Context,
    chain: str,
    network: str | None,
    database: Path | None,
    env_file: str,
    verbose: bool,
    quiet: bool,
) -> None:
    """Index and analyse blockchain accounts and transactions."""
    try:
        settings = Settings.from_env(env_file)
    except IndexerError as exc:
        raise click.ClickException(str(exc)) from None
    configure_logging(verbose=verbose, quiet=quiet, secrets=settings.secret_values())
    context.obj = _Options(chain, network, database, settings, quiet)


@cli.group()
def index() -> None:
    """Look up one account, ASA or app on the node."""


@index.command("account")
@click.argument("address")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=DEFAULT_RECENT_TRANSACTIONS,
    show_default=True,
    help="Recent transactions to show.",
)
@click.pass_obj
@_with_indexer
def index_account(indexer: Indexer, address: str, limit: int) -> None:
    """Show an account's state, balances in Algo, and its recent transactions."""
    _echo_fields(indexer.describe(indexer.account(address)))
    click.echo()
    for record in indexer.account_transactions(address, limit=limit):
        click.echo(_transaction_line(indexer, record))


@index.command("asset")
@click.argument("asa_id")
@click.pass_obj
@_with_indexer
def index_asset(indexer: Indexer, asa_id: str) -> None:
    """Show an ASA by its ASA ID."""
    _echo_fields(indexer.describe(indexer.asset(asa_id)))


@index.command("app")
@click.argument("app_id")
@click.pass_obj
@_with_indexer
def index_app(indexer: Indexer, app_id: str) -> None:
    """Show an application by its app ID."""
    _echo_fields(indexer.describe(indexer.contract(app_id)))


@cli.command()
@click.argument("address")
@click.pass_obj
def sync(options: _Options, address: str) -> None:
    """Store an account's new transactions; only what arrived since the last sync."""
    _sync(options, address, show_progress=not options.quiet)


@_with_indexer
def _sync(indexer: Indexer, address: str, *, show_progress: bool) -> None:
    def report_progress(stored: int) -> None:
        click.echo(f"\rStored {stored} transactions", nl=False, err=True)

    result = indexer.sync(address, on_progress=report_progress if show_progress else None)
    if show_progress and result.records_fetched:
        click.echo(err=True)
    click.echo(_sync_summary(result))


@cli.command()
@click.option("--days", type=click.IntRange(min=1), default=30, show_default=True)
@click.option("--account", help="Limit to one synced account.")
@click.pass_obj
@_with_indexer
def metrics(indexer: Indexer, days: int, account: str | None) -> None:
    """Print metrics over stored transactions as JSON; fees are in the chain's base unit."""
    report = indexer.metrics(days, account=account)
    click.echo(json.dumps(report.model_dump(mode="json"), indent=2))


@cli.command()
@click.option("--format", "export_format", type=click.Choice(EXPORT_FORMATS), required=True)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="File to write. CSV and JSON go to standard output without it.",
)
@click.option("--account", help="Limit to one synced account.")
@click.option("--days", type=click.IntRange(min=1), help="Limit to the last N days.")
@click.pass_obj
@_with_indexer
def export(
    indexer: Indexer,
    export_format: str,
    output: Path | None,
    account: str | None,
    days: int | None,
) -> None:
    """Export stored transactions."""
    text = indexer.export(export_format, output, account=account, days=days)
    if text is not None:
        sys.stdout.write(text.rstrip("\n") + "\n")


def main() -> None:
    cli()


def _echo_fields(fields: dict[str, str]) -> None:
    width = max(len(label) for label in fields)
    for label, value in fields.items():
        click.echo(f"{label.ljust(width)}  {value}")


def _transaction_line(indexer: Indexer, record: TransactionRecord) -> str:
    details = indexer.describe(record)
    return "  ".join([record.time.strftime("%Y-%m-%d %H:%M"), record.id, *details.values()])


def _sync_summary(result: SyncResult) -> str:
    verb = "Resumed and synced" if result.resumed else "Synced"
    return (
        f"{verb} {result.account} on {result.chain} {result.network}: "
        f"{result.records_fetched} transactions up to height {result.to_height}, "
        f"{result.requests_made} requests in {result.elapsed_seconds:.1f}s"
    )
