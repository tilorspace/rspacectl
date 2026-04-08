"""Error handling utilities for rspacectl."""

from typing import NoReturn

import typer
from rspace_client.client_base import ClientBase

from .output import err_console as error_console


def exit_with_error(message: str, code: int = 1) -> NoReturn:
    """Print an error message to stderr and exit with the given code."""
    error_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code=code)


def handle_api_error(exc: Exception) -> NoReturn:
    """Format and exit on SDK API errors."""
    if isinstance(exc, ClientBase.AuthenticationError):
        exit_with_error(
            "Authentication failed. Check your RSPACE_API_KEY is correct.\n"
            f"  Detail: {exc}"
        )
    elif isinstance(exc, ClientBase.ApiError):
        status = getattr(exc, "response_status_code", "unknown")
        exit_with_error(f"API error (HTTP {status}): {exc}")
    elif isinstance(exc, ClientBase.ConnectionError):
        exit_with_error(
            "Could not connect to RSpace. Check your RSPACE_URL and network connection.\n"
            f"  Detail: {exc}"
        )
    else:
        exit_with_error(str(exc))


def warn(message: str) -> None:
    """Print a warning to stderr (non-fatal)."""
    error_console.print(f"[yellow]Warning:[/yellow] {message}")
