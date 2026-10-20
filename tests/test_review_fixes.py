"""Tests for fixes from the RSpace 2.27 API / rspace-client review.

C4: explicit type + mismatching GlobalID prefix (get, delete)
C5: split passes a GlobalID and restores tags cleared by the bulk UPDATE
C6: create sample --expiry sends a date, not a datetime
"""

import datetime

import pytest
import typer
from rspace_client.inv.inv import BulkOperationResult, ByLocation, SamplePost

from rspacectl.commands import create, delete, get_cmd, list_cmd, move, split
from rspacectl.commands.list_cmd import _inv_pagination
from rspacectl.ids import check_id_type

TAGS = [{"value": "sst"}, {"value": "keep"}]


# ---------------------------------------------------------------------------
# C4: prefix / type mismatch
# ---------------------------------------------------------------------------


class TestCheckIdType:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("SS905", "subsample"),
            ("905", "subsample"),
            (905, "sample"),
            ("ss905", "subsample"),
            ("BE1", "container"),
            ("IC1", "bench"),
            ("NB3", "folder"),
            ("IN7", "instrument"),
            ("NT8", "instrument-template"),
        ],
    )
    def test_accepts_matching_or_numeric(self, value, expected):
        check_id_type(value, expected)

    @pytest.mark.parametrize(
        "value, expected",
        [("SA905", "subsample"), ("SS905", "sample"), ("SA905", "instrument"), ("XX1", "sample")],
    )
    def test_rejects_mismatch(self, value, expected):
        with pytest.raises(ValueError, match=value):
            check_id_type(value, expected)

    def test_message_names_both_types(self):
        with pytest.raises(ValueError, match="is a sample ID, not an instrument ID"):
            check_id_type("SA905", "instrument")


class TestGetTypeMismatch:
    def test_subsample_with_sample_id_exits(self, app_context, mock_inv):
        with pytest.raises(typer.Exit):
            get_cmd.get("subsample", "SA905", subsamples=False, content=False)
        mock_inv.get_subsample_by_id.assert_not_called()

    def test_alias_is_resolved_before_check(self, app_context, mock_inv):
        with pytest.raises(typer.Exit):
            get_cmd.get("sub", "SA905", subsamples=False, content=False)
        mock_inv.get_subsample_by_id.assert_not_called()

    def test_matching_prefix_still_works(self, app_context, mock_inv):
        mock_inv.get_subsample_by_id.return_value = {"id": 905, "globalId": "SS905"}
        get_cmd.get("subsample", "SS905", subsamples=False, content=False)
        mock_inv.get_subsample_by_id.assert_called_once_with(905)

    def test_numeric_id_unchecked(self, app_context, mock_inv):
        mock_inv.get_sample_by_id.return_value = {"id": 905, "globalId": "SA905"}
        get_cmd.get("sample", "905", subsamples=False, content=False)
        mock_inv.get_sample_by_id.assert_called_once_with(905)

    def test_container_id_with_bench_type(self, app_context, mock_inv):
        mock_inv.get_container_by_id.return_value = {"id": 1, "globalId": "IC1"}
        get_cmd.get("bench", "IC1", subsamples=False, content=False)
        mock_inv.get_container_by_id.assert_called_once_with(1, include_content=False)


class TestDeleteTypeMismatch:
    def test_mismatch_deletes_nothing(self, app_context, mock_inv):
        with pytest.raises(typer.Exit):
            delete.delete_instrument(["IN1", "SA905"])
        mock_inv.doDelete.assert_not_called()

    def test_matching_ids_are_deleted(self, app_context, mock_inv):
        delete.delete_instrument(["IN1", "2"])
        assert [c.args for c in mock_inv.doDelete.call_args_list] == [
            ("instruments", 1),
            ("instruments", 2),
        ]

    def test_container_rejects_sample(self, app_context, mock_inv):
        with pytest.raises(typer.Exit):
            delete.delete_container(["SA5"])
        mock_inv.doDelete.assert_not_called()

    def test_document_rejects_form(self, app_context, mock_eln):
        with pytest.raises(typer.Exit):
            delete.delete_document(["FM5"])
        mock_eln.doDelete.assert_not_called()


# ---------------------------------------------------------------------------
# C5: split
# ---------------------------------------------------------------------------


