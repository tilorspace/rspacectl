"""Output formatting for rspacectl.

Supports four output formats:
- table  (default): Rich table with auto-sized columns
- json:             Pretty-printed JSON via Rich
- csv:              RFC 4180 CSV to stdout
- quiet:            GlobalIDs (or numeric IDs) one per line, for piping
"""

import csv
import json
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from rich import print_json
from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

TIMESTAMP_WIDTH = 16  # "YYYY-MM-DD HH:MM"
_TIMESTAMP_FIELDS = frozenset({"created", "lastModified", "creationDate", "modificationDate"})


class OutputFormat(str, Enum):
    TABLE = "table"
    JSON = "json"
    CSV = "csv"
    QUIET = "quiet"


@dataclass
class ColumnDef:
    """Definition of a single table / CSV column."""

    key: str  # key path into the row dict, supports "a.b" for nested access
    title: str  # column header
    width: int = 0  # max display width; 0 = auto (Rich decides)
    style: str = ""  # Rich markup style, e.g. "bold cyan"


def _get_nested(d: Dict[str, Any], key: str) -> str:
    """Extract a possibly-nested value from a dict using dot notation.

    Examples:
        _get_nested({"a": {"b": 1}}, "a.b") -> "1"
        _get_nested({"name": "foo"}, "name") -> "foo"
    """
    parts = key.split(".", 1)
    val = d.get(parts[0], "")
    if len(parts) == 2 and isinstance(val, dict):
        return _get_nested(val, parts[1])
    if val is None:
        return ""
    return str(val)


def _truncate_timestamp(ts: str) -> str:
    """Shorten an ISO timestamp to 'YYYY-MM-DD HH:MM'."""
    if ts and len(ts) > TIMESTAMP_WIDTH:
        return ts[:TIMESTAMP_WIDTH].replace("T", " ")
    return ts


def _cell_value(row: Dict[str, Any], col: ColumnDef) -> str:
    """Extract and format the display value for a cell."""
    raw = _get_nested(row, col.key)
    # Auto-format timestamp fields
    if col.key in _TIMESTAMP_FIELDS:
        raw = _truncate_timestamp(raw)
    return raw


def print_result(
    data: List[Dict[str, Any]],
    columns: List[ColumnDef],
    fmt: OutputFormat,
    id_key: str = "globalId",
) -> None:
    """Render a list of dicts according to the chosen output format.

    Args:
        data:    list of result dicts from the SDK
        columns: column definitions for table/CSV output
        fmt:     output format
        id_key:  dict key used for quiet mode output (default: "globalId")
    """
    if fmt == OutputFormat.JSON:
        _print_json(data)
    elif fmt == OutputFormat.CSV:
        _print_csv(data, columns)
    elif fmt == OutputFormat.QUIET:
        _print_quiet(data, id_key)
    else:
        _print_table(data, columns)


def print_single(item: Dict[str, Any], fmt: OutputFormat, columns: List[ColumnDef]) -> None:
    """Render a single resource dict (e.g. from a 'get' command)."""
    if fmt == OutputFormat.JSON:
        _print_json(item)
    elif fmt == OutputFormat.QUIET:
        console.print(item.get("globalId") or item.get("id", ""))
    elif fmt == OutputFormat.CSV:
        _print_csv([item], columns)
    else:
        _print_detail_table(item, columns)


# ---------------------------------------------------------------------------
# Internal renderers
# ---------------------------------------------------------------------------


def _print_json(data: Any) -> None:
    print_json(json.dumps(data, default=str))


def _print_table(data: List[Dict[str, Any]], columns: List[ColumnDef]) -> None:
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)

    for col in columns:
        kwargs: Dict[str, Any] = {"no_wrap": False}
        if col.width > 0:
            kwargs["max_width"] = col.width
        if col.style:
            kwargs["style"] = col.style
        table.add_column(col.title, **kwargs)

    for row in data:
        table.add_row(*[_cell_value(row, col) for col in columns])

    if not data:
        console.print("[dim]No results.[/dim]")
    else:
        console.print(table)


def _print_detail_table(item: Dict[str, Any], columns: List[ColumnDef]) -> None:
    """Two-column key/value table for a single resource."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Field", style="bold", min_width=20)
    table.add_column("Value")

    for col in columns:
        table.add_row(col.title, _cell_value(item, col))

    console.print(table)


def _print_csv(data: List[Dict[str, Any]], columns: List[ColumnDef]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow([col.title for col in columns])
    for row in data:
        writer.writerow([_cell_value(row, col) for col in columns])


def _print_quiet(data: List[Dict[str, Any]], id_key: str) -> None:
    for row in data:
        val = row.get(id_key) or row.get("globalId") or row.get("id", "")
        console.print(val, highlight=False)


def print_page_info(response: Dict[str, Any], count: int) -> None:
    """Print a summary footer: 'Showing X–Y of TOTAL' for paginated responses.

    Reads totalHits / pageNumber / pageSize from the response envelope.
    No-ops silently if those keys aren't present (e.g. non-paginated endpoints).
    """
    total = response.get("totalHits")
    if total is None:
        return
    page = response.get("pageNumber", 0)
    page_size = response.get("pageSize", count)
    start = page * page_size + 1
    end = start + count - 1
    console.print(
        f"[dim]Showing {start}–{end} of {total}  (page {page}, use --page / --page-size to navigate)[/dim]"
    )


# ---------------------------------------------------------------------------
# Reusable column definitions
# ---------------------------------------------------------------------------

COL_GLOBAL_ID = ColumnDef("globalId", "Global ID", 10, "cyan")
COL_ID = ColumnDef("id", "ID", 8, "cyan")
COL_NAME_40 = ColumnDef("name", "Name", 40)
COL_NAME_35 = ColumnDef("name", "Name", 35)
COL_CREATED = ColumnDef("created", "Created", 16)
COL_MODIFIED = ColumnDef("lastModified", "Modified", 16)
COL_OWNER = ColumnDef("owner.username", "Owner", 20)
