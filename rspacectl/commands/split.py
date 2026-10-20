"""rspace split <subsample-id> — split a subsample into multiple new subsamples."""

from typing import Optional

import typer
from rspace_client.inv.inv import BulkOperationResult

from ..context import get_context
from ..exceptions import exit_with_error, handle_api_error, warn
from ..ids import parse_typed_id
from ..output import COL_GLOBAL_ID, COL_NAME_35, ColumnDef, console, err_console, print_result

_COLUMNS = [
    COL_GLOBAL_ID,
    COL_NAME_35,
    ColumnDef("quantity.numericValue", "Quantity", 10),
    ColumnDef("quantity.unitId", "Unit", 8),
]


def _restore_tags(inv, records: list, tags: list) -> list:
    """Re-apply ``tags`` to every split record in one bulk UPDATE; return the records.

    The client sets the new quantities with a bulk UPDATE that omits tags,
    and the API clears the tags of every record in it.
    """
    body = {
        "operationType": "UPDATE",
        "records": [{"type": "SUBSAMPLE", "id": r["id"], "tags": tags} for r in records],
    }
    try:
        result = BulkOperationResult(
            inv.retrieve_api_results("/bulk", request_type="POST", params=body)
        )
    except Exception as e:
        warn(f"Could not restore tags on the split subsamples: {e}")
        return records
    if result.error_results():
        warn(f"Could not restore tags on {len(result.error_results())} subsample(s).")
    restored = {r["record"]["id"]: r["record"] for r in result.success_results()}
    return [restored.get(r["id"], r) for r in records]


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
        ss_id = parse_typed_id(id, "subsample")
    except ValueError as e:
        exit_with_error(str(e))
    if count < 1:
        exit_with_error("--count must be at least 1.")
    if quantity is not None and quantity <= 0:
        exit_with_error("--quantity must be greater than 0.")

    try:
        # With --quantity the client needs the subsample's quantity anyway;
        # passing the record saves it a second GET, and it carries the tags.
        subsample = ctx.inv.get_subsample_by_id(ss_id) if quantity is not None else f"SS{ss_id}"
        result = ctx.inv.split_subsample(
            subsample=subsample,
            num_new_subsamples=count,
            quantity_per_subsample=quantity,
        )
    except Exception as e:
        handle_api_error(e)

    errors = []
    if isinstance(result, BulkOperationResult):
        # --quantity: the original subsample plus the new ones
        records = [r["record"] for r in result.success_results()]
        errors = result.error_results()
        tags = subsample.get("tags") or []
        if tags:
            records = _restore_tags(ctx.inv, records, tags)
    else:
        records = result if isinstance(result, list) else [result]

    new_count = sum(1 for r in records if r.get("id") != ss_id)
    console.print(f"[green]Created {new_count} new subsample(s).[/green]")
    if errors:
        err_console.print(f"[yellow]{len(errors)} error(s) during split.[/yellow]")
    print_result(records, _COLUMNS, ctx.output)
