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

Module layout
-------------
- _common.py — shared infrastructure (state, paths, helpers, progress)
- _export.py — everything export, plus the migrate_export command function
- _import.py — everything import, plus the migrate_import command function
- __init__.py — Typer app; registers the two commands; re-exports for tests
"""

from __future__ import annotations

import typer

# Re-exports for tests + power users.  Names start with an underscore by
# convention, so we import them explicitly rather than via ``from .x import *``
# (which would skip them).  Update this list when adding new helpers.
from ._common import (  # noqa: F401
    SCHEMA_VERSION,
    _ATTACHMENT_FIELD_TYPES,
    _CHECKPOINT_SUFFIX,
    _FIELD_SERVER_KEYS,
    _ImportState,
    _RESOURCE_SERVER_KEYS,
    _SNAPSHOT_JSON,
    _all_items_with_globalid,
    _attachments_dir,
    _export_warn,
    _export_warnings,
    _find_link,
    _icons_dir,
    _image_containers_dir,
    _images_dir,
    _load_checkpoint,
    _paginate,
    _preview_image_hash,
    _preview_image_url,
    _progress,
    _record_error,
    _resolve_input,
    _resolve_new_gid,
    _sanitise_template,
    _save_checkpoint,
    _snapshot_dir,
    _snapshot_json,
    _strip,
)
from ._export import (  # noqa: F401
    _collect_subsample_locations,
    _enrich_image_marker_indices,
    _export_attachment_extra_fields,
    _export_attachments,
    _export_containers,
    _export_files,
    _export_image_container,
    _export_preview_image,
    _export_samples,
    _export_template_icon,
    _export_templates,
    _walk_container,
    migrate_export,
)
from ._import import (  # noqa: F401
    _build_tmpl_field_name_map,
    _creation_fields_from_state,
    _derive_total_quantity,
    _field_updates_by_name,
    _image_marker_lookup,
    _import_attachments,
    _import_container_hierarchy,
    _import_containers_flat,
    _import_preview_images,
    _import_samples,
    _import_subsample_placements,
    _import_template_icons,
    _import_templates,
    _restore_subsample,
    migrate_import,
)

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")
app.command("export")(migrate_export)
app.command("import")(migrate_import)
