"""Tests for instrument commands (list, get, search, create, update, delete, tag, move)."""

import pytest
import typer
from rspace_client.inv.inv import ResultType

from rspacectl.commands import create, delete, get_cmd, list_cmd, move, search, tag, update
from rspacectl.ids import parse_id, resource_type
from rspacectl.output import _get_nested
from rspacectl.utils import free_grid_cells, instrument_location, put_inventory_item

INSTRUMENT = {
    "id": 32768,
    "globalId": "IN32768",
    "name": "Confocal microscope",
    "type": "INSTRUMENT",
    "created": "2026-07-03T14:34:40.000Z",
    "owner": {"username": "alice"},
    "parentContainers": [{"id": 5963780, "globalId": "BE5963780"}],
    "fields": [
        {"name": "Serial number", "type": "string", "content": "SN-1234"},
        {"name": "Mode", "type": "choice", "content": None, "selectedOptions": ["confocal"]},
    ],
    "extraFields": [{"name": "Room", "type": "text", "content": "B.204"}],
}

INSTRUMENT_TEMPLATE = {
    "id": 98314,
    "globalId": "NT98314",
    "name": "Instrument (PIDINST 1.0)",
    "type": "INSTRUMENT_TEMPLATE",
    "version": 1,
    "fields": [{"name": "Manufacturer", "type": "string"}],
}


def _list_kwargs(**overrides):
    """Explicit defaults: calling a Typer command directly skips option parsing."""
    kwargs = dict(
        query=None,
        owned_by=None,
        deleted=False,
        page=0,
        page_size=20,
        order_by=None,
        sort_order="asc",
    )
    kwargs.update(overrides)
    return kwargs


class TestIds:
    def test_instrument_prefix(self):
        assert resource_type("IN123") == "instrument"
        assert parse_id("IN123") == 123

    def test_instrument_template_prefix(self):
        assert resource_type("NT456") == "instrument-template"
        assert parse_id("NT456") == 456


class TestNestedListIndex:
    def test_indexes_into_list(self):
        assert _get_nested(INSTRUMENT, "parentContainers.0.globalId") == "BE5963780"

    def test_out_of_range_is_empty(self):
        assert _get_nested({"parentContainers": []}, "parentContainers.0.globalId") == ""

    def test_non_numeric_segment_is_empty(self):
        assert _get_nested(INSTRUMENT, "parentContainers.x.globalId") == ""


class TestListInstruments:
    def test_lists_with_filter(self, app_context, mock_inv, capsys):
        mock_inv.list_instruments.return_value = {"instruments": [INSTRUMENT], "totalHits": 1}
        list_cmd.list_instruments(**_list_kwargs(owned_by="alice", deleted=True))

        kwargs = mock_inv.list_instruments.call_args.kwargs
        assert kwargs["search_filter"].data["ownedBy"] == "alice"
        assert kwargs["search_filter"].data["deletedItems"] == "INCLUDE"
        out = capsys.readouterr().out
        assert "IN32768" in out
        assert "BE5963780" in out

    def test_query_uses_instrument_search(self, app_context, mock_inv):
        mock_inv.retrieve_api_results.return_value = {"records": [INSTRUMENT], "totalHits": 1}
        list_cmd.list_instruments(**_list_kwargs(query="microscope"))

        path = mock_inv.retrieve_api_results.call_args.args[0]
        params = mock_inv.retrieve_api_results.call_args.kwargs["params"]
        assert path == "/search"
        assert params["query"] == "microscope"
        assert params["resultType"] == ResultType.INSTRUMENT.name
        mock_inv.list_instruments.assert_not_called()

    def test_lists_instrument_templates(self, app_context, mock_inv, capsys):
        mock_inv.list_instrument_templates.return_value = {
            "templates": [INSTRUMENT_TEMPLATE],
            "totalHits": 1,
        }
        list_cmd.list_instrument_templates(page=0, page_size=20)

        mock_inv.list_instrument_templates.assert_called_once()
        assert "NT98314" in capsys.readouterr().out