def _bulk(*records):
    return BulkOperationResult(
        {"status": "COMPLETED", "results": [{"record": r, "error": None} for r in records]}
    )


class TestSplit:
    def test_even_split_passes_global_id(self, app_context, mock_inv):
        mock_inv.split_subsample.return_value = [{"id": 2, "globalId": "SS2"}]
        split.split("SS1", count=1, quantity=None)
        mock_inv.split_subsample.assert_called_once_with(
            subsample="SS1", num_new_subsamples=1, quantity_per_subsample=None
        )
        mock_inv.get_subsample_by_id.assert_not_called()

    def test_numeric_id_becomes_global_id(self, app_context, mock_inv):
        mock_inv.split_subsample.return_value = []
        split.split("1608", count=2, quantity=None)
        assert mock_inv.split_subsample.call_args.kwargs["subsample"] == "SS1608"

    @pytest.mark.parametrize("bad", ["SA1608", "IC5", "not-an-id"])
    def test_wrong_prefix_exits_before_split(self, app_context, mock_inv, bad):
        with pytest.raises(typer.Exit):
            split.split(bad, count=2, quantity=1.0)
        mock_inv.split_subsample.assert_not_called()

    @pytest.mark.parametrize("count, quantity", [(0, None), (2, 0.0), (2, -1.0)])
    def test_invalid_numbers_exit(self, app_context, mock_inv, count, quantity):
        with pytest.raises(typer.Exit):
            split.split("SS1", count=count, quantity=quantity)
        mock_inv.split_subsample.assert_not_called()

    def test_quantity_split_restores_tags(self, app_context, mock_inv, capsys):
        original = {"id": 1, "globalId": "SS1", "tags": TAGS}
        mock_inv.get_subsample_by_id.return_value = original
        mock_inv.split_subsample.return_value = _bulk(
            *({"id": i, "globalId": f"SS{i}", "tags": []} for i in (1, 2, 3))
        )
        mock_inv.retrieve_api_results.side_effect = lambda path, **kw: {
            "status": "COMPLETED",
            "results": [
                {"record": {**r, "globalId": f"SS{r['id']}"}, "error": None}
                for r in kw["params"]["records"]
            ],
        }

        split.split("SS1", count=2, quantity=0.1)

        # the fetched record is handed to the client, so it doesn't GET again
        mock_inv.get_subsample_by_id.assert_called_once_with(1)
        assert mock_inv.split_subsample.call_args.kwargs["subsample"] is original
        mock_inv.retrieve_api_results.assert_called_once()
        call = mock_inv.retrieve_api_results.call_args
        assert call.args[0] == "/bulk" and call.kwargs["request_type"] == "POST"
        assert call.kwargs["params"] == {
            "operationType": "UPDATE",
            "records": [{"type": "SUBSAMPLE", "id": i, "tags": TAGS} for i in (1, 2, 3)],
        }
        assert "Created 2 new subsample(s)" in capsys.readouterr().out

    def test_quantity_split_without_tags_skips_restore(self, app_context, mock_inv):
        mock_inv.get_subsample_by_id.return_value = {"id": 1, "globalId": "SS1", "tags": []}
        mock_inv.split_subsample.return_value = _bulk({"id": 1}, {"id": 2})
        split.split("SS1", count=1, quantity=0.1)
        mock_inv.retrieve_api_results.assert_not_called()

    def test_help_documents_unit_caveat(self):
        assert "rescale" in split.split.__doc__


# ---------------------------------------------------------------------------
# C6: create sample --expiry
# ---------------------------------------------------------------------------


def _sample_kwargs(**overrides):
    kwargs = dict(
        name="rspacectl-review-exp",
        template=None,
        field=None,
        quantity=None,
        unit=None,
        expiry=None,
        tag=None,
        description=None,
        subsample_count=None,
        from_csv=None,
    )
    kwargs.update(overrides)
    return kwargs


