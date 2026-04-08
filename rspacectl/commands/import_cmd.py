"""rspace import — import data into RSpace."""

from pathlib import Path
from typing import List, Optional

import typer

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import COL_CREATED, COL_GLOBAL_ID, COL_NAME_40, console, err_console, print_result

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")

_WORD_COLUMNS = [COL_GLOBAL_ID, COL_NAME_40, COL_CREATED]


@app.command("word")
def import_word(
    files: List[Path] = typer.Argument(..., help="Word document(s) to import (.doc, .docx, .odt)."),
    folder: Optional[str] = typer.Option(None, "--folder", help="Destination folder ID."),
) -> None:
    """Import one or more Word documents into RSpace as ELN documents.

    Example:

      rspace import word report.docx notes.docx --folder FL123
    """
    ctx = get_context()
    folder_id = parse_id(folder) if folder else None
    results = []
    failed = []

    for path in files:
        err_console.print(f"Importing: {path.name}")
        try:
            with open(path, "rb") as f:
                result = ctx.eln.import_word(file=f, folder_id=folder_id)
            results.append(result)
            console.print(f"  [green]✓[/green] {path.name} → {result.get('globalId')}")
        except FileNotFoundError:
            err_console.print(f"  [yellow]File not found, skipping:[/yellow] {path}")
            failed.append(str(path))
        except Exception as e:
            err_console.print(f"  [yellow]Failed:[/yellow] {path.name}: {e}")
            failed.append(str(path))

    console.print(f"[green]Imported {len(results)} document(s).[/green]")
    if failed:
        err_console.print(f"[yellow]Failed ({len(failed)}):[/yellow] {', '.join(failed)}")

    print_result(results, _WORD_COLUMNS, ctx.output)

    if failed:
        raise typer.Exit(1)


@app.command("tree")
def import_tree(
    directory: Path = typer.Argument(..., help="Directory to import recursively."),
    folder: Optional[str] = typer.Option(None, "--folder", help="Destination parent folder ID."),
    ignore_hidden: bool = typer.Option(True, "--ignore-hidden/--include-hidden", help="Skip hidden folders."),
    halt_on_error: bool = typer.Option(False, "--halt-on-error", help="Stop on first error."),
) -> None:
    """Import a directory tree into RSpace, preserving folder structure.

    Example:

      rspace import tree ./my-lab-data --folder FL123
    """
    ctx = get_context()
    folder_id = parse_id(folder) if folder else None
    err_console.print(f"Importing tree: {directory}")

    try:
        result = ctx.eln.import_tree(
            data_dir=str(directory),
            parent_folder_id=folder_id,
            ignore_hidden_folders=ignore_hidden,
            halt_on_error=halt_on_error,
        )
    except NotADirectoryError:
        err_console.print(f"[red]Not a directory:[/red] {directory}")
        raise typer.Exit(1)
    except Exception as e:
        handle_api_error(e)

    console.print("[green]Tree import complete.[/green]")
    if isinstance(result, dict):
        for key, val in result.items():
            if not key.startswith("_"):
                console.print(f"  [bold]{key}:[/bold] {val}")
