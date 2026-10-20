"""Tests for instrument / instrument-template support in ``rspace migrate export``."""

import json
from unittest.mock import MagicMock

import pytest

from rspacectl.commands import migrate


def _grid_parent(gid="IC10"):
    return {"globalId": gid, "cType": "GRID"}


class TestItemLocation:
    def test_grid_keeps_coords(self):
        inst = {"parentContainers": [_grid_parent()], "parentLocation": {"coordX": 2, "coordY": 3}}
        assert migrate._item_location(inst) == {
            "container_global_id": "IC10",
            "parent_ctype": "GRID",
            "grid_col": 2,
            "grid_row": 3,
        }

    def test_list_and_workbench_drop_coords(self):
        for ctype in ("LIST", "WORKBENCH"):
            inst = {
                "parentContainers": [{"globalId": "X1", "cType": ctype}],
                "parentLocation": {"coordX": 7, "coordY": 1},
            }
            loc = migrate._item_location(inst)
            assert loc["parent_ctype"] == ctype
            assert loc["grid_col"] is None and loc["grid_row"] is None

    def test_no_parent(self):
        assert migrate._item_location({"parentContainers": []}) is None


class TestPreviewImageUrl:
    def test_default_icon_is_not_an_image(self):
        item = {"_links": [{"rel": "image", "link": "http://x/api/inventory/v1/instrumentTemplates/5/icon/-1"}]}
        assert migrate._preview_image_url(item) is None

    def test_custom_image_kept(self):
        url = "http://x/api/inventory/v1/files/image/abc123"
        assert migrate._preview_image_url({"_links": [{"rel": "image", "link": url}]}) == url


class TestTemplateIcon:
    def test_instrument_template_uses_instrument_icon_endpoint(self, tmp_path):
        inv = MagicMock()
        name = migrate._export_template_icon(inv, {"id": 3, "globalId": "NT3", "iconId": 27}, tmp_path)
        assert name == "NT3_icon"
        inv.get_instrument_template_icon.assert_called_once_with(3, 27, str(tmp_path / "NT3_icon"))
        inv.get_sample_template_icon.assert_not_called()

    def test_default_icon_id_skipped(self, tmp_path):
        inv = MagicMock()
        assert migrate._export_template_icon(inv, {"id": 3, "globalId": "NT3", "iconId": -1}, tmp_path) is None
        inv.get_instrument_template_icon.assert_not_called()


class TestWalkContainerFindsInstruments:
    def test_collects_instruments_and_samples(self):
        inv = MagicMock()
        inv.get_container_by_id.return_value = {
            "globalId": "IC1",
            "cType": "LIST",
            "locations": [
                {"content": {"globalId": "IN5", "id": 5, "type": "INSTRUMENT"}},
                {"content": {"globalId": "SS9", "type": "SUBSAMPLE", "sample": {"id": 8}}},
            ],
        }
        samples, instruments = set(), set()
        out = migrate._walk_container(
            inv, {"id": 1}, 0, None, found_sample_ids=samples, found_instrument_ids=instruments
        )
        assert [c["globalId"] for c in out] == ["IC1"]
        assert samples == {8}
        assert instruments == {5}


class TestExportInstruments:
    def test_records_location_and_template_ref(self):
        inv = MagicMock()
        inv.get_instrument_by_id.return_value = {
            "globalId": "IN5",
            "templateId": 3,
            "parentContainers": [_grid_parent()],
            "parentLocation": {"coordX": 1, "coordY": 2},
        }
        instruments, refs = migrate._export_instruments(inv, ids=[5])
        assert refs == {"NT3"}
        assert instruments[0]["_migration"]["location"]["grid_row"] == 2

    def test_marker_index_for_instrument_in_image_container(self):
        containers = [{
            "globalId": "IC7",
            "cType": "IMAGE",
            "locations": [{"coordX": 10, "coordY": 10}, {"coordX": 50, "coordY": 60}],
        }]
        inst = {"_migration": {"location": {
            "container_global_id": "IC7", "parent_ctype": "IMAGE", "grid_col": 50, "grid_row": 60,
        }}}
        migrate._enrich_image_marker_indices(containers, [], [inst])
        assert inst["_migration"]["location"]["marker_index"] == 1


class TestMigrateExportCommand:
    """Selection logic of the export command (files skipped with --no-files)."""

    @pytest.fixture()
    def inv(self, app_context, mock_inv):
        mock_inv.get_sample_by_id.return_value = {"globalId": "SA2", "id": 2, "templateId": 4}
        mock_inv.get_sample_template_by_id.side_effect = lambda i: {"globalId": f"IT{i}", "id": i}
        mock_inv.get_instrument_by_id.side_effect = lambda i: {
            "globalId": f"IN{i}", "id": i, "templateId": 3, "parentContainers": [],
        }
        mock_inv.get_instrument_template_by_id.side_effect = lambda i: {"globalId": f"NT{i}", "id": i}
        mock_inv.get_container_by_id.return_value = {
            "globalId": "IC1", "id": 1, "cType": "LIST",
            "locations": [{"content": {"globalId": "IN6", "id": 6, "type": "INSTRUMENT"}}],
        }
        return mock_inv

    def _export(self, tmp_path, **kwargs):
        args = dict(
            output=tmp_path, all_resources=False, template_ids=None, container_ids=None,
            sample_ids=None, instrument_template_ids=None, instrument_ids=None, no_files=True,
        )
        args.update(kwargs)
        migrate.migrate_export(**args)
        return json.loads((tmp_path / "snapshot.json").read_text())

    def test_sample_only_exports_just_its_template(self, inv, tmp_path):
        blob = self._export(tmp_path, sample_ids=["SA2"])
        inv.list_sample_templates.assert_not_called()
        assert [t["globalId"] for t in blob["templates"]] == ["IT4"]

    def test_instrument_auto_includes_its_template(self, inv, tmp_path):
        blob = self._export(tmp_path, instrument_ids=["IN5"])
        assert blob["schema_version"] == 3
        assert [i["globalId"] for i in blob["instruments"]] == ["IN5"]
        assert [t["globalId"] for t in blob["instrument_templates"]] == ["NT3"]
        inv.list_instrument_templates.assert_not_called()
        assert blob["templates"] == [] and blob["samples"] == []

    def test_container_export_pulls_in_stored_instruments(self, inv, tmp_path):
        blob = self._export(tmp_path, container_ids=["IC1"])
        assert [i["globalId"] for i in blob["instruments"]] == ["IN6"]
        assert [t["globalId"] for t in blob["instrument_templates"]] == ["NT3"]

    def test_explicit_instrument_template(self, inv, tmp_path):
        blob = self._export(tmp_path, instrument_template_ids=["NT9"])
        assert [t["globalId"] for t in blob["instrument_templates"]] == ["NT9"]
        assert blob["instruments"] == []

    def test_requires_a_selection(self, inv, tmp_path):
        import typer

        with pytest.raises(typer.Exit):
            self._export(tmp_path)
