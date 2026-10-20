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

    def test_template_icon_placeholder_is_not_an_image(self):
        item = {"_links": [{"rel": "image", "link": "http://x/api/inventory/v1/instrumentTemplates/377/icon/28"}]}
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


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

from rspacectl.commands.migrate import _ImportState  # noqa: E402


def _nt(gid="NT1", tid=1, desc="d", options=("a", "b"), field_ids=(11, 12)):
    return {
        "id": tid,
        "globalId": gid,
        "name": "Scope",
        "description": desc,
        "iconId": 5,
        "version": 3,
        "created": "2026-01-01",
        "fields": [
            {"id": field_ids[0], "globalId": f"SF{field_ids[0]}", "name": "Serial",
             "type": "string", "mandatory": True, "columnIndex": 1, "content": None},
            {"id": field_ids[1], "globalId": f"SF{field_ids[1]}", "name": "Mode",
             "type": "choice", "columnIndex": 2, "definition": {"options": list(options)}},
        ],
    }


class TestTemplateFingerprint:
    def test_ignores_server_generated_values(self):
        other = _nt(gid="NT9", tid=9, field_ids=(91, 92))
        other.update(version=1, created="2027-02-02", iconId=-1, tags=[{"value": "x"}])
        assert migrate._template_fingerprint(_nt()) == migrate._template_fingerprint(other)

    @pytest.mark.parametrize("change", [
        dict(desc="other"),
        dict(options=("a", "b", "c")),
    ])
    def test_detects_real_differences(self, change):
        assert migrate._template_fingerprint(_nt()) != migrate._template_fingerprint(_nt(**change))


class TestPayloads:
    def test_instrument_template_payload_is_clean(self):
        payload = migrate._instrument_template_payload(_nt())
        assert payload == {
            "name": "Scope",
            "description": "d",
            "fields": [
                {"name": "Serial", "type": "string", "mandatory": True},
                {"name": "Mode", "type": "choice", "definition": {"options": ["a", "b"]}},
            ],
        }

    def test_field_values_follow_new_template_order(self):
        old = [
            {"name": "Mode", "selectedOptions": ["b"]},
            {"name": "Serial", "content": "SN"},
            {"name": "Gone", "content": "x"},
        ]
        new_fields = [
            {"name": "Serial", "columnIndex": 1},
            {"name": "Extra", "columnIndex": 2},
            {"name": "Mode", "columnIndex": 3},
        ]
        assert migrate._instrument_field_values(old, new_fields) == [
            {"content": "SN"}, {}, {"selectedOptions": ["b"]},
        ]

    def test_instrument_payload(self):
        inst = {
            "name": "I", "description": "D", "tags": [{"value": "t"}],
            "fields": [{"name": "Serial", "content": "SN"}],
            "extraFields": [{"id": 1, "globalId": "EF1", "name": "Room", "type": "text", "content": "B2"}],
            "barcodes": [{"id": 4, "data": "BC", "format": "BARCODE", "description": None}],
        }
        payload = migrate._instrument_payload(inst, {"id": 7, "fields": [{"name": "Serial"}]})
        assert payload == {
            "name": "I", "description": "D", "tags": [{"value": "t"}],
            "templateId": 7, "fields": [{"content": "SN"}],
            "extraFields": [{"name": "Room", "type": "text", "content": "B2"}],
            "barcodes": [{"data": "BC", "format": "BARCODE"}],
        }


