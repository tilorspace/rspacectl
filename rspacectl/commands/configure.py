"""rspace configure — interactive credential setup."""

import typer
from rich.console import Console

from ..config import (
    DEFAULT_PROFILE,
    _config_file,
    _keychain_service,
    list_profiles,
    save_config,
)

console = Console()


def configure(
    profile: str = typer.Option(
        DEFAULT_PROFILE, "--profile", "-p",
        help="Profile name to configure (default: 'default').",
    ),
    keychain: bool = typer.Option(
        False, "--keychain",
        help="Store credentials in the OS keychain instead of a dotenv file. "
             "Requires: pip install rspacectl[keychain]",
    ),
    list_: bool = typer.Option(
        False, "--list", "-l",
        help="List all configured profiles and exit.",
    ),
) -> None:
    """Interactively configure RSpace credentials.

    Credentials are saved to [cyan]~/.rspacectl[/cyan] (default profile) or
    [cyan]~/.rspacectl.<profile>[/cyan] for named profiles, with mode 600.

    Use [bold]--keychain[/bold] to store in the OS keychain (macOS Keychain,
    Windows Credential Manager, Linux Secret Service) — no plain-text file.

    Examples:

      rspace configure                          # set up default profile
      rspace configure --profile staging        # set up a named profile
      rspace configure --keychain               # store in OS keychain
      rspace configure --profile prod --keychain
      rspace configure --list                   # show all profiles
    """
    if list_:
        _cmd_list_profiles()
        return

    _cmd_interactive(profile, keychain)


# ---------------------------------------------------------------------------
# Sub-actions
# ---------------------------------------------------------------------------

def _cmd_list_profiles() -> None:
    profiles = list_profiles()
    console.print("[bold]Configured profiles:[/bold]")
    for name in profiles:
        file = _config_file(name)
        service = _keychain_service(name)
        sources = []
        if file.exists():
            sources.append(f"file: [cyan]{file}[/cyan]")
        try:
            import keyring  # type: ignore[import]
            if keyring.get_password(service, "api_key"):
                sources.append("keychain")
        except ImportError:
            pass
        source_str = "  (" + ", ".join(sources) + ")" if sources else ""
        console.print(f"  [bold]{name}[/bold]{source_str}")


def _cmd_interactive(profile: str, use_keychain: bool) -> None:
    console.print("[bold]RSpace CLI Configuration[/bold]")

    if use_keychain:
        service = _keychain_service(profile)
        console.print(
            f"Profile [bold]{profile}[/bold] — credentials will be stored in the "
            f"OS keychain (service: [cyan]{service}[/cyan])\n"
        )
    else:
        config_file = _config_file(profile)
        console.print(
            f"Profile [bold]{profile}[/bold] — credentials will be saved to "
            f"[cyan]{config_file}[/cyan]\n"
            "(chmod 600, readable only by you)\n"
        )

    url = typer.prompt("RSpace server URL (e.g. https://community.researchspace.com)")
    url = url.rstrip("/")
    api_key = typer.prompt("API key", hide_input=True)

    try:
        save_config(url, api_key, profile=profile, use_keychain=use_keychain)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)

    if use_keychain:
        service = _keychain_service(profile)
        console.print(
            f"\n[green]Saved.[/green] Credentials stored in OS keychain "
            f"(service: [cyan]{service}[/cyan])"
        )
    else:
        console.print(
            f"\n[green]Saved.[/green] Configuration written to "
            f"[cyan]{_config_file(profile)}[/cyan]"
        )
    console.print("Run [bold]rspace status[/bold] to verify the connection.")
