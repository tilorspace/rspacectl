"""rspace migrate — export/import complete inventory snapshots for server-to-server migration.

Workflow
--------
Export (source server):

    rspace migrate export --all --output my_snapshot/
    rspace migrate export --template IT1 --sample SA2 --output partial/

Import (target server, different --profile):

    rspace --profile target migrate import my_snapshot/
    rspace --profile target migrate import my_snapshot/ --dry-run
    rspace --profile target migrate import my_snapshot/ --checkpoint my_snapshot/checkpoint.json

    # Backward-compatible: plain JSON snapshot (no attachments)
    rspace --profile target migrate import snapshot.json

Snapshot folder layout
-----------------------
    snapshot_dir/
      snapshot.json           — inventory data (templates, containers, samples)
      attachments/
        SA123/IF456_report.pdf
        SS234/IF111_cert.pdf
        IC345/IF222_label.pdf
        IT12/IF333_data.csv
      images/
        SA123_preview.png     — sample / subsample / container preview image
      icons/
        IT12_icon.png         — template icon (iconId)
      image_containers/
        IC678_background.png  — IMAGE container background image
        IC678_locations.json  — [{coordX, coordY}] marker positions

Import algorithm (eight phases)
--------------------------------
1. Templates          — create flat; record old→new globalId + per-field id mapping
2. Containers (flat)  — create all containers at top level; record old→new globalId.
                        IMAGE containers are recreated with their background image
                        and marker locations when those are present in the snapshot.
3. Container hierarchy— move containers into their parents, shallowest depth first
4. Samples            — create from mapped template; restore field values + subsample metadata
5. Subsample placements — move each subsample into its recorded container
6. Attachments        — re-upload files to their new owner globalIds
7. Preview images     — set_image for templates, samples, subsamples, containers
8. Template icons     — set_sample_template_icon for templates

A checkpoint file is written after each phase so an interrupted import can
resume with --checkpoint without duplicating already-created resources.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import typer
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from rspace_client.inv.inv import Pagination

from ..context import get_context
from ..exceptions import warn
from ..ids import parse_id
from ..output import console, err_console

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")

SCHEMA_VERSION = 2
_CHECKPOINT_SUFFIX = ".checkpoint"
_SNAPSHOT_JSON = "snapshot.json"

# Keys that are server-generated and must be stripped before re-posting
_RESOURCE_SERVER_KEYS: frozenset = frozenset(
    {
        "id",
        "globalId",
        "created",
        "lastModified",
        "createdBy",
        "modifiedBy",
        "owner",
        "version",
        "historicalVersion",
        "links",
        "iconId",
        "permittedActions",
        "sharedWith",
    }
)
_FIELD_SERVER_KEYS: frozenset = frozenset({"id", "globalId", "created", "lastModified", "links"})
_ATTACHMENT_FIELD_TYPES: frozenset = frozenset({"attachment", "file"})


# ---------------------------------------------------------------------------
# Snapshot path helpers
# ---------------------------------------------------------------------------


def _snapshot_dir(output: Path) -> Path:
    """Return the snapshot directory given --output (always a directory)."""
    return output


def _snapshot_json(snapshot_dir: Path) -> Path:
    return snapshot_dir / _SNAPSHOT_JSON


def _resolve_input(input_path: Path) -> Tuple[Path, Path]:
    """Return (snapshot_dir, snapshot_json) from either a directory or a .json file."""
    if input_path.is_dir():
        return input_path, input_path / _SNAPSHOT_JSON
    # Legacy: bare .json file
    return input_path.parent, input_path


def _attachments_dir(snapshot_dir: Path) -> Path:
    return snapshot_dir / "attachments"


def _images_dir(snapshot_dir: Path) -> Path:
    return snapshot_dir / "images"


def _icons_dir(snapshot_dir: Path) -> Path:
    return snapshot_dir / "icons"


def _image_containers_dir(snapshot_dir: Path) -> Path:
    return snapshot_dir / "image_containers"


# ---------------------------------------------------------------------------
# Import state / checkpoint
# ---------------------------------------------------------------------------


@dataclass
class _ImportState:
    """Accumulated id mappings and progress, persisted as the checkpoint file."""

    id_map: Dict[str, str] = field(default_factory=dict)
    """old globalId → new globalId for every successfully created resource."""

    numeric_map: Dict[int, int] = field(default_factory=dict)
    """old numeric id → new numeric id (useful for SDK calls that need ints)."""

    completed_phases: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "id_map": self.id_map,
            "numeric_map": {str(k): v for k, v in self.numeric_map.items()},
            "completed_phases": self.completed_phases,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "_ImportState":
        s = cls()
        s.id_map = d.get("id_map", {})
        s.numeric_map = {int(k): v for k, v in d.get("numeric_map", {}).items()}
        s.completed_phases = d.get("completed_phases", [])
        s.errors = d.get("errors", [])
        return s


def _save_checkpoint(path: Path, state: _ImportState) -> None:
    path.write_text(json.dumps(state.to_dict(), indent=2))
    err_console.print(f"[dim]Checkpoint saved → {path}[/dim]")


def _load_checkpoint(path: Path) -> _ImportState:
    return _ImportState.from_dict(json.loads(path.read_text()))


def _record_error(state: _ImportState, message: str) -> None:
    """Warn the user and append the message to the import state's error list."""
    warn(message)
    state.errors.append(message)


def _resolve_new_gid(
    old_gid: str, state: _ImportState, kind: str
) -> Optional[str]:
    """Return the new globalId for an old one, or None if unmapped.

    Records a state error on miss.  In dry-run mode the caller normally
    populates ``state.id_map`` with identity mappings during earlier phases,
    so a miss here means the user skipped a prerequisite phase.
    """
    new_gid = state.id_map.get(old_gid)
    if not new_gid:
        _record_error(state, f"{kind} for {old_gid}: not in id_map — skipped")
        return None
    return new_gid


# Module-level export warning collector.  Reset at the start of each export
# so the final summary can tally any download failures.  Module-level state is
# acceptable here because each CLI invocation runs a single command in its own
# process; the helpers don't otherwise share state across calls.
_export_warnings: List[str] = []


def _export_warn(message: str) -> None:
    """Warn the user and record the message for the export summary."""
    warn(message)
    _export_warnings.append(message)


def _progress() -> Progress:
    """Build a Progress instance configured for this command's use.

    Renders to stderr (so progress doesn't pollute piped stdout) with a
    description, bar, item count, and elapsed time.  Use as a context manager
    around a phase's work loop.
    """
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err_console,
        transient=False,  # leave the final bar visible after completion
    )


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------


def _paginate(fetch_fn, data_key: str, page_size: int = 50, **kwargs) -> List[Dict]:
    """Exhaust all pages of a paginated inventory endpoint and return all records."""
    results: List[Dict] = []
    page = 0
    while True:
        pagination = Pagination(page_number=page, page_size=page_size)
        resp = fetch_fn(pagination=pagination, **kwargs)
        batch = resp.get(data_key, [])
        results.extend(batch)
        total = resp.get("totalHits", len(results))
        if len(results) >= total or not batch:
            break
        page += 1
    return results


# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------


def _strip(d: Dict, keys: frozenset) -> Dict:
    return {k: v for k, v in d.items() if k not in keys}


def _sanitise_template(tmpl: Dict) -> Dict:
    """Return a POST-ready template payload stripped of server-generated keys."""
    clean = _strip(tmpl, _RESOURCE_SERVER_KEYS)
    clean["fields"] = [_strip(f, _FIELD_SERVER_KEYS) for f in tmpl.get("fields", [])]
    return clean


# ---------------------------------------------------------------------------
# Link helpers
# ---------------------------------------------------------------------------


def _find_link(item: Dict, rel: str) -> Optional[str]:
    """Return the URL for the first link whose rel matches, or None.

    The RSpace inventory API uses ``_links`` (not ``links``), and the URL
    field is ``"link"`` (not ``"href"``).  We check both field names for
    resilience across API versions.
    """
    for key in ("_links", "links"):
        for link in item.get(key) or []:
            if link.get("rel") == rel:
                return link.get("link") or link.get("href")
    return None


# ---------------------------------------------------------------------------
# Export — file download helpers
# ---------------------------------------------------------------------------


