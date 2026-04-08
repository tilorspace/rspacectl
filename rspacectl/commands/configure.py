"""rspace configure — interactive credential setup."""

import typer
from rich.console import Console

from ..config import CONFIG_FILE, save_config

console = Console()


def configure() -> None:
    """Interactively configure RSpace credentials and save to [cyan]~/.rspacectl[/cyan]."""
    console.print("[bold]RSpace CLI Configuration[/bold]")
    console.print(
        f"Credentials will be saved to [cyan]{CONFIG_FILE}[/cyan]\n"
        "(chmod 600, readable only by you)\n"
    )

    url = typer.prompt("RSpace server URL (e.g. https://community.researchspace.com)")
    url = url.rstrip("/")

    api_key = typer.prompt("API key", hide_input=True)

    save_config(url, api_key)

    console.print(f"\n[green]Saved.[/green] Configuration written to [cyan]{CONFIG_FILE}[/cyan]")
    console.print("Run [bold]rspace status[/bold] to verify the connection.")
