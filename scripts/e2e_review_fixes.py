"""Live end-to-end checks for the API/client review fixes (C4, C5, C6).

Drives the real ``rspace`` CLI against a *local* RSpace server and checks the
server state after each step, not just exit codes:

- C6  ``create sample --expiry`` sends a date; ``--quantity/--unit`` work
- C5  ``split`` validates the ID, works with numeric IDs and keeps tags
- C4  ``get <type> <id>`` / ``delete <type> <ids>`` refuse mismatched prefixes
- ``delete document`` and ``get`` of workbenches (BE…) work

Every item it creates is named ``rspacectl-review-e2e-*`` and deleted at the
end, even when a check fails. It refuses to run against anything but
localhost, so it can never write to a shared server.

Usage (from the repo root, with rspacectl installed in the active env):

    python scripts/e2e_review_fixes.py [--profile NAME] [--rspace PATH]

Exits 0 when every check passes, 1 otherwise.
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
P = "rspacectl-review-e2e-"

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("--profile", default="default", help="rspacectl credential profile.")
parser.add_argument(
    "--rspace",
    default=shutil.which("rspace") or str(Path(sys.executable).with_name("rspace")),
    help="Path to the rspace executable (default: from PATH or this venv).",
)
opts = parser.parse_args()

url, _ = load_config(profile=opts.profile)
host = urlparse(url).hostname
if host not in LOCAL_HOSTS:
    sys.exit(f"Refusing to run against {url}: this script creates and deletes data. Local only.")
RSPACE = [opts.rspace, "--profile", opts.profile]
print(f"Running against {url} with {opts.rspace}")

results = []
created = {"sample": [], "container": [], "instrument": [], "document": [], "folder": []}


def run(*args, fmt="json"):
    p = subprocess.run(
        [*RSPACE, "-o", fmt, *args],
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
        l for l in p.stderr.splitlines() if "HTTP" not in l and "local development" not in l
    )
    return p.returncode, data, out, err


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def tags(rec):
    return sorted(t["value"] for t in rec.get("tags") or [])


def subsamples(sa):
    return run("get", sa)[1]["subSamples"]


try:
    # ------------------------------------------------------------------ C6
    print("\n== C6: create sample --expiry")
    rc, s, out, err = run(
        "create",
        "sample",
        "--name",
        P + "exp",
        "--expiry",
        "2027-01-02",
        "--tag",
        "t1,t2",
        "--quantity",
        "2",
        "--unit",
        "3",
    )
    check("valid expiry exits 0", rc == 0, err)
    sa = s["globalId"]
    created["sample"].append(sa)
    got = run("get", sa)[1]
    check(
        "server stored expiryDate 2027-01-02",
        got.get("expiryDate") == "2027-01-02",
        got.get("expiryDate"),
    )
    check(
        "quantity/unit stored (2 ml)",
        got["quantity"] == {"numericValue": 2, "unitId": 3},
        got["quantity"],
    )
    check("sample tags stored", tags(got) == ["t1", "t2"], tags(got))

    rc, s, out, err = run("create", "sample", "--name", P + "exp-leap", "--expiry", "2028-02-29")
    created["sample"].append(s["globalId"])
    check(
        "leap-day expiry accepted", run("get", s["globalId"])[1].get("expiryDate") == "2028-02-29"
    )

    before = run("search", P, fmt="quiet")[2].split()
    for bad in ["2027-13-01", "2027-02-30", "tomorrow", "02/01/2027", "2027-01-02T03:04:05", ""]:
        rc, _, _, err = run("create", "sample", "--name", P + "bad", "--expiry", bad)
        if bad == "":
            # empty string is falsy: treated as "no expiry"
            check("empty --expiry creates sample without expiry", rc == 0, err)
            continue
        check(
            f"invalid expiry {bad!r} exits 1 with clear error",
            rc == 1 and "expected a date as YYYY-MM-DD" in err,
            f"rc={rc} {err}",
        )
    after = run("search", P, fmt="quiet")[2].split()
    new = [g for g in after if g.startswith("SA") and g not in before]
    created["sample"].extend(new)
    check("invalid expiries created nothing (only the empty-string one)", len(new) == 1, new)

    # ------------------------------------------------------------------ C5
    print("\n== C5: split")
    ss = subsamples(sa)[0]["globalId"]
    run("tag", ss, "sst,keep", fmt="table")
    check("subsample tagged", tags(run("get", ss)[1]) == ["keep", "sst"])

    n_before = len(subsamples(sa))
    rc, _, _, err = run("split", sa, "--count", "2", "--quantity", "0.1")
    check("split with SA prefix exits 1", rc == 1 and "not a subsample ID" in err, err)
    rc, _, _, err = run("split", "IN1", "--count", "2")
    check("split with IN prefix exits 1", rc == 1, err)
    rc, _, _, err = run("split", "bogus", "--count", "2")
    check("split with garbage ID exits 1", rc == 1, err)
    rc, _, _, err = run("split", ss, "--count", "0")
    check("split --count 0 exits 1", rc == 1, err)
    rc, _, _, err = run("split", ss, "--count", "2", "--quantity", "0")
    check("split --quantity 0 exits 1", rc == 1, err)
    rc, _, _, err = run("split", ss, "--count", "3", "--quantity", "1")
    check(
        "split exceeding available quantity exits 1", rc == 1 and "Attempting to remove" in err, err
    )
    check("no subsamples created by rejected splits", len(subsamples(sa)) == n_before)

    num = ss[2:]
    rc, data, out, err = run("split", num, "--count", "2", "--quantity", "0.25")
    check("quantity split with numeric ID exits 0 (was AttributeError)", rc == 0, err)
    check("reports 2 new subsamples", "Created 2 new subsample(s)" in out, out[:80])
    check("JSON output shows restored tags", data and all(tags(r) == ["keep", "sst"] for r in data))
    subs = subsamples(sa)
    qty = {x["globalId"]: x["quantity"] for x in subs}
    check("3 subsamples after split", len(subs) == 3, list(qty))
    check("original keeps 1.5 ml", qty[ss] == {"numericValue": 1.5, "unitId": 3}, qty[ss])
    check(
        "new subsamples have 0.25 ml",
        all(q == {"numericValue": 0.25, "unitId": 3} for g, q in qty.items() if g != ss),
        qty,
    )
    check(
        "tags preserved on all subsamples (server state)",
        all(tags(run("get", x["globalId"])[1]) == ["keep", "sst"] for x in subs),
    )
    check("parent sample tags untouched", tags(run("get", sa)[1]) == ["t1", "t2"])

    # even split, lowercase prefix: server rescales ml -> µl
    target = next(g for g in qty if g != ss)
    rc, data, out, err = run("split", target.lower(), "--count", "1")
    check("even split with lowercase 'ss' exits 0", rc == 0, err)
    t = run("get", target)[1]
    check(
        "even split rescales 0.25 ml to 125 µl (unit caveat)",
        t["quantity"] == {"numericValue": 125, "unitId": 2},
        t["quantity"],
    )
    check("even split keeps tags", tags(t) == ["keep", "sst"])

    # quantity split on the rescaled subsample: --quantity is read in µl now
    rc, data, out, err = run("split", target, "--count", "1", "--quantity", "25")
    check("quantity split on rescaled subsample exits 0", rc == 0, err)
    q = {r["globalId"]: r["quantity"] for r in data}
    check(
        "--quantity read in current unit (125 µl -> 100 + 25 µl)",
        q.get(target) == {"numericValue": 100, "unitId": 2}
        and any(v == {"numericValue": 25, "unitId": 2} for g, v in q.items() if g != target),
        q,
    )

    # untagged subsample: quantity split must not invent tags
    rc, s2, _, _ = run(
        "create", "sample", "--name", P + "untagged", "--quantity", "1", "--unit", "3"
    )
    created["sample"].append(s2["globalId"])
    ss2 = s2["subSamples"][0]["globalId"]
    rc, data, _, err = run("split", ss2, "--count", "1", "--quantity", "0.5")
    check("quantity split of untagged subsample exits 0", rc == 0, err)
    check("untagged split stays untagged", all(tags(r) == [] for r in data))

    help_out = run("split", "--help", fmt="table")[2]
    check("split --help documents unit rescaling", "rescale" in help_out)

    # ------------------------------------------------------------------ C4 setup
    print("\n== C4: setup")
    ic = run("create", "container", "--name", P + "box", "--tag", "c1")[1]["globalId"]
    created["container"].append(ic)
    inst = run("create", "instrument", "--name", P + "scope", "--tag", "i1")[1]
    created["instrument"].append(inst["globalId"])
    be = inst["parentContainers"][0]["globalId"]
    doc = run("create", "document", "--name", P + "doc")[1]["globalId"]
    created["document"].append(doc)
    fl = run("create", "folder", "--name", P + "folder")[1]["globalId"]
    created["folder"].append(fl)
    nb = run("create", "notebook", "--name", P + "nb")[1]["globalId"]
    created["folder"].append(nb)
    it = (run("list", "templates")[1] or [{}])[0].get("globalId")
    nt = (run("list", "instrument-templates")[1] or [{}])[0].get("globalId")
    fm = (run("list", "forms")[1] or [{}])[0].get("globalId")
    print(f"   using {sa} {ss} {ic} {be} {inst['globalId']} {doc} {fl} {nb} {it} {nt} {fm}")

    # ------------------------------------------------------------------ C4 get
    print("\n== C4: get <type> <id>")
    matching = [
        ("sample", sa),
        ("subsample", ss),
        ("sub", ss),
        ("container", ic),
        ("container", be),
        ("bench", ic),
        ("instrument", inst["globalId"]),
        ("inst", inst["globalId"]),
        ("document", doc),
        ("doc", doc),
        ("folder", fl),
        ("folder", nb),
        ("notebook", nb),
        ("template", it),
        ("instrument-template", nt),
        ("form", fm),
    ]
    for typ, gid in matching:
        if not gid:
            continue
        rc, data, _, err = run("get", typ, gid)
        check(
            f"get {typ} {gid} returns {gid}",
            rc == 0 and data and data.get("globalId") == gid,
            f"rc={rc} {err}",
        )
    rc, data, _, err = run("get", "subsample", ss[2:])
    check("get subsample <numeric> still works", rc == 0 and data["globalId"] == ss, err)
    for args in [(be,), ("bench", be[2:]), ("container", be[2:])]:
        rc, data, _, err = run("get", *args)
        check(
            f"get {' '.join(args)} returns workbench {be}",
            rc == 0 and data and data.get("globalId") == be,
            f"rc={rc} {err}",
        )
    rc, data, _, err = run("get", be, "--content")
    check(
        f"get {be} --content lists the new instrument",
        rc == 0 and inst["globalId"] in json.dumps(data or {}),
        f"rc={rc} {err}",
    )

    mismatching = [
        ("subsample", sa),
        ("sample", ss),
        ("instrument", sa),
        ("container", sa),
        ("document", fm),
        ("form", doc),
        ("folder", doc),
        ("template", nt),
        ("instrument-template", it),
        ("sample", "XX1"),
    ]
    for typ, gid in mismatching:
        if not gid:
            continue
        rc, data, _, err = run("get", typ, gid)
        check(
            f"get {typ} {gid} exits 1", rc == 1 and data is None and "ID" in err, f"rc={rc} {err}"
        )
    rc, data, _, _ = run("get", sa)
    check("single-arg get still infers type", data and data["globalId"] == sa)

    # ------------------------------------------------------------------ C4 delete
    print("\n== C4: delete <type> <ids>")
    victims = [
        ("sample", ss, sa),
        ("sample", ic, ic),
        ("container", sa, sa),
        ("container", inst["globalId"], inst["globalId"]),
        ("instrument", sa, sa),
        ("instrument-template", inst["globalId"], inst["globalId"]),
        ("document", fl, fl),
        ("folder", doc, doc),
        ("form", doc, doc),
    ]
    for typ, gid, still_there in victims:
        rc, _, _, err = run("delete", typ, gid, fmt="table")
        alive = run("get", still_there)[1]
        check(
            f"delete {typ} {gid} refused, {still_there} survives",
            rc == 1 and "Nothing was deleted" in err and alive and not alive.get("deleted"),
            f"rc={rc} {err}",
        )

    doc2 = run("create", "document", "--name", P + "doc2")[1]["globalId"]
    created["document"].append(doc2)
    rc, _, out, err = run("delete", "document", doc2, fmt="table")
    check(f"delete document {doc2} works (was 404)", rc == 0 and "Deleted 1" in out, f"{out} {err}")
    if rc == 0:
        created["document"].remove(doc2)
    rc, gone, _, _ = run("get", doc2)
    check(f"{doc2} is gone after delete", rc != 0 or (gone or {}).get("deleted"))

    rc, _, _, err = run("delete", "sample", s2["globalId"], ic, fmt="table")
    alive = run("get", s2["globalId"])[1]
    check(
        "mixed batch (valid SA + IC) deletes nothing",
        rc == 1 and alive and not alive.get("deleted"),
        err,
    )

finally:
    print("\n== cleanup (also exercises valid deletes)")
    for typ, ids in [
        ("instrument", created["instrument"]),
        ("sample", created["sample"]),
        ("container", created["container"]),
        ("document", created["document"]),
        ("folder", created["folder"]),
    ]:
        if ids:
            rc, _, out, err = run("delete", typ, *ids, fmt="table")
            check(f"delete {typ} {' '.join(ids)}", rc == 0, f"{out.strip()} {err}")
    left = []
    for gid in run("search", P, fmt="quiet")[2].split():
        rec = run("get", gid)[1] or {}
        if (rec.get("name") or "").startswith(P) and not rec.get("deleted"):
            left.append(gid)
    check("nothing named " + P + "* left after cleanup", not left, left)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
for name, _, detail in failed:
    print(f"  FAIL {name}: {detail}")
sys.exit(1 if failed else 0)
