"""rspace list <noun> — paginated listing of resources."""

import datetime
from typing import Optional

import typer
from rspace_client.inv.inv import (
    DeletedItemFilter,
    Pagination,
    ResultType,
    SearchFilter,
)

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import (
    COL_CREATED,
    COL_GLOBAL_ID,
    COL_ID,
    COL_MODIFIED,
    COL_NAME_35,
    COL_NAME_40,
    COL_OWNER,
    ColumnDef,
    err_console,
    print_page_info,
    print_result,
)

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _eln_pagination(page: int, page_size: int, order_by: Optional[str], sort_order: str) -> dict:
    """Build kwargs dict for ELN client pagination parameters."""
    kwargs: dict = {"page_number": page, "page_size": page_size}
    if order_by:
        kwargs["order_by"] = f"{order_by} {sort_order}"
    return kwargs


def _inv_pagination(page: int, page_size: int, order_by: Optional[str], sort_order: str) -> Pagination:
    return Pagination(page_number=page, page_size=page_size, order_by=order_by, sort_order=sort_order)


# ---------------------------------------------------------------------------
# Shared folder/notebook listing (used by both list_folders and list_notebooks)
# ---------------------------------------------------------------------------

def _list_folder_type(parent: Optional[str], type_filter: str) -> None:
    ctx = get_context()
    columns = [COL_GLOBAL_ID, COL_NAME_40, COL_CREATED, COL_MODIFIED]
    try:
        folder_id = parse_id(parent) if parent else None
        result = ctx.eln.list_folder_tree(folder_id=folder_id, typesToInclude=[type_filter])
        data = result.get("records", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output)


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------

@app.command("documents")
def list_documents(
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Full-text search query."),
    tag: Optional[str] = typer.Option(None, "--tag", help="Filter by tag."),
    form: Optional[str] = typer.Option(None, "--form", help="Filter by form ID or name."),
    page: int = typer.Option(0, "--page", help="Page number (0-based)."),
    page_size: int = typer.Option(20, "--page-size", help="Results per page."),
    order_by: Optional[str] = typer.Option("lastModified", "--order-by", help="Sort field: name, created, lastModified."),
    sort_order: str = typer.Option("desc", "--sort-order", help="asc or desc."),
) -> None:
    """List ELN documents."""
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_40,
        ColumnDef("form.globalId", "Form", 10),
        COL_MODIFIED,
        ColumnDef("createdBy", "Owner", 20),
    ]
    try:
        kwargs = _eln_pagination(page, page_size, order_by, sort_order)
        if query or tag or form:
            parts = []
            if query:
                parts.append(query)
            if tag:
                parts.append(f"tag:{tag}")
            if form:
                parts.append(f"form:{form}")
            result = ctx.eln.get_documents(query=" ".join(parts), **kwargs)
        else:
            result = ctx.eln.get_documents(**kwargs)
        data = result.get("documents", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output)
    print_page_info(result, len(data))


# ---------------------------------------------------------------------------
# notebooks
# ---------------------------------------------------------------------------

@app.command("notebooks")
def list_notebooks(
    parent: Optional[str] = typer.Option(None, "--parent", help="Parent folder ID."),
) -> None:
    """List notebooks."""
    _list_folder_type(parent, "notebook")


# ---------------------------------------------------------------------------
# folders
# ---------------------------------------------------------------------------

@app.command("folders")
def list_folders(
    parent: Optional[str] = typer.Option(None, "--parent", help="Parent folder ID."),
) -> None:
    """List folders."""
    _list_folder_type(parent, "folder")


# ---------------------------------------------------------------------------
# samples
# ---------------------------------------------------------------------------

