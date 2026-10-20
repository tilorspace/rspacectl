"""Live end-to-end checks for instrument and instrument-template commands.

Covers create (template fields, --target with grid cells), get, list, search,
update (name/description/fields/tags, --latest-template, --propagate), tag,
move (list, grid cell, grid auto-fill, mixed with subsamples, tag
preservation, canStoreInstruments), attachments and delete.

Every item it creates is named ``rspacectl-review-e2e-instr-*`` and deleted
at the end, even when a check fails. Local servers only.

    python scripts/e2e_instruments.py [--profile NAME] [--rspace PATH]
"""

import json
import sys
import tempfile
from pathlib import Path

from _e2e import field_values, setup, tags

from rspacectl.utils import put_inventory_item

P = "rspacectl-review-e2e-instr-"
TEMPLATE = {
    "name": P + "template",
    "description": "e2e instrument template",
    "fields": [
        {"name": "Serial number", "type": "string", "mandatory": True},
        {"name": "Mode", "type": "choice", "definition": {"options": ["confocal", "widefield"]}},
        {"name": "Power (W)", "type": "number"},
        {"name": "Commissioned", "type": "date"},
    ],
}

e = setup(__doc__.split("\n\n")[0])
run, check, get = e.run, e.check, e.get
created = {"instrument": [], "sample": [], "container": [], "instrument-template": []}
tmp = Path(tempfile.mkdtemp(prefix="rspacectl-e2e-"))


def create(kind, *args):
    rc, data, _, err = run("create", kind, *args)
    if rc == 0 and data:
        created[kind].append(data["globalId"])
    return rc, data, err


def location(rec):
    parent = (rec.get("parentContainers") or [{}])[0].get("globalId")
    loc = rec.get("parentLocation") or {}
    return parent, (loc.get("coordY"), loc.get("coordX")) if loc.get("coordX") else None


