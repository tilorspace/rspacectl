"""rspace delete <noun> <id…> — delete one or more resources."""

from typing import List

import typer
from rich.console import Console

from ..context import get_context
from ..exceptions import exit_with_error
from ..ids import check_id_type, parse_id
from ..output import console, err_console
from ..utils import batch_run

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")


def _batch_delete(ids: List[str], delete_fn, resource_label: str, id_type: str) -> None:
    """Delete each id via delete_fn, report summary, exit 1 on any failure.

    Every GlobalID is checked against ``id_type`` before anything is deleted:
    the prefix is dropped when calling the API, so ``delete instrument SA905``
    would otherwise delete IN905.
    """
    for raw_id in ids:
        try:
            check_id_type(raw_id, id_type)
        except ValueError as e:
            exit_with_error(f"{e} Nothing was deleted.")
    successes, failures = batch_run(
        ids, lambda raw_id: delete_fn(parse_id(raw_id)), f"delete {resource_label}"
    )
    console.print(f"[green]Deleted {len(successes)} {resource_label}(s).[/green]")
    if failures:
        err_console.print(f"[yellow]Failed ({len(failures)}):[/yellow] {', '.join(failures)}")
        raise typer.Exit(1)


@app.command("document")
def delete_document(
    ids: List[str] = typer.Argument(..., help="Document ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more ELN documents."""
    ctx = get_context()
    _batch_delete(ids, ctx.eln.delete_document, "document", "document")


@app.command("sample")
def delete_sample(
    ids: List[str] = typer.Argument(..., help="Sample ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more inventory samples."""
    ctx = get_context()
    _batch_delete(ids, ctx.inv.delete_sample, "sample", "sample")


@app.command("container")
def delete_container(
    ids: List[str] = typer.Argument(..., help="Container ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more inventory containers."""
    ctx = get_context()
    # The Python SDK does not expose a dedicated delete_container method;
    # use the raw endpoint via the client's built-in doDelete helper.
    _batch_delete(ids, lambda id_: ctx.inv.doDelete("containers", id_), "container", "container")


@app.command("instrument")
def delete_instrument(
    ids: List[str] = typer.Argument(..., help="Instrument ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more inventory instruments."""
    ctx = get_context()
    _batch_delete(ids, ctx.inv.delete_instrument, "instrument", "instrument")


@app.command("instrument-template")
def delete_instrument_template(
    ids: List[str] = typer.Argument(..., help="Instrument template ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more inventory instrument templates."""
    ctx = get_context()
    _batch_delete(
        ids, ctx.inv.delete_instrument_template, "instrument template", "instrument-template"
    )


@app.command("form")
def delete_form(
    ids: List[str] = typer.Argument(..., help="Form ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more ELN forms (only forms in NEW state can be deleted)."""
    ctx = get_context()
    _batch_delete(ids, ctx.eln.delete_form, "form", "form")


@app.command("folder")
def delete_folder(
    ids: List[str] = typer.Argument(..., help="Folder/notebook ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more folders or notebooks."""
    ctx = get_context()
    _batch_delete(ids, ctx.eln.delete_folder, "folder", "folder")