@app.command("samples")
def list_samples(
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search query (name, description, tags)."),
    owned_by: Optional[str] = typer.Option(None, "--owned-by", help="Filter by owner username."),
    deleted: bool = typer.Option(False, "--deleted", help="Include deleted samples."),
    page: int = typer.Option(0, "--page"),
    page_size: int = typer.Option(20, "--page-size"),
    order_by: Optional[str] = typer.Option(None, "--order-by", help="Sort field: name, created, lastModified."),
    sort_order: str = typer.Option("asc", "--sort-order"),
) -> None:
    """List inventory samples."""
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_35,
        ColumnDef("quantity.numericValue", "Quantity", 10),
        ColumnDef("quantity.unitId", "Unit", 8),
        ColumnDef("subSamplesCount", "Subsamples", 10),
        COL_CREATED,
        COL_OWNER,
    ]
    try:
        pagination = _inv_pagination(page, page_size, order_by, sort_order)
        if query:
            result = ctx.inv.search(query=query, pagination=pagination, result_type=ResultType.SAMPLE)
            data = result.get("records", [])
        else:
            deleted_filter = DeletedItemFilter.INCLUDE if deleted else DeletedItemFilter.EXCLUDE
            sample_filter = SearchFilter(deleted_item_filter=deleted_filter, owned_by=owned_by)
            result = ctx.inv.list_samples(pagination=pagination, sample_filter=sample_filter)
            data = result.get("samples", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output)
    print_page_info(result, len(data))


# ---------------------------------------------------------------------------
# subsamples
# ---------------------------------------------------------------------------

@app.command("subsamples")
def list_subsamples(
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search query."),
    page: int = typer.Option(0, "--page"),
    page_size: int = typer.Option(20, "--page-size"),
) -> None:
    """List inventory subsamples."""
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_35,
        ColumnDef("quantity.numericValue", "Quantity", 10),
        ColumnDef("quantity.unitId", "Unit", 8),
        COL_CREATED,
    ]
    try:
        pagination = _inv_pagination(page, page_size, None, "asc")
        result = ctx.inv.search(query=query or "", pagination=pagination, result_type=ResultType.SUBSAMPLE)
        data = result.get("records", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output)
    print_page_info(result, len(data))


# ---------------------------------------------------------------------------
# containers
# ---------------------------------------------------------------------------

@app.command("containers")
def list_containers(
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search query (name, description, tags)."),
    page: int = typer.Option(0, "--page"),
    page_size: int = typer.Option(20, "--page-size"),
    order_by: Optional[str] = typer.Option(None, "--order-by", help="Sort field: name, created, lastModified."),
    sort_order: str = typer.Option("asc", "--sort-order"),
) -> None:
    """List top-level inventory containers."""
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_35,
        ColumnDef("cType", "Type", 10),
        COL_CREATED,
        COL_OWNER,
    ]
    try:
        pagination = _inv_pagination(page, page_size, order_by, sort_order)
        if query:
            result = ctx.inv.search(query=query, pagination=pagination, result_type=ResultType.CONTAINER)
            data = result.get("records", [])
        else:
            result = ctx.inv.list_top_level_containers(pagination=pagination)
            data = result.get("containers", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output)
    print_page_info(result, len(data))


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

@app.command("files")
def list_files(
    media_type: str = typer.Option("image", "--type", help="Media type: image, document, chemFile, etc."),
    page: int = typer.Option(0, "--page"),
    page_size: int = typer.Option(20, "--page-size"),
    order_by: Optional[str] = typer.Option("lastModified", "--order-by", help="Sort field: name, created, lastModified, size."),
    sort_order: str = typer.Option("desc", "--sort-order"),
) -> None:
    """List gallery files."""
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_40,
        ColumnDef("contentType", "Content Type", 25),
        ColumnDef("size", "Size", 12),
        COL_CREATED,
    ]
    try:
        result = ctx.eln.get_files(
            page_number=page,
            page_size=page_size,
            order_by=f"{order_by} {sort_order}",
            media_type=media_type,
        )
        data = result.get("files", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output)
    print_page_info(result, len(data))


# ---------------------------------------------------------------------------
# forms
# ---------------------------------------------------------------------------

@app.command("forms")
def list_forms(
    query: Optional[str] = typer.Option(None, "--query", "-q"),
    page: int = typer.Option(0, "--page"),
    page_size: int = typer.Option(20, "--page-size"),
    order_by: Optional[str] = typer.Option("lastModified", "--order-by", help="Sort field: name, created, lastModified."),
    sort_order: str = typer.Option("desc", "--sort-order"),
) -> None:
    """List document forms (templates)."""
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_35,
        ColumnDef("formState", "State", 12),
        ColumnDef("version", "Version", 8),
        COL_MODIFIED,
    ]
    try:
        result = ctx.eln.get_forms(
            query=query,
            order_by=f"{order_by} {sort_order}",
            page_number=page,
            page_size=page_size,
        )
        data = result.get("forms", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output)
    print_page_info(result, len(data))


