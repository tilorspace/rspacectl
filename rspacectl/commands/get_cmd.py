"""rspace get [<type>] <id> — fetch a single resource by ID.

The resource type can be given explicitly or inferred from a GlobalID prefix:

  rspace get SD123          # document   (inferred)
  rspace get NB456          # notebook   (inferred)
  rspace get SA789          # sample     (inferred)
  rspace get SS101          # subsample  (inferred)
  rspace get IC202          # container  (inferred)
  rspace get IT303          # template   (inferred)
  rspace get FM404          # form       (inferred)
  rspace get GL505          # file       (inferred)
  rspace get FL606          # folder     (inferred)

  rspace get document SD123 # explicit type (also accepts numeric IDs)
"""

from typing import Optional

import typer

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id, resource_type
from ..output import (
    COL_CREATED,
    COL_GLOBAL_ID,
    COL_MODIFIED,
    COL_NAME_35,
    COL_NAME_40,
    COL_OWNER,
    ColumnDef,
    OutputFormat,
    console,
    print_result,
    print_single,
)

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

_SUBSAMPLE_COLUMNS = [
    COL_GLOBAL_ID,
    COL_NAME_35,
    ColumnDef("quantity.numericValue", "Quantity", 10),
    ColumnDef("quantity.unitId", "Unit", 8),
]

_DOCUMENT_COLUMNS = [
    COL_GLOBAL_ID,
    COL_NAME_40,
    ColumnDef("form.globalId", "Form", 10),
    COL_MODIFIED,
    ColumnDef("createdBy", "Owner", 20),
]

# ---------------------------------------------------------------------------
# Per-type getter helpers
# ---------------------------------------------------------------------------

def _get_document(id: str) -> None:
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID, COL_NAME_40,
        ColumnDef("form.name", "Form", 20),
        ColumnDef("tags", "Tags", 30),
        COL_CREATED, COL_MODIFIED,
        ColumnDef("createdBy", "Owner", 20),
    ]
    try:
        result = ctx.eln.get_document(parse_id(id))
    except Exception as e:
        handle_api_error(e)
    print_single(result, ctx.output, columns)


def _get_sample(id: str, include_subsamples: bool = False) -> None:
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID, COL_NAME_35,
        ColumnDef("description", "Description", 40),
        ColumnDef("quantity.numericValue", "Quantity", 10),
        ColumnDef("quantity.unitId", "Unit", 8),
        ColumnDef("subSamplesCount", "Subsamples", 10),
        ColumnDef("expiryDate", "Expiry", 16),
        ColumnDef("tags", "Tags", 30),
        COL_CREATED, COL_OWNER,
    ]
    try:
        result = ctx.inv.get_sample_by_id(parse_id(id))
    except Exception as e:
        handle_api_error(e)
    print_single(result, ctx.output, columns)

    if include_subsamples:
        subsamples = result.get("subSamples", [])
        if ctx.output in (OutputFormat.TABLE, OutputFormat.CSV):
            console.print(f"\n[bold]Subsamples ({len(subsamples)})[/bold]")
            print_result(subsamples, _SUBSAMPLE_COLUMNS, ctx.output)


def _get_subsample(id: str) -> None:
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID, COL_NAME_35,
        ColumnDef("quantity.numericValue", "Quantity", 10),
        ColumnDef("quantity.unitId", "Unit", 8),
        ColumnDef("notes", "Notes", 40),
        COL_CREATED,
    ]
    try:
        result = ctx.inv.get_subsample_by_id(parse_id(id))
    except Exception as e:
        handle_api_error(e)
    print_single(result, ctx.output, columns)


def _get_container(id: str, include_content: bool = False) -> None:
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID, COL_NAME_35,
        ColumnDef("cType", "Type", 10),
        ColumnDef("description", "Description", 40),
        ColumnDef("canStoreSamples", "Stores Samples", 14),
        ColumnDef("canStoreContainers", "Stores Containers", 18),
        COL_CREATED, COL_OWNER,
    ]
    try:
        result = ctx.inv.get_container_by_id(parse_id(id), include_content=include_content)
    except Exception as e:
        handle_api_error(e)
    print_single(result, ctx.output, columns)


def _get_form(id: str) -> None:
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID, COL_NAME_40,
        ColumnDef("formState", "State", 12),
        ColumnDef("version", "Version", 8),
        ColumnDef("tags", "Tags", 30),
        COL_MODIFIED,
    ]
    try:
        result = ctx.eln.get_form(parse_id(id))
    except Exception as e:
        handle_api_error(e)
    print_single(result, ctx.output, columns)