class TestImportInstrumentTemplates:
    def _inv(self, target_templates):
        inv = MagicMock()
        inv.list_instrument_templates.return_value = {
            "templates": [{"id": t["id"]} for t in target_templates],
            "totalHits": len(target_templates),
        }
        inv.get_instrument_template_by_id.side_effect = {t["id"]: t for t in target_templates}.get
        inv.create_instrument_template.return_value = _nt(gid="NT50", tid=50, field_ids=(501, 502))
        return inv

    def test_reuses_identical_template(self):
        inv = self._inv([_nt(gid="NT8", tid=8, field_ids=(81, 82))])
        state = _ImportState()
        migrate._import_instrument_templates(inv, [_nt()], state, dry_run=False)
        inv.create_instrument_template.assert_not_called()
        assert state.id_map["NT1"] == "NT8"
        assert state.id_map["SF11"] == "81" and state.id_map["SF12"] == "82"
        assert state.reused_templates == ["NT8"]

    def test_lowest_id_wins_among_duplicates(self):
        inv = self._inv([_nt(gid="NT9", tid=9), _nt(gid="NT8", tid=8)])
        state = _ImportState()
        migrate._import_instrument_templates(inv, [_nt()], state, dry_run=False)
        assert state.id_map["NT1"] == "NT8"

    def test_same_name_but_different_creates_and_warns(self, capsys):
        inv = self._inv([_nt(gid="NT8", tid=8, options=("a",))])
        state = _ImportState()
        migrate._import_instrument_templates(inv, [_nt()], state, dry_run=False)
        inv.create_instrument_template.assert_called_once()
        assert state.id_map["NT1"] == "NT50" and state.id_map["SF11"] == "501"
        assert state.reused_templates == []
        assert "differs" in capsys.readouterr().err

    def test_no_reuse_skips_lookup(self):
        inv = self._inv([_nt(gid="NT8", tid=8)])
        state = _ImportState()
        migrate._import_instrument_templates(inv, [_nt()], state, dry_run=False, reuse=False)
        inv.list_instrument_templates.assert_not_called()
        inv.create_instrument_template.assert_called_once()


class TestImportInstruments:
    def test_creates_with_mapped_template(self):
        inv = MagicMock()
        inv.get_instrument_template_by_id.return_value = {"id": 50, "fields": [{"name": "Serial"}]}
        inv.retrieve_api_results.return_value = {"globalId": "IN90", "id": 90}
        state = _ImportState(id_map={"NT1": "NT50"})
        inst = {"globalId": "IN5", "id": 5, "name": "I", "templateId": 1,
                "fields": [{"name": "Serial", "content": "SN"}]}
        migrate._import_instruments(inv, [inst], state, dry_run=False)
        body = inv.retrieve_api_results.call_args.kwargs["params"]
        assert body["templateId"] == 50 and body["fields"] == [{"content": "SN"}]
        assert state.id_map["IN5"] == "IN90" and not state.errors


class TestImportInstrumentPlacements:
    def _inst(self, gid, loc):
        return {"globalId": gid, "_migration": {"location": loc}}

    def _puts(self, inv):
        return [
            (c.args[0], c.kwargs["params"].get("parentContainers"), c.kwargs["params"].get("parentLocation"))
            for c in inv.retrieve_api_results.call_args_list
            if c.kwargs.get("request_type") == "PUT"
        ]

    def test_grid_list_image_and_workbench(self, capsys):
        inv = MagicMock()
        inv.retrieve_api_results.return_value = {"tags": []}
        inv.get_container_by_id.return_value = {"locations": [{"id": 700}, {"id": 701}]}
        state = _ImportState(id_map={
            "IN1": "IN11", "IN2": "IN12", "IN3": "IN13", "IN4": "IN14",
            "IC1": "IC21", "IC2": "IC22", "IC3": "IC23",
        })
        instruments = [
            self._inst("IN1", {"container_global_id": "IC1", "parent_ctype": "GRID", "grid_col": 2, "grid_row": 1}),
            self._inst("IN2", {"container_global_id": "IC2", "parent_ctype": "LIST"}),
            self._inst("IN3", {"container_global_id": "IC3", "parent_ctype": "IMAGE",
                               "grid_col": 5, "grid_row": 5, "marker_index": 1}),
            self._inst("IN4", {"container_global_id": "BE2", "parent_ctype": "WORKBENCH"}),
        ]
        migrate._import_instrument_placements(inv, instruments, state, dry_run=False)
        assert self._puts(inv) == [
            ("/instruments/11", [{"id": 21}], {"coordX": 2, "coordY": 1}),
            ("/instruments/12", [{"id": 22}], None),
            ("/instruments/13", [{"id": 23}], {"id": 701}),
        ]
        assert not state.errors
        assert "workbench" in capsys.readouterr().err

    def test_container_missing_from_snapshot_is_recorded(self):
        inv = MagicMock()
        state = _ImportState(id_map={"IN1": "IN11"})
        loc = {"container_global_id": "IC99", "parent_ctype": "GRID", "grid_col": 1, "grid_row": 1}
        migrate._import_instrument_placements(inv, [self._inst("IN1", loc)], state, dry_run=False)
        assert self._puts(inv) == []
        assert any("IC99" in e for e in state.errors)


