"""Utilities for handling RSpace IDs.

RSpace uses two ID formats:
- Numeric:  12345
- GlobalID: SD12345 (documents), SA12345 (samples), SS12345 (subsamples),
            IC12345 (containers), IT12345 (templates), GL12345 (gallery files),
            IN12345 (instruments), NT12345 (instrument templates)

All CLI commands accept either format.
"""

import re
from typing import Union

# Map GlobalID prefixes to resource type names
GLOBAL_ID_PREFIXES = {
    "SD": "document",
    "NB": "notebook",
    "FL": "folder",
    "GL": "file",
    "FM": "form",
    "SA": "sample",
    "SS": "subsample",
    "IC": "container",
    "IT": "template",
    "IN": "instrument",
    "NT": "instrument-template",
    "BE": "bench",
    "GF": "group",
}

_GLOBAL_ID_RE = re.compile(r"^([A-Z]+)(\d+)$", re.IGNORECASE)


def parse_id(value: Union[str, int]) -> int:
    """Parse a numeric or GlobalID string into an integer ID.

    Examples:
        parse_id(12345)     -> 12345
        parse_id("12345")   -> 12345
        parse_id("SD12345") -> 12345
        parse_id("SA123")   -> 123
    """
    if isinstance(value, int):
        return value
    value = str(value).strip()
    m = _GLOBAL_ID_RE.match(value)
    if m:
        return int(m.group(2))
    try:
        return int(value)
    except ValueError:
        raise ValueError(
            f"'{value}' is not a valid RSpace ID. "
            f"Expected a number or GlobalID (e.g. SD123, SA456)."
        ) from None


def resource_type(global_id: str) -> str:
    """Return the resource type name for a GlobalID prefix, or 'unknown'."""
    m = _GLOBAL_ID_RE.match(global_id)
    if m:
        return GLOBAL_ID_PREFIXES.get(m.group(1).upper(), "unknown")
    return "unknown"


# Resource types that share an endpoint, so their prefixes are interchangeable
# when a command is given an explicit type.
_COMPATIBLE_TYPES = {
    "container": {"container", "bench"},
    "bench": {"container", "bench"},
    "folder": {"folder", "notebook"},
    "notebook": {"folder", "notebook"},
}


def check_id_type(value: Union[str, int], expected: str) -> None:
    """Raise ValueError if a GlobalID's prefix does not belong to ``expected``.

    Numeric IDs carry no type and always pass. Without this check a command
    given an explicit type silently drops the prefix, so e.g.
    ``rspace get subsample SA905`` would fetch SS905.
    """
    m = _GLOBAL_ID_RE.match(str(value).strip())
    if not m:
        return
    prefix = m.group(1).upper()
    actual = resource_type(str(value).strip())
    if actual == "unknown":
        raise ValueError(f"'{value}' has an unknown GlobalID prefix '{prefix}'.")
    if actual not in _COMPATIBLE_TYPES.get(expected, {expected}):
        raise ValueError(
            f"'{value}' is {_article(actual)} {actual} ID, not {_article(expected)} {expected} ID "
            f"(prefix '{prefix}')."
        )


def parse_typed_id(value: Union[str, int], expected: str) -> int:
    """check_id_type() then parse_id(): the numeric ID of a GlobalID of type ``expected``."""
    check_id_type(value, expected)
    return parse_id(value)


def _article(word: str) -> str:
    return "an" if word[0] in "aeiou" else "a"
