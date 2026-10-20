"""rspace split <subsample-id> — split a subsample into multiple new subsamples."""

from typing import Optional

import typer

from ..context import get_context
from ..exceptions import exit_with_error, handle_api_error, warn
from ..ids import check_id_type, parse_id
from ..output import COL_GLOBAL_ID, COL_NAME_35, ColumnDef, console, err_console, print_result
from ..utils import put_inventory_item

_COLUMNS = [
    COL_GLOBAL_ID,
    COL_NAME_35,
    ColumnDef("quantity.numericValue", "Quantity", 10),
    ColumnDef("quantity.unitId", "Unit", 8),
]


def _restore_tags(inv, records: list, tags: list) -> list:
    """Re-apply ``tags`` to every split record; return the updated records.

    The client sets the new quantities with a bulk UPDATE that omits tags,
    and the API clears the tags of every record in it.
    """
    restored = []
    for rec in records:
        try:
            restored.append(put_inventory_item(inv, "subSamples", rec["id"], {"tags": tags}))
        except Exception as e:
            warn(f"Could not restore tags on {rec.get('globalId', rec['id'])}: {e}")
            restored.append(rec)
    return restored


def split(
    id: str = typer.Argument(..., help="Subsample GlobalID or ID to split (e.g. SS123)."),
    count: int = typer.Option(..., "--count", "-n", help="Number of new subsamples to create."),
    quantity: Optional[float] = typer.Option(
        None,
        "--quantity",
        "-q",
        help="Quantity per new subsample, in the subsample's current unit "
        "(see 'rspace get'). If omitted, quantity is split evenly.",
    ),
) -> None:
    """Split a subsample into multiple new subsamples.

    Without --quantity the subsample is split into count+1 equal parts (the
    original keeps one part). With --quantity each new subsample gets that
    amount and the original keeps the rest.

    [bold]Units:[/bold] --quantity is read in the unit the subsample has
    before the split (e.g. ml). The server may rescale units while splitting
    (1 ml can come back as 500 µl halves); the final quantities are then
    written back in the original unit, so check the result with 'rspace get'.

    Examples:

      rspace split SS123 --count 4

      rspace split SS123 --count 3 --quantity 10.0
    """
    ctx = get_context()
    try:
        check_id_type(id, "subsample")
        global_id = f"SS{parse_id(id)}"
    except ValueError as e:
        exit_with_error(str(e))
    if count < 1:
        exit_with_error("--count must be at least 1.")
    if quantity is not None and quantity <= 0:
        exit_with_error("--quantity must be greater than 0.")

    tags = []
    try:
        if quantity is not None:
            tags = ctx.inv.get_subsample_by_id(global_id).get("tags") or []
        result = ctx.inv.split_subsample(
            subsample=global_id,
            num_new_subsamples=count,
            quantity_per_subsample=quantity,
        )
    except Exception as e:
        handle_api_error(e)

    if hasattr(result, "success_results"):
        # BulkOperationResult: the original subsample plus the new ones
        records = [r["record"] for r in result.success_results()]
        errors = result.error_results()
        if tags:
            records = _restore_tags(ctx.inv, records, tags)
        new_count = sum(1 for r in records if r.get("id") != parse_id(global_id))
        console.print(f"[green]Created {new_count} new subsample(s).[/green]")
        if errors:
            err_console.print(f"[yellow]{len(errors)} error(s) during split.[/yellow]")
        print_result(records, _COLUMNS, ctx.output)
    else:
        items = result if isinstance(result, list) else [result]
        console.print(f"[green]Created {len(items)} new subsample(s).[/green]")
        print_result(items, _COLUMNS, ctx.output)
