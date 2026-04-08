"""rspace split <subsample-id> — split a subsample into multiple new subsamples."""

from typing import Optional

import typer

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import COL_GLOBAL_ID, COL_NAME_35, ColumnDef, console, err_console, print_result

_COLUMNS = [
    COL_GLOBAL_ID,
    COL_NAME_35,
    ColumnDef("quantity.numericValue", "Quantity", 10),
    ColumnDef("quantity.unitId", "Unit", 8),
]


def split(
    id: str = typer.Argument(..., help="Subsample GlobalID or ID to split (e.g. SS123)."),
    count: int = typer.Option(..., "--count", "-n", help="Number of new subsamples to create."),
    quantity: Optional[float] = typer.Option(
        None,
        "--quantity",
        "-q",
        help="Quantity per new subsample. If omitted, quantity is split evenly.",
    ),
) -> None:
    """Split a subsample into multiple new subsamples.

    Examples:

      rspace split SS123 --count 4

      rspace split SS123 --count 3 --quantity 10.0
    """
    ctx = get_context()
    try:
        result = ctx.inv.split_subsample(
            subsample=parse_id(id),
            num_new_subsamples=count,
            quantity_per_subsample=quantity,
        )
    except Exception as e:
        handle_api_error(e)

    if hasattr(result, "success_results"):
        # BulkOperationResult
        new_subsamples = [r.get("record", r) for r in result.success_results()]
        errors = result.error_results()
        console.print(f"[green]Created {len(new_subsamples)} new subsample(s).[/green]")
        if errors:
            err_console.print(f"[yellow]{len(errors)} error(s) during split.[/yellow]")
        print_result(new_subsamples, _COLUMNS, ctx.output)
    else:
        items = result if isinstance(result, list) else [result]
        console.print(f"[green]Created {len(items)} new subsample(s).[/green]")
        print_result(items, _COLUMNS, ctx.output)
