"""The command line, a thin layer over the Python API."""

from __future__ import annotations

import errno
import functools
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Concatenate, ParamSpec, TextIO, TypeVar

import click

from multichain_indexer.api import Indexer
from multichain_indexer.core.config import DEFAULT_CONFIG_FILE, Settings
from multichain_indexer.core.errors import ConfigError, IndexerError, InvalidInput
from multichain_indexer.core.export import EXPORT_FORMATS
from multichain_indexer.core.models import SyncResult, TransactionRecord
from multichain_indexer.core.observability import configure_logging
from multichain_indexer.core.registry import PLANNED_CHAINS, available_chains
from multichain_indexer.crosschain import ALL_CHAINS, CrossChain, CrossChainReport
from multichain_indexer.db import Database, chosen_database, database_secrets, is_postgres
from multichain_indexer.follow import FollowSummary, default_interval
from multichain_indexer.pq import PQ_STATUS
from multichain_indexer.report import REPORT_FORMATS, metrics_table, write_report

if sys.platform == "win32":
    import ctypes
    import msvcrt
else:
    import select

_P = ParamSpec("_P")
_T = TypeVar("_T")

DEFAULT_RECENT_TRANSACTIONS = 10
EVM_CHAINS = ("ethereum", "arbitrum", "optimism", "base", "polygon", "bnb")
# Lines a data provider's terms require wherever its data is shown, keyed by the history
# source named in a record's native block. Human-readable output only, never in exports.
ATTRIBUTIONS = {"etherscan": "Powered by Etherscan.io APIs"}
# The exit code Click uses when the reader of a pipe goes away, as in `... | head`.
EXIT_OUTPUT_CLOSED = 1
# Some accounts of a cross-chain sync failed; the others were synced and reported.
EXIT_SOME_FAILED = 1
SERVE_HOST = "127.0.0.1"
SERVE_PORT = 8000
SERVE_REQUESTS_PER_MINUTE = 120
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# A write to a closed pipe fails with EPIPE on Linux and EINVAL on Windows.
CLOSED_PIPE_ERRNOS = frozenset({errno.EPIPE, errno.EINVAL})
# What Windows reports for a write to a pipe whose reader has gone: ERROR_NO_DATA ("the pipe
# is being closed") and ERROR_BROKEN_PIPE.
WINDOWS_CLOSED_PIPE_ERRORS = frozenset({232, 109})


class _ChainName(click.ParamType[str]):
    """A built-in or plugin chain. Installed plugins are looked up only when it is used."""

    name = "chain"

    def convert(self, value: str, param: click.Parameter | None, ctx: click.Context | None) -> str:
        try:
            chains = available_chains()
        except ConfigError as exc:
            self.fail(str(exc), param, ctx)
        if value not in (*chains, ALL_CHAINS):
            self.fail(
                f"{value!r} is not a known chain; choose from {', '.join(chains)}", param, ctx
            )
        return value


class _Options:
    """Options given before the command, resolved into an ``Indexer`` on demand."""

    def __init__(
        self,
        chain: str,
        network: str | None,
        database: Database,
        settings: Settings,
        quiet: bool,
    ) -> None:
        self.chain = chain
        self.network = network
        self.database = database
        self.settings = settings
        self.quiet = quiet

    @property
    def all_chains(self) -> bool:
        return self.chain == ALL_CHAINS

    def open_indexer(self) -> Indexer:
        if self.all_chains:
            raise InvalidInput(
                "this command works on one chain; --chain all is for metrics, export, sync "
                "and report"
            )
        return Indexer(self.chain, self.network, settings=self.settings, database=self.database)

    def open_cross_chain(self) -> CrossChain:
        return CrossChain(settings=self.settings, database=self.database)


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
    type=_ChainName(),
    default="algorand",
    show_default=True,
    help=(
        f"Chain to work on: {', '.join(PLANNED_CHAINS)}, a chain an installed plugin adds, "
        "or all (metrics, export, sync and report)."
    ),
)
@click.option(
    "--network",
    help=(
        "Network to read, such as mainnet, testnet, devnet, testnet4, signet or localnet. "
        "Defaults to the chain's network in config.toml, else Algorand testnet, Solana "
        "devnet, NEAR testnet, the EVM chain's testnet, such as Sepolia, or Bitcoin testnet4."
    ),
)
@click.option("--db", "database", help="SQLite file or postgresql:// URL.")
@click.option("--env-file", default=".env", show_default=True)
@click.option(
    "--config",
    "config_file",
    default=DEFAULT_CONFIG_FILE,
    show_default=True,
    help="Endpoints, networks, accounts and prices; never secrets.",
)
@click.option("-v", "--verbose", is_flag=True, help="Log progress as well as problems.")
@click.option("-q", "--quiet", is_flag=True, help="Log errors only and hide sync progress.")
@click.pass_context
def cli(
    context: click.Context,
    chain: str,
    network: str | None,
    database: str | None,
    env_file: str,
    config_file: str,
    verbose: bool,
    quiet: bool,
) -> None:
    """Index and analyse blockchain accounts and transactions."""
    try:
        settings = Settings.from_env(env_file, config_file=config_file)
    except IndexerError as exc:
        raise click.ClickException(str(exc)) from None
    chosen = None if database is None or is_postgres(database) else Path(database)
    target = chosen_database(settings, database if chosen is None else chosen)
    secrets = (*settings.secret_values(), *database_secrets(target))
    configure_logging(verbose=verbose, quiet=quiet, secrets=secrets)
    context.obj = _Options(chain, network, target, settings, quiet)


