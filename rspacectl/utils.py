"""Shared utility functions for rspacectl commands."""

import json
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Type

import typer

from .output import err_console


def parse_tags(tag_string: Optional[str], tag_class: Type) -> list:
    """Parse a comma-separated tag string into a list of tag objects.

    Args:
        tag_string: comma-separated tags, e.g. "buffers, enzymes"
        tag_class:  the SDK Tag class to instantiate (ELN and Inventory use
                    the same interface but are different imports)

    Returns:
        List of tag_class instances, or [] if tag_string is None/empty.
    """
    if not tag_string:
        return []
    return [tag_class(value=t.strip()) for t in tag_string.split(",") if t.strip()]


def load_json_file(path: Path) -> dict:
    """Read and parse a JSON file, exiting with an error if it can't be read."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        err_console.print(f"[red]File not found:[/red] {path}")
        raise typer.Exit(1)
    except json.JSONDecodeError as e:
        err_console.print(f"[red]Invalid JSON in {path}:[/red] {e}")
        raise typer.Exit(1)


def batch_run(
    ids: List[str],
    operation: Callable[[str], None],
    label: str,
) -> Tuple[List[str], List[str]]:
    """Run an operation on each ID, collecting successes and failures.

    Continues on individual errors rather than aborting early.

    Args:
        ids:       list of raw ID strings (numeric or GlobalID)
        operation: callable that takes a raw ID string and performs the action;
                   should raise on failure
        label:     resource label for console messages (e.g. "document")

    Returns:
        (successes, failures) — lists of ID strings
    """
    from .exceptions import warn

    successes, failures = [], []
    for raw_id in ids:
        try:
            operation(raw_id)
            successes.append(raw_id)
        except Exception as e:
            warn(f"Failed to {label} {raw_id}: {e}")
            failures.append(raw_id)
    return successes, failures


# ---------------------------------------------------------------------------
# Inventory PUT
# ---------------------------------------------------------------------------


def put_inventory_item(inv, endpoint: str, item_id: int, body: dict) -> dict:
    """PUT a partial update to /<endpoint>/<item_id> without losing the item's tags.

    The Inventory API clears an item's tags on any PUT whose body omits
    ``tags`` (seen on samples and instruments, RSpace 2.27; the client's
    ``rename`` is affected too). Unless the caller sets tags explicitly, the
    current tags are fetched and sent back unchanged.
    """
    if "tags" not in body:
        current = inv.retrieve_api_results(f"/{endpoint}/{item_id}")
        body = {**body, "tags": current.get("tags") or []}
    return inv.retrieve_api_results(f"/{endpoint}/{item_id}", request_type="PUT", params=body)


def set_item_image(inv, global_id: str, file) -> dict:
    """Tag-preserving replacement for the client's ``set_image``.

    ``set_image`` PUTs only ``newBase64Image``, which clears the item's tags
    (see put_inventory_item). Works for any inventory GlobalID (SA, SS, IC,
    IT, IN, NT).
    """
    import base64

    from rspace_client.inv.inv import Id

    item = Id(global_id)
    image_b64 = base64.b64encode(file.read()).decode("ascii")
    body = {"newBase64Image": "data:image/png;base64," + image_b64}
    return put_inventory_item(inv, item.get_api_endpoint(), item.as_id(), body)


# ---------------------------------------------------------------------------
# Instrument placement
# ---------------------------------------------------------------------------
# The client's add_items_to_*_container helpers only accept subsamples and
# containers, but the API moves an instrument via PUT /instruments/{id} with
# parentContainers (+ parentLocation for grid cells). Grid coordinates follow
# the client's GridLocation convention: coordX = column, coordY = row (1-based).


def instrument_location(
    container_id: int,
    row: Optional[int] = None,
    col: Optional[int] = None,
    location_id: Optional[int] = None,
) -> dict:
    """Request body fragment that places an instrument in a container.

    Pass ``row``/``col`` for a GRID cell, or ``location_id`` for an IMAGE
    container marker; neither for a LIST container.
    """
    body: dict = {"parentContainers": [{"id": container_id}]}
    if location_id is not None:
        body["parentLocation"] = {"id": location_id}
    elif row is not None and col is not None:
        body["parentLocation"] = {"coordX": col, "coordY": row}
    return body


def place_instrument(
    inv,
    instrument_id: int,
    container_id: int,
    row: Optional[int] = None,
    col: Optional[int] = None,
    location_id: Optional[int] = None,
) -> dict:
    """Move an instrument into a container (grid cell, image marker, or list)."""
    return put_inventory_item(
        inv,
        "instruments",
        instrument_id,
        instrument_location(container_id, row, col, location_id),
    )


def free_grid_cells(
    container: dict, start_row: int = 1, start_col: int = 1, by_column: bool = False
) -> List[Tuple[int, int]]:
    """Return empty (row, col) cells of a GRID container in fill order.

    ``container`` must have been fetched with include_content=True so that
    occupied ``locations`` are present. Cells before (start_row, start_col) in
    fill order are skipped, matching ByRow / ByColumn semantics.
    """
    layout = container.get("gridLayout") or {}
    rows = layout.get("rowsNumber") or 0
    cols = layout.get("columnsNumber") or 0
    occupied = {
        (loc.get("coordY"), loc.get("coordX"))
        for loc in container.get("locations") or []
        if loc.get("content")
    }
    if by_column:
        order = [(r, c) for c in range(1, cols + 1) for r in range(1, rows + 1)]
        start = (start_col, start_row)
        key = lambda rc: (rc[1], rc[0])  # noqa: E731
    else:
        order = [(r, c) for r in range(1, rows + 1) for c in range(1, cols + 1)]
        start = (start_row, start_col)
        key = lambda rc: rc  # noqa: E731
    return [rc for rc in order if key(rc) >= start and rc not in occupied]