def _export_attachments(inv, item: Dict, item_dir: Path) -> List[Dict]:
    """Download all attachments for one inventory item into item_dir.

    Returns a list of metadata dicts that will be stored in _migration so the
    importer knows which files to re-upload.
    """
    attachments = item.get("attachments") or item.get("storedFiles") or []
    if not attachments:
        return []
    item_dir.mkdir(parents=True, exist_ok=True)
    meta = []
    for att in attachments:
        att_id = att.get("id") or parse_id(att.get("globalId", ""))
        if not att_id:
            continue
        filename = att.get("name") or f"attachment_{att_id}"
        local_name = f"{att.get('globalId', att_id)}_{filename}"
        dest = item_dir / local_name
        try:
            inv.download_attachment_by_id(att_id, str(dest))
            meta.append({"globalId": att.get("globalId"), "filename": filename, "local": local_name})
        except Exception as exc:
            _export_warn(f"Could not download attachment {att.get('globalId')} for {item.get('globalId')}: {exc}")
    return meta


def _export_attachment_extra_fields(inv, item: Dict, item_dir: Path) -> List[Dict]:
    """Download files referenced by attachment-type extraFields.

    Returns a list of {field_name, local} metadata entries.
    """
    meta = []
    for ef in item.get("extraFields") or []:
        if ef.get("type") not in _ATTACHMENT_FIELD_TYPES:
            continue
        content = ef.get("content")
        if not content:
            continue
        # content may be a globalId (IF…) or a numeric id or a URL — try to parse
        field_name = ef.get("name") or "field"
        att_id = None
        if isinstance(content, str) and content.startswith("IF"):
            att_id = parse_id(content)
            att_gid = content
        elif isinstance(content, int):
            att_id = content
            att_gid = f"IF{content}"
        else:
            _export_warn(
                f"extraField {field_name!r} on {item.get('globalId')}: "
                f"unrecognised attachment content {content!r} — skipped."
            )
            continue
        item_dir.mkdir(parents=True, exist_ok=True)
        local_name = f"ef_{att_gid}_{field_name}"
        dest = item_dir / local_name
        try:
            inv.download_attachment_by_id(att_id, str(dest))
            meta.append({"field_name": field_name, "globalId": att_gid, "local": local_name})
        except Exception as exc:
            _export_warn(
                f"Could not download extraField attachment {att_gid} "
                f"({field_name!r}) for {item.get('globalId')}: {exc}"
            )
    return meta


def _export_preview_image(inv, item: Dict, images_dir: Path) -> Optional[str]:
    """Download the preview image for an inventory item (SA/SS/IC/IT).

    The image link appears in the item's ``links`` array only when a preview
    image has been set *and* the API includes it in GET responses.  In practice
    the link is sometimes absent even when an image exists, so we fall back to
    probing candidate URLs derived from the globalId.

    A 404 means no image is set; any other error is warned.
    Returns the local filename relative to images_dir, or None.
    """
    import requests as _requests  # noqa: PLC0415 — lazy import to avoid hard dep at module load

    gid = item.get("globalId", "")
    if not gid:
        return None

    # The image URL is a hash-based path (/files/image/{hash}) that cannot be
    # constructed from the globalId — we rely entirely on the _links array.
    # Prefer "image" (full resolution); fall back to "thumbnail".
    url = _find_link(item, "image") or _find_link(item, "thumbnail")
    if not url:
        return None

    local_name = f"{gid}_preview.png"
    dest = images_dir / local_name
    headers = {"apiKey": inv.api_key, "Accept": "application/octet-stream"}

    try:
        resp = _requests.get(url, headers=headers, stream=True)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        if "json" in resp.headers.get("Content-Type", ""):
            return None
        images_dir.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=128):
                fh.write(chunk)
        return local_name
    except Exception as exc:
        _export_warn(f"Could not download preview image for {gid}: {exc}")
        return None


def _export_template_icon(inv, tmpl: Dict, icons_dir: Path) -> Optional[str]:
    """Download a template's icon (iconId). Returns local filename or None."""
    icon_id = tmpl.get("iconId")
    tmpl_id = tmpl.get("id")
    if not icon_id or not tmpl_id:
        return None
    gid = tmpl.get("globalId", f"IT{tmpl_id}")
    local_name = f"{gid}_icon"
    dest = icons_dir / local_name
    icons_dir.mkdir(parents=True, exist_ok=True)
    try:
        inv.get_sample_template_icon(tmpl_id, icon_id, str(dest))
        return local_name
    except Exception as exc:
        _export_warn(f"Could not download icon for template {gid}: {exc}")
        return None


def _export_image_container(inv, container: Dict, ic_dir: Path) -> Dict:
    """Download the background image and record marker locations for an IMAGE container.

    Returns a dict {background_local, locations} to store in _migration, or {}.
    """
    if container.get("cType") != "IMAGE":
        return {}
    gid = container.get("globalId", "unknown")
    result: Dict[str, Any] = {}

    # Background image — the GET response includes a link with rel=locationsImage
    # (or rel=image) pointing to the background PNG.
    for rel in ("locationsImage", "image"):
        href = _find_link(container, rel)
        if href:
            ic_dir.mkdir(parents=True, exist_ok=True)
            local_name = f"{gid}_background.png"
            dest = ic_dir / local_name
            try:
                inv.download_link_to_file(href, str(dest))
                result["background_local"] = local_name
            except Exception as exc:
                _export_warn(f"Could not download background image for IMAGE container {gid}: {exc}")
            break

    if "background_local" not in result:
        _export_warn(
            f"IMAGE container {gid} ({container.get('name')!r}): "
            "no background image link found in API response — background will not be migrated."
        )

    # Marker locations from the locations array
    locations = []
    for loc in container.get("locations") or []:
        x = loc.get("coordX")
        y = loc.get("coordY")
        if x is not None and y is not None:
            locations.append({"coordX": x, "coordY": y})
    if locations:
        ic_dir.mkdir(parents=True, exist_ok=True)
        loc_file = ic_dir / f"{gid}_locations.json"
        loc_file.write_text(json.dumps(locations))
        result["locations_local"] = f"{gid}_locations.json"
        result["locations"] = locations

    return result


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def _export_templates(inv, ids: Optional[List[int]] = None) -> List[Dict]:
    """Fetch full template details for all (or selected) templates."""
    if ids:
        templates = []
        for tid in ids:
            err_console.print(f"  Fetching template {tid}…")
            templates.append(inv.get_sample_template_by_id(tid))
        return templates
    err_console.print("  Listing all templates (paginated)…")
    stubs = _paginate(inv.list_sample_templates, "templates")
    templates = []
    for stub in stubs:
        templates.append(inv.get_sample_template_by_id(stub["id"]))
    return templates