@cli.group()
def index() -> None:
    """Look up one account, Algorand ASA or app, Solana SPL token or program, or contract,
    or one Bitcoin address or transaction."""


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
    """Show an account's state, balance in ALGO, SOL or NEAR, and its recent transactions."""
    _echo_fields(indexer.describe(indexer.account(address)))
    click.echo()
    records = indexer.account_transactions(address, limit=limit)
    for record in records:
        click.echo(_transaction_line(indexer, record))
    for line in _attributions(records):
        click.echo(line)


@index.command("address")
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
def index_address(indexer: Indexer, address: str, limit: int) -> None:
    """Show a Bitcoin address or P2PK public key: balance in BTC, UTXOs and recent txids."""
    _require_chain(indexer, "bitcoin", "index address")
    _echo_fields(indexer.describe(indexer.account(address)))
    click.echo()
    for record in indexer.account_transactions(address, limit=limit):
        click.echo(_transaction_line(indexer, record))


@index.command("tx")
@click.argument("txid")
@click.pass_obj
@_with_indexer
def index_tx(indexer: Indexer, txid: str) -> None:
    """Show one Bitcoin transaction by its txid: fee in sats, fee rate in sat/vB, inputs and
    outputs."""
    _require_chain(indexer, "bitcoin", "index tx")
    record = indexer.transaction(txid)
    _echo_fields(
        {
            "Txid": record.id,
            "Time": record.time.strftime("%Y-%m-%d %H:%M UTC"),
            "Sender": record.sender,
            "Receiver": record.receiver or "none",
            **indexer.describe(record),
        }
    )


@index.command("asset")
@click.argument("asa_id")
@click.pass_obj
@_with_indexer
def index_asset(indexer: Indexer, asa_id: str) -> None:
    """Show an Algorand ASA by its ASA ID."""
    _require_chain(indexer, "algorand", "index asset")
    _echo_fields(indexer.describe(indexer.asset(asa_id)))


@index.command("token")
@click.argument("mint")
@click.pass_obj
@_with_indexer
def index_token(indexer: Indexer, mint: str) -> None:
    """Show a Solana SPL Token or Token-2022 mint, or an EVM chain's ERC-20 or BEP-20 token."""
    _require_chain(indexer, ("solana", *EVM_CHAINS), "index token")
    _echo_fields(indexer.describe(indexer.asset(mint)))


@index.command("app")
@click.argument("app_id")
@click.pass_obj
@_with_indexer
def index_app(indexer: Indexer, app_id: str) -> None:
    """Show an Algorand application by its app ID."""
    _require_chain(indexer, "algorand", "index app")
    _echo_fields(indexer.describe(indexer.contract(app_id)))


@index.command("program")
@click.argument("program_id")
@click.pass_obj
@_with_indexer
def index_program(indexer: Indexer, program_id: str) -> None:
    """Show a Solana program by its program ID."""
    _require_chain(indexer, "solana", "index program")
    _echo_fields(indexer.describe(indexer.contract(program_id)))


@index.command("contract")
@click.argument("contract_id")
@click.pass_obj
@_with_indexer
def index_contract(indexer: Indexer, contract_id: str) -> None:
    """Show the contract deployed on a NEAR account ID, or at an EVM contract address."""
    _require_chain(indexer, ("near", *EVM_CHAINS), "index contract")
    _echo_fields(indexer.describe(indexer.contract(contract_id)))


