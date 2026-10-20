"""rspace move <id…> --target <container-id> — move inventory items into a container."""

from typing import List, Optional

import typer
from rspace_client.inv.inv import ByColumn, ByLocation, ByRow, GridLocation

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id, resource_type
from ..output import console, err_console
from ..utils import batch_run, free_grid_cells, get_container, place_instrument

_STRATEGY_CHOICES = ["row", "column"]


def move(
    ids: List[str] = typer.Argument(..., help="Item GlobalID(s) or ID(s) to move."),
    target: str = typer.Option(..., "--target", "-t", help="Target container GlobalID or ID."),
    strategy: Optional[str] = typer.Option(
        None,
        "--strategy",
        "-s",
        help="Auto-fill strategy for grid containers: 'row' or 'column'.",
    ),
    row: Optional[int] = typer.Option(
        None, "--row", help="Starting row index (1-based) for grid placement."
    ),
    col: Optional[int] = typer.Option(
        None, "--col", help="Starting column index (1-based) for grid placement."
    ),
) -> None:
    """Move one or more inventory items (subsamples, containers, instruments) into a container.

    For list containers, simply provide --target.
    For grid containers, use --strategy row|column (auto-fill) or --row/--col for exact placement.
    Instruments are filled into free grid cells; move them separately from other items
    when placing into a grid.

    Examples:

      rspace move SS123 SS124 --target IC456

      rspace move SS123 SS124 --target IC456 --strategy row

      rspace move SS123 --target IC456 --row 2 --col 3

      rspace move IN12 IN13 --target IC456 --strategy row
    """
    if strategy and strategy not in _STRATEGY_CHOICES:
        err_console.print(
            f"[red]Invalid strategy '{strategy}'. Choose: {', '.join(_STRATEGY_CHOICES)}[/red]"
        )
        raise typer.Exit(1)

    ctx = get_context()

    instrument_ids = [i for i in ids if resource_type(i) == "instrument"]
    if instrument_ids:
        other_ids = [i for i in ids if i not in instrument_ids]
        _move_instruments(ctx, instrument_ids, other_ids, target, strategy, row, col)
        if not other_ids:
            return
        ids = other_ids

    try:
        target_id = parse_id(target)
        container_info = get_container(ctx.inv, target)
        ctype = container_info.get("cType", "LIST")
        # LIST containers return "gridLayout": null, not a missing key.
        grid_layout = container_info.get("gridLayout") or {}
        total_cols = grid_layout.get("columnsNumber", 10)
        total_rows = grid_layout.get("rowsNumber", 10)

        if ctype == "GRID" and (strategy or row or col):
            start_col = col or 1
            start_row = row or 1
            if strategy == "column":
                placement = ByColumn(start_col, start_row, total_cols, total_rows, *ids)
            elif row and col:
                placement = ByLocation([GridLocation(x=col, y=row)], *ids)
            else:
                placement = ByRow(start_col, start_row, total_cols, total_rows, *ids)
            result = ctx.inv.add_items_to_grid_container(
                target_container_id=target_id,
                grid_placement=placement,
            )
        else:
            result = ctx.inv.add_items_to_list_container(target_id, *ids)

    except Exception as e:
        handle_api_error(e)

    if hasattr(result, "is_ok"):
        if result.is_ok():
            console.print(f"[green]Moved {len(ids)} item(s) to container {target}.[/green]")
        else:
            errors = result.error_results()
            err_console.print(f"[yellow]Completed with {len(errors)} error(s):[/yellow]")
            for err in errors:
                err_console.print(f"  {err}")
            raise typer.Exit(1)
    else:
        console.print(f"[green]Moved {len(ids)} item(s) to container {target}.[/green]")


def _move_instruments(
    ctx,
    instrument_ids: List[str],
    other_ids: List[str],
    target: str,
    strategy: Optional[str],
    row: Optional[int],
    col: Optional[int],
) -> None:
    """Move instruments one by one via PUT (the client's container helpers reject them)."""
    try:
        target_id = parse_id(target)
        container = get_container(ctx.inv, target, include_content=True)
    except Exception as e:
        handle_api_error(e)
        return  # handle_api_error always exits, but be explicit

    if container.get("canStoreInstruments") is False:
        err_console.print(f"[red]Container {target} cannot store instruments.[/red]")
        raise typer.Exit(1)

    cells: List[Optional[tuple]] = [None] * len(instrument_ids)
    if container.get("cType") == "GRID":
        if other_ids:
            err_console.print(
                "[red]Move instruments into a grid container separately from other items.[/red]"
            )
            raise typer.Exit(1)
        if row and col and not strategy:
            if len(instrument_ids) > 1:
                err_console.print(
                    "[red]--row/--col places a single instrument; use --strategy for several.[/red]"
                )
                raise typer.Exit(1)
            cells = [(row, col)]
        else:
            free = free_grid_cells(
                container, start_row=row or 1, start_col=col or 1, by_column=strategy == "column"
            )
            if len(free) < len(instrument_ids):
                err_console.print(
                    f"[red]Container {target} has {len(free)} free cell(s) from the start "
                    f"position, but {len(instrument_ids)} instrument(s) to place.[/red]"
                )
                raise typer.Exit(1)
            cells = free[: len(instrument_ids)]

    placements = dict(zip(instrument_ids, cells))

    def _place(raw_id: str) -> None:
        cell = placements[raw_id]
        r, c = cell if cell else (None, None)
        place_instrument(ctx.inv, parse_id(raw_id), target_id, row=r, col=c)

    successes, failures = batch_run(instrument_ids, _place, "move instrument")
    console.print(f"[green]Moved {len(successes)} instrument(s) to container {target}.[/green]")
    if failures:
        err_console.print(f"[yellow]Failed ({len(failures)}):[/yellow] {', '.join(failures)}")
        raise typer.Exit(1)