class TestGetInstrument:
    def test_infers_instrument_from_prefix(self, app_context, mock_inv, capsys):
        mock_inv.get_instrument_by_id.return_value = INSTRUMENT
        get_cmd.get("IN32768", None, subsamples=False, content=False)

        mock_inv.get_instrument_by_id.assert_called_once_with(32768)
        out = capsys.readouterr().out
        assert "Confocal microscope" in out
        assert "SN-1234" in out
        assert "confocal" in out
        assert "B.204" in out

    def test_infers_instrument_template_from_prefix(self, app_context, mock_inv, capsys):
        mock_inv.get_instrument_template_by_id.return_value = INSTRUMENT_TEMPLATE
        get_cmd.get("NT98314", None, subsamples=False, content=False)

        mock_inv.get_instrument_template_by_id.assert_called_once_with(98314)
        assert "Manufacturer" in capsys.readouterr().out

    @pytest.mark.parametrize("alias", ["instrument", "inst", "instr"])
    def test_explicit_type_with_numeric_id(self, app_context, mock_inv, alias):
        mock_inv.get_instrument_by_id.return_value = INSTRUMENT
        get_cmd.get(alias, "32768", subsamples=False, content=False)
        mock_inv.get_instrument_by_id.assert_called_once_with(32768)

    def test_explicit_instrument_template_type(self, app_context, mock_inv):
        mock_inv.get_instrument_template_by_id.return_value = INSTRUMENT_TEMPLATE
        get_cmd.get("instrument-template", "98314", subsamples=False, content=False)
        mock_inv.get_instrument_template_by_id.assert_called_once_with(98314)


class TestSearchInstruments:
    def test_type_instruments(self, app_context, mock_inv, mock_eln):
        mock_inv.search.return_value = {"records": [INSTRUMENT]}
        search.search("microscope", type="instruments", page=0, page_size=20)

        assert mock_inv.search.call_args.kwargs["result_type"] is ResultType.INSTRUMENT
        mock_eln.get_documents.assert_not_called()

    def test_invalid_type_still_rejected(self, app_context):
        with pytest.raises(typer.Exit):
            search.search("x", type="instrument", page=0, page_size=20)


# ---------------------------------------------------------------------------
# Write commands
# ---------------------------------------------------------------------------

GRID_2x2 = {
    "id": 854,
    "globalId": "IC854",
    "cType": "GRID",
    "canStoreInstruments": True,
    "gridLayout": {"rowsNumber": 2, "columnsNumber": 2},
    # (row 1, col 1) is taken
    "locations": [{"coordX": 1, "coordY": 1, "content": {"globalId": "SS1"}}],
}
LIST = {"id": 855, "globalId": "IC855", "cType": "LIST", "canStoreInstruments": True}


def _puts(mock_inv):
    """(path, body) of every PUT sent through retrieve_api_results."""
    return [
        (c.args[0], c.kwargs["params"])
        for c in mock_inv.retrieve_api_results.call_args_list
        if c.kwargs.get("request_type") == "PUT"
    ]


class TestPlacementHelpers:
    def test_location_list(self):
        assert instrument_location(5) == {"parentContainers": [{"id": 5}]}

    def test_location_grid_maps_row_col_to_y_x(self):
        body = instrument_location(5, row=2, col=1)
        assert body["parentLocation"] == {"coordX": 1, "coordY": 2}

    def test_free_cells_by_row_skips_occupied(self):
        assert free_grid_cells(GRID_2x2) == [(1, 2), (2, 1), (2, 2)]

    def test_free_cells_by_column(self):
        assert free_grid_cells(GRID_2x2, by_column=True) == [(2, 1), (1, 2), (2, 2)]

    def test_free_cells_from_start(self):
        assert free_grid_cells(GRID_2x2, start_row=2, start_col=1) == [(2, 1), (2, 2)]


class TestPutPreservesTags:
    """The API clears tags on any PUT that omits them; the helper must re-send them."""

    def test_resends_current_tags(self, mock_inv):
        mock_inv.retrieve_api_results.side_effect = [{"tags": [{"value": "keep"}]}, {"ok": 1}]
        put_inventory_item(mock_inv, "instruments", 7, {"description": "x"})
        path, body = _puts(mock_inv)[0]
        assert path == "/instruments/7"
        assert body == {"description": "x", "tags": [{"value": "keep"}]}

    def test_explicit_tags_skip_lookup(self, mock_inv):
        put_inventory_item(mock_inv, "samples", 7, {"tags": []})
        assert mock_inv.retrieve_api_results.call_count == 1