try:
    # ------------------------------------------------------------------ setup
    e.section("instrument template")
    tfile = tmp / "template.json"
    tfile.write_text(json.dumps(TEMPLATE))
    rc, nt, err = create("instrument-template", "--from-file", tfile)
    check("create instrument-template --from-file exits 0", rc == 0, err)
    nt_gid = nt["globalId"]
    t = get(nt_gid)
    check(
        "template has the 4 fields in order",
        [f["name"] for f in t["fields"]] == [f["name"] for f in TEMPLATE["fields"]],
        [f["name"] for f in t["fields"]],
    )
    check("mandatory flag stored", t["fields"][0].get("mandatory") is True)
    rc, _, out, _ = run("get", nt_gid, fmt="table")
    check("get NT… (table) lists template fields", "Serial number" in out and "confocal" in out)
    check(
        "list instrument-templates includes it",
        nt_gid in e.find_named(P, "list", "instrument-templates"),
    )
    rc, _, _, err = run("create", "instrument-template", "--from-file", tmp / "missing.json")
    check("create instrument-template with missing file exits 1", rc != 0, err)

    e.section("containers")
    rc, lst, _ = create("container", "--name", P + "list", "--tag", "c1")
    rc, grid, _ = create(
        "container", "--name", P + "grid", "--type", "grid", "--rows", 2, "--cols", 3
    )
    rc, noinst, _ = create("container", "--name", P + "noinstr")
    lst, grid, noinst = lst["globalId"], grid["globalId"], noinst["globalId"]
    inv = e.inv()
    put_inventory_item(inv, "containers", int(noinst[2:]), {"canStoreInstruments": False})
    check(
        "container set to canStoreInstruments=false",
        get(noinst).get("canStoreInstruments") is False,
    )

    # ------------------------------------------------------------------ create
    e.section("create instrument")
    before = run("list", "instruments", "-q", P, fmt="quiet")[2].split()
    rc, _, err = create("instrument", "--name", P + "nofield", "--template", nt_gid)
    check("missing mandatory field exits 1", rc == 1, err)
    rc, _, err = create("instrument", "--name", P + "rowonly", "--row", 1)
    check("--row without --target exits 1", rc == 1, err)
    rc, _, err = create(
        "instrument", "--name", P + "badcell", "--target", grid, "--row", 5, "--col", 1
    )
    check("grid cell outside the grid is rejected", rc != 0, err)
    after = run("list", "instruments", "-q", P, fmt="quiet")[2].split()
    check(
        "rejected creates left nothing behind",
        sorted(after) == sorted(before),
        set(after) - set(before),
    )
    # create (like create sample) warns about an unknown field name and drops
    # it; update exits 1 instead.
    rc, _, err = create(
        "instrument",
        "--name",
        P + "badfield",
        "--template",
        nt_gid,
        "--field",
        "Serial number=X",
        "--field",
        "Nope=1",
    )
    check(
        "unknown field on create warns and is ignored",
        rc == 0 and "Unknown field name ignored: 'Nope'" in err,
        err,
    )

    rc, a, err = create(
        "instrument",
        "--name",
        P + "A",
        "--template",
        nt_gid,
        "--field",
        "Serial number=SN-1",
        "--field",
        "Mode=confocal",
        "--field",
        "Power (W)=2.5",
        "--field",
        "Commissioned=2026-01-15",
        "--tag",
        "i1,i2",
        "--description",
        "first",
    )
    check("create with template fields exits 0", rc == 0, err)
    a = get(a["globalId"])
    fv = field_values(a)
    check(
        "field values stored",
        fv.get("Serial number") == "SN-1"
        and fv.get("Mode") == ["confocal"]
        and str(fv.get("Power (W)")) in ("2.5", "2.50")
        and fv.get("Commissioned") == "2026-01-15",
        fv,
    )
    check("tags and description stored", tags(a) == ["i1", "i2"] and a["description"] == "first")
    check("templateId points to the new template", a.get("templateId") == int(nt_gid[2:]))
    check("default location is the workbench", location(a)[0].startswith("BE"), location(a))
    bench = location(a)[0]

    rc, b, err = create("instrument", "--name", P + "B", "--target", lst, "--tag", "b1")
    check("create --target LIST exits 0", rc == 0, err)
    check("B stored in list container", location(get(b["globalId"]))[0] == lst)
    rc, c, err = create("instrument", "--name", P + "C", "--target", grid, "--row", 2, "--col", 3)
    check("create --target GRID --row 2 --col 3 exits 0", rc == 0, err)
    check(
        "C in grid cell row 2 / col 3",
        location(get(c["globalId"])) == (grid, (2, 3)),
        location(get(c["globalId"])),
    )
    rc, d, err = create(
        "instrument", "--name", P + "D", "--template", nt_gid, "--field", "Serial number=SN-4"
    )
    a, b, c, d = a["globalId"], b["globalId"], c["globalId"], d["globalId"]

    # ------------------------------------------------------------------ read
    e.section("get / list / search")
    rc, _, out, _ = run("get", a, fmt="table")
    check("get IN… (table) shows field values", "SN-1" in out and "confocal" in out, out[-300:])
    rc, data, _, err = run("list", "instruments", "-q", P)
    check(
        "list instruments -q finds all 4",
        rc == 0 and {a, b, c, d} <= {x["globalId"] for x in data or []},
        err,
    )
    rc, _, out, _ = run("list", "instruments", "-q", P, "--page-size", 2, fmt="quiet")
    check("list --page-size 2 returns 2 IDs on stdout only", len(out.split()) == 2, out)
    rc, data, _, _ = run(
        "list", "instruments", "--order-by", "created", "--sort-order", "desc", "--page-size", 5
    )
    check(
        "list --order-by created desc shows newest first",
        data and data[0]["globalId"] == d,
        [x["globalId"] for x in data or []],
    )
    rc, data, _, err = run("search", P + "A", "--type", "instruments")
    gids = [x["globalId"] for x in data or []]
    check(
        "search --type instruments returns only IN…",
        rc == 0 and a in gids and all(g.startswith("IN") for g in gids),
        gids,
    )

    # ------------------------------------------------------------------ update
    e.section("update / tag")
    rc, _, _, err = run("update", "instrument", a, "--name", P + "A2", "--description", "renamed")
    x = get(a)
    check(
        "update --name/--description",
        rc == 0 and x["name"] == P + "A2" and x["description"] == "renamed",
        err,
    )
    check("tags survive name/description update", tags(x) == ["i1", "i2"], tags(x))
    rc, _, _, err = run(
        "update", "instrument", a, "--field", "Serial number=SN-1b", "--field", "Mode=widefield"
    )
    x = get(a)
    check(
        "update --field changes values",
        rc == 0
        and field_values(x)["Serial number"] == "SN-1b"
        and field_values(x)["Mode"] == ["widefield"],
        field_values(x),
    )
    check("tags survive field update", tags(x) == ["i1", "i2"], tags(x))
    check("other fields untouched by field update", field_values(x)["Commissioned"] == "2026-01-15")
    rc, _, _, err = run("update", "instrument", a, "--field", "Nope=1")
    check("update unknown field exits 1", rc == 1, err)
    rc, _, _, err = run("update", "instrument", a)
    check("update with nothing to do exits 1", rc == 1, err)
    rc, _, _, err = run("update", "instrument", a, "--tag", "x1")
    check("update --tag replaces tags", rc == 0 and tags(get(a)) == ["x1"], tags(get(a)))
    rc, _, _, err = run("tag", a, "t1,t2", fmt="table")
    check("tag IN…", rc == 0 and tags(get(a)) == ["t1", "t2"], err)
    rc, _, _, err = run("tag", nt_gid, "nt1", fmt="table")
    t2 = get(nt_gid)
    check("tag NT…", rc == 0 and tags(t2) == ["nt1"], err)

    e.section("template versions")
    # A metadata PUT bumps the template version, so A and D are now outdated.
    check(
        "tagging the template bumped its version",
        t2["version"] > t["version"],
        (t["version"], t2["version"]),
    )
    check(
        "A reports the old template version",
        get(a).get("templateVersion") == t["version"],
        get(a).get("templateVersion"),
    )
    rc, _, _, err = run("update", "instrument", a, "--latest-template")
    check(
        "update instrument --latest-template",
        rc == 0 and get(a).get("templateVersion") == t2["version"],
        err,
    )
    check("tags survive --latest-template", tags(get(a)) == ["t1", "t2"], tags(get(a)))
    rc, _, _, err = run("update", "instrument-template", nt_gid, "--propagate")
    check(
        "update instrument-template --propagate updates D",
        rc == 0 and get(d).get("templateVersion") == t2["version"],
        err,
    )
    rc, _, _, err = run("update", "instrument-template", nt_gid)
    check("update instrument-template without --propagate exits 1", rc == 1, err)

    # ------------------------------------------------------------------ move
    e.section("move")
    rc, _, _, err = run("move", a, "--target", lst, fmt="table")
    check("move IN… to list container", rc == 0 and location(get(a))[0] == lst, err)
    check("tags survive move", tags(get(a)) == ["t1", "t2"], tags(get(a)))
    rc, _, _, err = run("move", a, "--target", grid, "--row", 1, "--col", 2, fmt="table")
    check(
        "move IN… to grid cell 1/2",
        rc == 0 and location(get(a)) == (grid, (1, 2)),
        f"{err} {location(get(a))}",
    )
    rc, _, _, err = run("move", a, "--target", grid, "--row", 2, "--col", 3, fmt="table")
    check("move onto an occupied cell fails", rc != 0 and location(get(a)) == (grid, (1, 2)), err)
    rc, _, _, err = run("move", b, d, "--target", grid, "--row", 1, "--col", 1, fmt="table")
    check("--row/--col with two instruments exits 1", rc == 1, err)
    rc, _, _, err = run("move", b, d, "--target", grid, "--strategy", "row", fmt="table")
    cells = {location(get(b))[1], location(get(d))[1]}
    check(
        "--strategy row fills free cells (1/1, 1/3)",
        rc == 0 and cells == {(1, 1), (1, 3)},
        f"{err} {cells}",
    )
    # free cells from row 2 / col 1: (2,1) and (2,2); three instruments don't fit
    rc, _, _, err = run(
        "move", a, b, d, "--target", grid, "--strategy", "row", "--row", 2, "--col", 1, fmt="table"
    )
    check("auto-fill with too few free cells exits 1", rc == 1 and "free cell" in err, err)
    rc, _, _, err = run("move", a, "--target", noinst, fmt="table")
    check(
        "move into canStoreInstruments=false exits 1", rc == 1 and location(get(a))[0] == grid, err
    )

    rc, s, err = create("sample", "--name", P + "sample")
    ss = s["subSamples"][0]["globalId"]
    rc, _, _, err = run("move", a, ss, "--target", lst, fmt="table")
    check(
        "mixed move (IN + SS) into list container",
        rc == 0 and location(get(a))[0] == lst and location(get(ss))[0] == lst,
        err,
    )
    rc, _, _, err = run("move", a, ss, "--target", grid, fmt="table")
    check("mixed move into a grid container exits 1", rc == 1, err)
    rc, _, _, err = run("move", a, "--target", bench, fmt="table")
    check("move IN… back to the workbench", rc == 0 and location(get(a))[0] == bench, err)

    # ------------------------------------------------------------------ files
    e.section("attachments")
    f = tmp / "manual.txt"
    f.write_text("rspacectl e2e attachment\n")
    rc, att, _, err = run("upload", "attachment", f, a)
    check("upload attachment to IN…", rc == 0, err)
    atts = get(a).get("attachments") or []
    check(
        "attachment listed on the instrument with its name",
        [x["name"] for x in atts] == ["manual.txt"],
        atts,
    )
    if atts:
        dl = tmp / "dl"
        dl.mkdir()
        rc, _, _, err = run("download", "attachment", atts[0]["id"], "-d", dl, fmt="table")
        got = list(dl.iterdir())
        check(
            "download attachment round-trips content",
            rc == 0 and got and got[0].read_text() == f.read_text(),
            f"{err} {got}",
        )
    check("tags survive attachment upload", tags(get(a)) == ["t1", "t2"], tags(get(a)))

    # ------------------------------------------------------------------ delete
    e.section("delete")
    rc, _, _, err = run("delete", "instrument", d, fmt="table")
    x = get(d)
    check("delete instrument marks it deleted", rc == 0 and (x is None or x.get("deleted")), err)
    created["instrument"].remove(d)
    sa = s["globalId"]
    rc, _, _, err = run("delete", "sample", sa, fmt="table")
    x = get(sa)
    check(
        "delete sample with a subsample in a container is refused and reported",
        rc == 1 and "did not delete" in err and x and not x.get("deleted"),
        err,
    )
    rc, _, _, err = run("move", ss, "--target", bench, fmt="table")
    check("move SS… back to the workbench", rc == 0 and location(get(ss))[0] == bench, err)
    rc, data, _, _ = run("list", "instruments", "-q", P + "D", "--deleted", "--page-size", 100)
    check("list instruments -q … --deleted includes it", d in [y["globalId"] for y in data or []])
    rc, data, _, _ = run("list", "instruments", "-q", P + "D", "--page-size", 100)
    check(
        "list instruments without --deleted omits it", d not in [y["globalId"] for y in data or []]
    )

finally:
    e.section("cleanup")
    for kind in ["instrument", "sample", "container", "instrument-template"]:
        if created[kind]:
            rc, _, out, err = run("delete", kind, *created[kind], fmt="table")
            check(f"delete {kind} {' '.join(created[kind])}", rc == 0, f"{out.strip()} {err}")
    # search is fuzzy, so find_named filters by name
    left = (
        e.find_named(P, "list", "instruments", "-q", P)
        + e.find_named(P, "list", "samples", "-q", P)
        + e.find_named(P, "search", P, "--type", "containers")
        + e.find_named(P, "list", "instrument-templates")
    )
    check("nothing named " + P + "* left", not left, left)

sys.exit(e.report())
