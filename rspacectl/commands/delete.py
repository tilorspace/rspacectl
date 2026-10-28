"""rspace delete <noun> <id…> — delete one or more resources."""

from typing import List

import typer

from ..context import get_context
from ..exceptions import exit_with_error
from ..ids import check_id_type, parse_id
from ..output import console, err_console
from ..utils import batch_run

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")


def _batch_delete(ids: List[str], client, endpoint: str, id_type: str) -> None:
    """DELETE /<endpoint>/<id> for each id, report a summary, exit 1 on any failure.

    Every GlobalID is checked against ``id_type`` before anything is deleted:
    the prefix is dropped when calling the API, so ``delete instrument SA905``
    would otherwise delete IN905.

    The endpoint is called directly via ``client.doDelete`` rather than the
    client's ``delete_*`` helpers (rspace-client 2.7.4): some build
    "//documents/{id}" (404), and most drop the response, which is the only
    sign of a refusal the server answers with HTTP 200 and ``deleted: false``
    (e.g. a sample whose subsamples are stored in containers).
    """
    label = id_type.replace("-", " ")
    for raw_id in ids:
        try:
            check_id_type(raw_id, id_type)
        except ValueError as e:
            exit_with_error(f"{e} Nothing was deleted.")

    def _delete(raw_id: str) -> None:
        result = client.doDelete(endpoint, parse_id(raw_id))
        if isinstance(result, dict) and result.get("deleted") is False:
            raise RuntimeError(
                "the server did not delete it"
                + (" (canBeDeleted: false)" if result.get("canBeDeleted") is False else "")
            )

    successes, failures = batch_run(ids, _delete, f"delete {label}")
    console.print(f"[green]Deleted {len(successes)} {label}(s).[/green]")
    if failures:
        err_console.print(f"[yellow]Failed ({len(failures)}):[/yellow] {', '.join(failures)}")
        raise typer.Exit(1)


@app.command("document")
def delete_document(
    ids: List[str] = typer.Argument(..., help="Document ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more ELN documents."""
    _batch_delete(ids, get_context().eln, "documents", "document")


@app.command("sample")
def delete_sample(
    ids: List[str] = typer.Argument(..., help="Sample ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more inventory samples.

    The server refuses to delete a sample while any of its subsamples is
    stored in a container; move them to your workbench first.
    """
    _batch_delete(ids, get_context().inv, "samples", "sample")


@app.command("container")
def delete_container(
    ids: List[str] = typer.Argument(..., help="Container ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more inventory containers."""
    _batch_delete(ids, get_context().inv, "containers", "container")


@app.command("instrument")
def delete_instrument(
    ids: List[str] = typer.Argument(..., help="Instrument ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more inventory instruments."""
    _batch_delete(ids, get_context().inv, "instruments", "instrument")


@app.command("instrument-template")
def delete_instrument_template(
    ids: List[str] = typer.Argument(..., help="Instrument template ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more inventory instrument templates."""
    _batch_delete(ids, get_context().inv, "instrumentTemplates", "instrument-template")


@app.command("form")
def delete_form(
    ids: List[str] = typer.Argument(..., help="Form ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more ELN forms (only forms in NEW state can be deleted)."""
    _batch_delete(ids, get_context().eln, "forms", "form")


@app.command("folder")
def delete_folder(
    ids: List[str] = typer.Argument(..., help="Folder/notebook ID(s) or GlobalID(s)."),
) -> None:
    """Delete one or more folders or notebooks."""
    _batch_delete(ids, get_context().eln, "folders", "folder")
