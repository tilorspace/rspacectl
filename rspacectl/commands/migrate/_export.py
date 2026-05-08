"""Export side of the migrate command.

Walks templates / containers / samples on the source server, downloads
attachments, preview images, template icons, and IMAGE container backgrounds,
and writes a snapshot directory consumable by ``_import.py``.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import typer

from ...context import get_context
from ...exceptions import warn
from ...ids import parse_id
from ...output import console, err_console
from ._common import (
    _ATTACHMENT_FIELD_TYPES,
    SCHEMA_VERSION,
    _all_items_with_globalid,
    _attachments_dir,
    _export_warn,
    _export_warnings,
    _find_link,
    _icons_dir,
    _image_containers_dir,
    _images_dir,
    _paginate,
    _preview_image_hash,
    _preview_image_url,
    _progress,
    _snapshot_dir,
    _snapshot_json,
)


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


def _export_preview_image(
    inv,
    item: Dict,
    images_dir: Path,
    skip_hashes: Optional[Set[str]] = None,
) -> Optional[str]:
    """Download the preview image for an inventory item (SA/SS/IC/IT).

    Skips download when the URL's image-hash is in ``skip_hashes`` — used
    to filter out RSpace's default thumbnails (the same hash gets served
    for every item that hasn't had a custom image uploaded).

    A 404 means no image is set; any other error is warned.
    Returns the local filename relative to images_dir, or None.
    """
    import requests as _requests  # noqa: PLC0415 — lazy import to avoid hard dep at module load

    gid = item.get("globalId", "")
    if not gid:
        return None

    # The image URL is a hash-based path (/files/image/{hash}) that cannot be
    # constructed from the globalId — we rely entirely on the _links array.
    url = _preview_image_url(item)
    if not url:
        return None

    if skip_hashes is not None:
        h = _preview_image_hash(url)
        if h and h in skip_hashes:
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
# Export — fetch helpers (templates, containers, samples)
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

    # Identify default thumbnails: RSpace's image storage is content-addressed,
    # so the same hash gets reused for every item that hasn't had a custom
    # preview uploaded. Hashes referenced by ≥2 items are almost certainly
    # defaults; skip them to avoid round-tripping the same placeholder image
    # for every sample/subsample/container in the snapshot.
    all_items = (
        list(templates)
        + list(containers)
        + list(samples)
        + [ss for s in samples for ss in s.get("subSamples", [])]
    )
    hash_counts: Dict[str, int] = {}
    for it in all_items:
        h = _preview_image_hash(_preview_image_url(it) or "")
        if h:
            hash_counts[h] = hash_counts.get(h, 0) + 1
    skip_hashes = {h for h, n in hash_counts.items() if n >= 2}
    if skip_hashes:
        err_console.print(
            f"  [dim]Skipping {len(skip_hashes)} default thumbnail hash(es) "
            f"shared by multiple items.[/dim]"
        )

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

            preview_local = _export_preview_image(inv, tmpl, img_dir, skip_hashes)
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

            preview_local = _export_preview_image(inv, c, img_dir, skip_hashes)
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

            preview_local = _export_preview_image(inv, sample, img_dir, skip_hashes)
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

                preview_local = _export_preview_image(inv, ss, img_dir, skip_hashes)
                if preview_local:
                    ss_mig["preview_local"] = preview_local
                prog.advance(task)


# ---------------------------------------------------------------------------
# Command function (decorated in __init__.py)
# ---------------------------------------------------------------------------


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