@cli.command()
@click.argument("address", required=False)
@click.option("--follow", is_flag=True, help="Keep syncing as new transactions arrive.")
@click.option(
    "--interval",
    type=click.FloatRange(min=1),
    help="Seconds between rounds with --follow; each chain has its own default.",
)
@click.pass_obj
def sync(options: _Options, address: str | None, follow: bool, interval: float | None) -> None:
    """Store an account's new transactions; only what arrived since the last sync.

    With --chain all, sync every account listed under [accounts] in config.toml, chains in
    parallel; mainnet accounts only with --network mainnet."""
    if options.all_chains:
        if follow:
            raise click.UsageError("--follow works on one account of one chain")
        if address is not None:
            raise click.UsageError("--chain all syncs the accounts in config.toml; give no address")
        _sync_all(options)
        return
    if address is None:
        raise click.UsageError("give the account to sync")
    if follow:
        _follow(options, address, interval)
        return
    _sync(options, address, show_progress=not options.quiet)


@_with_indexer
def _follow(indexer: Indexer, address: str, interval: float | None) -> None:
    pause = interval or default_interval(indexer.profile.name)
    tally = FollowSummary()

    def report(result: SyncResult) -> None:
        tally.add(result)
        if result.records_fetched:
            click.echo(_sync_summary(result))

    click.echo(f"Following {address} every {pause:g}s; press Ctrl-C to stop.", err=True)
    try:
        indexer.follow(address, interval=pause, on_sync=report)
    except KeyboardInterrupt:
        click.echo(
            f"Stopped after {tally.rounds} rounds: {tally.records_fetched} transactions, "
            f"{tally.requests_made} requests.",
            err=True,
        )


def _sync_all(options: _Options) -> None:
    with options.open_cross_chain() as everything:
        try:
            outcomes = everything.sync(options.network)
        except IndexerError as exc:
            raise click.ClickException(str(exc)) from None
    for outcome in outcomes:
        if outcome.result is not None:
            click.echo(_sync_summary(outcome.result))
            continue
        where = f"{outcome.account} on {outcome.chain} {outcome.network}"
        click.echo(f"Failed {where}: {outcome.error}", err=True)
    if any(outcome.error is not None for outcome in outcomes):
        sys.exit(EXIT_SOME_FAILED)


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
@click.option("--json", "as_json", is_flag=True, help="With --chain all, print JSON.")
@click.pass_obj
def metrics(options: _Options, days: int, account: str | None, as_json: bool) -> None:
    """Print metrics over stored transactions as JSON; fees are in the chain's base unit.

    With --chain all, print every stored chain and network side by side."""
    if not options.all_chains:
        _chain_metrics(options, days, account)
        return
    if account is not None:
        raise click.UsageError("--account works on one chain, not with --chain all")
    report = _cross_chain_report(options, days)
    if as_json:
        click.echo(json.dumps(report.model_dump(mode="json"), indent=2))
        return
    for line in metrics_table(report):
        click.echo(line)


@_with_indexer
def _chain_metrics(indexer: Indexer, days: int, account: str | None) -> None:
    report = indexer.metrics(days, account=account)
    click.echo(json.dumps(report.model_dump(mode="json"), indent=2))


@cli.command()
@click.option(
    "--format",
    "report_format",
    type=click.Choice(REPORT_FORMATS),
    default="html",
    show_default=True,
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="File to write; standard output without it.",
)
@click.option("--days", type=click.IntRange(min=1), default=30, show_default=True)
@click.pass_obj
def report(options: _Options, report_format: str, output: Path | None, days: int) -> None:
    """Compare every stored chain: activity, fees and active accounts, as an offline HTML
    page, CSV or JSON."""
    text = write_report(_cross_chain_report(options, days), report_format, output)
    if text is not None:
        click.echo(text.rstrip("\n"))


def _cross_chain_report(options: _Options, days: int) -> CrossChainReport:
    # A chain that fails reports its error in its own row, so nothing here raises.
    with options.open_cross_chain() as everything:
        return everything.metrics(days)


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
def export(
    options: _Options,
    export_format: str,
    output: Path | None,
    account: str | None,
    days: int | None,
) -> None:
    """Export stored transactions. With --chain all, every chain in one table."""
    if options.all_chains:
        if account is not None:
            raise click.UsageError("--account works on one chain, not with --chain all")
        text = _cross_chain_export(options, export_format, output, days)
    else:
        text = _chain_export(options, export_format, output, account, days)
    if text is not None:
        click.echo(text.rstrip("\n"))


def _cross_chain_export(
    options: _Options, export_format: str, output: Path | None, days: int | None
) -> str | None:
    with options.open_cross_chain() as everything:
        try:
            return everything.export(export_format, output, days=days)
        except IndexerError as exc:
            raise click.ClickException(str(exc)) from None


@_with_indexer
def _chain_export(
    indexer: Indexer,
    export_format: str,
    output: Path | None,
    account: str | None,
    days: int | None,
) -> str | None:
    return indexer.export(export_format, output, account=account, days=days)


