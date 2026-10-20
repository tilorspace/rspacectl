"""Tests for fixes from the RSpace 2.27 API / rspace-client review.

C4: explicit type + mismatching GlobalID prefix (get, delete)
C5: split passes a GlobalID and restores tags cleared by the bulk UPDATE
C6: create sample --expiry sends a date, not a datetime
"""

import datetime

import pytest
import typer
from rspace_client.inv.inv import BulkOperationResult, SamplePost

from rspacectl.commands import create, delete, get_cmd, split
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

    def test_bench_id_with_container_type(self, app_context, mock_inv):
        mock_inv.get_container_by_id.return_value = {"id": 1, "globalId": "BE1"}
        get_cmd.get("container", "BE1", subsamples=False, content=False)
        mock_inv.get_container_by_id.assert_called_once_with(1, include_content=False)


class TestDeleteTypeMismatch:
    def test_mismatch_deletes_nothing(self, app_context, mock_inv):
        with pytest.raises(typer.Exit):
            delete.delete_instrument(["IN1", "SA905"])
        mock_inv.delete_instrument.assert_not_called()

    def test_matching_ids_are_deleted(self, app_context, mock_inv):
        delete.delete_instrument(["IN1", "2"])
        assert [c.args for c in mock_inv.delete_instrument.call_args_list] == [(1,), (2,)]

    def test_container_rejects_sample(self, app_context, mock_inv):
        with pytest.raises(typer.Exit):
            delete.delete_container(["SA5"])
        mock_inv.doDelete.assert_not_called()

    def test_document_rejects_form(self, app_context, mock_eln):
        with pytest.raises(typer.Exit):
            delete.delete_document(["FM5"])
        mock_eln.delete_document.assert_not_called()


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
        mock_inv.get_subsample_by_id.return_value = {"id": 1, "globalId": "SS1", "tags": TAGS}
        mock_inv.split_subsample.return_value = _bulk(
            {"id": 1, "globalId": "SS1", "tags": []},
            {"id": 2, "globalId": "SS2", "tags": []},
            {"id": 3, "globalId": "SS3", "tags": []},
        )
        mock_inv.retrieve_api_results.side_effect = lambda path, **kw: {
            "id": int(path.rsplit("/", 1)[1]),
            "globalId": "SS" + path.rsplit("/", 1)[1],
            "tags": kw["params"]["tags"],
        }

        split.split("SS1", count=2, quantity=0.1)

        assert mock_inv.split_subsample.call_args.kwargs["subsample"] == "SS1"
        puts = [
            (c.args[0], c.kwargs["params"])
            for c in mock_inv.retrieve_api_results.call_args_list
            if c.kwargs.get("request_type") == "PUT"
        ]
        assert puts == [(f"/subSamples/{i}", {"tags": TAGS}) for i in (1, 2, 3)]
        assert "Created 2 new subsample(s)" in capsys.readouterr().out

    def test_quantity_split_without_tags_skips_puts(self, app_context, mock_inv):
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
