"""rspace status — check server availability."""

from rich.console import Console

from ..context import get_context
from ..exceptions import handle_api_error

console = Console()


def status() -> None:
    """Check connectivity and display RSpace server status."""
    ctx = get_context()
    try:
        result = ctx.eln.get_status()
    except Exception as e:
        handle_api_error(e)

    # The status response has fields like: serverVersion, message, etc.
    console.print("[bold green]Connected[/bold green]")
    for key, value in result.items():
        if not key.startswith("_"):
            console.print(f"  [bold]{key}:[/bold] {value}")