def _walk_container(
    inv,
    stub: Dict,
    depth: int,
    parent_global_id: Optional[str],
    parent_grid_col: Optional[int] = None,
    parent_grid_row: Optional[int] = None,
    found_sample_ids: Optional[Set[int]] = None,
) -> List[Dict]:
    """Recursively fetch a container and all its child containers.

    Each record is annotated with ``_migration`` metadata so the importer can
    recreate the hierarchy without making additional API calls.

    ``parent_grid_col``/``parent_grid_row`` record where this container sits
    inside its parent if the parent is a GRID container; both are ``None`` for
    LIST parents.

    If ``found_sample_ids`` is a set, parent sample IDs of any subsamples
    encountered in the location contents will be added to it so the caller
    can auto-include those samples in the export.
    """
    container_id = stub.get("id") or parse_id(stub.get("globalId", ""))
    full = inv.get_container_by_id(container_id, include_content=True)
    full["_migration"] = {
        "depth": depth,
        "parent_global_id": parent_global_id,
        "parent_grid_col": parent_grid_col,
        "parent_grid_row": parent_grid_row,
    }
    collected = [full]

    # Sanity-check: warn if the API returned fewer locations than contentSummary
    # reports. This would indicate silent truncation (e.g. future API pagination).
    content_summary = full.get("contentSummary") or {}
    reported_total = content_summary.get("totalCount")
    locations_returned = len(full.get("locations") or [])
    if reported_total is not None and locations_returned < reported_total:
        warn(
            f"Container {full.get('globalId')} ({full.get('name')!r}): "
            f"contentSummary reports {reported_total} item(s) but only "
            f"{locations_returned} location(s) were returned — some items may "
            "be missing from the export."
        )

    # The API returns child items in locations[*].content (coordX/Y give grid pos).
    # storedContainers / content.content are kept as fallbacks for older API versions.
    children_with_pos: List[Tuple[Dict, Optional[int], Optional[int]]] = []

    for loc in full.get("locations", []):
        item = loc.get("content") or {}
        gid = item.get("globalId", "")
        if item.get("type") == "CONTAINER" or gid.startswith("IC"):
            children_with_pos.append((item, loc.get("coordX"), loc.get("coordY")))
        elif gid.startswith("SS") and found_sample_ids is not None:
            # Subsample stub — record the parent sample ID for auto-inclusion
            sample_ref = item.get("sample") or {}
            parent_sample_id = sample_ref.get("id")
            if parent_sample_id:
                found_sample_ids.add(parent_sample_id)

    if not children_with_pos:
        # storedContainers / content.content are legacy fallback fields from older
        # API versions. Items here may lack a numeric 'id' and carry no grid
        # position; we synthesise id from globalId if needed.
        for item in (full.get("storedContainers") or []):
            if not item.get("id") and item.get("globalId"):
                item = dict(item, id=parse_id(item["globalId"]))
            children_with_pos.append((item, None, None))

    if not children_with_pos:
        for item in (full.get("content") or {}).get("content", []):
            if item.get("globalId", "").startswith("IC"):
                if not item.get("id") and item.get("globalId"):
                    item = dict(item, id=parse_id(item["globalId"]))
                children_with_pos.append((item, None, None))

    # Only pass grid coords when THIS container is a GRID — for LIST containers
    # the coordX/Y values are sequential slot IDs, not meaningful grid positions.
    is_grid = full.get("cType") == "GRID"
    for child, col, row in children_with_pos:
        collected.extend(
            _walk_container(
                inv, child, depth + 1, full["globalId"],
                parent_grid_col=col if is_grid else None,
                parent_grid_row=row if is_grid else None,
                found_sample_ids=found_sample_ids,
            )
        )

    return collected


def _export_containers(
    inv, ids: Optional[List[int]] = None, warn_attachments: bool = False
) -> Tuple[List[Dict], Set[int]]:
    """Walk the container tree from all (or selected) roots.

    Returns ``(containers, found_sample_ids)`` where ``found_sample_ids`` is
    the set of numeric sample IDs whose subsamples were found inside the walked
    containers.  When ``ids`` is ``None`` (export-all) samples are handled
    separately, so the set will be empty.
    """
    if ids:
        roots = [inv.get_container_by_id(cid) for cid in ids]
    else:
        err_console.print("  Listing all top-level containers (paginated)…")
        roots = _paginate(inv.list_top_level_containers, "containers")

    containers: List[Dict] = []
    found_sample_ids: Set[int] = set()
    container_attachment_count = 0
    # Only auto-collect sample IDs when exporting specific containers
    collect_samples = ids is not None
    for root in roots:
        err_console.print(f"  Walking tree from {root.get('globalId')} ({root.get('name')})…")
        containers.extend(
            _walk_container(
                inv, root, depth=0, parent_global_id=None,
                found_sample_ids=found_sample_ids if collect_samples else None,
            )
        )

    for c in containers:
        has_attachment = (
            bool(c.get("attachments") or c.get("storedFiles"))
            or any(
                ef.get("type") in _ATTACHMENT_FIELD_TYPES
                for ef in (c.get("extraFields") or [])
            )
        )
        if has_attachment:
            container_attachment_count += 1

    if warn_attachments and container_attachment_count:
        warn(
            f"{container_attachment_count} container(s) have file attachments — "
            "attachments are not included (--no-files was set). Re-attach files manually after import."
        )

    return containers, found_sample_ids


def _collect_subsample_locations(sample: Dict) -> List[Dict]:
    """Extract where each subsample sits (container + optional grid position).

    The RSpace API returns the grid position in the subsample's own
    ``parentLocation`` field (``{"coordX": col, "coordY": row}``), not inside
    the ``parentContainers`` entry.  We record ``grid_col`` / ``grid_row`` only
    when the immediate parent is a GRID container — for LIST parents the coords
    are sequential slot IDs that carry no meaningful position.
    """
    locations = []
    for ss in sample.get("subSamples", []):
        parents = ss.get("parentContainers") or []
        if not parents:
            continue
        parent = parents[0]  # a subsample lives in exactly one container at a time
        is_grid = parent.get("cType") == "GRID"
        parent_loc = ss.get("parentLocation") or {}
        locations.append(
            {
                "subsample_global_id": ss["globalId"],
                "container_global_id": parent.get("globalId"),
                "grid_col": parent_loc.get("coordX") if is_grid else None,
                "grid_row": parent_loc.get("coordY") if is_grid else None,
            }
        )
    return locations


def _export_samples(
    inv,
    ids: Optional[List[int]] = None,
    warn_attachments: bool = False,
) -> Tuple[List[Dict], Set[str]]:
    """Fetch full sample details including embedded subsamples.

    Returns ``(samples, referenced_template_global_ids)`` so the caller can
    pull in any templates not already in the export.
    """
    if ids:
        stubs = [{"id": sid} for sid in ids]
    else:
        err_console.print("  Listing all samples (paginated)…")
        stubs = _paginate(inv.list_samples, "samples")

    samples: List[Dict] = []
    ref_template_gids: Set[str] = set()
    attachment_count = 0

    for stub in stubs:
        full = inv.get_sample_by_id(stub["id"])

        # Collect referenced template global ID for auto-inclusion.
        # The API may return the template reference as sampleTemplate.globalId,
        # templateGlobalId, or just a numeric templateId — handle all three.
        tmpl_ref = full.get("sampleTemplate") or {}
        tmpl_gid = (
            tmpl_ref.get("globalId")
            or full.get("templateGlobalId")
        )
        if not tmpl_gid and full.get("templateId"):
            tmpl_gid = f"IT{full['templateId']}"
        if tmpl_gid:
            ref_template_gids.add(tmpl_gid)

        # Capture subsample container placements
        full["_migration"] = {"subsample_locations": _collect_subsample_locations(full)}

        # Warn about attachments and attachment-type extraFields (not migrated in this version)
        for ss in full.get("subSamples", []):
            has_attachment = bool(ss.get("attachments") or ss.get("storedFiles")) or any(
                ef.get("type") in _ATTACHMENT_FIELD_TYPES
                for ef in (ss.get("extraFields") or [])
            )
            if has_attachment:
                attachment_count += 1
        has_sample_attachment = bool(full.get("attachments") or full.get("storedFiles")) or any(
            ef.get("type") in _ATTACHMENT_FIELD_TYPES
            for ef in (full.get("extraFields") or [])
        )
        if has_sample_attachment:
            attachment_count += 1

        samples.append(full)

    if warn_attachments and attachment_count:
        warn(
            f"{attachment_count} sample/subsample item(s) have file attachments — "
            "attachments are not included (--no-files was set). Re-attach files manually after import."
        )

    return samples, ref_template_gids


# ---------------------------------------------------------------------------
# Export — files phase (runs after data export, modifies _migration in-place)
# ---------------------------------------------------------------------------


