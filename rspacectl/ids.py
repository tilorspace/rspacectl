"""Utilities for handling RSpace IDs.

RSpace uses two ID formats:
- Numeric:  12345
- GlobalID: SD12345 (documents), SA12345 (samples), SS12345 (subsamples),
            IC12345 (containers), IT12345 (templates), GL12345 (gallery files)

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
        )


def resource_type(global_id: str) -> str:
    """Return the resource type name for a GlobalID prefix, or 'unknown'."""
    m = _GLOBAL_ID_RE.match(global_id)
    if m:
        return GLOBAL_ID_PREFIXES.get(m.group(1).upper(), "unknown")
    return "unknown"
