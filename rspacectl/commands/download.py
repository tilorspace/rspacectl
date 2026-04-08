"""rspace download — download gallery files or inventory attachments."""

from pathlib import Path
from typing import List, Optional

import typer

from ..context import get_context
from ..exceptions import handle_api_error, warn
from ..ids import parse_id
from ..output import console, err_console

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")


def _download_items(ids: List[str], output_dir: Path, get_info_fn, download_fn, label: str) -> None:
    """Generic download loop — fetch metadata, derive filename, download, report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    success, failed = [], []

    for raw_id in ids:
        item_id = parse_id(raw_id)
        try:
            info = get_info_fn(item_id)
            # Strip any path components from the server-supplied name to prevent
            # path traversal (e.g. a malicious server returning "../../.bashrc").
            raw_name = info.get("name", f"{label}_{item_id}")
            filename = Path(raw_name).name or f"{label}_{item_id}"
            dest = output_dir / filename
            err_console.print(f"Downloading: {filename}")
            download_fn(item_id, str(dest))
            success.append(str(dest))
            console.print(f"  [green]✓[/green] {dest}")
        except Exception as e:
            warn(f"Failed to download {label} {raw_id}: {e}")
            failed.append(raw_id)

    console.print(f"[green]Downloaded {len(success)} {label}(s) to {output_dir}[/green]")
    if failed:
        err_console.print(f"[yellow]Failed ({len(failed)}):[/yellow] {', '.join(failed)}")
        raise typer.Exit(1)


@app.command("file")
def download_file(
    ids: List[str] = typer.Argument(..., help="Gallery file ID(s) or GlobalID(s)."),
    output_dir: Path = typer.Option(Path("."), "--output-dir", "-d", help="Directory to save files."),
) -> None:
    """Download one or more gallery files."""
    ctx = get_context()
    _download_items(ids, output_dir, ctx.eln.get_file_info, ctx.eln.download_file, "file")


@app.command("attachment")
def download_attachment(
    ids: List[str] = typer.Argument(..., help="Attachment ID(s)."),
    output_dir: Path = typer.Option(Path("."), "--output-dir", "-d", help="Directory to save files."),
) -> None:
    """Download one or more inventory attachments."""
    ctx = get_context()
    _download_items(
        ids, output_dir,
        ctx.inv.get_attachment_by_id,
        ctx.inv.download_attachment_by_id,
        "attachment",
    )
