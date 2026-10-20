"""Tests for the read-only instrument commands (list, get, search)."""

import pytest
import typer
from rspace_client.inv.inv import ResultType

from rspacectl.commands import get_cmd, list_cmd, search
from rspacectl.ids import parse_id, resource_type
from rspacectl.output import _get_nested

INSTRUMENT = {
    "id": 32768,
    "globalId": "IN32768",
    "name": "Confocal microscope",
    "type": "INSTRUMENT",
    "created": "2026-07-03T14:34:40.000Z",
    "owner": {"username": "alice"},
    "parentContainers": [{"id": 5963780, "globalId": "BE5963780"}],
    "fields": [{"name": "Serial number", "type": "string", "mandatory": True}],
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
        mock_inv.search.return_value = {"records": [INSTRUMENT], "totalHits": 1}
        list_cmd.list_instruments(**_list_kwargs(query="microscope"))

        mock_inv.search.assert_called_once()
        assert mock_inv.search.call_args.kwargs["result_type"] is ResultType.INSTRUMENT
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
        assert "Serial number" in out

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