def _get_template(id: str) -> None:
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID, COL_NAME_35,
        ColumnDef("description", "Description", 40),
        COL_CREATED, COL_MODIFIED, COL_OWNER,
    ]
    try:
        result = ctx.inv.get_sample_template_by_id(parse_id(id))
    except Exception as e:
        handle_api_error(e)
    print_single(result, ctx.output, columns)


def _get_file(id: str) -> None:
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID, COL_NAME_40,
        ColumnDef("contentType", "Content Type", 25),
        ColumnDef("size", "Size", 12),
        COL_CREATED,
    ]
    try:
        result = ctx.eln.get_file_info(parse_id(id))
    except Exception as e:
        handle_api_error(e)
    print_single(result, ctx.output, columns)


def _get_folder(id: str, include_content: bool = False) -> None:
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID, COL_NAME_40,
        ColumnDef("notebook", "Is Notebook", 12),
        COL_CREATED, COL_MODIFIED,
    ]
    try:
        result = ctx.eln.get_folder(parse_id(id))
    except Exception as e:
        handle_api_error(e)
    print_single(result, ctx.output, columns)

    if include_content:
        try:
            tree = ctx.eln.list_folder_tree(folder_id=parse_id(id), typesToInclude=["document"])
            documents = tree.get("records", [])
        except Exception as e:
            handle_api_error(e)
        if ctx.output in (OutputFormat.TABLE, OutputFormat.CSV):
            label = "notebook" if result.get("notebook") else "folder"
            console.print(f"\n[bold]Documents in {label} ({len(documents)})[/bold]")
        print_result(documents, _DOCUMENT_COLUMNS, ctx.output)


# ---------------------------------------------------------------------------
# Dispatch table: resource_type() string → handler
# ---------------------------------------------------------------------------

_DISPATCH = {
    "document":  _get_document,
    "notebook":  _get_folder,
    "folder":    _get_folder,
    "sample":    _get_sample,
    "subsample": _get_subsample,
    "container": _get_container,
    "bench":     _get_container,
    "form":      _get_form,
    "template":  _get_template,
    "file":      _get_file,
}

_CONTENT_TYPES = {"folder", "notebook", "container", "bench"}
_SUBSAMPLE_TYPES = {"sample"}

_TYPE_ALIASES = {
    "doc":       "document",
    "nb":        "notebook",
    "note":      "notebook",
    "subsample": "subsample",
    "sub":       "subsample",
    "container": "container",
    "cont":      "container",
    "tmpl":      "template",
    "templ":     "template",
}


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------

def get(
    type_or_id: str = typer.Argument(
        ...,
        help="GlobalID (e.g. SD123) — type is inferred — or explicit type name (document, sample, …).",
    ),
    id: Optional[str] = typer.Argument(
        None,
        help="ID when an explicit type is given as the first argument.",
    ),
    subsamples: bool = typer.Option(
        False, "--subsamples",
        help="[sample] Also list subsamples.",
    ),
    content: bool = typer.Option(
        False, "--content",
        help="[folder/notebook/container] Also list contents.",
    ),
) -> None:
    """Get a single resource by GlobalID or explicit type.

    [bold]Inferred from GlobalID prefix (recommended):[/bold]

      rspace get SD123        document
      rspace get NB456        notebook
      rspace get FL789        folder
      rspace get SA101        sample
      rspace get SS202        subsample
      rspace get IC303        container
      rspace get IT404        sample template
      rspace get FM505        form
      rspace get GL606        gallery file

    [bold]Explicit type (needed for plain numeric IDs):[/bold]

      rspace get document 123
      rspace get sample   456
    """
    # ---- resolve rtype and actual_id --------------------------------
    if id is None:
        # Single argument — must be a GlobalID
        actual_id = type_or_id
        rtype = resource_type(actual_id)
        if rtype == "unknown":
            typer.echo(
                f"Cannot infer resource type from '{actual_id}'. "
                "Use a GlobalID (e.g. SD123) or provide an explicit type: "
                "rspace get <type> <id>.",
                err=True,
            )
            raise typer.Exit(1)
    else:
        # Two arguments — first is the type name
        actual_id = id
        rtype = _TYPE_ALIASES.get(type_or_id.lower(), type_or_id.lower())

    handler = _DISPATCH.get(rtype)
    if handler is None:
        typer.echo(
            f"Unknown resource type '{rtype}'. "
            f"Valid types: {', '.join(sorted(_DISPATCH))}.",
            err=True,
        )
        raise typer.Exit(1)

    # ---- invoke handler with applicable flags -----------------------
    if rtype in _CONTENT_TYPES:
        handler(actual_id, include_content=content)
    elif rtype in _SUBSAMPLE_TYPES:
        handler(actual_id, include_subsamples=subsamples)
    else:
        handler(actual_id)
