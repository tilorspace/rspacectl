"""Shared helpers for the live end-to-end scripts in this folder.

Each script drives the real ``rspace`` CLI against a *local* RSpace server,
checks the server state after each step and deletes what it created.
``setup()`` refuses anything but localhost, so a script can never write to a
shared server.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from rspacectl.config import load_config

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class E2E:
    """CLI runner plus a list of named checks."""

    def __init__(self, rspace: str, profile: str, url: str, api_key: str):
        self.cmd = [rspace, "--profile", profile]
        self.url = url
        self.api_key = api_key
        self.results = []

    # ---- running the CLI ---------------------------------------------------

    def run(self, *args, fmt="json"):
        """Run ``rspace -o <fmt> <args>``; return (rc, parsed_json, stdout, stderr)."""
        p = subprocess.run(
            [*self.cmd, "-o", fmt, *[str(a) for a in args]],
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "COLUMNS": "200"},
        )
        data = None
        out = p.stdout
        starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
        if starts and fmt == "json":
            try:
                data = json.loads(out[min(starts) :])
            except json.JSONDecodeError:
                pass
        err = " ".join(
            line
            for line in p.stderr.splitlines()
            if "uses HTTP" not in line and "local development" not in line
        )
        return p.returncode, data, out, err

    def get(self, gid):
        """Fetch an item as JSON via ``rspace get``; None if it can't be read."""
        rc, data, _, _ = self.run("get", gid)
        return data if rc == 0 else None

    def find_named(self, prefix, *list_args):
        """Every record named ``prefix…`` from a paged list/search command.

        ``list_args`` is e.g. ("list", "instruments", "-q", prefix). Pages of
        100 (the API maximum) are fetched until a short page; a failing call
        raises instead of passing for "nothing found".
        """
        found = []
        for page in range(100):
            rc, data, _, err = self.run(*list_args, "--page", page, "--page-size", 100)
            if rc != 0:
                raise RuntimeError(f"{' '.join(map(str, list_args))} failed: {err}")
            data = data or []
            found += [x["globalId"] for x in data if (x.get("name") or "").startswith(prefix)]
            if len(data) < 100:
                return found
        return found

    # ---- direct API access for setup the CLI doesn't cover -----------------

    def inv(self):
        from rspace_client.inv.inv import InventoryClient

        return InventoryClient(self.url, self.api_key)

    # ---- checks ------------------------------------------------------------

    def check(self, name, cond, detail=""):
        self.results.append((name, bool(cond), detail))
        print(
            ("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else "")
        )
        return bool(cond)

    def section(self, title):
        print(f"\n== {title}")

    def report(self):
        failed = [r for r in self.results if not r[1]]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} passed")
        for name, _, detail in failed:
            print(f"  FAIL {name}: {detail}")
        return 1 if failed else 0


def tags(rec):
    return sorted(t["value"] for t in (rec or {}).get("tags") or [])


def field_values(rec):
    """{field name: content or selected options} for an item's template fields."""
    out = {}
    for f in (rec or {}).get("fields") or []:
        out[f["name"]] = f.get("selectedOptions") or f.get("content")
    return out


def setup(description: str) -> E2E:
    """Parse --profile/--rspace, refuse non-local servers, return an E2E runner."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--profile", default="default", help="rspacectl credential profile.")
    parser.add_argument(
        "--rspace",
        default=shutil.which("rspace") or str(Path(sys.executable).with_name("rspace")),
        help="Path to the rspace executable (default: from PATH or this venv).",
    )
    opts = parser.parse_args()
    url, api_key = load_config(profile=opts.profile)
    if urlparse(url).hostname not in LOCAL_HOSTS:
        sys.exit(
            f"Refusing to run against {url}: this script creates and deletes data. Local only."
        )
    print(f"Running against {url} with {opts.rspace}")
    return E2E(opts.rspace, opts.profile, url, api_key)