def _export_files(
    inv,
    templates: List[Dict],
    containers: List[Dict],
    samples: List[Dict],
    snapshot_dir: Path,
) -> None:
    """Download all attachments, preview images, icons, and IMAGE container backgrounds.

    Mutates the ``_migration`` dict on each item in-place so the paths are
    serialised into snapshot.json alongside the inventory data.
    """
    att_dir = _attachments_dir(snapshot_dir)
    img_dir = _images_dir(snapshot_dir)
    ico_dir = _icons_dir(snapshot_dir)
    ic_dir = _image_containers_dir(snapshot_dir)

    err_console.print("\n[bold]Exporting files…[/bold]")

    # Total = templates + containers + samples + every subsample.  Used to
    # drive the progress bar so users see how far through file downloads we are.
    n_subsamples = sum(len(s.get("subSamples", [])) for s in samples)
    total_items = len(templates) + len(containers) + len(samples) + n_subsamples

    with _progress() as prog:
        task = prog.add_task("[cyan]Downloading files", total=total_items)

        # --- Templates: attachments + preview image + icon ---
        for tmpl in templates:
            gid = tmpl.get("globalId", "")
            mig = tmpl.setdefault("_migration", {})

            att_meta = _export_attachments(inv, tmpl, att_dir / gid)
            ef_meta = _export_attachment_extra_fields(inv, tmpl, att_dir / gid)
            if att_meta or ef_meta:
                mig["attachments"] = att_meta
                mig["attachment_extra_fields"] = ef_meta

            preview_local = _export_preview_image(inv, tmpl, img_dir)
            if preview_local:
                mig["preview_local"] = preview_local

            icon_local = _export_template_icon(inv, tmpl, ico_dir)
            if icon_local:
                mig["icon_local"] = icon_local
            prog.advance(task)

        # --- Containers: attachments + preview image + IMAGE background ---
        for c in containers:
            gid = c.get("globalId", "")
            mig = c.setdefault("_migration", {})

            att_meta = _export_attachments(inv, c, att_dir / gid)
            ef_meta = _export_attachment_extra_fields(inv, c, att_dir / gid)
            if att_meta or ef_meta:
                mig["attachments"] = att_meta
                mig["attachment_extra_fields"] = ef_meta

            preview_local = _export_preview_image(inv, c, img_dir)
            if preview_local:
                mig["preview_local"] = preview_local

            if c.get("cType") == "IMAGE":
                ic_meta = _export_image_container(inv, c, ic_dir)
                if ic_meta:
                    mig["image_container"] = ic_meta
            prog.advance(task)

        # --- Samples + subsamples: attachments + preview image ---
        for sample in samples:
            sa_gid = sample.get("globalId", "")
            mig = sample.setdefault("_migration", {})

            att_meta = _export_attachments(inv, sample, att_dir / sa_gid)
            ef_meta = _export_attachment_extra_fields(inv, sample, att_dir / sa_gid)
            if att_meta or ef_meta:
                mig["attachments"] = att_meta
                mig["attachment_extra_fields"] = ef_meta

            preview_local = _export_preview_image(inv, sample, img_dir)
            if preview_local:
                mig["preview_local"] = preview_local
            prog.advance(task)

            for ss in sample.get("subSamples", []):
                ss_gid = ss.get("globalId", "")
                ss_mig = ss.setdefault("_migration", {})

                att_meta = _export_attachments(inv, ss, att_dir / ss_gid)
                ef_meta = _export_attachment_extra_fields(inv, ss, att_dir / ss_gid)
                if att_meta or ef_meta:
                    ss_mig["attachments"] = att_meta
                    ss_mig["attachment_extra_fields"] = ef_meta

                preview_local = _export_preview_image(inv, ss, img_dir)
                if preview_local:
                    ss_mig["preview_local"] = preview_local
                prog.advance(task)


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------


def _import_templates(inv, templates: List[Dict], state: _ImportState, dry_run: bool) -> None:
    """Phase 1 — create templates and record id + field mappings."""
    err_console.print(f"\n[bold]Phase 1[/bold] — importing {len(templates)} template(s)…")
    for tmpl in templates:
        old_gid = tmpl["globalId"]
        old_fields = tmpl.get("fields", [])

        if dry_run:
            console.print(f"  [dim]DRY RUN[/dim]  would create template: {tmpl['name']!r}")
            # Use identity mapping so downstream dry-run phases can resolve IDs
            state.id_map[old_gid] = old_gid
            state.numeric_map[tmpl["id"]] = tmpl["id"]
            for old_f in old_fields:
                if old_f.get("globalId"):
                    state.id_map[old_f["globalId"]] = str(old_f["id"])
            continue

        try:
            payload = _sanitise_template(tmpl)
            new_tmpl = inv.create_sample_template(sample_template_post=payload)
            new_gid = new_tmpl["globalId"]
            state.id_map[old_gid] = new_gid
            state.numeric_map[tmpl["id"]] = new_tmpl["id"]

            # Map per-field IDs by position — field order is stable from the definition
            for old_f, new_f in zip(old_fields, new_tmpl.get("fields", [])):
                if old_f.get("globalId") and new_f.get("id"):
                    # Store old field globalId → new field numeric id
                    state.id_map[old_f["globalId"]] = str(new_f["id"])

            console.print(
                f"  [green]✓[/green]  [cyan]{old_gid}[/cyan] → [cyan]{new_gid}[/cyan]"
                f"  ({tmpl['name']})"
            )
        except Exception as exc:
            _record_error(state, f"Template {old_gid} ({tmpl.get('name')!r}): {exc}")


def _import_containers_flat(
    inv, containers: List[Dict], state: _ImportState, dry_run: bool, snapshot_dir: Path
) -> None:
    """Phase 2 — create all containers at top level (hierarchy restored in Phase 3).

    IMAGE containers are now recreated as IMAGE when a background image is present
    in the snapshot; otherwise they fall back to LIST with a warning.
    """
    err_console.print(
        f"\n[bold]Phase 2[/bold] — creating {len(containers)} container(s) (flat)…"
    )
    ic_dir = _image_containers_dir(snapshot_dir)

    for c in containers:
        old_gid = c["globalId"]

        if dry_run:
            console.print(
                f"  [dim]DRY RUN[/dim]  would create container: "
                f"{c['name']!r} ({c.get('cType', 'LIST')})"
            )
            # Use identity mapping so Phase 3 hierarchy restore can resolve IDs
            state.id_map[old_gid] = old_gid
            state.numeric_map[c["id"]] = c["id"]
            continue

        try:
            ctype = c.get("cType", "LIST")
            tags = c.get("tags") or []
            desc = c.get("description") or ""
            can_samples = c.get("canStoreSamples", True)
            can_containers = c.get("canStoreContainers", True)
            extra_fields = c.get("extraFields") or []

            if ctype == "GRID":
                layout = c.get("gridLayout") or {}
                new_c = inv.create_grid_container(
                    name=c["name"],
                    row_count=layout.get("rowsNumber", 1),
                    column_count=layout.get("columnsNumber", 1),
                    tags=tags,
                    description=desc,
                    extra_fields=extra_fields,
                    can_store_samples=can_samples,
                    can_store_containers=can_containers,
                )
            elif ctype == "IMAGE":
                ic_meta = (c.get("_migration") or {}).get("image_container") or {}
                bg_local = ic_meta.get("background_local")
                bg_path = ic_dir / bg_local if bg_local else None

                if bg_path and bg_path.exists():
                    from rspace_client.inv.inv import ImageContainerPost
                    locations = ic_meta.get("locations") or []
                    location_tuples = [(loc["coordX"], loc["coordY"]) for loc in locations]
                    post = ImageContainerPost(
                        name=c["name"],
                        image_file=str(bg_path),
                        locations=location_tuples,
                        tags=tags,
                        description=desc,
                        extra_fields=extra_fields,
                        can_store_containers=can_containers,
                        can_store_samples=can_samples,
                    )
                    new_c = inv.create_image_container(post)
                else:
                    warn(
                        f"Container {old_gid} ({c['name']!r}) is an IMAGE container but "
                        "no background image was found in the snapshot — "
                        "recreating as a LIST container."
                    )
                    new_c = inv.create_list_container(
                        name=c["name"],
                        tags=tags,
                        description=desc,
                        extra_fields=extra_fields,
                        can_store_samples=can_samples,
                        can_store_containers=can_containers,
                    )
            else:
                new_c = inv.create_list_container(
                    name=c["name"],
                    tags=tags,
                    description=desc,
                    extra_fields=extra_fields,
                    can_store_samples=can_samples,
                    can_store_containers=can_containers,
                )

            new_gid = new_c["globalId"]
            state.id_map[old_gid] = new_gid
            state.numeric_map[c["id"]] = new_c["id"]
            console.print(
                f"  [green]✓[/green]  [cyan]{old_gid}[/cyan] → [cyan]{new_gid}[/cyan]"
                f"  ({c['name']}, {ctype})"
            )
        except Exception as exc:
            _record_error(state, f"Container {old_gid} ({c.get('name')!r}): {exc}")


