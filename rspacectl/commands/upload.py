"""rspace upload — upload files or inventory attachments."""

from pathlib import Path
from typing import List, Optional

import typer

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import (
    COL_CREATED,
    COL_GLOBAL_ID,
    COL_NAME_40,
    ColumnDef,
    console,
    err_console,
    print_result,
)
from ..utils import batch_run

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")


@app.command("file")
def upload_file(
    paths: List[Path] = typer.Argument(..., help="File path(s) to upload to the Gallery."),
    folder: Optional[str] = typer.Option(None, "--folder", help="Destination gallery folder ID."),
    caption: Optional[str] = typer.Option(
        None, "--caption", help="Caption applied to all uploaded files."
    ),
) -> None:
    """Upload one or more files to the RSpace Gallery."""
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_40,
        ColumnDef("contentType", "Content Type", 25),
        ColumnDef("size", "Size", 12),
    ]
    folder_id = parse_id(folder) if folder else None
    results = []
    failed = []

    for path in paths:
        err_console.print(f"Uploading: {path.name}")
        try:
            with open(path, "rb") as f:
                result = ctx.eln.upload_file(file=f, folder_id=folder_id, caption=caption)
            results.append(result)
        except FileNotFoundError:
            err_console.print(f"[yellow]File not found, skipping:[/yellow] {path}")
            failed.append(str(path))
        except Exception as e:
            err_console.print(f"[yellow]Failed to upload {path.name}:[/yellow] {e}")
            failed.append(str(path))

    console.print(f"[green]Uploaded {len(results)} file(s).[/green]")
    if failed:
        err_console.print(f"[yellow]Failed ({len(failed)}):[/yellow] {', '.join(failed)}")

    print_result(results, columns, ctx.output)

    if failed:
        raise typer.Exit(1)


@app.command("attachment")
def upload_attachment(
    path: Path = typer.Argument(..., help="File to attach."),
    item_id: str = typer.Argument(..., help="Inventory item GlobalID to attach to."),
) -> None:
    """Attach a file to an inventory sample, subsample, or container."""
    ctx = get_context()
    err_console.print(f"Uploading attachment: {path.name} → {item_id}")
    try:
        with open(path, "rb") as f:
            ctx.inv.upload_attachment_by_global_id(item_id, f)
    except FileNotFoundError:
        err_console.print(f"[red]File not found:[/red] {path}")
        raise typer.Exit(1)
    except Exception as e:
        handle_api_error(e)

    console.print("[green]Attachment uploaded successfully.[/green]")