class TestCreateSampleExpiry:
    def test_passes_date_not_datetime(self, app_context, mock_inv):
        mock_inv.create_sample.return_value = {"globalId": "SA1"}
        create.create_sample(**_sample_kwargs(expiry="2027-01-02"))
        expiry = mock_inv.create_sample.call_args.kwargs["expiry_date"]
        assert type(expiry) is datetime.date
        assert expiry == datetime.date(2027, 1, 2)

    def test_client_serialises_date_only(self):
        post = SamplePost("x", expiry_date=datetime.date(2027, 1, 2))
        assert post.data["expiryDate"] == "2027-01-02"

    @pytest.mark.parametrize("bad", ["2027-13-01", "tomorrow", "02/01/2027"])
    def test_invalid_expiry_exits_before_api_call(self, app_context, mock_inv, bad):
        with pytest.raises(typer.Exit):
            create.create_sample(**_sample_kwargs(expiry=bad))
        mock_inv.create_sample.assert_not_called()

    def test_no_expiry_passes_none(self, app_context, mock_inv):
        mock_inv.create_sample.return_value = {"globalId": "SA1"}
        create.create_sample(**_sample_kwargs())
        assert mock_inv.create_sample.call_args.kwargs["expiry_date"] is None


class TestCreateSampleQuantity:
    def test_quantity_and_unit_build_client_quantity(self, app_context, mock_inv):
        mock_inv.create_sample.return_value = {"globalId": "SA1"}
        create.create_sample(**_sample_kwargs(quantity=1.5, unit=3))
        qty = mock_inv.create_sample.call_args.kwargs["total_quantity"]
        assert qty._toDict() == {"numericValue": 1.5, "unitId": 3}


# ---------------------------------------------------------------------------
# Found by the live end-to-end run (scripts/e2e_review_fixes.py)
# ---------------------------------------------------------------------------


class TestDeleteDocumentEndpoint:
    def test_uses_documents_endpoint_without_double_slash(self, app_context, mock_eln):
        delete.delete_document(["SD5"])
        mock_eln.doDelete.assert_called_once_with("documents", 5)


class TestGetWorkbench:
    BENCH = {"id": 2, "globalId": "BE2", "name": "WB alice", "cType": "WORKBENCH"}

    @pytest.mark.parametrize("args", [("BE2", None), ("container", "BE2"), ("bench", "BE2")])
    def test_be_prefix_uses_workbench_endpoint(self, app_context, mock_inv, args):
        mock_inv.retrieve_api_results.return_value = self.BENCH
        get_cmd.get(*args, subsamples=False, content=False)
        mock_inv.retrieve_api_results.assert_called_once_with("/workbenches/2?includeContent=false")
        mock_inv.get_container_by_id.assert_not_called()

    def test_content_flag_is_passed(self, app_context, mock_inv):
        mock_inv.retrieve_api_results.return_value = self.BENCH
        get_cmd.get("BE2", None, subsamples=False, content=True)
        mock_inv.retrieve_api_results.assert_called_once_with("/workbenches/2?includeContent=true")

    def test_numeric_container_id_falls_back_to_workbench(self, app_context, mock_inv):
        err = Exception("Container with id 2 is a workbench")
        err.response_status_code = 422
        mock_inv.get_container_by_id.side_effect = err
        mock_inv.retrieve_api_results.return_value = self.BENCH
        get_cmd.get("container", "2", subsamples=False, content=False)
        mock_inv.retrieve_api_results.assert_called_once_with("/workbenches/2?includeContent=false")

    def test_ic_prefix_does_not_fall_back(self, app_context, mock_inv):
        err = Exception("Container with id 2 is a workbench")
        err.response_status_code = 422
        mock_inv.get_container_by_id.side_effect = err
        with pytest.raises(typer.Exit):
            get_cmd.get("IC2", None, subsamples=False, content=False)
        mock_inv.retrieve_api_results.assert_not_called()

    def test_other_container_errors_are_reported(self, app_context, mock_inv):
        mock_inv.get_container_by_id.side_effect = Exception("not found")
        with pytest.raises(typer.Exit):
            get_cmd.get("container", "2", subsamples=False, content=False)
        mock_inv.retrieve_api_results.assert_not_called()


# ---------------------------------------------------------------------------
# Found by the live instrument run (scripts/e2e_instruments.py)
# ---------------------------------------------------------------------------


class TestInventoryOrderBy:
    @pytest.mark.parametrize(
        "given, sent",
        [("created", "creationDate"), ("lastModified", "modificationDate"), ("name", "name")],
    )
    def test_cli_names_are_mapped(self, given, sent):
        assert _inv_pagination(0, 20, given, "desc").data["orderBy"] == f"{sent} desc"


