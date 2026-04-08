"""Shared utility functions for rspacectl commands."""

import json
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Type

import typer

from .output import err_console


def parse_tags(tag_string: Optional[str], tag_class: Type) -> list:
    """Parse a comma-separated tag string into a list of tag objects.

    Args:
        tag_string: comma-separated tags, e.g. "buffers, enzymes"
        tag_class:  the SDK Tag class to instantiate (ELN and Inventory use
                    the same interface but are different imports)

    Returns:
        List of tag_class instances, or [] if tag_string is None/empty.
    """
    if not tag_string:
        return []
    return [tag_class(value=t.strip()) for t in tag_string.split(",") if t.strip()]


def load_json_file(path: Path) -> dict:
    """Read and parse a JSON file, exiting with an error if it can't be read."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        err_console.print(f"[red]File not found:[/red] {path}")
        raise typer.Exit(1)
    except json.JSONDecodeError as e:
        err_console.print(f"[red]Invalid JSON in {path}:[/red] {e}")
        raise typer.Exit(1)


def batch_run(
    ids: List[str],
    operation: Callable[[str], None],
    label: str,
) -> Tuple[List[str], List[str]]:
    """Run an operation on each ID, collecting successes and failures.

    Continues on individual errors rather than aborting early.

    Args:
        ids:       list of raw ID strings (numeric or GlobalID)
        operation: callable that takes a raw ID string and performs the action;
                   should raise on failure
        label:     resource label for console messages (e.g. "document")

    Returns:
        (successes, failures) — lists of ID strings
    """
    from .exceptions import warn

    successes, failures = [], []
    for raw_id in ids:
        try:
            operation(raw_id)
            successes.append(raw_id)
        except Exception as e:
            warn(f"Failed to {label} {raw_id}: {e}")
            failures.append(raw_id)
    return successes, failures
