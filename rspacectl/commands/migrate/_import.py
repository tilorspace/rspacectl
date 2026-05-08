"""Import side of the migrate command.

Reads a snapshot directory produced by ``_export.py`` and recreates
templates, containers, samples, subsample placements, attachments, preview
images, and template icons on the target server in eight ordered phases.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import typer

from ...context import get_context
from ...exceptions import warn
from ...ids import parse_id
from ...output import console, err_console
from ._common import (
    SCHEMA_VERSION,
    _ImportState,
    _all_items_with_globalid,
    _attachments_dir,
    _icons_dir,
    _image_containers_dir,
    _images_dir,
    _load_checkpoint,
    _progress,
    _record_error,
    _resolve_input,
    _resolve_new_gid,
    _sanitise_template,
    _save_checkpoint,
)


# ---------------------------------------------------------------------------
# Phase 1 — templates
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


# ---------------------------------------------------------------------------
# Phases 2 & 3 — containers (flat + hierarchy)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phase 4 — samples + subsamples
# ---------------------------------------------------------------------------


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


def _derive_total_quantity(sample: Dict):
    """Return a ``Quantity`` to seed the new sample's unit dimension, or None.

    RSpace's ``create_sample`` accepts a ``total_quantity`` (a SDK ``Quantity``
    object) which determines the unit dimension all subsamples inherit.  Once
    set, individual subsample quantities can only be updated within the same
    dimension (e.g. ml ↔ l, but not ml → g).  Without this, the new sample
    falls back to the template's default unit and per-subsample quantity
    updates fail with 422 "Incoming unit X is incompatible with stored unit Y".

    Strategy:
    1. Prefer the source sample's own ``quantity`` (the API-reported total).
    2. Otherwise compute ``sum(numericValue) for subsamples sharing a unitId``,
       picking the unit of the first quantity-bearing subsample.

    Returns None when the source has no quantity information at all.
    """
    sample_qty = sample.get("quantity")
    if sample_qty and sample_qty.get("unitId") is not None:
        from rspace_client.inv.inv import Quantity
        return Quantity(
            sample_qty.get("numericValue") or 0.0,
            {"id": sample_qty["unitId"]},
        )

    # Fall back to summing matching-unit subsample quantities.
    for old_ss in sample.get("subSamples", []):
        qty = old_ss.get("quantity") or {}
        unit_id = qty.get("unitId")
        if unit_id is None:
            continue
        total_value = sum(
            (ss.get("quantity") or {}).get("numericValue") or 0.0
            for ss in sample.get("subSamples", [])
            if (ss.get("quantity") or {}).get("unitId") == unit_id
        )
        from rspace_client.inv.inv import Quantity
        # Use a non-zero default so RSpace doesn't reject the create payload.
        return Quantity(total_value or 1.0, {"id": unit_id})

    return None


def _creation_fields_from_state(
    old_fields: List[Dict],
    old_tmpl_gid: str,
    tmpl_field_name_map: Dict[str, Dict[str, str]],
    state: _ImportState,
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
    state: _ImportState,
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

            # Seed the unit dimension from the source so per-subsample quantity
            # updates in _restore_subsample don't fail with 422 (unit mismatch).
            total_quantity = _derive_total_quantity(sample)

            new_sample = inv.create_sample(
                name=sample["name"],
                tags=sample.get("tags") or [],
                description=sample.get("description"),
                sample_template_id=new_tmpl_id,
                subsample_count=max(len(old_subsamples), 1),
                fields=creation_fields,
                total_quantity=total_quantity,
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


# ---------------------------------------------------------------------------
# Phase 5 — subsample placements
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phases 6, 7, 8 — file uploads
# ---------------------------------------------------------------------------


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
# Command function (decorated in __init__.py)
# ---------------------------------------------------------------------------


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
