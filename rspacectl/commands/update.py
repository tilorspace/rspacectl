"""rspace update <noun> <id> — modify existing resources."""

from typing import Optional

import typer
from rspace_client.inv.inv import Tag as InvTag

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id
from ..output import COL_GLOBAL_ID, COL_MODIFIED, COL_NAME_35, COL_NAME_40, ColumnDef, console, print_single
from ..utils import parse_tags

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------

@app.command("document")
def update_document(
    id: str = typer.Argument(..., help="Document ID or GlobalID."),
    name: Optional[str] = typer.Option(None, "--name", "-n"),
    tag: Optional[str] = typer.Option(None, "--tag", help="Replace tags (comma-separated)."),
    content: Optional[str] = typer.Option(None, "--content", help="Replace first field content (HTML)."),
    append: Optional[str] = typer.Option(None, "--append", help="Append HTML content to first field."),
    prepend: Optional[str] = typer.Option(None, "--prepend", help="Prepend HTML content to first field."),
) -> None:
    """Update an ELN document's name, tags, or content."""
    ctx = get_context()
    columns = [COL_GLOBAL_ID, COL_NAME_40, ColumnDef("tags", "Tags", 30), COL_MODIFIED]
    doc_id = parse_id(id)
    try:
        if append:
            result = ctx.eln.append_content(doc_id, append)
        elif prepend:
            result = ctx.eln.prepend_content(doc_id, prepend)
        else:
            fields = [{"content": content}] if content else None
            result = ctx.eln.update_document(document_id=doc_id, name=name, tags=tag, fields=fields)
    except Exception as e:
        handle_api_error(e)

    console.print(f"[green]Updated document[/green] {result.get('globalId')}")
    print_single(result, ctx.output, columns)


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------

@app.command("sample")
def update_sample(
    id: str = typer.Argument(..., help="Sample ID or GlobalID."),
    name: Optional[str] = typer.Option(None, "--name", "-n"),
    description: Optional[str] = typer.Option(None, "--description", "-d"),
    tag: Optional[str] = typer.Option(None, "--tag", help="Replace tags (comma-separated)."),
) -> None:
    """Update an inventory sample's name, description, or tags."""
    ctx = get_context()
    columns = [COL_GLOBAL_ID, COL_NAME_35, ColumnDef("description", "Description", 40), COL_MODIFIED]

    if not name and not description and not tag:
        typer.echo("Nothing to update. Provide at least one of --name, --description, --tag.", err=True)
        raise typer.Exit(1)

    try:
        result = None
        if name:
            # inv.rename accepts either numeric id or globalId string
            result = ctx.inv.rename(id, name)
        if description is not None or tag is not None:
            patch_body: dict = {}
            if description is not None:
                patch_body["description"] = description
            if tag is not None:
                patch_body["tags"] = parse_tags(tag, InvTag)
            result = ctx.inv.retrieve_api_results(
                f"/samples/{parse_id(id)}",
                request_type="PUT",
                params=patch_body,
            )
    except Exception as e:
        handle_api_error(e)

    console.print(f"[green]Updated sample[/green] {result.get('globalId')}")
    print_single(result, ctx.output, columns)
