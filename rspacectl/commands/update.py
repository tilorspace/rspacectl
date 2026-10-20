"""rspace update <noun> <id> — modify existing resources."""

from typing import List, Optional

import typer
from rspace_client.inv.inv import Tag as InvTag

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import (
    COL_GLOBAL_ID,
    COL_MODIFIED,
    COL_NAME_35,
    COL_NAME_40,
    ColumnDef,
    console,
    err_console,
    print_single,
)
from ..utils import parse_tags, put_inventory_item

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------


@app.command("document")
def update_document(
    id: str = typer.Argument(..., help="Document ID or GlobalID."),
    name: Optional[str] = typer.Option(None, "--name", "-n"),
    tag: Optional[str] = typer.Option(None, "--tag", help="Replace tags (comma-separated)."),
    content: Optional[str] = typer.Option(None, "--content", help="Replace field content (HTML)."),
    append: Optional[str] = typer.Option(None, "--append", help="Append HTML content to a field."),
    prepend: Optional[str] = typer.Option(
        None, "--prepend", help="Prepend HTML content to a field."
    ),
    field_id: Optional[int] = typer.Option(
        None,
        "--field-id",
        help="Target a specific field by its numeric ID (use with --content). "
        "Find field IDs with: rspace get SD123 -o json",
    ),
    field_index: int = typer.Option(
        0,
        "--field-index",
        help="Target a specific field by 0-based index (use with --append or --prepend). "
        "Defaults to 0 (first field).",
    ),
) -> None:
    """Update an ELN document's name, tags, or content.

    Target a specific field with [bold]--field-id[/bold] (for --content) or
    [bold]--field-index[/bold] (for --append / --prepend). Field IDs and indices
    can be found by inspecting the document:

      rspace get SD123 -o json
    """
    ctx = get_context()
    columns = [COL_GLOBAL_ID, COL_NAME_40, ColumnDef("tags", "Tags", 30), COL_MODIFIED]
    doc_id = parse_id(id)
    try:
        if append:
            result = ctx.eln.append_content(doc_id, append, field_index=field_index)
        elif prepend:
            result = ctx.eln.prepend_content(doc_id, prepend, field_index=field_index)
        else:
            if content:
                field_entry: dict = {"content": content}
                if field_id is not None:
                    field_entry["id"] = field_id
                fields = [field_entry]
            else:
                fields = None
            result = ctx.eln.update_document(document_id=doc_id, name=name, tags=tag, fields=fields)
    except Exception as e:
        handle_api_error(e)

    console.print(f"[green]Updated document[/green] {result.get('globalId')}")
    print_single(result, ctx.output, columns)


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------


def _update_inv_item(
    endpoint: str,
    id: str,
    name: Optional[str],
    description: Optional[str],
    tag: Optional[str],
    extra_body: Optional[dict] = None,
) -> Optional[dict]:
    """PUT name/description/tags/extra fields to /<endpoint>/<id> in one request.

    Existing tags are preserved unless --tag is given (see put_inventory_item).
    Returns the API response, or None when nothing was sent.
    """
    ctx = get_context()
    body: dict = dict(extra_body or {})
    if name:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if tag is not None:
        body["tags"] = parse_tags(tag, InvTag)
    if not body:
        return None
    try:
        return put_inventory_item(ctx.inv, endpoint, parse_id(id), body)
    except Exception as e:
        handle_api_error(e)


@app.command("sample")
def update_sample(
    id: str = typer.Argument(..., help="Sample ID or GlobalID."),
    name: Optional[str] = typer.Option(None, "--name", "-n"),
    description: Optional[str] = typer.Option(None, "--description", "-d"),
    tag: Optional[str] = typer.Option(None, "--tag", help="Replace tags (comma-separated)."),
) -> None:
    """Update an inventory sample's name, description, or tags."""
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_35,
        ColumnDef("description", "Description", 40),
        COL_MODIFIED,
    ]

    if not name and not description and not tag:
        typer.echo(
            "Nothing to update. Provide at least one of --name, --description, --tag.", err=True
        )
        raise typer.Exit(1)

    result = _update_inv_item("samples", id, name, description, tag)

    console.print(f"[green]Updated sample[/green] {result.get('globalId')}")
    print_single(result, ctx.output, columns)


