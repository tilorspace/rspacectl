# Plan: Instrument support in rspacectl

Branch: `feat/instruments` (from `main`)
Status: plan, not implemented

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
| **placement in containers** | **Gap.** Instruments have `parentContainers`/`parentLocation`, and containers expose `canStoreInstruments` and `contentSummary.instrumentCount`. But `Id.is_movable()` only accepts `SS`/`IC`, so `add_items_to_{list,grid,image}_container` reject `IN` IDs. |
| update | No `update_instrument` helper. Use `PUT /instruments/{id}`, as `update sample` already does for `/samples/{id}`. |

**Prerequisite:** the `rspace-client>=2.7.4` bump being done in the other session must be
on this branch first (rebase onto it once merged).

**Placement gap, preferred fix:** send a small upstream PR to rspace-client that makes
`Id.is_movable()` accept `IN`. Until it ships, use a local helper `_move_items(inv, target, gids, location=None)`
in `rspacectl/utils.py`. It calls the same `PUT /containers/{id}` payload as the client's
`_do_add_to_*` methods, skipping the movability check. Both `rspace move` and the migrate
placement phase will use this helper, so the workaround lives in one place.

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
  - `--target` places the instrument after creation, using the `_move_items` helper.
  - Optional `--from-csv` bulk path: **not in v1**, because the client has no bulk create for instruments.
- `create instrument-template --from-file def.json`: mirrors `create template`; calls `create_instrument_template`.

### 1.5 `update` (`commands/update.py`)
- `update instrument ID [--name] [--description] [--tag] [--field k=v ...]`: same shape as `update sample` (rename, then `PUT /instruments/{id}`).
- `update instrument ID --latest-template`: calls `update_instrument_to_latest_template_version`.
- `update instrument-template ID --propagate`: calls `update_instrument_template_instruments`.
- Refactor: pull the shared rename-plus-PUT logic into `_update_inv_item(endpoint, id, name, description, tag)` so sample and instrument stay identical.

### 1.6 `delete` (`commands/delete.py`)
- `delete instrument ID...` and `delete instrument-template ID...` via `_batch_delete`.
- Optional, to discuss: `restore` support. No other type has a restore command yet, so leave it out of v1 for consistency.

### 1.7 Cross-cutting commands
- `tag`: add `"instrument": "instruments"` and `"instrument-template": "instrumentTemplates"` to `_INV_ENDPOINTS`; update the help text.
- `search`: add `"instruments": ResultType.INSTRUMENT` to `_INV_TYPE_MAP`.
- `move`: accept `IN` IDs via `_move_items`; validate `canStoreInstruments` on the target and give a clear error if it is false.
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
| 2 | `instrument_templates` | **new**: `create_instrument_template` from a sanitised payload (no `defaultUnitId`); map field IDs by position, like phase 1 |
| 3–4 | `containers_flat`, `containers_hierarchy` | unchanged |
| 5 | `samples` | unchanged |
| 6 | `instruments` | **new**: `create_instrument` with the mapped `templateId` and fields mapped by name (reuse `_field_updates_by_name` / `_build_tmpl_field_name_map`), plus tags, description and barcodes |
| 7 | `subsample_placements` | unchanged |
| 8 | `instrument_placements` | **new**: same GRID/IMAGE/LIST logic as subsamples, through `_move_items`. Refactor the shared body of `_import_subsample_placements` into a `_place_items` helper so both phases use one implementation. Items whose parent is a workbench (`BE…`) are left on the importing user's bench, as subsamples are today. |
| 9–11 | `attachments`, `preview_images`, `template_icons` | extended via `_all_items_with_globalid`. Icons go to `set_instrument_template_icon` for `NT` IDs. |

- New flag `--skip-instruments` (skips phases 2, 6 and 8). `--skip-templates` keeps its current meaning.
- `--dry-run` uses the identity mapping, as the existing phases do.
- Update the module docstrings in `migrate/__init__.py` (snapshot layout, phase list) and the re-export list.

### 2.4 Tests (`tests/test_migrate.py`)
- Export: instruments found via a container walk; `--instrument` auto-includes its template.
- Import: instrument template and instrument creation with field mapping; placement into GRID, LIST and IMAGE containers; checkpoint resume from a v2-era checkpoint (old keys only); v2 snapshot without instrument keys.

## Part 3: Docs
- README: add instruments to `list`, `get`, `create`, `update`, `tag`, `delete`, `search`, `move` and `migrate`, and to the GlobalID prefix table (`IN`, `NT`).
- CHANGELOG entry, if the repo starts keeping one.

## Suggested delivery order (separate commits or PRs)
1. Rebase onto the `rspace-client>=2.7.4` bump.
2. IDs, `list`, `get`, `search`, `tag` (read-only and low risk).
3. `create`, `update`, `delete`, `move`, plus the `_move_items` helper.
4. Migrate export.
5. Migrate import (phases 2, 6 and 8) and the `_place_items` refactor.
6. README.
7. Upstream rspace-client PR for `Id.is_movable()`; drop the workaround once it is released.

## Open questions
1. Does `POST /instruments` accept a target location at creation, as samples do with `TargetLocation`? If it does, `create instrument --target` and the migrate placement can be done in one call. Verify against the API docs or a test server.
2. Should migrate keep instrument **owner/sharing**? Current migrate doesn't do this for any type, so the proposal is no.
3. `restore` for deleted instruments: add it now (the client supports it) or keep parity with the other types?
4. Instrument templates often come from a sysadmin-provided PIDINST template. On import, should a template whose name matches an existing target template be reused instead of duplicated (for example, a `--reuse-templates` flag)?
