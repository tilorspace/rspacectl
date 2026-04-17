"""rspace create <noun> — create new resources."""

import csv as csv_module
import datetime
import json
from pathlib import Path
from typing import List, Optional

import typer
from rspace_client.inv.inv import Quantity, SamplePost
from rspace_client.inv.inv import Tag as InvTag

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import (
    COL_CREATED,
    COL_GLOBAL_ID,
    COL_NAME_35,
    COL_NAME_40,
    ColumnDef,
    console,
    err_console,
    print_result,
    print_single,
)
from ..utils import load_json_file, parse_tags

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")

_SIMPLE_COLUMNS = [COL_GLOBAL_ID, COL_NAME_40, COL_CREATED]


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------


@app.command("document")
def create_document(
    name: str = typer.Option(..., "--name", "-n", help="Document name."),
    folder: Optional[str] = typer.Option(None, "--folder", help="Parent folder ID."),
    tag: Optional[str] = typer.Option(None, "--tag", help="Comma-separated tags."),
    form: Optional[str] = typer.Option(None, "--form", help="Form ID to use."),
    content: Optional[str] = typer.Option(
        None, "--content", help="HTML content for the first field."
    ),
) -> None:
    """Create a new ELN document."""
    ctx = get_context()
    try:
        result = ctx.eln.create_document(
            name=name,
            parent_folder_id=parse_id(folder) if folder else None,
            tags=tag,
            form_id=parse_id(form) if form else None,
            fields=[{"content": content}] if content else None,
        )
    except Exception as e:
        handle_api_error(e)
    console.print(f"[green]Created document[/green] {result.get('globalId')}")
    print_single(result, ctx.output, _SIMPLE_COLUMNS)


# ---------------------------------------------------------------------------
# notebook + folder  (unified via helper)
# ---------------------------------------------------------------------------


def _create_folder_like(name: str, parent_id: Optional[str], notebook: bool) -> None:
    ctx = get_context()
    label = "notebook" if notebook else "folder"
    try:
        result = ctx.eln.create_folder(
            name=name,
            parent_folder_id=parse_id(parent_id) if parent_id else None,
            notebook=notebook,
        )
    except Exception as e:
        handle_api_error(e)
    console.print(f"[green]Created {label}[/green] {result.get('globalId')}")
    print_single(result, ctx.output, _SIMPLE_COLUMNS)


@app.command("notebook")
def create_notebook(
    name: str = typer.Option(..., "--name", "-n", help="Notebook name."),
    folder: Optional[str] = typer.Option(None, "--folder", help="Parent folder ID."),
) -> None:
    """Create a new notebook."""
    _create_folder_like(name, folder, notebook=True)


@app.command("folder")
def create_folder(
    name: str = typer.Option(..., "--name", "-n", help="Folder name."),
    parent: Optional[str] = typer.Option(None, "--parent", help="Parent folder ID."),
) -> None:
    """Create a new folder."""
    _create_folder_like(name, parent, notebook=False)


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------


def _parse_fields(field_args: List[str]) -> dict:
    """Parse a list of 'NAME=VALUE' strings into a {name: value} dict."""
    parsed = {}
    for arg in field_args:
        if "=" not in arg:
            err_console.print(f"[red]Invalid --field format (expected NAME=VALUE):[/red] {arg}")
            raise typer.Exit(1)
        name, _, value = arg.partition("=")
        parsed[name.strip()] = value.strip()
    return parsed


def _build_fields_post(template_fields: list, field_values: dict) -> list:
    """
    Build the fields list for create_sample POST.
    Matches supplied name→value pairs against the template's field order,
    inserting empty dicts for unprovided fields to preserve positional ordering.
    """
    posts = []
    for f in template_fields:
        fname = f.get("name", "")
        ftype = f.get("type", "").lower()
        value = field_values.get(fname)
        if value is None:
            posts.append({})
            continue
        if ftype == "choice":
            posts.append({"selectedOptions": [v.strip() for v in value.split(",")]})
        elif ftype == "radio":
            posts.append({"selectedOptions": [value]})
        else:
            posts.append({"content": value})
    return posts


def _validate_mandatory_fields(template_fields: list, field_values: dict) -> None:
    """Fail fast if any mandatory template fields are missing from field_values."""
    missing = [
        f["name"]
        for f in template_fields
        if f.get("mandatory") and f.get("name") not in field_values
    ]
    if missing:
        err_console.print("[red]Missing mandatory template fields:[/red]")
        for m in missing:
            err_console.print(f"  --field \"{m}=<value>\"")
        raise typer.Exit(1)