def _import_container_hierarchy(
    inv, containers: List[Dict], state: _ImportState, dry_run: bool
) -> None:
    """Phase 3 — move containers into their parents, shallowest depth first."""
    needs_move = [
        c
        for c in containers
        if c.get("_migration", {}).get("parent_global_id") and c["globalId"] in state.id_map
    ]
    if not needs_move:
        err_console.print("\n[bold]Phase 3[/bold] — no container nesting to restore.")
        return

    needs_move.sort(key=lambda c: c["_migration"]["depth"])
    err_console.print(
        f"\n[bold]Phase 3[/bold] — restoring hierarchy for {len(needs_move)} container(s)…"
    )

    for c in needs_move:
        old_gid = c["globalId"]
        old_parent_gid = c["_migration"]["parent_global_id"]
        new_gid = state.id_map.get(old_gid)
        new_parent_gid = state.id_map.get(old_parent_gid)

        if not new_gid or not new_parent_gid:
            _record_error(
                state,
                f"Container {old_gid}: parent {old_parent_gid} not in id_map — skipped",
            )
            continue

        if dry_run:
            migration = c.get("_migration", {})
            col = migration.get("parent_grid_col")
            row = migration.get("parent_grid_row")
            placement_note = f" at grid ({col},{row})" if col is not None and row is not None else ""
            console.print(
                f"  [dim]DRY RUN[/dim]  would nest [cyan]{new_gid}[/cyan]"
                f" → [cyan]{new_parent_gid}[/cyan]{placement_note}"
            )
            continue

        try:
            migration = c.get("_migration", {})
            col = migration.get("parent_grid_col")
            row = migration.get("parent_grid_row")
            # Items being moved require the globalId string (IC/SS prefix) so that
            # the SDK's Id.is_movable() check passes; target container accepts numeric.
            new_parent_id = parse_id(new_parent_gid)

            if col is not None and row is not None:
                from rspace_client.inv.inv import ByLocation, GridLocation

                # ByLocation(locations, *items_to_move) — locations list is first arg
                placement = ByLocation([GridLocation(x=col, y=row)], new_gid)
                inv.add_items_to_grid_container(
                    target_container_id=new_parent_id,
                    grid_placement=placement,
                )
            else:
                inv.add_items_to_list_container(new_parent_id, new_gid)

            console.print(
                f"  [green]✓[/green]  [cyan]{new_gid}[/cyan] → [cyan]{new_parent_gid}[/cyan]"
            )
        except Exception as exc:
            _record_error(
                state, f"Container hierarchy {old_gid} → {old_parent_gid}: {exc}"
            )


def _field_updates_by_name(old_fields: List[Dict], new_fields: List[Dict]) -> List[Dict]:
    """Match old field values to new field objects by field name.

    Returns a list of ``{"id": <new_numeric_id>, "content": <old_value>}`` dicts
    ready for the sample PUT payload.  Fields present in old but absent from new
    (name mismatch) are silently skipped.
    """
    new_by_name: Dict[str, Dict] = {f["name"]: f for f in new_fields}
    updates = []
    seen_names: Set[str] = set()
    for old_f in old_fields:
        name = old_f.get("name")
        if not name:
            continue
        if name in seen_names:
            warn(
                f"Duplicate field name {name!r} on template — "
                "only the first occurrence will be migrated."
            )
            continue
        seen_names.add(name)
        content = old_f.get("content") or old_f.get("value") or old_f.get("data")
        if content is None:
            continue
        new_f = new_by_name.get(name)
        if new_f is None:
            continue
        updates.append({"id": new_f["id"], "content": content})
    return updates


def _build_tmpl_field_name_map(templates: List[Dict]) -> Dict[str, Dict[str, str]]:
    """Return {old_tmpl_globalId: {field_name: old_field_globalId}} from the exported templates.

    Used in Phase 4 to pre-populate mandatory template fields at sample creation time,
    avoiding a mandatory-field validation failure when the two-step create→update approach
    would leave required fields empty during the initial POST.
    """
    result: Dict[str, Dict[str, str]] = {}
    for tmpl in templates:
        gid = tmpl.get("globalId")
        if gid:
            result[gid] = {f["name"]: f["globalId"] for f in tmpl.get("fields", []) if f.get("name") and f.get("globalId")}
    return result


def _creation_fields_from_state(
    old_fields: List[Dict],
    old_tmpl_gid: str,
    tmpl_field_name_map: Dict[str, Dict[str, str]],
    state: "_ImportState",
) -> Optional[List[Dict]]:
    """Build the ``fields`` list for ``create_sample`` using template field IDs.

    When a template has mandatory fields, ``create_sample`` must include non-empty
    values for those fields or the API rejects the request.  The RSpace API accepts
    template field IDs (not sample-instance field IDs) in the creation POST body.

    The API also requires that if ``fields`` is provided it must contain an entry for
    EVERY template field (not just the non-empty ones), so we iterate over all
    template fields and fill in values from the old sample where available.

    Returns ``None`` if the template has no fields mapped in ``state.id_map``.
    """
    name_to_old_gid = tmpl_field_name_map.get(old_tmpl_gid, {})
    if not name_to_old_gid:
        return None

    # Build name→content lookup from the old sample's field values
    old_by_name: Dict[str, Any] = {}
    for old_f in old_fields:
        name = old_f.get("name")
        if name:
            content = old_f.get("content") or old_f.get("value") or old_f.get("data")
            old_by_name[name] = content  # None if all fallbacks are falsy

    fields_list: List[Dict] = []
    for field_name, old_field_gid in name_to_old_gid.items():
        new_field_id_str = state.id_map.get(old_field_gid)
        if not new_field_id_str:
            continue
        try:
            entry: Dict[str, Any] = {"id": int(new_field_id_str)}
            content = old_by_name.get(field_name)
            if content is not None:
                entry["content"] = content
            fields_list.append(entry)
        except (ValueError, TypeError):
            continue

    return fields_list if fields_list else None


def _import_samples(
    inv,
    samples: List[Dict],
    state: "_ImportState",
    dry_run: bool,
    templates: Optional[List[Dict]] = None,
) -> None:
    """Phase 4 — create samples from templates; restore field values and subsample metadata."""
    err_console.print(f"\n[bold]Phase 4[/bold] — importing {len(samples)} sample(s)…")

    # Build name→old_field_globalId maps per template so we can pre-populate mandatory
    # fields at creation time (avoids API rejections for empty mandatory fields).
    tmpl_field_name_map = _build_tmpl_field_name_map(templates or [])

    for sample in samples:
        old_sa_gid = sample["globalId"]
        old_subsamples = sample.get("subSamples", [])
        old_fields = sample.get("fields", [])

        # Resolve template — handle sampleTemplate.globalId, templateGlobalId,
        # or bare numeric templateId (as produced by the current API).
        tmpl_ref = sample.get("sampleTemplate") or {}
        old_tmpl_gid = (
            tmpl_ref.get("globalId")
            or sample.get("templateGlobalId")
        )
        if not old_tmpl_gid and sample.get("templateId"):
            old_tmpl_gid = f"IT{sample['templateId']}"
        new_tmpl_id = None
        if old_tmpl_gid:
            new_tmpl_gid = state.id_map.get(old_tmpl_gid)
            if new_tmpl_gid:
                new_tmpl_id = parse_id(new_tmpl_gid)
            else:
                warn(f"Sample {old_sa_gid}: template {old_tmpl_gid} not in id_map — created without template.")

        if dry_run:
            console.print(
                f"  [dim]DRY RUN[/dim]  would create sample: {sample['name']!r}"
                f" ({len(old_subsamples)} subsample(s))"
            )
            # Use identity mapping so Phase 5 placements can resolve IDs
            state.id_map[old_sa_gid] = old_sa_gid
            state.numeric_map[sample["id"]] = sample["id"]
            for old_ss in old_subsamples:
                state.id_map[old_ss["globalId"]] = old_ss["globalId"]
                state.numeric_map[old_ss["id"]] = old_ss["id"]
            continue

        try:
            # Pre-populate template fields in the creation POST to satisfy mandatory
            # field validation (uses template field IDs, not sample-instance field IDs).
            creation_fields = None
            if old_tmpl_gid and new_tmpl_id and old_fields:
                creation_fields = _creation_fields_from_state(
                    old_fields, old_tmpl_gid, tmpl_field_name_map, state
                )

            new_sample = inv.create_sample(
                name=sample["name"],
                tags=sample.get("tags") or [],
                description=sample.get("description"),
                sample_template_id=new_tmpl_id,
                subsample_count=max(len(old_subsamples), 1),
                fields=creation_fields,
            )
            new_sa_gid = new_sample["globalId"]
            state.id_map[old_sa_gid] = new_sa_gid
            state.numeric_map[sample["id"]] = new_sample["id"]

            # Map subsample IDs by creation order (API preserves insertion order)
            new_subsamples = new_sample.get("subSamples", [])
            for old_ss, new_ss in zip(old_subsamples, new_subsamples):
                state.id_map[old_ss["globalId"]] = new_ss["globalId"]
                state.numeric_map[old_ss["id"]] = new_ss["id"]

            # Restore custom field values (name-matched against sample field IDs).
            # This is also the fallback path when creation_fields was None.
            if old_fields and new_sample.get("fields"):
                field_updates = _field_updates_by_name(old_fields, new_sample["fields"])
                if field_updates:
                    put_params: Dict[str, Any] = {"fields": field_updates}
                    # Include tags in the PUT to avoid the API clearing them
                    sample_tags = sample.get("tags")
                    if sample_tags:
                        put_params["tags"] = sample_tags
                    inv.retrieve_api_results(
                        f"/samples/{new_sample['id']}",
                        request_type="PUT",
                        params=put_params,
                    )

            # Restore per-subsample names and quantities
            for old_ss, new_ss in zip(old_subsamples, new_subsamples):
                _restore_subsample(inv, old_ss, new_ss["id"], state)

            console.print(
                f"  [green]✓[/green]  [cyan]{old_sa_gid}[/cyan] → [cyan]{new_sa_gid}[/cyan]"
                f"  ({sample['name']}, {len(old_subsamples)} subsample(s))"
            )
        except Exception as exc:
            _record_error(state, f"Sample {old_sa_gid} ({sample.get('name')!r}): {exc}")


