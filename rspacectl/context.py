"""Application context: holds initialised SDK clients and output format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rspace_client import ELNClient, InventoryClient

from .output import OutputFormat

_ctx: Optional["AppContext"] = None


@dataclass
class AppContext:
    eln: ELNClient
    inv: InventoryClient
    output: OutputFormat


def set_context(ctx: AppContext) -> None:
    global _ctx
    _ctx = ctx


def get_context() -> AppContext:
    if _ctx is None:
        raise RuntimeError(
            "AppContext not initialised. This is a bug — "
            "the root callback should always run before a command."
        )
    return _ctx