class TestImportFilesForInstruments:
    def test_icons_skip_reused_and_use_instrument_endpoint(self, tmp_path):
        (tmp_path / "icons").mkdir()
        for name in ("NT1_icon", "NT2_icon"):
            (tmp_path / "icons" / name).write_bytes(b"png")
        inv = MagicMock()
        state = _ImportState(id_map={"NT1": "NT10", "NT2": "NT20"}, reused_templates=["NT20"])
        nts = [
            {"globalId": "NT1", "_migration": {"icon_local": "NT1_icon"}},
            {"globalId": "NT2", "_migration": {"icon_local": "NT2_icon"}},
        ]
        migrate._import_template_icons(inv, [], state, False, tmp_path, instrument_templates=nts)
        assert [c.args[0] for c in inv.set_instrument_template_icon.call_args_list] == [10]
        inv.set_sample_template_icon.assert_not_called()

    def test_preview_image_keeps_tags_and_skips_reused(self, tmp_path):
        (tmp_path / "images").mkdir()
        for name in ("IN1_preview.png", "NT2_preview.png"):
            (tmp_path / "images" / name).write_bytes(b"png")
        inv = MagicMock()
        inv.retrieve_api_results.return_value = {"tags": [{"value": "keep"}]}
        state = _ImportState(id_map={"IN1": "IN11", "NT2": "NT20"}, reused_templates=["NT20"])
        migrate._import_preview_images(
            inv, [], [], [], state, False, tmp_path,
            instrument_templates=[{"globalId": "NT2", "_migration": {"preview_local": "NT2_preview.png"}}],
            instruments=[{"globalId": "IN1", "_migration": {"preview_local": "IN1_preview.png"}}],
        )
        inv.set_image.assert_not_called()  # the client's set_image clears tags
        (put,) = [c for c in inv.retrieve_api_results.call_args_list if c.kwargs.get("request_type") == "PUT"]
        assert put.args[0] == "/instruments/11"
        assert put.kwargs["params"]["tags"] == [{"value": "keep"}]
        assert put.kwargs["params"]["newBase64Image"].startswith("data:image/png;base64,")

    def test_attachment_uploaded_under_original_name(self, tmp_path):
        (tmp_path / "attachments" / "IN1").mkdir(parents=True)
        (tmp_path / "attachments" / "IN1" / "IF9_manual.pdf").write_bytes(b"%PDF")
        inv = MagicMock()
        state = _ImportState(id_map={"IN1": "IN11"})
        inst = {"globalId": "IN1", "_migration": {"attachments": [
            {"globalId": "IF9", "filename": "manual.pdf", "local": "IF9_manual.pdf"}]}}
        migrate._import_attachments(inv, [], [], [], state, False, tmp_path, instruments=[inst])
        gid, (name, _fh) = inv.upload_attachment.call_args.args
        assert (gid, name) == ("IN11", "manual.pdf")


