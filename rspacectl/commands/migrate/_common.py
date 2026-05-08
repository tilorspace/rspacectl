"""Shared infrastructure for the migrate export/import flows.

Constants, snapshot path helpers, the import-state dataclass, checkpoint
load/save, error/warning helpers, and a few small utilities (pagination,
sanitisation, link extraction, item iteration, image-URL parsing).

Both ``_export.py`` and ``_import.py`` depend on this module; nothing in
this module depends on either of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from rspace_client.inv.inv import Pagination

from ...exceptions import warn
from ...output import err_console


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2
_CHECKPOINT_SUFFIX = ".checkpoint"
_SNAPSHOT_JSON = "snapshot.json"

# Keys that are server-generated and must be stripped before re-posting
_RESOURCE_SERVER_KEYS: frozenset = frozenset(
    {
        "id",
        "globalId",
        "created",
        "lastModified",
        "createdBy",
        "modifiedBy",
        "owner",
        "version",
        "historicalVersion",
        "links",
        "iconId",
        "permittedActions",
        "sharedWith",
    }
)
_FIELD_SERVER_KEYS: frozenset = frozenset({"id", "globalId", "created", "lastModified", "links"})
_ATTACHMENT_FIELD_TYPES: frozenset = frozenset({"attachment", "file"})


# ---------------------------------------------------------------------------
# Snapshot path helpers
# ---------------------------------------------------------------------------


def _snapshot_dir(output: Path) -> Path:
    """Return the snapshot directory given --output (always a directory)."""
    return output


def _snapshot_json(snapshot_dir: Path) -> Path:
    return snapshot_dir / _SNAPSHOT_JSON


def _resolve_input(input_path: Path) -> Tuple[Path, Path]:
    """Return (snapshot_dir, snapshot_json) from either a directory or a .json file."""
    if input_path.is_dir():
        return input_path, input_path / _SNAPSHOT_JSON
    # Legacy: bare .json file
    return input_path.parent, input_path


def _attachments_dir(snapshot_dir: Path) -> Path:
    return snapshot_dir / "attachments"


def _images_dir(snapshot_dir: Path) -> Path:
    return snapshot_dir / "images"


def _icons_dir(snapshot_dir: Path) -> Path:
    return snapshot_dir / "icons"


def _image_containers_dir(snapshot_dir: Path) -> Path:
    return snapshot_dir / "image_containers"


# ---------------------------------------------------------------------------
# Import state / checkpoint
# ---------------------------------------------------------------------------


@dataclass
class _ImportState:
    """Accumulated id mappings and progress, persisted as the checkpoint file."""

    id_map: Dict[str, str] = field(default_factory=dict)
    """old globalId → new globalId for every successfully created resource."""

    numeric_map: Dict[int, int] = field(default_factory=dict)
    """old numeric id → new numeric id (useful for SDK calls that need ints)."""

    completed_phases: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "id_map": self.id_map,
            "numeric_map": {str(k): v for k, v in self.numeric_map.items()},
            "completed_phases": self.completed_phases,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "_ImportState":
        s = cls()
        s.id_map = d.get("id_map", {})
        s.numeric_map = {int(k): v for k, v in d.get("numeric_map", {}).items()}
        s.completed_phases = d.get("completed_phases", [])
        s.errors = d.get("errors", [])
        return s


def _save_checkpoint(path: Path, state: _ImportState) -> None:
    path.write_text(json.dumps(state.to_dict(), indent=2))
    err_console.print(f"[dim]Checkpoint saved → {path}[/dim]")


def _load_checkpoint(path: Path) -> _ImportState:
    return _ImportState.from_dict(json.loads(path.read_text()))


def _record_error(state: _ImportState, message: str) -> None:
    """Warn the user and append the message to the import state's error list."""
    warn(message)
    state.errors.append(message)


def _resolve_new_gid(
    old_gid: str, state: _ImportState, kind: str
) -> Optional[str]:
    """Return the new globalId for an old one, or None if unmapped.

    Records a state error on miss.  In dry-run mode the caller normally
    populates ``state.id_map`` with identity mappings during earlier phases,
    so a miss here means the user skipped a prerequisite phase.
    """
    new_gid = state.id_map.get(old_gid)
    if not new_gid:
        _record_error(state, f"{kind} for {old_gid}: not in id_map — skipped")
        return None
    return new_gid


