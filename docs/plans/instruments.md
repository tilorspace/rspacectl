# Plan: Instrument support in rspacectl

Branch: `feat/instruments` (from `main`)
Status: implemented on `feat/instruments` (PR #5). Kept as the design record.

## Goal

Treat **instruments** (`IN…`) and **instrument templates** (`NT…`) as first-class
inventory objects. They should work with the same verbs, flags and output conventions
as samples, containers and sample templates, and `rspace migrate` should export and
import them alongside the existing data.

## What the API and client give us

From `rspace-client` 2.7.4 (`rspace_client/inv/inv.py`) and a live check on the demo server:

| Need | Client support |
|---|---|
| list / get / create / delete / restore instruments | `list_instruments`, `get_instrument_by_id`, `create_instrument` (`InstrumentPost`: name, tags, description, extra_fields, `templateId`, fields, barcodes), `delete_instrument`, `restore_instrument` |
| instrument templates | `list_instrument_templates`, `get_instrument_template_by_id`, `create_instrument_template` (`InstrumentTemplateBuilder`, no default unit), `delete_/restore_instrument_template`, `set_/get_instrument_template_icon` |
| template versioning | `update_instrument_to_latest_template_version`, `update_instrument_template_instruments` |
| search | `ResultType.INSTRUMENT`, `ResultType.INSTRUMENT_TEMPLATE` |
| generic item ops | `Id` knows `IN` → `instruments` and `NT` → `instrumentTemplates`, so `rename`, `upload_attachment_by_global_id` and raw `PUT /instruments/{id}` work |
| **placement in containers** | Client gap: `Id.is_movable()` only accepts `SS`/`IC`, so `add_items_to_{list,grid,image}_container` reject `IN` IDs. **The server API covers it** (see "Placement" below). |
| update | No `update_instrument` helper. Use `PUT /instruments/{id}`, as `update sample` already does for `/samples/{id}`. |
| bulk create | The client docstring says instruments can't be imported from CSV, but the server's 2.27 spec documents `/import/parseFile?recordType=INSTRUMENTS` and `importSettings.instrumentSettings`. A possible later `import instruments`; not in v1. |

**Prerequisite:** the `rspace-client>=2.7.4` bump being done in the other session must be
on this branch first (rebase onto it once merged).

### Placement (answer to open question 1, checked against the spec)

Source: `rspace_api_inventory_specs_2_27_0.yaml`, served by the RSpace apiDocs page on demos.researchspace.com.

- **Moving (documented):** `PUT /instruments/{id}` moves an instrument when the body has
  `parentContainers: [{id}]`. Grid and image containers also need `parentLocation`
  (`{coordX, coordY}` for grid, `{id}` for an image marker). This is the same shape as
  subsamples and containers, so no container-side workaround is needed.
- **At creation:** the `POST /instruments` body uses the `Instrument` schema, which inherits
  `parentContainers`/`parentLocation` from `InventoryRecordInfo`. But the POST description and
  examples never mention placement; only `importPidinst` documents a `newTargetLocation`.
  **So placement at creation is probably accepted, but the docs don't promise it.**
  **Verified live (local RSpace 2.27.0, 2026-10-20):** POST with `parentContainers` places the
  instrument in a LIST container. POST with `parentContainers` + `parentLocation {coordX, coordY}`
  places it in the right GRID cell. PUT moves it bench → GRID cell → LIST. The client's
  `add_items_to_list_container` still rejects `IN` IDs. New containers report `canStoreInstruments: true`.
  Decision: `create instrument --target` sends the location in the POST (one call). Migrate keeps a
  separate placement phase (PUT) so placement survives resume and stays parallel to subsamples.
- One helper, `_place_instrument(inv, gid, container_id, location=None)`, in `rspacectl/utils.py`.
  Both `rspace move` and the migrate placement phase use it. An upstream rspace-client PR making
  `Id.is_movable()` accept `IN` is a nice-to-have, not a blocker.

## Naming conventions (match existing commands)

- Plural nouns for `list` (`samples`, `containers`) → `instruments`, `instrument-templates`.
- Singular nouns for `create`/`update`/`delete` (`sample`, `template`) → `instrument`, `instrument-template`.
- Same column helpers (`COL_GLOBAL_ID`, `COL_NAME_35`, `COL_CREATED`, `COL_OWNER`), `print_result` and `print_page_info`, `handle_api_error`.
- Same pagination flags (`--page`, `--page-size`, `--order-by`, `--sort-order`), `--query/-q`, `--owned-by`, `--deleted`.
- `rspace get` infers the type from the GlobalID prefix.

## Part 1: CLI commands

### 1.1 IDs (`rspacectl/ids.py`)
- Add `"IN": "instrument"` and `"NT": "instrument-template"` to `GLOBAL_ID_PREFIXES`; update the module docstring.
- Tests in `tests/test_ids.py`.

### 1.2 `list` (`commands/list_cmd.py`)
- `list instruments`: mirrors `list samples`. `--query` goes to `inv.search(result_type=ResultType.INSTRUMENT)`. Otherwise `list_instruments(pagination, SearchFilter(deleted, owned_by))`. Data key is `instruments`. Columns: GlobalID, Name, Template (`templateId`, if present), Location (`parentContainers.0.globalId`), Created, Owner.
- `list instrument-templates`: mirrors `list templates`; data key is `templates`.

### 1.3 `get` (`commands/get_cmd.py`)
- `_get_instrument`: summary table plus extra fields.
- `_get_instrument_template`: reuses `_print_template_fields`.
- Register in `_DISPATCH`. Add aliases `inst`, `instrument-template`, `itmpl`. Add docstring lines for `rspace get IN123` and `NT456`.

### 1.4 `create` (`commands/create.py`)
- `create instrument NAME [--template NT…] [--field k=v ...] [--description] [--tag] [--target IC…]`
  - Reuse `_parse_fields`, `_build_fields_post` and `_validate_mandatory_fields` against `get_instrument_template_by_id`. These helpers are template-agnostic, so they only need their docstrings updated.
  - `--target IC… [--row R --col C]` places the instrument after creation via `_place_instrument` (see Placement).
  - Optional `--from-csv` bulk path: **not in v1**, because the client has no bulk create for instruments.
- `create instrument-template --from-file def.json`: mirrors `create template`; calls `create_instrument_template`.

### 1.5 `update` (`commands/update.py`)
- `update instrument ID [--name] [--description] [--tag] [--field k=v ...]`: same shape as `update sample` (rename, then `PUT /instruments/{id}`).
- `update instrument ID --latest-template`: calls `update_instrument_to_latest_template_version`.
- `update instrument-template ID --propagate`: calls `update_instrument_template_instruments`.
- Refactor: pull the shared rename-plus-PUT logic into `_update_inv_item(endpoint, id, name, description, tag)` so sample and instrument stay identical.

### 1.6 `delete` (`commands/delete.py`)
- `delete instrument ID...` and `delete instrument-template ID...` via `_batch_delete`.
- `restore`: **deferred** (decision). Add it later for all inventory types together.

### 1.7 Cross-cutting commands
- `tag`: add `"instrument": "instruments"` and `"instrument-template": "instrumentTemplates"` to `_INV_ENDPOINTS`; update the help text.
- `search`: add `"instruments": ResultType.INSTRUMENT` to `_INV_TYPE_MAP`.
- `move`: accept `IN` IDs. Route them to `_place_instrument` (one PUT per instrument), and keep `SS`/`IC` on the existing `add_items_to_*` path. Mixed lists are allowed. Check `canStoreInstruments` on the target and give a clear error if it is false.
- `upload attachment` / `download attachment`: these already work by GlobalID. Add tests and docs only.
- `split`, `share` and `export` don't apply to instruments.

### 1.8 Tests
New file `tests/test_instruments.py`. It uses Typer `CliRunner` with the existing `app_context` / `mock_inv` fixtures and asserts on the client calls for each command above, including GlobalID inference in `get` and `tag`.

## Part 2: `rspace migrate`

### 2.1 Snapshot format
- Bump `SCHEMA_VERSION` to 3. Add two top-level keys: `instrument_templates` and `instruments`.
- The importer treats missing keys as `[]`, so v1 and v2 snapshots still import. Add a test.
- Instruments carry `_migration` metadata like subsamples do: `parent_global_id`, plus grid coordinates or an image marker index from `parentLocation`.

### 2.2 Export (`_export.py`)
- `--instrument IN…` and `--instrument-template NT…` flags, repeatable. `--all` includes both types.
- `_export_instrument_templates(inv, ids)`: mirrors `_export_templates`.
- `_export_instruments(inv, ids)`: full records via `get_instrument_by_id`; returns referenced template GlobalIDs so they can be auto-included, as samples do.
- `_walk_container`: also collect instrument IDs found in `locations[*].content` (type `INSTRUMENT`) into a `found_instrument_ids` set. A container export then pulls in its instruments the same way it pulls in samples.
- `_enrich_image_marker_indices`: extend to instrument placements.
- `_all_items_with_globalid`: include instruments and instrument templates, so attachments and preview images go through the existing `_export_files` path. Instrument template icons go to `icons/NT…_icon.png`.
- Summary output: add counts for both types.

### 2.3 Import (`_import.py`)
New phases. Phase *keys* stay stable and new keys are added, so existing checkpoints keep resuming correctly. Only the displayed numbers change.

| # | key | change |
|---|---|---|
| 1 | `templates` | unchanged |
| 2 | `instrument_templates` | **new**: reuse an identical existing template if there is one (see 2.5), otherwise `create_instrument_template` from a sanitised payload (no `defaultUnitId`). Map field IDs by position, like phase 1. |
| 3–4 | `containers_flat`, `containers_hierarchy` | unchanged |
| 5 | `samples` | unchanged |
| 6 | `instruments` | **new**: `create_instrument` with the mapped `templateId` and fields mapped by name (reuse `_field_updates_by_name` / `_build_tmpl_field_name_map`), plus tags, description and barcodes |
| 7 | `subsample_placements` | unchanged |
| 8 | `instrument_placements` | **new**: same GRID/IMAGE/LIST target resolution as subsamples (including the image-marker lookup), but the move goes through `_place_instrument` (`PUT /instruments/{id}`). Pull the shared "resolve target container + location" code out of `_import_subsample_placements` so both phases use it. Items whose parent is a workbench (`BE…`) stay on the importing user's bench, as subsamples do today. |
| 9–11 | `attachments`, `preview_images`, `template_icons` | extended via `_all_items_with_globalid`. Icons go to `set_instrument_template_icon` for `NT` IDs. |

- New flag `--skip-instruments` (skips phases 2, 6 and 8). `--skip-templates` keeps its current meaning.
- `--dry-run` uses the identity mapping, as the existing phases do.
- Update the module docstrings in `migrate/__init__.py` (snapshot layout, phase list) and the re-export list.

### 2.4 Out of scope (decisions)
- Owner and sharing are **not** migrated, same as every other type today.

### 2.5 Reusing identical instrument templates (decision: yes, only if identical)
Instrument templates are often shared, for example the sysadmin-provided "Instrument (PIDINST 1.0)".
Creating a copy on every import would multiply them, so phase 2 reuses an existing template when it is **identical**:

- At the start of phase 2, fetch all instrument templates visible on the target (full records).
- Build a canonical fingerprint for each template and for each snapshot template:
  `name`, `description`, and the ordered list of field definitions (`name`, `type`, `mandatory`,
  options / `selectedOptions` definitions, `defaultValue`, unit or other type-specific definition).
  Strip all server keys (`_RESOURCE_SERVER_KEYS`, `_FIELD_SERVER_KEYS`, `version`, `owner`, icon).
- If exactly one target template matches, map old → existing GlobalID, map field IDs by position,
  and log `↺ reused NT… (identical)`. Nothing is created and no icon is uploaded for it (it's not ours).
- If more than one matches, reuse the lowest ID and log that duplicates exist. If there's no exact
  match (even when only the name matches), create a new template, and warn when the name collides
  so the user knows why there are two.
- `--no-reuse-templates` forces creation, for users who want a clean, separate copy.
- Dry run reports "would reuse" or "would create" for each template.
- Follow-up, not in this plan: the same matching could apply to sample templates in phase 1.

### 2.6 Tests (`tests/test_migrate.py`)
- Export: instruments found via a container walk; `--instrument` auto-includes its template.
- Import: identical template reused (no create call, field ID mapping correct); near-identical template (one field option differs) gets created, with a name-collision warning; `--no-reuse-templates`; instrument creation with field mapping; placement into GRID, LIST and IMAGE containers; checkpoint resume from a v2-era checkpoint (old keys only); v2 snapshot without instrument keys.

## Part 3: Docs
- README: add instruments to `list`, `get`, `create`, `update`, `tag`, `delete`, `search`, `move` and `migrate`, and to the GlobalID prefix table (`IN`, `NT`).
- CHANGELOG entry, if the repo starts keeping one.

## Suggested delivery order (separate commits or PRs)
1. Rebase onto the `rspace-client>=2.7.4` bump.
2. IDs, `list`, `get`, `search`, `tag` (read-only and low risk).
3. `create`, `update`, `delete`, `move`, plus the `_place_instrument` helper and the live POST-placement check.
4. Migrate export.
5. Migrate import (phases 2, 6 and 8) and the `_place_items` refactor.
6. README.
7. Optional: upstream rspace-client PR adding `IN` to `Id.is_movable()` and an `update_instrument` helper.

## Decisions (2026-10-20)
1. Placement at creation: works (verified live). Used by `create instrument --target`; moves use PUT.
2. Owner and sharing are not migrated.
3. `restore`: later, for all inventory types together.
4. Reuse an existing instrument template on import only when it is identical (section 2.5).