@cli.command()
@click.option("--host", default=SERVE_HOST, show_default=True, help="Address to listen on.")
@click.option("--port", type=click.IntRange(1, 65535), default=SERVE_PORT, show_default=True)
@click.option(
    "--requests-per-minute",
    type=click.IntRange(min=1),
    default=SERVE_REQUESTS_PER_MINUTE,
    show_default=True,
    help="What each client may send.",
)
@click.pass_obj
def serve(options: _Options, host: str, port: int, requests_per_minute: int) -> None:
    """Serve the database read-only over HTTP, with OpenAPI docs at /docs. Needs the serve
    extra."""
    try:
        import uvicorn

        from multichain_indexer.server import create_app
    except ImportError:
        raise click.ClickException(
            "serve needs the serve extra: pip install 'multichain-analytics-indexer[serve]'"
        ) from None
    if host not in LOOPBACK_HOSTS:
        click.echo(
            f"Listening on {host}: anyone who can reach it can read this database.", err=True
        )
    app = create_app(
        settings=options.settings,
        database=options.database,
        requests_per_minute=requests_per_minute,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning" if options.quiet else "info")


@cli.command("pq-status")
def pq_status() -> None:
    """Show where each planned chain stands on post-quantum signatures, with sources."""
    width = max(len(f"{row.chain} {row.network}") for row in PQ_STATUS)
    for row in PQ_STATUS:
        found = f"{row.scheme}, {row.since}" if row.scheme else ""
        name = f"{row.chain} {row.network}".ljust(width)
        click.echo(f"{name}  {row.state.value}{'  ' + found if found else ''}")
        click.echo(f"{' ' * width}  {row.note}")
        click.echo(f"{' ' * width}  {row.source} (checked {row.checked_on})")


def main() -> None:
    """Run the command line; a reader closing the output pipe ends it quietly."""
    try:
        cli()
    except OSError as error:
        if not _is_closed_output(error):
            raise
        _discard_pending_output()
        sys.exit(EXIT_OUTPUT_CLOSED)


def _is_closed_output(error: OSError) -> bool:
    # EINVAL has other causes, such as a bad export path, so the error alone proves nothing.
    # A stream that still cannot flush proves it. When the failed write was larger than the
    # stream's buffer, nothing is left to flush, so the pipe itself is asked.
    if error.errno not in CLOSED_PIPE_ERRNOS:
        return False
    return any(_cannot_flush(stream) or _reader_gone(stream) for stream in (sys.stdout, sys.stderr))


def _cannot_flush(stream: TextIO) -> bool:
    try:
        stream.flush()
    except OSError:
        return True
    return False


def _reader_gone(stream: TextIO) -> bool:
    """Say whether ``stream`` is a pipe whose reader has gone, without writing to it."""
    try:
        return _pipe_closed(stream.fileno())
    except (OSError, ValueError):
        return False


if sys.platform == "win32":

    def _pipe_closed(descriptor: int) -> bool:
        # A write of no bytes still reaches the pipe, which refuses it once its reader has
        # gone; on a healthy stream it writes nothing. Python's own zero-length write
        # returns before reaching the pipe, so WriteFile is called directly.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        written = ctypes.c_ulong(0)
        handle = msvcrt.get_osfhandle(descriptor)
        if kernel32.WriteFile(handle, None, 0, ctypes.byref(written), None):
            return False
        return ctypes.get_last_error() in WINDOWS_CLOSED_PIPE_ERRORS

else:

    def _pipe_closed(descriptor: int) -> bool:
        # The kernel flags an error on a pipe's write end once no reader is left.
        poller = select.poll()
        poller.register(descriptor, select.POLLOUT)
        return any(events & select.POLLERR for _, events in poller.poll(0))


def _discard_pending_output() -> None:
    # Python flushes these streams again on exit, which would report the same failure.
    devnull = os.open(os.devnull, os.O_WRONLY)
    for stream in (sys.stdout, sys.stderr):
        os.dup2(devnull, stream.fileno())
    os.close(devnull)


def _require_chain(indexer: Indexer, chains: str | tuple[str, ...], command: str) -> None:
    allowed = (chains,) if isinstance(chains, str) else chains
    if indexer.profile.name not in allowed:
        raise InvalidInput(f"{command} is for {', '.join(allowed)}, not {indexer.profile.name}")


def _echo_fields(fields: dict[str, str]) -> None:
    width = max(len(label) for label in fields)
    for label, value in fields.items():
        click.echo(f"{label.ljust(width)}  {value}")


def _attributions(records: list[TransactionRecord]) -> list[str]:
    sources = {getattr(record.native, "history_source", None) for record in records}
    return [line for source, line in ATTRIBUTIONS.items() if source in sources]


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