# Module-level export warning collector.  Reset at the start of each export
# so the final summary can tally any download failures.  Module-level state is
# acceptable here because each CLI invocation runs a single command in its own
# process; the helpers don't otherwise share state across calls.
_export_warnings: List[str] = []


def _export_warn(message: str) -> None:
    """Warn the user and record the message for the export summary."""
    warn(message)
    _export_warnings.append(message)


def _progress() -> Progress:
    """Build a Progress instance configured for this command's use.

    Renders to stderr (so progress doesn't pollute piped stdout) with a
    description, bar, item count, and elapsed time.  Use as a context manager
    around a phase's work loop.
    """
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err_console,
        transient=False,  # leave the final bar visible after completion
    )


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------


def _paginate(fetch_fn, data_key: str, page_size: int = 50, **kwargs) -> List[Dict]:
    """Exhaust all pages of a paginated inventory endpoint and return all records."""
    results: List[Dict] = []
    page = 0
    while True:
        pagination = Pagination(page_number=page, page_size=page_size)
        resp = fetch_fn(pagination=pagination, **kwargs)
        batch = resp.get(data_key, [])
        results.extend(batch)
        total = resp.get("totalHits", len(results))
        if len(results) >= total or not batch:
            break
        page += 1
    return results


# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------


def _strip(d: Dict, keys: frozenset) -> Dict:
    return {k: v for k, v in d.items() if k not in keys}


def _sanitise_template(tmpl: Dict) -> Dict:
    """Return a POST-ready template payload stripped of server-generated keys."""
    clean = _strip(tmpl, _RESOURCE_SERVER_KEYS)
    clean["fields"] = [_strip(f, _FIELD_SERVER_KEYS) for f in tmpl.get("fields", [])]
    return clean


# ---------------------------------------------------------------------------
# Link helpers
# ---------------------------------------------------------------------------


def _find_link(item: Dict, rel: str) -> Optional[str]:
    """Return the URL for the first link whose rel matches, or None.

    The RSpace inventory API uses ``_links`` (not ``links``), and the URL
    field is ``"link"`` (not ``"href"``).  We check both field names for
    resilience across API versions.
    """
    for key in ("_links", "links"):
        for link in item.get(key) or []:
            if link.get("rel") == rel:
                return link.get("link") or link.get("href")
    return None


def _preview_image_url(item: Dict) -> Optional[str]:
    """Return the preview-image URL for an item, or None if no image link exists.

    Prefers the full-resolution ``rel=image`` link; falls back to ``thumbnail``.
    """
    return _find_link(item, "image") or _find_link(item, "thumbnail")


def _preview_image_hash(url: str) -> Optional[str]:
    """Extract the content-addressed hash from an RSpace preview-image URL.

    URLs have the form ``…/files/image/{hash}``.  The hash is identical
    whenever two items share the same image content (RSpace's image storage
    is content-addressed), which lets us identify default-thumbnail reuse.
    Returns None if the URL does not match the expected pattern.
    """
    if not url:
        return None
    marker = "/files/image/"
    idx = url.find(marker)
    if idx == -1:
        return None
    candidate = url[idx + len(marker):].split("?", 1)[0].split("/", 1)[0]
    return candidate or None


# ---------------------------------------------------------------------------
# Iteration helper used by both export-summary and import phases 6/7
# ---------------------------------------------------------------------------


def _all_items_with_globalid(
    templates: List[Dict],
    containers: List[Dict],
    samples: List[Dict],
) -> List[Tuple[str, Dict]]:
    """Flatten templates + containers + samples (incl. subsamples) into
    ``(globalId, item)`` pairs.  Used by phases 6/7/8 which iterate over
    every inventory object that might carry per-item migration metadata.
    """
    return (
        [(t["globalId"], t) for t in templates]
        + [(c["globalId"], c) for c in containers]
        + [(s["globalId"], s) for s in samples]
        + [
            (ss["globalId"], ss)
            for s in samples
            for ss in s.get("subSamples", [])
        ]
    )