class TestCreateInstrument:
    def _kwargs(self, **overrides):
        kwargs = dict(
            name="Confocal", template=None, field=None, tag=None, description=None,
            target=None, row=None, col=None,
        )
        kwargs.update(overrides)
        return kwargs

    def test_posts_template_fields_and_grid_location(self, app_context, mock_inv):
        mock_inv.get_instrument_template_by_id.return_value = {
            "fields": [
                {"name": "Serial", "type": "string", "mandatory": True},
                {"name": "Mode", "type": "choice"},
                {"name": "Power", "type": "number"},
            ]
        }
        mock_inv.retrieve_api_results.return_value = {"globalId": "IN1"}
        create.create_instrument(**self._kwargs(
            template="NT9", field=["Serial=SN-1", "Mode=a,b"], tag="lab",
            target="IC854", row=2, col=1,
        ))
        call = mock_inv.retrieve_api_results.call_args
        assert call.args[0] == "/instruments"
        body = call.kwargs["params"]
        assert body["templateId"] == 9
        assert body["fields"] == [{"content": "SN-1"}, {"selectedOptions": ["a", "b"]}, {}]
        assert body["parentContainers"] == [{"id": 854}]
        assert body["parentLocation"] == {"coordX": 1, "coordY": 2}

    def test_missing_mandatory_field_exits(self, app_context, mock_inv):
        mock_inv.get_instrument_template_by_id.return_value = {
            "fields": [{"name": "Serial", "type": "string", "mandatory": True}]
        }
        with pytest.raises(typer.Exit):
            create.create_instrument(**self._kwargs(template="NT9"))
        mock_inv.retrieve_api_results.assert_not_called()

    @pytest.mark.parametrize("overrides", [
        {"row": 1},                             # row without col
        {"row": 1, "col": 1},                   # cell without --target
        {"field": ["Serial=1"]},                # --field without --template
    ])
    def test_invalid_option_combinations_exit(self, app_context, mock_inv, overrides):
        with pytest.raises(typer.Exit):
            create.create_instrument(**self._kwargs(**overrides))
        mock_inv.retrieve_api_results.assert_not_called()

    def test_create_instrument_template(self, app_context, mock_inv, tmp_path):
        f = tmp_path / "nt.json"
        f.write_text('{"name": "T", "fields": []}')
        mock_inv.create_instrument_template.return_value = {"globalId": "NT1"}
        create.create_instrument_template(from_file=f)
        mock_inv.create_instrument_template.assert_called_once_with(
            instrument_template_post={"name": "T", "fields": []}
        )


class TestUpdateInstrument:
    def _kwargs(self, **overrides):
        kwargs = dict(name=None, description=None, tag=None, field=None, latest_template=False)
        kwargs.update(overrides)
        return kwargs

    def test_single_put_with_name_fields_and_preserved_tags(self, app_context, mock_inv):
        current = {
            "tags": [{"value": "keep"}],
            "fields": [{"id": 11, "name": "Serial", "type": "string"},
                       {"id": 12, "name": "Mode", "type": "radio"}],
        }
        mock_inv.get_instrument_by_id.return_value = current
        mock_inv.retrieve_api_results.side_effect = [current, {"globalId": "IN5"}]
        update.update_instrument("IN5", **self._kwargs(name="New", field=["Serial=S2", "Mode=x"]))

        mock_inv.rename.assert_not_called()  # rename alone would wipe tags
        (path, body), = _puts(mock_inv)
        assert path == "/instruments/5"
        assert body["name"] == "New"
        assert body["tags"] == [{"value": "keep"}]
        assert body["fields"] == [{"id": 11, "content": "S2"}, {"id": 12, "selectedOptions": ["x"]}]

    def test_unknown_field_exits(self, app_context, mock_inv):
        mock_inv.get_instrument_by_id.return_value = {"fields": [{"id": 1, "name": "A"}]}
        with pytest.raises(typer.Exit):
            update.update_instrument("IN5", **self._kwargs(field=["B=1"]))

    def test_nothing_to_update_exits(self, app_context):
        with pytest.raises(typer.Exit):
            update.update_instrument("IN5", **self._kwargs())

    def test_latest_template_only(self, app_context, mock_inv):
        mock_inv.update_instrument_to_latest_template_version.return_value = {"globalId": "IN5"}
        update.update_instrument("IN5", **self._kwargs(latest_template=True))
        mock_inv.update_instrument_to_latest_template_version.assert_called_once_with(5)
        assert _puts(mock_inv) == []

    def test_propagate_reports_errors(self, app_context, mock_inv):
        mock_inv.update_instrument_template_instruments.return_value = {
            "results": [{"record": {"globalId": "IN1"}}, {"error": "locked"}]
        }
        with pytest.raises(typer.Exit):
            update.update_instrument_template("NT3", propagate=True)
        mock_inv.update_instrument_template_instruments.assert_called_once_with(3)

    def test_update_sample_keeps_tags(self, app_context, mock_inv):
        mock_inv.retrieve_api_results.side_effect = [{"tags": [{"value": "t"}]}, {"globalId": "SA1"}]
        update.update_sample("SA1", name="n", description="d", tag=None)
        mock_inv.rename.assert_not_called()
        (_, body), = _puts(mock_inv)
        assert body == {"name": "n", "description": "d", "tags": [{"value": "t"}]}