# ---------------------------------------------------------------------------
# templates (sample templates)
# ---------------------------------------------------------------------------

@app.command("templates")
def list_templates(
    page: int = typer.Option(0, "--page"),
    page_size: int = typer.Option(20, "--page-size"),
) -> None:
    """List inventory sample templates."""
    ctx = get_context()
    columns = [COL_GLOBAL_ID, COL_NAME_35, COL_CREATED, COL_MODIFIED, COL_OWNER]
    try:
        pagination = _inv_pagination(page, page_size, None, "asc")
        result = ctx.inv.list_sample_templates(pagination=pagination)
        data = result.get("templates", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output)
    print_page_info(result, len(data))


# ---------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------

@app.command("groups")
def list_groups() -> None:
    """List groups the current user belongs to."""
    ctx = get_context()
    columns = [
        COL_ID,
        COL_NAME_35,
        ColumnDef("type", "Type", 15),
        ColumnDef("role", "Role", 12),
    ]
    try:
        result = ctx.eln.get_groups()
        data = result.get("groups", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output, id_key="id")


# ---------------------------------------------------------------------------
# users (sysadmin)
# ---------------------------------------------------------------------------

@app.command("users")
def list_users(
    created_before: Optional[str] = typer.Option(None, "--created-before", help="ISO date, e.g. 2024-01-01"),
    page: int = typer.Option(0, "--page"),
    page_size: int = typer.Option(20, "--page-size"),
) -> None:
    """List users (sysadmin only)."""
    ctx = get_context()
    columns = [
        COL_ID,
        ColumnDef("username", "Username", 20),
        ColumnDef("email", "Email", 30),
        ColumnDef("firstName", "First Name", 15),
        ColumnDef("lastName", "Last Name", 15),
    ]
    try:
        cb = created_before or datetime.date.today().isoformat()
        result = ctx.eln.get_users(
            page_number=page,
            page_size=page_size,
            tempaccount_only=False,
            created_before=cb,
        )
        data = result.get("users", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output, id_key="id")
    print_page_info(result, len(data))


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------

@app.command("activity")
def list_activity(
    date_from: Optional[str] = typer.Option(None, "--from", help="Start date (ISO format)."),
    date_to: Optional[str] = typer.Option(None, "--to", help="End date (ISO format)."),
    action: Optional[str] = typer.Option(None, "--action", help="Filter by action type."),
    user: Optional[str] = typer.Option(None, "--user", help="Filter by username."),
    global_id: Optional[str] = typer.Option(None, "--id", help="Filter by resource GlobalID."),
    page: int = typer.Option(0, "--page"),
    page_size: int = typer.Option(20, "--page-size"),
) -> None:
    """List audit trail activity."""
    ctx = get_context()
    columns = [
        ColumnDef("timestamp", "Timestamp", 16),
        ColumnDef("username", "User", 20),
        ColumnDef("action", "Action", 20),
        ColumnDef("domain", "Domain", 15),
        ColumnDef("recordId", "Record ID", 12),
    ]
    try:
        date_from_dt = datetime.date.fromisoformat(date_from) if date_from else None
        date_to_dt = datetime.date.fromisoformat(date_to) if date_to else None
        result = ctx.eln.get_activity(
            page_number=page,
            page_size=page_size,
            date_from=date_from_dt,
            date_to=date_to_dt,
            actions=[action] if action else None,
            users=[user] if user else None,
            global_id=global_id,
        )
        data = result.get("activity", [])
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output, id_key="recordId")
    print_page_info(result, len(data))


# ---------------------------------------------------------------------------
# workbenches
# ---------------------------------------------------------------------------

@app.command("workbenches")
def list_workbenches() -> None:
    """List inventory workbenches."""
    ctx = get_context()
    columns = [COL_GLOBAL_ID, COL_NAME_35, COL_OWNER]
    try:
        data = ctx.inv.get_workbenches()
        # SDK returns a list; guard against a single-item dict response
        if not isinstance(data, list):
            data = [data] if data else []
    except Exception as e:
        handle_api_error(e)
    print_result(data, columns, ctx.output)