class TestListQueryFilters:
    def _kwargs(self, **overrides):
        kwargs = dict(
            query="x",
            owned_by=None,
            deleted=False,
            page=0,
            page_size=20,
            order_by=None,
            sort_order="asc",
        )
        kwargs.update(overrides)
        return kwargs

    @pytest.mark.parametrize("command", ["list_samples", "list_instruments"])
    def test_query_passes_deleted_and_owner(self, app_context, mock_inv, command):
        mock_inv.retrieve_api_results.return_value = {"records": [], "totalHits": 0}
        getattr(list_cmd, command)(**self._kwargs(deleted=True, owned_by="alice"))
        params = mock_inv.retrieve_api_results.call_args.kwargs["params"]
        assert params["deletedItems"] == "INCLUDE"
        assert params["ownedBy"] == "alice"

    def test_query_without_filters_sends_none(self, app_context, mock_inv):
        mock_inv.retrieve_api_results.return_value = {"records": [], "totalHits": 0}
        list_cmd.list_samples(**self._kwargs())
        params = mock_inv.retrieve_api_results.call_args.kwargs["params"]
        assert params["deletedItems"] == "EXCLUDE" and "ownedBy" not in params


class TestMoveTargets:
    def test_subsample_into_list_container(self, app_context, mock_inv):
        mock_inv.get_container_by_id.return_value = {"cType": "LIST", "gridLayout": None}
        move.move(["SS1"], target="IC5", strategy=None, row=None, col=None)
        mock_inv.add_items_to_list_container.assert_called_once_with(5, "SS1")

    def test_instrument_onto_workbench(self, app_context, mock_inv):
        mock_inv.retrieve_api_results.side_effect = lambda path, **kw: (
            {"id": 2, "globalId": "BE2", "cType": "WORKBENCH"}
            if path.startswith("/workbenches/")
            else {"tags": []}
        )
        move.move(["IN7"], target="BE2", strategy=None, row=None, col=None)
        paths = [c.args[0] for c in mock_inv.retrieve_api_results.call_args_list]
        assert paths[0] == "/workbenches/2?includeContent=true"
        put = mock_inv.retrieve_api_results.call_args_list[-1]
        assert put.args[0] == "/instruments/7"
        assert put.kwargs["params"]["parentContainers"] == [{"id": 2}]
        mock_inv.get_container_by_id.assert_not_called()

    def test_subsample_into_grid_cell(self, app_context, mock_inv):
        mock_inv.get_container_by_id.return_value = {
            "cType": "GRID",
            "gridLayout": {"columnsNumber": 3, "rowsNumber": 2},
        }
        move.move(["SS1"], target="IC5", strategy=None, row=2, col=1)
        placement = mock_inv.add_items_to_grid_container.call_args.kwargs["grid_placement"]
        assert isinstance(placement, ByLocation)
        assert (placement.locations[0].x, placement.locations[0].y) == (1, 2)

    @pytest.mark.parametrize("ids", [["SS1"], ["IN1"]])
    def test_non_container_target_exits(self, app_context, mock_inv, ids):
        with pytest.raises(typer.Exit):
            move.move(ids, target="SA5", strategy=None, row=None, col=None)
        mock_inv.get_container_by_id.assert_not_called()
        mock_inv.retrieve_api_results.assert_not_called()


class TestDeleteReportsRefusals:
    def test_sample_refused_with_http_200(self, app_context, mock_inv):
        mock_inv.doDelete.return_value = {"id": 5, "deleted": False, "canBeDeleted": False}
        with pytest.raises(typer.Exit):
            delete.delete_sample(["SA5"])
        mock_inv.doDelete.assert_called_once_with("samples", 5)

    def test_sample_deleted(self, app_context, mock_inv):
        mock_inv.doDelete.return_value = {"id": 5, "deleted": True}
        delete.delete_sample(["SA5"])

    def test_instrument_refusal_reported_too(self, app_context, mock_inv):
        mock_inv.doDelete.return_value = {"id": 5, "deleted": False}
        with pytest.raises(typer.Exit):
            delete.delete_instrument(["IN5"])

    def test_empty_response_counts_as_success(self, app_context, mock_eln):
        mock_eln.doDelete.return_value = None
        delete.delete_folder(["FL5"])