def _restore_subsample(inv, old_ss: Dict, new_ss_id: int, state: _ImportState) -> None:
    """Update a newly-created subsample to match the exported name, quantity, and tags."""
    patch: Dict[str, Any] = {}
    if old_ss.get("name"):
        patch["name"] = old_ss["name"]
    qty = old_ss.get("quantity")
    if qty:
        patch["quantity"] = qty
    notes = old_ss.get("notes")
    if notes:
        patch["notes"] = notes
    tags = old_ss.get("tags")
    if tags:
        patch["tags"] = tags
    if not patch:
        return
    try:
        inv.retrieve_api_results(
            f"/subSamples/{new_ss_id}",
            request_type="PUT",
            params=patch,
        )
    except Exception as exc:
        _record_error(state, f"Subsample {new_ss_id} update: {exc}")


def _import_subsample_placements(
    inv, samples: List[Dict], state: _ImportState, dry_run: bool
) -> None:
    """Phase 5 — move subsamples into their recorded containers."""
    placements = [
        loc
        for sample in samples
        for loc in sample.get("_migration", {}).get("subsample_locations", [])
    ]

    if not placements:
        err_console.print("\n[bold]Phase 5[/bold] — no subsample placements to restore.")
        return

    err_console.print(
        f"\n[bold]Phase 5[/bold] — placing {len(placements)} subsample(s) into containers…"
    )

    workbench_skipped: List[str] = []

    for loc in placements:
        old_ss_gid = loc["subsample_global_id"]
        old_c_gid = loc["container_global_id"]

        # Workbench containers (BE-prefix) are per-user and can't be migrated —
        # collect them for a single summary warning rather than per-item errors.
        if old_c_gid and old_c_gid.startswith("BE"):
            workbench_skipped.append(old_ss_gid)
            continue

        new_ss_gid = state.id_map.get(old_ss_gid)
        new_c_gid = state.id_map.get(old_c_gid)

        if not new_ss_gid or not new_c_gid:
            _record_error(
                state,
                f"Subsample placement {old_ss_gid} → {old_c_gid}: "
                "one or both IDs not in id_map — skipped",
            )
            continue

        if dry_run:
            console.print(
                f"  [dim]DRY RUN[/dim]  would place [cyan]{new_ss_gid}[/cyan]"
                f" → [cyan]{new_c_gid}[/cyan]"
            )
            continue

        try:
            # Items being moved require the globalId string (SS prefix) so that
            # the SDK's Id.is_movable() check passes; target container accepts numeric.
            new_c_id = parse_id(new_c_gid)
            row = loc.get("grid_row")
            col = loc.get("grid_col")

            if row is not None and col is not None:
                from rspace_client.inv.inv import ByLocation, GridLocation

                # ByLocation(locations, *items_to_move) — locations list is first arg
                placement = ByLocation([GridLocation(x=col, y=row)], new_ss_gid)
                inv.add_items_to_grid_container(
                    target_container_id=new_c_id,
                    grid_placement=placement,
                )
            else:
                inv.add_items_to_list_container(new_c_id, new_ss_gid)

            console.print(
                f"  [green]✓[/green]  [cyan]{new_ss_gid}[/cyan] → [cyan]{new_c_gid}[/cyan]"
            )
        except Exception as exc:
            _record_error(state, f"Subsample placement {old_ss_gid} → {old_c_gid}: {exc}")

    if workbench_skipped:
        warn(
            f"{len(workbench_skipped)} subsample(s) were in workbench containers (BE-prefix) "
            "on the source server — workbenches are personal and cannot be migrated. "
            "These subsamples will remain unplaced after import."
        )


def _all_items_with_globalid(
    templates: List[Dict],
    containers: List[Dict],
    samples: List[Dict],
) -> List[Tuple[str, Dict]]:
    """Flatten templates + containers + samples (incl. subsamples) into
    ``(globalId, item)`` pairs.  Used by phases 6/7/8 which iterate over
    every inventory object that might carry per-item migration metadata.
    """
    return (
        [(t["globalId"], t) for t in templates]
        + [(c["globalId"], c) for c in containers]
        + [(s["globalId"], s) for s in samples]
        + [
            (ss["globalId"], ss)
            for s in samples
            for ss in s.get("subSamples", [])
        ]
    )


def _import_attachments(
    inv,
    templates: List[Dict],
    containers: List[Dict],
    samples: List[Dict],
    state: _ImportState,
    dry_run: bool,
    snapshot_dir: Path,
) -> None:
    """Phase 6 — re-upload attachments to their new owner globalIds."""
    att_dir = _attachments_dir(snapshot_dir)
    if not att_dir.exists():
        err_console.print("\n[bold]Phase 6[/bold] — no attachments directory; skipping.")
        return

    all_items = _all_items_with_globalid(templates, containers, samples)

    total = sum(
        len((item.get("_migration") or {}).get("attachments") or [])
        for _, item in all_items
    )
    if total == 0:
        err_console.print("\n[bold]Phase 6[/bold] — no attachments to restore.")
        return

    err_console.print(f"\n[bold]Phase 6[/bold] — restoring {total} attachment(s)…")

    with _progress() as prog:
        task = prog.add_task("[cyan]Uploading attachments", total=total)

        for old_gid, item in all_items:
            mig = item.get("_migration") or {}
            att_list = mig.get("attachments") or []
            if not att_list:
                continue

            new_gid = _resolve_new_gid(old_gid, state, "Attachments")
            if not new_gid:
                prog.advance(task, len(att_list))
                continue

            item_att_dir = att_dir / old_gid
            for att in att_list:
                local_name = att.get("local")
                filename = att.get("filename", local_name)
                src = item_att_dir / local_name if local_name else None
                if not src or not src.exists():
                    warn(f"Attachment file missing: {src} — skipped")
                    prog.advance(task)
                    continue
                if dry_run:
                    prog.console.print(
                        f"  [dim]DRY RUN[/dim]  would upload {filename!r} → [cyan]{new_gid}[/cyan]"
                    )
                    prog.advance(task)
                    continue
                try:
                    with open(src, "rb") as fh:
                        inv.upload_attachment(new_gid, fh)
                except Exception as exc:
                    _record_error(state, f"Attachment {filename!r} for {old_gid}: {exc}")
                prog.advance(task)


