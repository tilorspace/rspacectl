"""Tests for the output formatting layer."""

import csv
import io
import json
from unittest.mock import patch

import pytest

from rspacectl.output import ColumnDef, OutputFormat, _cell_value, _get_nested, _truncate_timestamp, print_result


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestGetNested:
    def test_simple_key(self):
        assert _get_nested({"name": "Alice"}, "name") == "Alice"

    def test_nested_key(self):
        assert _get_nested({"owner": {"username": "bob"}}, "owner.username") == "bob"

    def test_missing_key_returns_empty(self):
        assert _get_nested({"name": "x"}, "missing") == ""

    def test_none_value_returns_empty(self):
        assert _get_nested({"name": None}, "name") == ""

    def test_deeply_nested(self):
        d = {"a": {"b": {"c": "deep"}}}
        assert _get_nested(d, "a.b") == str({"c": "deep"})


class TestTruncateTimestamp:
    def test_long_timestamp(self):
        ts = "2024-01-15T10:30:00.000Z"
        assert _truncate_timestamp(ts) == "2024-01-15 10:30"

    def test_short_timestamp_unchanged(self):
        ts = "2024-01-15"
        assert _truncate_timestamp(ts) == "2024-01-15"

    def test_empty_string(self):
        assert _truncate_timestamp("") == ""


class TestCellValue:
    def test_basic_value(self):
        col = ColumnDef(key="name", title="Name")
        row = {"name": "Sample A"}
        assert _cell_value(row, col) == "Sample A"

    def test_timestamp_field_truncated(self):
        col = ColumnDef(key="lastModified", title="Modified")
        row = {"lastModified": "2024-06-01T12:00:00.000Z"}
        result = _cell_value(row, col)
        assert len(result) <= 16
        assert "2024-06-01" in result

    def test_nested_field(self):
        col = ColumnDef(key="owner.username", title="Owner")
        row = {"owner": {"username": "charlie"}}
        assert _cell_value(row, col) == "charlie"


# ---------------------------------------------------------------------------
# Integration tests for print_result
# ---------------------------------------------------------------------------

COLUMNS = [
    ColumnDef("globalId", "Global ID", 10, "cyan"),
    ColumnDef("name", "Name", 40),
    ColumnDef("lastModified", "Modified", 16),
]

DATA = [
    {"globalId": "SD1", "name": "Doc A", "lastModified": "2024-01-01T10:00:00Z"},
    {"globalId": "SD2", "name": "Doc B", "lastModified": "2024-02-01T09:00:00Z"},
]


class TestPrintResultJson:
    def test_outputs_valid_json(self, capsys):
        print_result(DATA, COLUMNS, OutputFormat.JSON)
        captured = capsys.readouterr()
        # Rich print_json goes to stdout; strip ANSI codes via a simple parse
        # just verify it's parseable
        text = captured.out
        # remove ANSI escape sequences for JSON parsing
        import re
        clean = re.sub(r"\x1b\[[0-9;]*m", "", text).strip()
        parsed = json.loads(clean)
        assert len(parsed) == 2
        assert parsed[0]["globalId"] == "SD1"


class TestPrintResultCsv:
    def test_outputs_csv_with_headers(self, capsys):
        print_result(DATA, COLUMNS, OutputFormat.CSV)
        captured = capsys.readouterr()
        reader = csv.reader(io.StringIO(captured.out))
        rows = list(reader)
        assert rows[0] == ["Global ID", "Name", "Modified"]
        assert rows[1][0] == "SD1"
        assert rows[2][0] == "SD2"

    def test_csv_row_count(self, capsys):
        print_result(DATA, COLUMNS, OutputFormat.CSV)
        captured = capsys.readouterr()
        reader = csv.reader(io.StringIO(captured.out))
        rows = [r for r in reader if r]  # skip blank lines
        assert len(rows) == 3  # header + 2 data rows


class TestPrintResultQuiet:
    def test_prints_global_ids(self, capsys):
        print_result(DATA, COLUMNS, OutputFormat.QUIET)
        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        assert "SD1" in lines[0]
        assert "SD2" in lines[1]

    def test_empty_data_prints_nothing(self, capsys):
        print_result([], COLUMNS, OutputFormat.QUIET)
        captured = capsys.readouterr()
        assert captured.out.strip() == ""


class TestPrintResultTable:
    def test_empty_data_prints_no_results(self, capsys):
        print_result([], COLUMNS, OutputFormat.TABLE)
        captured = capsys.readouterr()
        assert "No results" in captured.out
