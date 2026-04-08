"""rspace export — export ELN data to XML or HTML archive."""

from pathlib import Path
from typing import List, Optional

import typer

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import console, err_console

_FORMAT_CHOICES = ["xml", "html"]
_SCOPE_CHOICES = ["user", "group", "selection"]


def export(
    format: str = typer.Option("xml", "--format", "-f", help="Export format: xml or html."),
    scope: str = typer.Option("user", "--scope", "-s", help="Export scope: user, group, or selection."),
    ids: Optional[List[str]] = typer.Option(
        None, "--id", help="Item IDs for selection scope (repeat for multiple).",
    ),
    output_file: Path = typer.Option(Path("rspace_export.zip"), "--output-file", "-o"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for export and download (default: wait)."),
    uid: Optional[str] = typer.Option(None, "--uid", help="User ID for user-scoped exports (sysadmin)."),
    include_revisions: bool = typer.Option(False, "--include-revisions"),
) -> None:
    """Export RSpace ELN data to an archive file.

    Examples:

      rspace export --format xml --scope user --output-file my_export.zip

      rspace export --format html --scope selection --id SD123 --id SD456
    """
    if format not in _FORMAT_CHOICES:
        err_console.print(f"[red]Invalid format '{format}'. Choose: {', '.join(_FORMAT_CHOICES)}[/red]")
        raise typer.Exit(1)
    if scope not in _SCOPE_CHOICES:
        err_console.print(f"[red]Invalid scope '{scope}'. Choose: {', '.join(_SCOPE_CHOICES)}[/red]")
        raise typer.Exit(1)
    if scope == "selection" and not ids:
        err_console.print("[red]--id is required when --scope is 'selection'.[/red]")
        raise typer.Exit(1)

    ctx = get_context()

    try:
        if scope == "selection":
            item_ids = [parse_id(i) for i in ids]
            if wait:
                err_console.print(f"Exporting {len(item_ids)} item(s) as {format.upper()}…")
                saved = ctx.eln.download_export_selection(
                    export_format=format,
                    file_path=str(output_file),
                    item_ids=item_ids,
                    include_revision_history=include_revisions,
                )
                console.print(f"[green]Export saved to:[/green] {saved}")
            else:
                job = ctx.eln.start_export_selection(
                    export_format=format,
                    item_ids=item_ids,
                    include_revisions=include_revisions,
                )
                console.print(f"[green]Export job started:[/green] {job.get('id')}")
        else:
            if wait:
                err_console.print(f"Exporting {scope} data as {format.upper()}…")
                saved = ctx.eln.export_and_download(
                    export_format=format,
                    scope=scope,
                    file_path=str(output_file),
                    uid=parse_id(uid) if uid else None,
                    include_revisions=include_revisions,
                )
                console.print(f"[green]Export saved to:[/green] {saved}")
            else:
                job = ctx.eln.start_export(
                    export_format=format,
                    scope=scope,
                    uid=parse_id(uid) if uid else None,
                    include_revisions=include_revisions,
                )
                console.print(f"[green]Export job started:[/green] {job.get('id')}")

    except Exception as e:
        handle_api_error(e)
