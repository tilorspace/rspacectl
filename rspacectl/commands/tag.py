"""rspace tag <id> <tags> — set tags on any RSpace resource."""

import typer
from rspace_client.inv.inv import Tag as InvTag

from ..context import get_context
from ..exceptions import handle_api_error
from ..ids import parse_id, resource_type
from ..output import console
from ..utils import parse_tags

# ELN types that tag via update_document (documents and notebooks share the same endpoint)
_ELN_TYPES = {"document", "notebook"}

# Inventory type → API endpoint segment
_INV_ENDPOINTS: dict = {
    "sample": "samples",
    "subsample": "subSamples",
    "container": "containers",
    "template": "sampleTemplates",
}


def tag(
    id: str = typer.Argument(..., help="GlobalID or numeric ID (e.g. SD123, SA456)."),
    tags: str = typer.Argument(..., help="Comma-separated tags to set, replacing existing tags."),
) -> None:
    """Set tags on a document, notebook, sample, subsample, container, or template.

    Replaces all existing tags on the resource. Pass a GlobalID so the resource
    type is inferred automatically:

      rspace tag SD123 "lab,experiment"
      rspace tag NB456 "methods,protocols"
      rspace tag SA789 "reagent,validated"
      rspace tag IC101 "freezer,-80C"
    """
    ctx = get_context()
    rtype = resource_type(id)
    numeric_id = parse_id(id)

    try:
        if rtype in _ELN_TYPES:
            result = ctx.eln.update_document(document_id=numeric_id, tags=tags)
            console.print(
                f"[green]Tagged {rtype}[/green] {result.get('globalId')}: "
                f"{result.get('tags', '')}"
            )
        elif rtype in _INV_ENDPOINTS:
            endpoint = _INV_ENDPOINTS[rtype]
            tag_objects = parse_tags(tags, InvTag)
            result = ctx.inv.retrieve_api_results(
                f"/{endpoint}/{numeric_id}",
                request_type="PUT",
                params={"tags": tag_objects},
            )
            tag_vals = ", ".join(t.get("value", "") for t in (result.get("tags") or []))
            console.print(
                f"[green]Tagged {rtype}[/green] {result.get('globalId')}: {tag_vals}"
            )
        else:
            typer.echo(
                f"Cannot tag resource type '{rtype}' (ID: {id}). "
                "Use a GlobalID: SD (document), NB (notebook), SA (sample), "
                "SS (subsample), IC (container), IT (template).",
                err=True,
            )
            raise typer.Exit(1)
    except Exception as e:
        handle_api_error(e)