def _import_preview_images(
    inv,
    templates: List[Dict],
    containers: List[Dict],
    samples: List[Dict],
    state: _ImportState,
    dry_run: bool,
    snapshot_dir: Path,
) -> None:
    """Phase 7 — set preview images for templates, samples, subsamples, and containers."""
    img_dir = _images_dir(snapshot_dir)
    if not img_dir.exists():
        err_console.print("\n[bold]Phase 7[/bold] — no images directory; skipping.")
        return

    all_items = _all_items_with_globalid(templates, containers, samples)

    total = sum(
        1 for _, item in all_items if (item.get("_migration") or {}).get("preview_local")
    )
    if total == 0:
        err_console.print("\n[bold]Phase 7[/bold] — no preview images to restore.")
        return

    err_console.print(f"\n[bold]Phase 7[/bold] — restoring {total} preview image(s)…")

    with _progress() as prog:
        task = prog.add_task("[cyan]Uploading preview images", total=total)

        for old_gid, item in all_items:
            mig = item.get("_migration") or {}
            preview_local = mig.get("preview_local")
            if not preview_local:
                continue

            new_gid = _resolve_new_gid(old_gid, state, "Preview image")
            if not new_gid:
                prog.advance(task)
                continue

            src = img_dir / preview_local
            if not src.exists():
                warn(f"Preview image file missing: {src} — skipped")
                prog.advance(task)
                continue

            if dry_run:
                prog.console.print(
                    f"  [dim]DRY RUN[/dim]  would set image for [cyan]{new_gid}[/cyan]"
                )
                prog.advance(task)
                continue

            try:
                with open(src, "rb") as fh:
                    inv.set_image(new_gid, fh)
            except Exception as exc:
                _record_error(state, f"Preview image for {old_gid}: {exc}")
            prog.advance(task)


def _import_template_icons(
    inv,
    templates: List[Dict],
    state: _ImportState,
    dry_run: bool,
    snapshot_dir: Path,
) -> None:
    """Phase 8 — set icons for templates."""
    ico_dir = _icons_dir(snapshot_dir)
    if not ico_dir.exists():
        err_console.print("\n[bold]Phase 8[/bold] — no icons directory; skipping.")
        return

    total = sum(1 for t in templates if (t.get("_migration") or {}).get("icon_local"))
    if total == 0:
        err_console.print("\n[bold]Phase 8[/bold] — no template icons to restore.")
        return

    err_console.print(f"\n[bold]Phase 8[/bold] — restoring {total} template icon(s)…")

    for tmpl in templates:
        mig = tmpl.get("_migration") or {}
        icon_local = mig.get("icon_local")
        if not icon_local:
            continue

        old_gid = tmpl["globalId"]
        new_gid = _resolve_new_gid(old_gid, state, "Template icon")
        if not new_gid:
            continue
        new_tmpl_id = parse_id(new_gid)

        src = ico_dir / icon_local
        if not src.exists():
            warn(f"Template icon file missing: {src} — skipped")
            continue

        if dry_run:
            console.print(
                f"  [dim]DRY RUN[/dim]  would set icon for template [cyan]{new_gid}[/cyan]"
            )
            continue

        try:
            with open(src, "rb") as fh:
                inv.set_sample_template_icon(new_tmpl_id, fh)
            console.print(f"  [green]✓[/green]  icon → template [cyan]{new_gid}[/cyan]")
        except Exception as exc:
            _record_error(state, f"Template icon for {old_gid}: {exc}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("export")
def migrate_export(
    output: Path = typer.Option(..., "--output", "-o", help="Output snapshot directory."),
    all_resources: bool = typer.Option(False, "--all", help="Export all inventory resources."),
    template_ids: Optional[List[str]] = typer.Option(
        None,
        "--template",
        help="Template GlobalID(s) to export (repeat flag for multiple).",
        metavar="GLOBAL_ID",
    ),
    container_ids: Optional[List[str]] = typer.Option(
        None,
        "--container",
        help="Container GlobalID(s) to export (includes the full subtree).",
        metavar="GLOBAL_ID",
    ),
    sample_ids: Optional[List[str]] = typer.Option(
        None,
        "--sample",
        help="Sample GlobalID(s) to export (referenced templates auto-included).",
        metavar="GLOBAL_ID",
    ),
    no_files: bool = typer.Option(
        False,
        "--no-files",
        help="Skip downloading attachments, preview images, and icons (data only).",
    ),
) -> None:
    """Export inventory to a local snapshot directory for migration.

    The snapshot is a directory containing snapshot.json (inventory data) and
    sub-directories for attachments, preview images, template icons, and
    IMAGE container backgrounds.  Import it with 'rspace migrate import'.

    Examples:

      rspace migrate export --all --output full_snapshot/

      rspace migrate export --template IT123 --sample SA456 --output partial/

      rspace migrate export --container IC789 --output freezer_a/

      rspace migrate export --all --output data_only/ --no-files
    """
    if not any([all_resources, template_ids, container_ids, sample_ids]):
        err_console.print(
            "[red]Specify --all, or at least one of "
            "--template / --container / --sample.[/red]"
        )
        raise typer.Exit(1)

    snapshot_dir = _snapshot_dir(output)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Fresh slate for export-side warnings, so the final summary tally is accurate.
    _export_warnings.clear()

    ctx = get_context()
    inv = ctx.inv
    source_url = str(
        getattr(inv, "base_url", getattr(inv, "_url", getattr(inv, "rspace_url", "unknown")))
    )

    blob: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source_url": source_url,
            "scope": "all" if all_resources else "selected",
        },
        "templates": [],
        "containers": [],
        "samples": [],
    }

    # ---- Templates -------------------------------------------------------
    if all_resources or template_ids or sample_ids:
        t_ids = (
            [parse_id(t) for t in template_ids]
            if template_ids and not all_resources
            else None
        )
        err_console.print("[bold]Exporting templates…[/bold]")
        try:
            blob["templates"] = _export_templates(inv, ids=t_ids)
        except Exception as exc:
            err_console.print(f"[red]Export failed (templates): {exc}[/red]")
            raise typer.Exit(1)
        console.print(f"  Collected [green]{len(blob['templates'])}[/green] template(s).")

    # ---- Containers ------------------------------------------------------
    container_found_sample_ids: Set[int] = set()
    if all_resources or container_ids:
        c_ids = (
            [parse_id(c) for c in container_ids]
            if container_ids and not all_resources
            else None
        )
        err_console.print("[bold]Exporting containers…[/bold]")
        try:
            blob["containers"], container_found_sample_ids = _export_containers(inv, ids=c_ids, warn_attachments=no_files)
        except Exception as exc:
            err_console.print(f"[red]Export failed (containers): {exc}[/red]")
            raise typer.Exit(1)
        console.print(f"  Collected [green]{len(blob['containers'])}[/green] container(s).")
        if container_found_sample_ids:
            err_console.print(
                f"  Found [green]{len(container_found_sample_ids)}[/green] sample(s)"
                " referenced by subsamples inside the walked containers."
            )

    # ---- Samples ---------------------------------------------------------
    # Include explicitly requested samples, plus any discovered via container walk
    effective_sample_ids: Optional[List[str]] = sample_ids
    if container_found_sample_ids and not all_resources:
        # Merge container-discovered sample IDs with any explicitly passed ones
        explicit_numeric = {parse_id(s) for s in (sample_ids or [])}
        all_numeric = container_found_sample_ids | explicit_numeric
        effective_sample_ids = [str(sid) for sid in all_numeric]
        if not sample_ids:
            err_console.print(
                "[bold]Exporting samples found in containers…[/bold]"
            )

    if all_resources or effective_sample_ids:
        s_ids = (
            [parse_id(s) for s in effective_sample_ids]
            if effective_sample_ids and not all_resources
            else None
        )
        if not container_found_sample_ids or sample_ids:
            err_console.print("[bold]Exporting samples…[/bold]")
        try:
            samples, ref_tmpl_gids = _export_samples(inv, ids=s_ids, warn_attachments=no_files)
            blob["samples"] = samples

            # Auto-include any templates referenced by selected samples
            if not all_resources and ref_tmpl_gids:
                existing_gids = {t["globalId"] for t in blob["templates"]}
                missing_gids = ref_tmpl_gids - existing_gids
                if missing_gids:
                    err_console.print(
                        f"  Auto-including {len(missing_gids)} referenced template(s)…"
                    )
                    missing_ids = [parse_id(g) for g in missing_gids]
                    blob["templates"].extend(_export_templates(inv, ids=missing_ids))
        except Exception as exc:
            err_console.print(f"[red]Export failed (samples): {exc}[/red]")
            raise typer.Exit(1)
        console.print(f"  Collected [green]{len(blob['samples'])}[/green] sample(s).")

    # ---- Files (attachments, images, icons) ------------------------------
    if not no_files:
        try:
            _export_files(
                inv,
                blob["templates"],
                blob["containers"],
                blob["samples"],
                snapshot_dir,
            )
        except Exception as exc:
            err_console.print(f"[red]File export error: {exc}[/red]")
            # Non-fatal: the JSON snapshot is still written

    # ---- Write blob ------------------------------------------------------
    json_path = _snapshot_json(snapshot_dir)
    json_path.write_text(json.dumps(blob, indent=2, default=str))

    # Tally file export stats from the _migration dicts written by _export_files.
    all_items = [
        item
        for _, item in _all_items_with_globalid(
            blob["templates"], blob["containers"], blob["samples"]
        )
    ]
    n_attachments = sum(
        len((item.get("_migration") or {}).get("attachments") or [])
        for item in all_items
    )
    n_images = sum(
        1 for item in all_items if (item.get("_migration") or {}).get("preview_local")
    )
    n_icons = sum(
        1 for t in blob["templates"] if (t.get("_migration") or {}).get("icon_local")
    )
    n_ic_backgrounds = sum(
        1 for c in blob["containers"]
        if (c.get("_migration") or {}).get("image_container", {}).get("background_local")
    )

    console.print(f"\n[green]Snapshot written to:[/green] {snapshot_dir}/")
    console.print(
        f"  Templates: [green]{len(blob['templates'])}[/green]  "
        f"Containers: [green]{len(blob['containers'])}[/green]  "
        f"Samples: [green]{len(blob['samples'])}[/green]"
    )
    if not no_files:
        parts = []
        if n_attachments:
            parts.append(f"Attachments: [green]{n_attachments}[/green]")
        if n_images:
            parts.append(f"Preview images: [green]{n_images}[/green]")
        if n_icons:
            parts.append(f"Template icons: [green]{n_icons}[/green]")
        if n_ic_backgrounds:
            parts.append(f"IMAGE backgrounds: [green]{n_ic_backgrounds}[/green]")
        if parts:
            console.print("  " + "  ".join(parts))
        else:
            console.print("  [dim]No files exported.[/dim]")

    if _export_warnings:
        console.print(
            f"  [yellow]Warnings: {len(_export_warnings)}[/yellow] "
            "(see messages above)"
        )