# ---------------------------------------------------------------------------
# instrument
# ---------------------------------------------------------------------------


def _field_updates(current_fields: list, field_args: List[str]) -> list:
    """Map NAME=VALUE pairs onto the instrument's existing fields (by name) for a PUT."""
    by_name = {f.get("name"): f for f in current_fields}
    updates = []
    for arg in field_args:
        if "=" not in arg:
            err_console.print(f"[red]Invalid --field format (expected NAME=VALUE):[/red] {arg}")
            raise typer.Exit(1)
        fname, _, value = (s.strip() for s in arg.partition("="))
        current = by_name.get(fname)
        if current is None:
            err_console.print(
                f"[red]Unknown field '{fname}'.[/red] Known: {', '.join(sorted(by_name)) or '(none)'}"
            )
            raise typer.Exit(1)
        ftype = (current.get("type") or "").lower()
        if ftype == "choice":
            updates.append({"id": current["id"], "selectedOptions": [v.strip() for v in value.split(",")]})
        elif ftype == "radio":
            updates.append({"id": current["id"], "selectedOptions": [value]})
        else:
            updates.append({"id": current["id"], "content": value})
    return updates


@app.command("instrument")
def update_instrument(
    id: str = typer.Argument(..., help="Instrument ID or GlobalID."),
    name: Optional[str] = typer.Option(None, "--name", "-n"),
    description: Optional[str] = typer.Option(None, "--description", "-d"),
    tag: Optional[str] = typer.Option(None, "--tag", help="Replace tags (comma-separated)."),
    field: Optional[List[str]] = typer.Option(
        None, "--field", help="Set a template field as NAME=VALUE. Repeatable."
    ),
    latest_template: bool = typer.Option(
        False, "--latest-template", help="Apply the latest version of the instrument's template."
    ),
) -> None:
    """Update an inventory instrument's name, description, tags, or field values.

      rspace update instrument IN12 --field "Serial Number=SN-42" --tag "lab,confocal"

      rspace update instrument IN12 --latest-template
    """
    ctx = get_context()
    columns = [
        COL_GLOBAL_ID,
        COL_NAME_35,
        ColumnDef("description", "Description", 40),
        ColumnDef("templateVersion", "Template Ver.", 13),
        COL_MODIFIED,
    ]

    if not any([name, description, tag, field, latest_template]):
        typer.echo(
            "Nothing to update. Provide at least one of --name, --description, --tag, "
            "--field, --latest-template.",
            err=True,
        )
        raise typer.Exit(1)

    result = None
    if latest_template:
        try:
            result = ctx.inv.update_instrument_to_latest_template_version(parse_id(id))
        except Exception as e:
            handle_api_error(e)

    extra_body = None
    if field:
        try:
            current = ctx.inv.get_instrument_by_id(parse_id(id))
        except Exception as e:
            handle_api_error(e)
            return  # handle_api_error always exits, but be explicit
        extra_body = {"fields": _field_updates(current.get("fields") or [], field)}

    result = _update_inv_item("instruments", id, name, description, tag, extra_body) or result

    console.print(f"[green]Updated instrument[/green] {result.get('globalId')}")
    print_single(result, ctx.output, columns)


@app.command("instrument-template")
def update_instrument_template(
    id: str = typer.Argument(..., help="Instrument template ID or GlobalID."),
    propagate: bool = typer.Option(
        False,
        "--propagate",
        help="Update your instruments created from this template to its latest version.",
    ),
) -> None:
    """Apply an instrument template's latest version to the instruments made from it."""
    ctx = get_context()
    if not propagate:
        typer.echo("Nothing to update. Use --propagate.", err=True)
        raise typer.Exit(1)
    try:
        result = ctx.inv.update_instrument_template_instruments(parse_id(id))
    except Exception as e:
        handle_api_error(e)
        return  # handle_api_error always exits, but be explicit

    results = (result or {}).get("results") or []
    errors = [r for r in results if r.get("error")]
    console.print(
        f"[green]Updated {len(results) - len(errors)} instrument(s)[/green] from template {id}."
    )
    if errors:
        err_console.print(f"[yellow]{len(errors)} error(s):[/yellow]")
        for r in errors:
            err_console.print(f"  {r.get('error')}")
        raise typer.Exit(1)
