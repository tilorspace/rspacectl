"""rspace move <id…> --target <container-id> — move inventory items into a container."""

from typing import List, Optional

import typer
from rspace_client.inv.inv import ByColumn, ByLocation, ByRow, GridLocation

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import console, err_console

_STRATEGY_CHOICES = ["row", "column"]


def move(
    ids: List[str] = typer.Argument(..., help="Item GlobalID(s) or ID(s) to move."),
    target: str = typer.Option(..., "--target", "-t", help="Target container GlobalID or ID."),
    strategy: Optional[str] = typer.Option(
        None, "--strategy", "-s",
        help="Auto-fill strategy for grid containers: 'row' or 'column'.",
    ),
    row: Optional[int] = typer.Option(None, "--row", help="Starting row index (1-based) for grid placement."),
    col: Optional[int] = typer.Option(None, "--col", help="Starting column index (1-based) for grid placement."),
) -> None:
    """Move one or more inventory items into a container.

    For list containers, simply provide --target.
    For grid containers, use --strategy row|column (auto-fill) or --row/--col for exact placement.

    Examples:

      rspace move SS123 SS124 --target IC456

      rspace move SS123 SS124 --target IC456 --strategy row

      rspace move SS123 --target IC456 --row 2 --col 3
    """
    if strategy and strategy not in _STRATEGY_CHOICES:
        err_console.print(f"[red]Invalid strategy '{strategy}'. Choose: {', '.join(_STRATEGY_CHOICES)}[/red]")
        raise typer.Exit(1)

    ctx = get_context()

    try:
        target_id = parse_id(target)
        container_info = ctx.inv.get_container_by_id(target_id)
        ctype = container_info.get("cType", "LIST")
        grid_layout = container_info.get("gridLayout", {})
        total_cols = grid_layout.get("columnsNumber", 10)
        total_rows = grid_layout.get("rowsNumber", 10)

        if ctype == "GRID" and (strategy or row or col):
            start_col = col or 1
            start_row = row or 1
            if strategy == "column":
                placement = ByColumn(start_col, start_row, total_cols, total_rows, *ids)
            elif row and col:
                placement = ByLocation(locations=[GridLocation(x=col, y=row)], *ids)
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
