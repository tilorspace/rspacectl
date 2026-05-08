"""Tests for the rspace migrate export/import command.

Covers pure helpers, the import phase functions (with a mocked SDK), and
the full export→import round-trip on a fixture blob.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from rspacectl.commands import migrate
from rspacectl.commands.migrate import (
    _ImportState,
    _build_tmpl_field_name_map,
    _collect_subsample_locations,
    _creation_fields_from_state,
    _field_updates_by_name,
    _find_link,
    _paginate,
    _preview_image_hash,
    _preview_image_url,
    _resolve_new_gid,
    _sanitise_template,
    _strip,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestStrip:
    def test_removes_keys(self):
        out = _strip({"a": 1, "b": 2, "c": 3}, frozenset({"b"}))
        assert out == {"a": 1, "c": 3}

    def test_no_keys_to_strip(self):
        assert _strip({"a": 1}, frozenset()) == {"a": 1}

    def test_all_keys_stripped(self):
        assert _strip({"a": 1}, frozenset({"a"})) == {}


class TestSanitiseTemplate:
    def test_strips_server_keys_from_template_and_fields(self):
        tmpl = {
            "id": 1,
            "globalId": "IT1",
            "name": "Reagent",
            "created": "2024-01-01",
            "fields": [
                {"id": 10, "globalId": "SF10", "name": "lot", "type": "text"},
                {"id": 11, "globalId": "SF11", "name": "supplier", "type": "text"},
            ],
        }
        clean = _sanitise_template(tmpl)
        assert "id" not in clean
        assert "globalId" not in clean
        assert "created" not in clean
        assert clean["name"] == "Reagent"
        for f in clean["fields"]:
            assert "id" not in f
            assert "globalId" not in f
            assert f["name"] in {"lot", "supplier"}


class TestFindLink:
    def test_underscore_links_with_link_field(self):
        item = {"_links": [{"rel": "image", "link": "http://x/img"}]}
        assert _find_link(item, "image") == "http://x/img"

    def test_underscore_links_with_href_fallback(self):
        item = {"_links": [{"rel": "image", "href": "http://x/img"}]}
        assert _find_link(item, "image") == "http://x/img"

    def test_plain_links_field(self):
        item = {"links": [{"rel": "image", "href": "http://x/img"}]}
        assert _find_link(item, "image") == "http://x/img"

    def test_missing_rel(self):
        item = {"_links": [{"rel": "self", "link": "http://x/self"}]}
        assert _find_link(item, "image") is None

    def test_no_links(self):
        assert _find_link({}, "image") is None
        assert _find_link({"_links": None}, "image") is None
        assert _find_link({"_links": []}, "image") is None


class TestPreviewImageUrlAndHash:
    def test_url_from_image_link(self):
        item = {"_links": [{"rel": "image", "link": "https://x/api/v1/files/image/HASH123"}]}
        assert _preview_image_url(item) == "https://x/api/v1/files/image/HASH123"

    def test_url_falls_back_to_thumbnail(self):
        item = {"_links": [{"rel": "thumbnail", "link": "https://x/files/image/HT"}]}
        assert _preview_image_url(item) == "https://x/files/image/HT"

    def test_url_none_when_no_image_link(self):
        item = {"_links": [{"rel": "self", "link": "https://x/self"}]}
        assert _preview_image_url(item) is None

    def test_hash_from_canonical_url(self):
        url = "https://demos.researchspace.com/api/inventory/v1/files/image/abc123"
        assert _preview_image_hash(url) == "abc123"

    def test_hash_strips_query(self):
        url = "https://x/files/image/abc123?foo=bar"
        assert _preview_image_hash(url) == "abc123"

    def test_hash_returns_none_for_unrelated_url(self):
        assert _preview_image_hash("https://example.com/foo") is None

    def test_hash_handles_empty_input(self):
        assert _preview_image_hash("") is None
        assert _preview_image_hash(None) is None


class TestPaginate:
    def test_single_page(self):
        fetch = MagicMock(return_value={"items": [1, 2, 3], "totalHits": 3})
        out = _paginate(fetch, "items")
        assert out == [1, 2, 3]
        assert fetch.call_count == 1

    def test_multiple_pages(self):
        fetch = MagicMock(
            side_effect=[
                {"items": [1, 2], "totalHits": 5},
                {"items": [3, 4], "totalHits": 5},
                {"items": [5], "totalHits": 5},
            ]
        )
        out = _paginate(fetch, "items", page_size=2)
        assert out == [1, 2, 3, 4, 5]
        assert fetch.call_count == 3

    def test_empty_batch_terminates(self):
        # Server says totalHits=99 but returns nothing — don't loop forever.
        fetch = MagicMock(return_value={"items": [], "totalHits": 99})
        out = _paginate(fetch, "items")
        assert out == []
        assert fetch.call_count == 1


class TestResolveNewGid:
    def test_returns_mapping(self):
        state = _ImportState(id_map={"SA1": "SA100"})
        assert _resolve_new_gid("SA1", state, "Sample") == "SA100"
        assert state.errors == []

    def test_records_error_on_miss(self):
        state = _ImportState()
        result = _resolve_new_gid("SA1", state, "Sample")
        assert result is None
        assert len(state.errors) == 1
        assert "SA1" in state.errors[0]
        assert "not in id_map" in state.errors[0]


class TestFieldUpdatesByName:
    def test_matches_by_name(self):
        old = [{"name": "lot", "content": "ABC"}, {"name": "supplier", "content": "Sigma"}]
        new = [{"id": 100, "name": "lot"}, {"id": 101, "name": "supplier"}]
        out = _field_updates_by_name(old, new)
        assert {"id": 100, "content": "ABC"} in out
        assert {"id": 101, "content": "Sigma"} in out

    def test_skips_missing_name_in_new(self):
        old = [{"name": "lot", "content": "ABC"}]
        new = [{"id": 100, "name": "supplier"}]
        assert _field_updates_by_name(old, new) == []

    def test_skips_empty_content(self):
        old = [{"name": "lot", "content": None}]
        new = [{"id": 100, "name": "lot"}]
        assert _field_updates_by_name(old, new) == []

    def test_falls_back_to_value_or_data(self):
        old = [{"name": "lot", "value": "FROM_VALUE"}]
        new = [{"id": 100, "name": "lot"}]
        assert _field_updates_by_name(old, new) == [{"id": 100, "content": "FROM_VALUE"}]


class TestBuildTmplFieldNameMap:
    def test_indexes_by_template_then_field_name(self):
        templates = [
            {
                "globalId": "IT1",
                "fields": [
                    {"name": "lot", "globalId": "SF10"},
                    {"name": "supplier", "globalId": "SF11"},
                ],
            }
        ]
        out = _build_tmpl_field_name_map(templates)
        assert out == {"IT1": {"lot": "SF10", "supplier": "SF11"}}

    def test_skips_fields_missing_name_or_globalid(self):
        templates = [
            {
                "globalId": "IT1",
                "fields": [
                    {"name": "lot", "globalId": "SF10"},
                    {"name": "incomplete"},  # no globalId
                ],
            }
        ]
        assert _build_tmpl_field_name_map(templates) == {"IT1": {"lot": "SF10"}}


class TestCreationFieldsFromState:
    def test_uses_id_map_from_state(self):
        old_fields = [{"name": "lot", "content": "ABC"}]
        tmpl_field_name_map = {"IT1": {"lot": "SF10"}}
        state = _ImportState(id_map={"SF10": "200"})

        out = _creation_fields_from_state(old_fields, "IT1", tmpl_field_name_map, state)
        assert out == [{"id": 200, "content": "ABC"}]

    def test_returns_none_if_no_template_fields(self):
        out = _creation_fields_from_state([], "IT1", {}, _ImportState())
        assert out is None

    def test_includes_field_with_no_content(self):
        # API requires every template field to be in the payload, even if empty.
        old_fields: list = []
        tmpl_field_name_map = {"IT1": {"lot": "SF10"}}
        state = _ImportState(id_map={"SF10": "200"})
        out = _creation_fields_from_state(old_fields, "IT1", tmpl_field_name_map, state)
        assert out == [{"id": 200}]


class TestCollectSubsampleLocations:
    def test_grid_parent_records_coords(self):
        sample = {
            "subSamples": [
                {
                    "globalId": "SS1",
                    "parentContainers": [{"globalId": "IC10", "cType": "GRID"}],
                    "parentLocation": {"coordX": 2, "coordY": 3},
                }
            ]
        }
        out = _collect_subsample_locations(sample)
        assert out == [
            {
                "subsample_global_id": "SS1",
                "container_global_id": "IC10",
                "grid_col": 2,
                "grid_row": 3,
            }
        ]

    def test_list_parent_strips_coords(self):
        sample = {
            "subSamples": [
                {
                    "globalId": "SS1",
                    "parentContainers": [{"globalId": "IC10", "cType": "LIST"}],
                    "parentLocation": {"coordX": 5, "coordY": 7},
                }
            ]
        }
        out = _collect_subsample_locations(sample)
        # LIST coords are slot IDs, not meaningful — stripped.
        assert out[0]["grid_col"] is None
        assert out[0]["grid_row"] is None

    def test_skips_subsample_without_parent(self):
        sample = {"subSamples": [{"globalId": "SS1", "parentContainers": []}]}
        assert _collect_subsample_locations(sample) == []


# ---------------------------------------------------------------------------
# Import phases (with mocked SDK)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_inv():
    inv = MagicMock()
    inv.api_key = "test-key"
    return inv


@pytest.fixture
def empty_state():
    return _ImportState()


class TestImportTemplates:
    def test_creates_and_records_mapping(self, mock_inv, empty_state):
        old_tmpl = {
            "id": 1,
            "globalId": "IT1",
            "name": "Reagent",
            "fields": [{"id": 10, "globalId": "SF10", "name": "lot"}],
        }
        mock_inv.create_sample_template.return_value = {
            "id": 100,
            "globalId": "IT100",
            "fields": [{"id": 1000, "name": "lot"}],
        }
        migrate._import_templates(mock_inv, [old_tmpl], empty_state, dry_run=False)
        assert empty_state.id_map["IT1"] == "IT100"
        assert empty_state.numeric_map[1] == 100
        # field-id mapping: old globalId → new numeric id (as string)
        assert empty_state.id_map["SF10"] == "1000"
        mock_inv.create_sample_template.assert_called_once()

    def test_dry_run_uses_identity_mapping(self, mock_inv, empty_state):
        tmpl = {
            "id": 1,
            "globalId": "IT1",
            "name": "Reagent",
            "fields": [{"id": 10, "globalId": "SF10", "name": "lot"}],
        }
        migrate._import_templates(mock_inv, [tmpl], empty_state, dry_run=True)
        assert empty_state.id_map["IT1"] == "IT1"
        assert empty_state.id_map["SF10"] == "10"
        mock_inv.create_sample_template.assert_not_called()

    def test_records_error_on_failure(self, mock_inv, empty_state):
        mock_inv.create_sample_template.side_effect = RuntimeError("boom")
        tmpl = {"id": 1, "globalId": "IT1", "name": "T", "fields": []}
        migrate._import_templates(mock_inv, [tmpl], empty_state, dry_run=False)
        assert len(empty_state.errors) == 1
        assert "IT1" in empty_state.errors[0]
        assert "boom" in empty_state.errors[0]


class TestImportContainersFlat:
    def test_creates_list_container(self, mock_inv, empty_state, tmp_path):
        c = {"id": 100, "globalId": "IC100", "name": "Shelf", "cType": "LIST"}
        mock_inv.create_list_container.return_value = {
            "id": 200,
            "globalId": "IC200",
        }
        migrate._import_containers_flat(mock_inv, [c], empty_state, dry_run=False, snapshot_dir=tmp_path)
        assert empty_state.id_map["IC100"] == "IC200"
        mock_inv.create_list_container.assert_called_once()

    def test_creates_grid_container(self, mock_inv, empty_state, tmp_path):
        c = {
            "id": 100,
            "globalId": "IC100",
            "name": "Rack",
            "cType": "GRID",
            "gridLayout": {"rowsNumber": 8, "columnsNumber": 12},
        }
        mock_inv.create_grid_container.return_value = {"id": 200, "globalId": "IC200"}
        migrate._import_containers_flat(mock_inv, [c], empty_state, dry_run=False, snapshot_dir=tmp_path)
        kwargs = mock_inv.create_grid_container.call_args.kwargs
        assert kwargs["row_count"] == 8
        assert kwargs["column_count"] == 12

    def test_image_container_falls_back_to_list_without_background(
        self, mock_inv, empty_state, tmp_path
    ):
        # No image_containers/ dir present — falls back to LIST.
        c = {"id": 100, "globalId": "IC100", "name": "Map", "cType": "IMAGE"}
        mock_inv.create_list_container.return_value = {"id": 200, "globalId": "IC200"}
        migrate._import_containers_flat(mock_inv, [c], empty_state, dry_run=False, snapshot_dir=tmp_path)
        mock_inv.create_list_container.assert_called_once()
        mock_inv.create_image_container.assert_not_called()


class TestImportContainerHierarchy:
    def test_moves_list_child_into_parent(self, mock_inv, empty_state):
        containers = [
            {
                "globalId": "IC_PARENT",
                "_migration": {"depth": 0, "parent_global_id": None},
            },
            {
                "globalId": "IC_CHILD",
                "_migration": {
                    "depth": 1,
                    "parent_global_id": "IC_PARENT",
                    "parent_grid_col": None,
                    "parent_grid_row": None,
                },
            },
        ]
        empty_state.id_map["IC_PARENT"] = "IC500"
        empty_state.id_map["IC_CHILD"] = "IC501"

        migrate._import_container_hierarchy(mock_inv, containers, empty_state, dry_run=False)
        # parent id resolved via parse_id → 500
        mock_inv.add_items_to_list_container.assert_called_once_with(500, "IC501")

    def test_moves_grid_child_with_coords(self, mock_inv, empty_state):
        containers = [
            {
                "globalId": "IC_PARENT",
                "_migration": {"depth": 0, "parent_global_id": None},
            },
            {
                "globalId": "IC_CHILD",
                "_migration": {
                    "depth": 1,
                    "parent_global_id": "IC_PARENT",
                    "parent_grid_col": 3,
                    "parent_grid_row": 4,
                },
            },
        ]
        empty_state.id_map["IC_PARENT"] = "IC500"
        empty_state.id_map["IC_CHILD"] = "IC501"

        migrate._import_container_hierarchy(mock_inv, containers, empty_state, dry_run=False)
        mock_inv.add_items_to_grid_container.assert_called_once()
        mock_inv.add_items_to_list_container.assert_not_called()


class TestImportSubsamplePlacements:
    def test_skips_workbench_parents(self, mock_inv, empty_state):
        samples = [
            {
                "_migration": {
                    "subsample_locations": [
                        {
                            "subsample_global_id": "SS1",
                            "container_global_id": "BE10",  # Workbench
                        }
                    ]
                }
            }
        ]
        migrate._import_subsample_placements(mock_inv, samples, empty_state, dry_run=False)
        mock_inv.add_items_to_list_container.assert_not_called()
        # Workbench skips don't go into errors (they're summarised as a warning)
        assert empty_state.errors == []


# ---------------------------------------------------------------------------
# End-to-end import on a fixture blob
# ---------------------------------------------------------------------------


def _make_target_inv():
    """Return a mock inv configured to fake successful create_* calls.

    Each create_* returns a fresh new globalId by adding 1000 to the request.
    """
    inv = MagicMock()
    inv.api_key = "test-key"

    counter = {"next": 1000}

    def make_response(name=None, **kwargs):
        n = counter["next"]
        counter["next"] += 1
        return {"id": n, "globalId": f"NEW{n}", "name": name, "fields": [], "subSamples": []}

    inv.create_list_container.side_effect = lambda **kw: {"id": 500 + counter["next"], "globalId": f"IC{500 + counter['next']}", "name": kw.get("name")}

    def fake_create_template(sample_template_post=None, **kw):
        n = counter["next"]
        counter["next"] += 1
        return {"id": n, "globalId": f"IT{n}", "fields": [{"id": n + 1, "name": f["name"]} for f in (sample_template_post or {}).get("fields", [])]}

    def fake_create_sample(name=None, subsample_count=1, **kw):
        n = counter["next"]
        counter["next"] += 1
        return {
            "id": n,
            "globalId": f"SA{n}",
            "name": name,
            "fields": [],
            "subSamples": [
                {"id": n + i + 1, "globalId": f"SS{n + i + 1}"}
                for i in range(subsample_count)
            ],
        }

    inv.create_sample_template.side_effect = fake_create_template
    inv.create_sample.side_effect = fake_create_sample

    def fake_create_list(name=None, **kw):
        n = counter["next"]
        counter["next"] += 1
        return {"id": n, "globalId": f"IC{n}", "name": name}

    inv.create_list_container.side_effect = fake_create_list
    return inv


class TestEndToEndImport:
    def test_simple_blob_imports_cleanly(self, tmp_path):
        """One template, one container, one sample with one subsample → end-to-end."""
        inv = _make_target_inv()
        state = _ImportState()

        templates = [
            {"id": 1, "globalId": "IT1", "name": "T", "fields": []},
        ]
        containers = [
            {
                "id": 10,
                "globalId": "IC10",
                "name": "Box",
                "cType": "LIST",
                "_migration": {"depth": 0, "parent_global_id": None},
            },
        ]
        samples = [
            {
                "id": 100,
                "globalId": "SA100",
                "name": "Reagent",
                "templateId": 1,
                "fields": [],
                "subSamples": [
                    {
                        "id": 1000,
                        "globalId": "SS1000",
                        "name": "tube 1",
                    }
                ],
                "_migration": {
                    "subsample_locations": [
                        {
                            "subsample_global_id": "SS1000",
                            "container_global_id": "IC10",
                            "grid_col": None,
                            "grid_row": None,
                        }
                    ]
                },
            }
        ]

        migrate._import_templates(inv, templates, state, dry_run=False)
        migrate._import_containers_flat(inv, containers, state, dry_run=False, snapshot_dir=tmp_path)
        migrate._import_container_hierarchy(inv, containers, state, dry_run=False)
        migrate._import_samples(inv, samples, state, dry_run=False, templates=templates)
        migrate._import_subsample_placements(inv, samples, state, dry_run=False)

        # All old IDs got mapped
        assert "IT1" in state.id_map
        assert "IC10" in state.id_map
        assert "SA100" in state.id_map
        assert "SS1000" in state.id_map
        # Subsample got placed into the new container
        inv.add_items_to_list_container.assert_called()
        # No errors recorded
        assert state.errors == []


# ---------------------------------------------------------------------------
# Backward compat: legacy plain-JSON snapshot
# ---------------------------------------------------------------------------


class TestResolveInput:
    def test_directory_input(self, tmp_path):
        (tmp_path / "snapshot.json").write_text("{}")
        snap_dir, json_path = migrate._resolve_input(tmp_path)
        assert snap_dir == tmp_path
        assert json_path == tmp_path / "snapshot.json"

    def test_legacy_json_file_input(self, tmp_path):
        json_file = tmp_path / "old.json"
        json_file.write_text("{}")
        snap_dir, json_path = migrate._resolve_input(json_file)
        assert snap_dir == tmp_path
        assert json_path == json_file
