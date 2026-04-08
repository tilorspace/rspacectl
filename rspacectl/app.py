"""Root Typer application for rspacectl."""

from typing import Optional

import typer
from rich.console import Console

from . import __version__
from .config import ConfigError, load_config
from .context import AppContext, set_context
from .output import OutputFormat

# ---------------------------------------------------------------------------
# Sub-app imports — each command module registers itself here
# ---------------------------------------------------------------------------
from .commands import (
    configure,
    status,
    search,
    list_cmd,
    get_cmd,
    create,
    update,
    delete,
    upload,
    download,
    move,
    split,
    share,
    export,
    import_cmd,
    tag,
)

console = Console()

app = typer.Typer(
    name="rspace",
    help="Command-line interface for RSpace ELN and Inventory.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

# ---------------------------------------------------------------------------
# Register sub-commands
# ---------------------------------------------------------------------------
app.add_typer(list_cmd.app, name="list", help="List resources (documents, samples, containers…)")
app.command("get")(get_cmd.get)
app.add_typer(create.app, name="create", help="Create a new resource")
app.add_typer(update.app, name="update", help="Update an existing resource")
app.add_typer(delete.app, name="delete", help="Delete one or more resources")
app.add_typer(upload.app, name="upload", help="Upload files or attachments")
app.add_typer(download.app, name="download", help="Download files or attachments")
app.add_typer(import_cmd.app, name="import", help="Import data into RSpace")

app.command("configure")(configure.configure)
app.command("status")(status.status)
app.command("search")(search.search)
app.command("move")(move.move)
app.command("split")(split.split)
app.command("share")(share.share)
app.command("export")(export.export)
app.command("tag")(tag.tag)


# ---------------------------------------------------------------------------
# Root callback — runs before every command, initialises context
# ---------------------------------------------------------------------------

@app.callback()
def root_callback(
    ctx: typer.Context,
    output: OutputFormat = typer.Option(
        OutputFormat.TABLE,
        "--output",
        "-o",
        help="Output format: table, json, csv, or quiet (IDs only).",
        show_default=True,
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        envvar="RSPACE_URL",
        help="RSpace server URL (overrides config file).",
        show_default=False,
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="RSPACE_API_KEY",
        help="RSpace API key (overrides config file). Prefer env var or config file — CLI flags are visible in process listings.",
        show_default=False,
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show version and exit.",
        is_eager=True,
    ),
) -> None:
    """[bold]rspacectl[/bold] — RSpace command-line tool.

    Credentials are read from [cyan]~/.rspacectl[/cyan], or from the
    [cyan]RSPACE_URL[/cyan] and [cyan]RSPACE_API_KEY[/cyan] environment variables.

    Run [bold]rspace configure[/bold] to set up credentials interactively.
    """
    if version:
        console.print(f"rspacectl {__version__}")
        raise typer.Exit()

    # configure command doesn't need a live client
    if ctx.invoked_subcommand == "configure":
        return

    try:
        resolved_url, resolved_key = load_config(url, api_key)
    except ConfigError as e:
        console.print(f"[bold red]Configuration error:[/bold red] {e}")
        raise typer.Exit(code=1)

    from rspace_client import ELNClient, InventoryClient

    eln = ELNClient(resolved_url, resolved_key)
    inv = InventoryClient(resolved_url, resolved_key)

    set_context(AppContext(eln=eln, inv=inv, output=output))


def main() -> None:
    """Entry point for the 'rspace' binary."""
    app()
