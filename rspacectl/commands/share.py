"""rspace share <id…> — share ELN documents with a group."""

from typing import List, Optional

import typer

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import COL_GLOBAL_ID, ColumnDef, console, err_console, print_result

_PERMISSION_CHOICES = ["read", "edit"]

_COLUMNS = [
    ColumnDef("id", "Share ID", 10, "cyan"),
    ColumnDef("itemId", "Item ID", 10),
    ColumnDef("groupId", "Group ID", 10),
    ColumnDef("operation", "Permission", 10),
]


def share(
    ids: List[str] = typer.Argument(..., help="Document GlobalID(s) or ID(s) to share."),
    group: str = typer.Option(..., "--group", "-g", help="Group ID to share with."),
    permission: str = typer.Option("read", "--permission", "-p", help="Permission level: read or edit."),
    shared_folder: Optional[str] = typer.Option(
        None, "--shared-folder", help="Shared folder ID (uses group default if omitted).",
    ),
) -> None:
    """Share one or more ELN documents with a group.

    Example:

      rspace share SD123 SD124 --group 5 --permission edit
    """
    if permission not in _PERMISSION_CHOICES:
        err_console.print(f"[red]Invalid permission '{permission}'. Choose: {', '.join(_PERMISSION_CHOICES)}[/red]")
        raise typer.Exit(1)

    ctx = get_context()
    item_ids = [parse_id(i) for i in ids]

    try:
        result = ctx.eln.shareDocuments(
            itemsToShare=item_ids,
            groupId=parse_id(group),
            sharedFolderId=parse_id(shared_folder) if shared_folder else None,
            permission=permission.upper(),
        )
        data = result.get("shareInfos", result if isinstance(result, list) else [result])
    except Exception as e:
        handle_api_error(e)

    console.print(f"[green]Shared {len(item_ids)} item(s) with group {group}.[/green]")
    print_result(data, _COLUMNS, ctx.output, id_key="id")