@app.command("sample")
def create_sample(
    name: str = typer.Option(..., "--name", "-n", help="Sample name."),
    template: Optional[str] = typer.Option(None, "--template", help="Sample template ID."),
    field: Optional[List[str]] = typer.Option(
        None, "--field", help="Template field value as NAME=VALUE. Repeatable."
    ),
    quantity: Optional[float] = typer.Option(
        None, "--quantity", "-q", help="Total quantity value."
    ),
    unit: Optional[int] = typer.Option(None, "--unit", "-u", help="Unit ID (RSpace unit system)."),
    expiry: Optional[str] = typer.Option(
        None, "--expiry", help="Expiry date (ISO format, e.g. 2025-12-31)."
    ),
    tag: Optional[str] = typer.Option(None, "--tag", help="Comma-separated tags."),
    description: Optional[str] = typer.Option(None, "--description", "-d"),
    subsample_count: Optional[int] = typer.Option(None, "--subsample-count"),
    from_csv: Optional[Path] = typer.Option(
        None, "--from-csv", help="CSV file for bulk sample creation."
    ),
) -> None:
    """Create a new inventory sample. Use --from-csv for bulk creation.

    To populate template fields use --field repeatedly:

      rspace create sample --name X --template IT123 \\
        --field "pH=7.4" --field "Source=Commercial"
    """
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_35,
        ColumnDef("quantity.numericValue", "Quantity", 10),
        COL_CREATED,
    ]

    if from_csv:
        _bulk_create_from_csv(ctx, from_csv, columns)
        return

    field_values = _parse_fields(field or [])

    template_fields = []
    if template:
        try:
            tmpl = ctx.inv.get_sample_template_by_id(parse_id(template))
            template_fields = tmpl.get("fields", [])
        except Exception as e:
            handle_api_error(e)
        _validate_mandatory_fields(template_fields, field_values)

    fields_post = _build_fields_post(template_fields, field_values) if template_fields else None

    try:
        total_quantity = (
            Quantity(numericValue=quantity, unitId=unit)
            if quantity is not None and unit is not None
            else None
        )
        expiry_dt = datetime.datetime.fromisoformat(expiry) if expiry else None

        result = ctx.inv.create_sample(
            name=name,
            tags=parse_tags(tag, InvTag),
            description=description,
            sample_template_id=parse_id(template) if template else None,
            total_quantity=total_quantity,
            expiry_date=expiry_dt,
            subsample_count=subsample_count,
            fields=fields_post,
        )
    except Exception as e:
        handle_api_error(e)

    console.print(f"[green]Created sample[/green] {result.get('globalId')}")
    print_single(result, ctx.output, columns)


def _bulk_create_from_csv(ctx, csv_path: Path, columns: list) -> None:
    """Create multiple samples from a CSV file (name column required)."""
    posts = []
    try:
        with csv_path.open() as f:
            reader = csv_module.DictReader(f)
            for row in reader:
                name = row.get("name") or row.get("Name")
                if not name:
                    err_console.print(f"[yellow]Skipping row without 'name':[/yellow] {row}")
                    continue
                posts.append(SamplePost(name=name))
    except FileNotFoundError:
        err_console.print(f"[red]File not found:[/red] {csv_path}")
        raise typer.Exit(1)

    if not posts:
        err_console.print("[red]No valid rows found in CSV.[/red]")
        raise typer.Exit(1)

    try:
        result = ctx.inv.bulk_create_sample(*posts)
    except Exception as e:
        handle_api_error(e)

    successes = result.success_results()
    errors = result.error_results()
    console.print(f"[green]Created {len(successes)} sample(s).[/green]")
    if errors:
        err_console.print(f"[yellow]{len(errors)} error(s):[/yellow]")
        for err in errors:
            err_console.print(f"  {err}")


# ---------------------------------------------------------------------------
# container
# ---------------------------------------------------------------------------