class TestMoveInstruments:
    def _move(self, ids, target, strategy=None, row=None, col=None):
        move.move(ids, target=target, strategy=strategy, row=row, col=col)

    def _placed(self, mock_inv):
        return [
            (path, body.get("parentContainers"), body.get("parentLocation"))
            for path, body in _puts(mock_inv)
        ]

    def test_list_container(self, app_context, mock_inv):
        mock_inv.get_container_by_id.return_value = LIST
        self._move(["IN1", "IN2"], "IC855")
        assert self._placed(mock_inv) == [
            ("/instruments/1", [{"id": 855}], None),
            ("/instruments/2", [{"id": 855}], None),
        ]
        mock_inv.add_items_to_list_container.assert_not_called()

    def test_grid_fills_free_cells(self, app_context, mock_inv):
        mock_inv.get_container_by_id.return_value = GRID_2x2
        self._move(["IN1", "IN2"], "IC854", strategy="row")
        assert [loc for _, _, loc in self._placed(mock_inv)] == [
            {"coordX": 2, "coordY": 1},
            {"coordX": 1, "coordY": 2},
        ]

    def test_grid_exact_cell(self, app_context, mock_inv):
        mock_inv.get_container_by_id.return_value = GRID_2x2
        self._move(["IN1"], "IC854", row=2, col=2)
        assert self._placed(mock_inv)[0][2] == {"coordX": 2, "coordY": 2}

    @pytest.mark.parametrize("ids,kwargs", [
        (["IN1", "IN2"], {"row": 1, "col": 2}),          # exact cell for several
        (["IN1", "IN2", "IN3", "IN4"], {"strategy": "row"}),  # only 3 free cells
        (["IN1", "SS9"], {}),                            # mixed items into a grid
    ])
    def test_grid_rejections(self, app_context, mock_inv, ids, kwargs):
        mock_inv.get_container_by_id.return_value = GRID_2x2
        with pytest.raises(typer.Exit):
            self._move(ids, "IC854", **kwargs)
        assert _puts(mock_inv) == []

    def test_container_that_cannot_store_instruments(self, app_context, mock_inv):
        mock_inv.get_container_by_id.return_value = {**LIST, "canStoreInstruments": False}
        with pytest.raises(typer.Exit):
            self._move(["IN1"], "IC855")

    def test_mixed_list_routes_both_paths(self, app_context, mock_inv):
        mock_inv.get_container_by_id.return_value = LIST
        mock_inv.add_items_to_list_container.return_value = None
        self._move(["IN1", "SS9"], "IC855")
        assert self._placed(mock_inv) == [("/instruments/1", [{"id": 855}], None)]
        mock_inv.add_items_to_list_container.assert_called_once_with(855, "SS9")


class TestDeleteAndTag:
    def test_delete_instruments(self, app_context, mock_inv):
        delete.delete_instrument(["IN1", "2"])
        assert [c.args for c in mock_inv.doDelete.call_args_list] == [
            ("instruments", 1), ("instruments", 2)
        ]

    def test_delete_instrument_template(self, app_context, mock_inv):
        delete.delete_instrument_template(["NT3"])
        mock_inv.doDelete.assert_called_once_with("instrumentTemplates", 3)

    def test_delete_container_uses_relative_path(self, app_context, mock_inv):
        # doDelete prepends "/", so a leading slash produced //containers (404).
        delete.delete_container(["IC7"])
        mock_inv.doDelete.assert_called_once_with("containers", 7)

    @pytest.mark.parametrize("gid,endpoint", [
        ("IN4", "/instruments/4"),
        ("NT5", "/instrumentTemplates/5"),
    ])
    def test_tag_endpoints(self, app_context, mock_inv, gid, endpoint):
        mock_inv.retrieve_api_results.return_value = {"globalId": gid, "tags": []}
        tag.tag(gid, "a,b")
        assert mock_inv.retrieve_api_results.call_args.args[0] == endpoint