@app.command("import")
def migrate_import(
    input_path: Path = typer.Argument(
        ..., help="Snapshot directory (or legacy .json file) produced by 'rspace migrate export'."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate and preview what would be created, without making any changes.",
    ),
    checkpoint_file: Optional[Path] = typer.Option(
        None,
        "--checkpoint",
        help=(
            "Checkpoint file to resume an interrupted import. "
            "Defaults to <snapshot_dir>/checkpoint.json."
        ),
    ),
    skip_templates: bool = typer.Option(False, "--skip-templates", help="Skip Phase 1."),
    skip_containers: bool = typer.Option(False, "--skip-containers", help="Skip Phases 2 & 3."),
    skip_samples: bool = typer.Option(False, "--skip-samples", help="Skip Phases 4 & 5."),
    skip_files: bool = typer.Option(False, "--skip-files", help="Skip Phases 6–8 (attachments, images, icons)."),
) -> None:
    """Import an inventory snapshot to the current RSpace server.

    Accepts a snapshot directory produced by 'rspace migrate export', or a
    legacy plain JSON snapshot file (no attachments will be restored in that case).

    Recreates templates, containers, samples, subsample placements, attachments,
    preview images, and template icons in eight ordered phases.  A checkpoint file
    is written after each phase so a failed import can be resumed with --checkpoint.

    To migrate between servers:

      # 1. Export from source
      rspace --profile source migrate export --all --output snap/

      # 2. Import to target
      rspace --profile target migrate import snap/

    Examples:

      rspace migrate import snap/ --dry-run

      rspace migrate import snap/

      rspace migrate import snap/ --checkpoint snap/checkpoint.json
    """
    snapshot_dir, json_path = _resolve_input(input_path)

    if not json_path.exists():
        err_console.print(f"[red]Snapshot JSON not found:[/red] {json_path}")
        raise typer.Exit(1)

    try:
        blob = json.loads(json_path.read_text())
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]Invalid JSON in snapshot:[/red] {exc}")
        raise typer.Exit(1)

    schema_ver = blob.get("schema_version", 0)
    if schema_ver != SCHEMA_VERSION:
        warn(
            f"Snapshot schema version {schema_ver} does not match expected "
            f"{SCHEMA_VERSION} — proceeding with caution."
        )

    templates: List[Dict] = blob.get("templates", [])
    containers: List[Dict] = blob.get("containers", [])
    samples: List[Dict] = blob.get("samples", [])
    meta: Dict = blob.get("meta", {})

    if dry_run:
        console.print("[bold yellow]DRY RUN — no changes will be made.[/bold yellow]")

    console.print(
        f"\nSnapshot from [cyan]{meta.get('source_url', 'unknown')}[/cyan]"
        f"  exported [cyan]{meta.get('exported_at', 'unknown')}[/cyan]"
    )
    console.print(
        f"Contains: [green]{len(templates)}[/green] template(s)  "
        f"[green]{len(containers)}[/green] container(s)  "
        f"[green]{len(samples)}[/green] sample(s)"
    )

    # Resolve / load checkpoint
    if checkpoint_file is None:
        checkpoint_file = snapshot_dir / "checkpoint.json"

    if checkpoint_file.exists():
        state = _load_checkpoint(checkpoint_file)
        console.print(
            f"[dim]Resuming from checkpoint ({checkpoint_file}). "
            f"Completed: {state.completed_phases}[/dim]"
        )
    else:
        state = _ImportState()

    ctx = get_context()
    inv = ctx.inv

    # Each phase is (number, key, label, skip_flag, runner-callable taking no args).
    # Wrapping each runner in a closure keeps the call-site declarative.
    phases = [
        (1, "templates", "templates", skip_templates,
         lambda: _import_templates(inv, templates, state, dry_run)),
        (2, "containers_flat", "containers flat", skip_containers,
         lambda: _import_containers_flat(inv, containers, state, dry_run, snapshot_dir)),
        (3, "containers_hierarchy", "container hierarchy", skip_containers,
         lambda: _import_container_hierarchy(inv, containers, state, dry_run)),
        (4, "samples", "samples", skip_samples,
         lambda: _import_samples(inv, samples, state, dry_run, templates=templates)),
        (5, "subsample_placements", "subsample placements", skip_samples,
         lambda: _import_subsample_placements(inv, samples, state, dry_run)),
        (6, "attachments", "attachments", skip_files,
         lambda: _import_attachments(inv, templates, containers, samples, state, dry_run, snapshot_dir)),
        (7, "preview_images", "preview images", skip_files,
         lambda: _import_preview_images(inv, templates, containers, samples, state, dry_run, snapshot_dir)),
        (8, "template_icons", "template icons", skip_files,
         lambda: _import_template_icons(inv, templates, state, dry_run, snapshot_dir)),
    ]

    for num, key, label, skip, run in phases:
        if skip or key in state.completed_phases:
            err_console.print(f"\n[dim]Phase {num} ({label}) — skipped.[/dim]")
            continue
        run()
        if not dry_run:
            state.completed_phases.append(key)
            _save_checkpoint(checkpoint_file, state)

    # ---- Summary -------------------------------------------------------
    console.print("\n" + "─" * 60)
    if state.errors:
        err_console.print(
            f"[yellow]Completed with {len(state.errors)} error(s) "
            f"(checkpoint kept at {checkpoint_file}):[/yellow]"
        )
        for err in state.errors:
            err_console.print(f"  [yellow]•[/yellow] {err}")
        raise typer.Exit(1)

    console.print("[green]Migration complete — no errors.[/green]")
    if not dry_run and checkpoint_file.exists():
        checkpoint_file.unlink(missing_ok=True)
        err_console.print(f"[dim]Checkpoint removed.[/dim]")
