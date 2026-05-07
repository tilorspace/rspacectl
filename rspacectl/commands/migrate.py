"""rspace migrate — export/import complete inventory snapshots for server-to-server migration.

Workflow
--------
Export (source server):

    rspace migrate export --all --output snapshot.json
    rspace migrate export --template IT1 --sample SA2 --output partial.json

Import (target server, different --profile):

    rspace --profile target migrate import snapshot.json
    rspace --profile target migrate import snapshot.json --dry-run
    rspace --profile target migrate import snapshot.json --checkpoint snapshot.json.checkpoint

Import algorithm (five phases)
-------------------------------
1. Templates   — create flat; record old→new globalId + per-field id mapping
2. Containers (flat) — create all containers at top level; record old→new globalId
3. Container hierarchy — move containers into their parents, shallowest depth first
4. Samples     — create from mapped template; name-match field values; update subsample names/quantities
5. Subsample placements — move each subsample into its recorded container (exact grid position preserved)

A checkpoint file (<snapshot>.checkpoint) is written after each phase so an
interrupted import can resume with --checkpoint without duplicating data.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import typer
from rspace_client.inv.inv import Pagination

from ..context import get_context
from ..exceptions import warn
from ..ids import parse_id
from ..output import console, err_console

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")

SCHEMA_VERSION = 1
_CHECKPOINT_SUFFIX = ".checkpoint"

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


def _sanitise_container(c: Dict) -> Dict:
    return _strip(c, _RESOURCE_SERVER_KEYS)


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
    full = inv.get_container_by_id(stub["id"], include_content=True)
    full["_migration"] = {
        "depth": depth,
        "parent_global_id": parent_global_id,
        "parent_grid_col": parent_grid_col,
        "parent_grid_row": parent_grid_row,
    }
    collected = [full]

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
        for item in (full.get("storedContainers") or []):
            children_with_pos.append((item, None, None))

    if not children_with_pos:
        for item in (full.get("content") or {}).get("content", []):
            if item.get("globalId", "").startswith("IC"):
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
    inv, ids: Optional[List[int]] = None
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
    return containers, found_sample_ids


def _collect_subsample_locations(sample: Dict) -> List[Dict]:
    """Extract where each subsample sits (container + optional grid position)."""
    locations = []
    for ss in sample.get("subSamples", []):
        parents = ss.get("parentContainers") or []
        if not parents:
            continue
        parent = parents[0]  # a subsample lives in exactly one container at a time
        grid = parent.get("gridLocation") or parent.get("location") or {}
        locations.append(
            {
                "subsample_global_id": ss["globalId"],
                "container_global_id": parent.get("globalId"),
                "grid_row": grid.get("rowIndex") or grid.get("row"),
                "grid_col": grid.get("colIndex") or grid.get("col"),
            }
        )
    return locations


def _export_samples(
    inv,
    ids: Optional[List[int]] = None,
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

        # Warn about attachments (not migrated in this version)
        for ss in full.get("subSamples", []):
            if ss.get("attachments") or ss.get("storedFiles"):
                attachment_count += 1
        if full.get("attachments") or full.get("storedFiles"):
            attachment_count += 1

        samples.append(full)

    if attachment_count:
        warn(
            f"{attachment_count} item(s) have file attachments — attachments are not "
            "included in this snapshot. Re-attach files manually after import."
        )

    return samples, ref_template_gids


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
    inv, containers: List[Dict], state: _ImportState, dry_run: bool
) -> None:
    """Phase 2 — create all containers at top level (hierarchy restored in Phase 3)."""
    err_console.print(
        f"\n[bold]Phase 2[/bold] — creating {len(containers)} container(s) (flat)…"
    )
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

            if ctype == "GRID":
                layout = c.get("gridLayout") or {}
                new_c = inv.create_grid_container(
                    name=c["name"],
                    row_count=layout.get("rowsNumber", 1),
                    column_count=layout.get("columnsNumber", 1),
                    tags=tags,
                    description=desc,
                    can_store_samples=can_samples,
                    can_store_containers=can_containers,
                )
            else:
                new_c = inv.create_list_container(
                    name=c["name"],
                    tags=tags,
                    description=desc,
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

                placement = ByLocation(new_gid, locations=[GridLocation(x=col, y=row)])
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
                    inv.retrieve_api_results(
                        f"/samples/{new_sample['id']}",
                        request_type="PUT",
                        params={"fields": field_updates},
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
    """Update a newly-created subsample to match the exported name and quantity."""
    patch: Dict[str, Any] = {}
    if old_ss.get("name"):
        patch["name"] = old_ss["name"]
    qty = old_ss.get("quantity")
    if qty:
        patch["quantity"] = qty
    notes = old_ss.get("notes")
    if notes:
        patch["notes"] = notes
    if not patch:
        return
    try:
        inv.retrieve_api_results(
            f"/subSamples/{new_ss_id}",
            request_type="PUT",
            params=patch,
        )
    except Exception as exc:
        warn(f"Could not update subsample {new_ss_id}: {exc}")
        state.errors.append(f"Subsample {new_ss_id} update: {exc}")


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

                placement = ByLocation(new_ss_gid, locations=[GridLocation(x=col, y=row)])
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


def _record_error(state: _ImportState, message: str) -> None:
    warn(message)
    state.errors.append(message)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("export")
def migrate_export(
    output: Path = typer.Option(..., "--output", "-o", help="Output snapshot file (JSON)."),
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
) -> None:
    """Export inventory to a local snapshot file for migration.

    The snapshot is a self-contained JSON file capturing templates,
    containers (full hierarchy), samples and their subsample locations.
    It can be imported to any RSpace server with 'rspace migrate import'.

    Examples:

      rspace migrate export --all --output full_snapshot.json

      rspace migrate export --template IT123 --sample SA456 --output partial.json

      rspace migrate export --container IC789 --output freezer_a.json
    """
    if not any([all_resources, template_ids, container_ids, sample_ids]):
        err_console.print(
            "[red]Specify --all, or at least one of "
            "--template / --container / --sample.[/red]"
        )
        raise typer.Exit(1)

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
            blob["containers"], container_found_sample_ids = _export_containers(inv, ids=c_ids)
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
            samples, ref_tmpl_gids = _export_samples(inv, ids=s_ids)
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

    # ---- Write blob ------------------------------------------------------
    output.write_text(json.dumps(blob, indent=2, default=str))
    console.print(f"\n[green]Snapshot written to:[/green] {output}")
    console.print(
        f"  Templates: [green]{len(blob['templates'])}[/green]  "
        f"Containers: [green]{len(blob['containers'])}[/green]  "
        f"Samples: [green]{len(blob['samples'])}[/green]"
    )


@app.command("import")
def migrate_import(
    input_file: Path = typer.Argument(
        ..., help="Snapshot file produced by 'rspace migrate export'."
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
            "Defaults to <snapshot>.checkpoint alongside the input file."
        ),
    ),
    skip_templates: bool = typer.Option(False, "--skip-templates", help="Skip Phase 1."),
    skip_containers: bool = typer.Option(False, "--skip-containers", help="Skip Phases 2 & 3."),
    skip_samples: bool = typer.Option(False, "--skip-samples", help="Skip Phases 4 & 5."),
) -> None:
    """Import an inventory snapshot to the current RSpace server.

    Recreates templates, containers (preserving hierarchy), samples, and
    subsample container placements in five ordered phases.  A checkpoint file
    is written after each phase so a failed import can be resumed with
    --checkpoint without duplicating already-created resources.

    To migrate between servers:

      # 1. Export from source
      rspace --profile source migrate export --all --output snap.json

      # 2. Import to target
      rspace --profile target migrate import snap.json

    Examples:

      rspace migrate import snap.json --dry-run

      rspace migrate import snap.json

      rspace migrate import snap.json --checkpoint snap.json.checkpoint
    """
    if not input_file.exists():
        err_console.print(f"[red]File not found:[/red] {input_file}")
        raise typer.Exit(1)

    try:
        blob = json.loads(input_file.read_text())
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
        checkpoint_file = input_file.with_suffix(input_file.suffix + _CHECKPOINT_SUFFIX)

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

    # ---- Phase 1: Templates --------------------------------------------
    if not skip_templates and "templates" not in state.completed_phases:
        _import_templates(inv, templates, state, dry_run)
        if not dry_run:
            state.completed_phases.append("templates")
            _save_checkpoint(checkpoint_file, state)
    else:
        err_console.print("\n[dim]Phase 1 (templates) — skipped.[/dim]")

    # ---- Phase 2: Containers (flat) ------------------------------------
    if not skip_containers and "containers_flat" not in state.completed_phases:
        _import_containers_flat(inv, containers, state, dry_run)
        if not dry_run:
            state.completed_phases.append("containers_flat")
            _save_checkpoint(checkpoint_file, state)
    else:
        err_console.print("\n[dim]Phase 2 (containers flat) — skipped.[/dim]")

    # ---- Phase 3: Container hierarchy ----------------------------------
    if not skip_containers and "containers_hierarchy" not in state.completed_phases:
        _import_container_hierarchy(inv, containers, state, dry_run)
        if not dry_run:
            state.completed_phases.append("containers_hierarchy")
            _save_checkpoint(checkpoint_file, state)
    else:
        err_console.print("\n[dim]Phase 3 (container hierarchy) — skipped.[/dim]")

    # ---- Phase 4: Samples + subsamples ---------------------------------
    if not skip_samples and "samples" not in state.completed_phases:
        _import_samples(inv, samples, state, dry_run, templates=templates)
        if not dry_run:
            state.completed_phases.append("samples")
            _save_checkpoint(checkpoint_file, state)
    else:
        err_console.print("\n[dim]Phase 4 (samples) — skipped.[/dim]")

    # ---- Phase 5: Subsample placements ---------------------------------
    if not skip_samples and "subsample_placements" not in state.completed_phases:
        _import_subsample_placements(inv, samples, state, dry_run)
        if not dry_run:
            state.completed_phases.append("subsample_placements")
            _save_checkpoint(checkpoint_file, state)
    else:
        err_console.print("\n[dim]Phase 5 (subsample placements) — skipped.[/dim]")

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