@app.command("container")
def create_container(
    name: str = typer.Option(..., "--name", "-n", help="Container name."),
    type: str = typer.Option("list", "--type", "-t", help="Container type: list or grid."),
    rows: Optional[int] = typer.Option(None, "--rows", help="Row count (grid containers only)."),
    cols: Optional[int] = typer.Option(None, "--cols", help="Column count (grid containers only)."),
    tag: Optional[str] = typer.Option(None, "--tag", help="Comma-separated tags."),
    description: Optional[str] = typer.Option(None, "--description", "-d"),
    no_samples: bool = typer.Option(False, "--no-samples", help="Disallow storing samples."),
    no_containers: bool = typer.Option(
        False, "--no-containers", help="Disallow storing containers."
    ),
) -> None:
    """Create a new inventory container (list or grid)."""
    ctx = get_context()
    columns = [COL_GLOBAL_ID, COL_NAME_35, ColumnDef("cType", "Type", 10), COL_CREATED]
    tags_list = parse_tags(tag, InvTag)

    try:
        if type == "grid":
            if not rows or not cols:
                err_console.print("[red]--rows and --cols are required for grid containers.[/red]")
                raise typer.Exit(1)
            result = ctx.inv.create_grid_container(
                name=name,
                row_count=rows,
                column_count=cols,
                tags=tags_list,
                description=description,
                can_store_samples=not no_samples,
                can_store_containers=not no_containers,
            )
        else:
            result = ctx.inv.create_list_container(
                name=name,
                tags=tags_list,
                description=description,
                can_store_samples=not no_samples,
                can_store_containers=not no_containers,
            )
    except Exception as e:
        handle_api_error(e)

    console.print(f"[green]Created container[/green] {result.get('globalId')}")
    print_single(result, ctx.output, columns)


# ---------------------------------------------------------------------------
# form
# ---------------------------------------------------------------------------


@app.command("form")
def create_form(
    name: str = typer.Option(..., "--name", "-n", help="Form name."),
    fields_file: Optional[Path] = typer.Option(
        None, "--fields-file", help="JSON file defining form fields."
    ),
    tag: Optional[str] = typer.Option(None, "--tag"),
    publish: bool = typer.Option(False, "--publish", help="Publish the form after creation."),
) -> None:
    """Create a new ELN form definition."""
    ctx = get_context()
    columns = [COL_GLOBAL_ID, COL_NAME_40, ColumnDef("formState", "State", 12)]
    fields = load_json_file(fields_file) if fields_file else None

    try:
        result = ctx.eln.create_form(name=name, tags=tag, fields=fields)
        if publish:
            result = ctx.eln.publish_form(result["id"])
    except Exception as e:
        handle_api_error(e)

    console.print(f"[green]Created form[/green] {result.get('globalId')}")
    print_single(result, ctx.output, columns)


# ---------------------------------------------------------------------------
# template (sample template)
# ---------------------------------------------------------------------------


@app.command("template")
def create_template(
    from_file: Path = typer.Option(
        ..., "--from-file", help="JSON file defining the sample template."
    ),
) -> None:
    """Create a new inventory sample template from a JSON definition file."""
    ctx = get_context()
    columns = [COL_GLOBAL_ID, COL_NAME_40, COL_CREATED]
    template_post = load_json_file(from_file)

    try:
        result = ctx.inv.create_sample_template(sample_template_post=template_post)
    except Exception as e:
        handle_api_error(e)

    console.print(f"[green]Created template[/green] {result.get('globalId')}")
    print_single(result, ctx.output, columns)


# ---------------------------------------------------------------------------
# user (sysadmin)
# ---------------------------------------------------------------------------


@app.command("user")
def create_user(
    username: str = typer.Option(..., "--username"),
    email: str = typer.Option(..., "--email"),
    first_name: str = typer.Option(..., "--first-name"),
    last_name: str = typer.Option(..., "--last-name"),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True),
    role: str = typer.Option("ROLE_USER", "--role", help="User role (ROLE_USER or ROLE_ADMIN)."),
) -> None:
    """Create a new RSpace user (sysadmin only)."""
    ctx = get_context()
    columns = [
        ColumnDef("id", "ID", 8),
        ColumnDef("username", "Username", 20),
        ColumnDef("email", "Email", 30),
    ]
    try:
        result = ctx.eln.retrieve_api_results(
            "/sysadmin/users",
            request_type="POST",
            params={
                "username": username,
                "email": email,
                "firstName": first_name,
                "lastName": last_name,
                "password": password,
                "role": role,
            },
        )
    except Exception as e:
        handle_api_error(e)

    console.print(f"[green]Created user[/green] {result.get('username')}")
    print_single(result, ctx.output, columns)
