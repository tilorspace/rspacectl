"""Tests for ID parsing utilities."""

import pytest

from rspacectl.ids import parse_id, resource_type


class TestParseId:
    def test_numeric_int(self):
        assert parse_id(12345) == 12345

    def test_numeric_string(self):
        assert parse_id("12345") == 12345

    def test_global_id_document(self):
        assert parse_id("SD123") == 123

    def test_global_id_sample(self):
        assert parse_id("SA456") == 456

    def test_global_id_subsample(self):
        assert parse_id("SS789") == 789

    def test_global_id_container(self):
        assert parse_id("IC100") == 100

    def test_global_id_template(self):
        assert parse_id("IT42") == 42

    def test_global_id_gallery(self):
        assert parse_id("GL999") == 999

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="not a valid RSpace ID"):
            parse_id("not-an-id")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_id("")

    def test_whitespace_stripped(self):
        assert parse_id("  SD123  ") == 123


class TestResourceType:
    def test_document_prefix(self):
        assert resource_type("SD123") == "document"

    def test_sample_prefix(self):
        assert resource_type("SA456") == "sample"

    def test_container_prefix(self):
        assert resource_type("IC100") == "container"

    def test_unknown_prefix(self):
        assert resource_type("XX999") == "unknown"

    def test_numeric_id(self):
        assert resource_type("12345") == "unknown"
