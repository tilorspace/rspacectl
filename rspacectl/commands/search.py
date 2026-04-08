"""rspace search <query> — cross-entity full-text search."""

from typing import Optional

import typer
from rspace_client.inv.inv import Pagination, ResultType

from ..context import get_context
from ..output import (
    COL_CREATED,
    COL_GLOBAL_ID,
    COL_MODIFIED,
    COL_NAME_40,
    ColumnDef,
    err_console,
    print_result,
)

_TYPE_CHOICES = ["documents", "samples", "subsamples", "containers", "all"]

_INV_TYPE_MAP = {
    "samples": ResultType.SAMPLE,
    "subsamples": ResultType.SUBSAMPLE,
    "containers": ResultType.CONTAINER,
}

_COMBINED_COLUMNS = [
    COL_GLOBAL_ID,
    COL_NAME_40,
    ColumnDef("type", "Type", 12),
    COL_MODIFIED,
]


def search(
    query: str = typer.Argument(..., help="Search query string."),
    type: str = typer.Option(
        "all", "--type", "-t", help=f"Resource type: {', '.join(_TYPE_CHOICES)}."
    ),
    page: int = typer.Option(0, "--page"),
    page_size: int = typer.Option(20, "--page-size"),
) -> None:
    """Search across documents and/or inventory items."""
    if type not in _TYPE_CHOICES:
        err_console.print(
            f"[red]Invalid --type '{type}'. Choose from: {', '.join(_TYPE_CHOICES)}[/red]"
        )
        raise typer.Exit(1)

    ctx = get_context()
    results: list = []

    if type in ("documents", "all"):
        try:
            res = ctx.eln.get_documents(query=query, page_number=page, page_size=page_size)
            docs = res.get("documents", [])
            for d in docs:
                d["type"] = "document"
            results.extend(docs)
        except Exception as e:
            err_console.print(f"[yellow]Warning:[/yellow] ELN search failed: {e}")

    if type != "documents":
        try:
            pagination = Pagination(page_number=page, page_size=page_size)
            # None result_type means search all inventory types
            result_type = _INV_TYPE_MAP.get(type)
            res = ctx.inv.search(query=query, pagination=pagination, result_type=result_type)
            results.extend(res.get("records", []))
        except Exception as e:
            err_console.print(f"[yellow]Warning:[/yellow] Inventory search failed: {e}")

    print_result(results, _COMBINED_COLUMNS, ctx.output)