class TestImportCommand:
    def _snapshot(self, tmp_path, **blob):
        data = {"schema_version": 2, "meta": {}, "templates": [], "containers": [], "samples": []}
        data.update(blob)
        (tmp_path / "snapshot.json").write_text(json.dumps(data))
        return tmp_path

    def _run(self, snap, **kwargs):
        args = dict(
            input_path=snap, dry_run=False, checkpoint_file=None, skip_templates=False,
            skip_containers=False, skip_samples=False, skip_instruments=False,
            skip_files=True, no_reuse_templates=False,
        )
        args.update(kwargs)
        migrate.migrate_import(**args)

    def test_v2_snapshot_imports_without_warning(self, app_context, mock_inv, tmp_path, capsys):
        self._run(self._snapshot(tmp_path))
        err = capsys.readouterr().err
        assert "schema version" not in err
        mock_inv.list_instrument_templates.assert_not_called()

    def test_old_checkpoint_resumes_into_new_phases(self, app_context, mock_inv, tmp_path):
        """A checkpoint from before instruments existed: done phases are skipped,
        the new instrument phases still run."""
        mock_inv.list_instrument_templates.return_value = {"templates": [], "totalHits": 0}
        mock_inv.create_instrument_template.return_value = _nt(gid="NT50", tid=50)
        mock_inv.retrieve_api_results.return_value = {"globalId": "IN90", "id": 90}
        snap = self._snapshot(
            tmp_path, schema_version=3, instrument_templates=[_nt()],
            instruments=[{"globalId": "IN5", "id": 5, "name": "I", "_migration": {"location": None}}],
        )
        (snap / "checkpoint.json").write_text(json.dumps({
            "id_map": {}, "numeric_map": {}, "errors": [],
            "completed_phases": ["templates", "containers_flat", "containers_hierarchy", "samples"],
        }))
        self._run(snap)
        mock_inv.create_sample_template.assert_not_called()
        mock_inv.create_instrument_template.assert_called_once()
        assert mock_inv.retrieve_api_results.call_args.args[0] == "/instruments"

    def test_skip_instruments(self, app_context, mock_inv, tmp_path):
        snap = self._snapshot(tmp_path, schema_version=3, instrument_templates=[_nt()])
        self._run(snap, skip_instruments=True)
        mock_inv.list_instrument_templates.assert_not_called()
        mock_inv.create_instrument_template.assert_not_called()


class TestImageContainerImport:
    """Markers go in the POST; a follow-up PUT (which clears tags) is only a fallback."""

    def _snapshot_container(self, tmp_path):
        ic_dir = tmp_path / "image_containers"
        ic_dir.mkdir()
        (ic_dir / "IC5_background.png").write_bytes(b"\x89PNG")
        return {
            "id": 5, "globalId": "IC5", "name": "Map", "cType": "IMAGE",
            "tags": [{"value": "freezer"}],
            "_migration": {"image_container": {
                "background_local": "IC5_background.png",
                "locations": [{"coordX": 10, "coordY": 10}, {"coordX": 40, "coordY": 60}],
            }},
        }

    def _inv(self, persisted):
        inv = MagicMock()
        inv.create_image_container.return_value = {"id": 50, "globalId": "IC50"}
        inv.get_container_by_id.return_value = {
            "locations": [{"coordX": x, "coordY": y} for x, y in persisted]
        }
        return inv

    def test_markers_in_post_no_put(self, tmp_path):
        inv = self._inv([(10, 10), (40, 60)])
        state = _ImportState()
        migrate._import_containers_flat(inv, [self._snapshot_container(tmp_path)], state, False, tmp_path)
        post = inv.create_image_container.call_args.args[0]
        assert [(loc["coordX"], loc["coordY"]) for loc in post.data["locations"]] == [(10, 10), (40, 60)]
        inv.add_locations_to_image_container.assert_not_called()
        assert state.id_map["IC5"] == "IC50" and not state.errors

    def test_fallback_adds_missing_markers_and_restores_tags(self, tmp_path):
        inv = self._inv([(10, 10)])
        state = _ImportState()
        migrate._import_containers_flat(inv, [self._snapshot_container(tmp_path)], state, False, tmp_path)
        assert inv.add_locations_to_image_container.call_args.args[1:] == ((40, 60),)
        put = inv.retrieve_api_results.call_args
        assert put.args[0] == "/containers/50"
        assert put.kwargs["params"] == {"tags": [{"value": "freezer"}]}
