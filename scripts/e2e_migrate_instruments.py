"""Live end-to-end round trip for migrating instruments and instrument templates.

Builds a small inventory on the local server (an instrument template with an
icon; a LIST container holding a GRID and an IMAGE container; instruments in
a grid cell, on an image marker, in the list and on the bench; a subsample in
the grid), exports it with ``rspace migrate export`` and imports it back into
the same server. Then it checks what import created: template reuse, field
values, tags, extra fields, barcodes, attachments, preview images, grid
cells, image markers, workbench items, and --skip-instruments,
--no-reuse-templates, --dry-run, checkpoint resume and v2 snapshots.

The server is both source and target, so an "identical" instrument template
is the source template itself.

Every item it creates is named ``rspacectl-review-e2e-mig-*`` (imported
copies keep that name) and deleted at the end, even when a check fails.
Local servers only.

    python scripts/e2e_migrate_instruments.py [--profile NAME] [--rspace PATH]
"""

import json
import re
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from _e2e import field_values, location, setup, tags

from rspacectl.ids import parse_id
from rspacectl.utils import place_instrument, put_inventory_item, set_item_image

P = "rspacectl-review-e2e-mig-"
TEMPLATE = {
    "name": P + "template",
    "description": "e2e migrate instrument template",
    "fields": [
        {"name": "Serial number", "type": "string", "mandatory": True},
        {"name": "Mode", "type": "choice", "definition": {"options": ["confocal", "widefield"]}},
        {"name": "Power (W)", "type": "number"},
    ],
}
GID = re.compile(r"\b([A-Z]{2}\d+)\s+→\s+([A-Z]{2}\d+)\b")

e = setup(__doc__.split("\n\n")[0])
run, check, get = e.run, e.check, e.get
inv = e.inv()
tmp = Path(tempfile.mkdtemp(prefix="rspacectl-e2e-mig-"))
template_ids = []  # every instrument template we created (incl. imported copies)
bench = None


def png(path: Path, rgb=(200, 30, 30), size=64) -> Path:
    """Write a small solid-colour PNG without third-party libraries."""
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


def gid_of(rc_data):
    rc, data, _, err = rc_data
    if rc != 0 or not data:
        raise RuntimeError(f"setup step failed: {err}")
    return data["globalId"]


def has_image(rec):
    return any(link.get("rel") == "image" for link in (rec or {}).get("_links") or [])


def markers(container_gid):
    c = inv.get_container_by_id(parse_id(container_gid), include_content=True)
    return sorted(((loc["coordX"], loc["coordY"]), loc["id"]) for loc in c.get("locations") or [])


def migrate_import(snapshot, *flags):
    rc, _, out, err = run("migrate", "import", snapshot, *flags, fmt="table")
    mapping = {}
    for old, new in GID.findall(out):
        mapping.setdefault(old, new)  # first mention is the create/reuse line
    return rc, mapping, out, err


def our_template_ids():
    return e.find_named(P, "list", "instrument-templates")


def our_instruments():
    return sorted(e.find_named(P, "list", "instruments", "-q", P))


try:
    # ================================================================== source
    e.section("source data")
    tfile = tmp / "template.json"
    tfile.write_text(json.dumps(TEMPLATE))
    nt = gid_of(run("create", "instrument-template", "--from-file", tfile))
    template_ids.append(nt)
    run("tag", nt, "nt-src", fmt="table")
    with open(png(tmp / "icon.png", (30, 30, 200)), "rb") as f:
        inv.set_instrument_template_icon(parse_id(nt), f)
    nt_rec = get(nt)

    c1 = gid_of(run("create", "container", "--name", P + "top", "--tag", "c1,c2"))
    grid = gid_of(
        run(
            "create",
            "container",
            "--name",
            P + "grid",
            "--type",
            "grid",
            "--rows",
            2,
            "--cols",
            3,
            "--tag",
            "g1",
        )
    )
    from rspace_client.inv.inv import ImageContainerPost

    with open(png(tmp / "background.png", (240, 240, 240), 100), "rb") as f:
        img = inv.create_image_container(
            ImageContainerPost(P + "image", f, locations=[(10, 10), (60, 40)])
        )["globalId"]
    for child in (grid, img):
        run("move", child, "--target", c1, fmt="table")

    a = gid_of(
        run(
            "create",
            "instrument",
            "--name",
            P + "A",
            "--template",
            nt,
            "--field",
            "Serial number=SN-A",
            "--field",
            "Mode=widefield",
            "--field",
            "Power (W)=3",
            "--tag",
            "a1,a2",
            "--description",
            "instrument A",
            "--target",
            grid,
            "--row",
            1,
            "--col",
            2,
        )
    )
    put_inventory_item(
        inv,
        "instruments",
        parse_id(a),
        {
            "extraFields": [
                {"newFieldRequest": True, "name": "Room", "type": "text", "content": "B.204"}
            ]
        },
    )
    put_inventory_item(
        inv,
        "instruments",
        parse_id(a),
        {
            "barcodes": [
                {
                    "newBarcodeRequest": True,
                    "data": "BC-A-123",
                    "format": "BARCODE",
                    "description": "e2e",
                }
            ]
        },
    )
    (tmp / "manual.txt").write_text("instrument A manual\n")
    run("upload", "attachment", tmp / "manual.txt", a, fmt="table")
    with open(png(tmp / "preview.png", (30, 160, 30)), "rb") as f:
        set_item_image(inv, a, f)

    b = gid_of(run("create", "instrument", "--name", P + "B", "--tag", "b1"))
    marker_coords, marker_ids = zip(*markers(img), strict=False)
    place_instrument(inv, parse_id(b), parse_id(img), location_id=marker_ids[1])
    c = gid_of(run("create", "instrument", "--name", P + "C", "--target", c1))
    d = gid_of(
        run(
            "create",
            "instrument",
            "--name",
            P + "D",
            "--template",
            nt,
            "--field",
            "Serial number=SN-D",
        )
    )
    s_rec = run("create", "sample", "--name", P + "sample")[1]
    ss = s_rec["subSamples"][0]["globalId"]
    run("move", ss, "--target", grid, "--row", 2, "--col", 1, fmt="table")

    src = {g: get(g) for g in (a, b, c, d, c1, grid, img, ss)}
    bench = location(src[d])[0]
    check(
        "source A has extra field, barcode, attachment, image, tags",
        src[a].get("extraFields")
        and src[a].get("barcodes")
        and src[a].get("attachments")
        and has_image(src[a])
        and tags(src[a]) == ["a1", "a2"],
        {k: bool(src[a].get(k)) for k in ("extraFields", "barcodes", "attachments", "tags")},
    )
    check(
        "source B sits on marker 2 of the image container",
        location(src[b])[0] == img,
        location(src[b]),
    )
    check(
        "source grid/image containers are inside the top container",
        location(src[grid])[0] == c1 and location(src[img])[0] == c1,
    )
    print(f"   source: {nt} {c1} {grid} {img} A={a} B={b} C={c} D={d} {ss} bench={bench}")

    # ================================================================== export
    e.section("export")
    snap = tmp / "snap"
    rc, _, out, err = run(
        "migrate", "export", "--container", c1, "--instrument", d, "--output", snap, fmt="table"
    )
    check("export --container --instrument exits 0", rc == 0, err)
    blob = json.loads((snap / "snapshot.json").read_text())
    insts = {i["globalId"]: i for i in blob.get("instruments", [])}
    check("snapshot schema_version 3", blob.get("schema_version") == 3, blob.get("schema_version"))
    check(
        "container walk + --instrument collected A, B, C, D",
        set(insts) == {a, b, c, d},
        sorted(insts),
    )
    check(
        "referenced instrument template auto-included",
        [t["globalId"] for t in blob.get("instrument_templates", [])] == [nt],
    )
    check(
        "containers exported: top, grid, image",
        {x["globalId"] for x in blob.get("containers", [])} == {c1, grid, img},
    )
    check(
        "sample in the grid exported",
        [x["globalId"] for x in blob.get("samples", [])] == [s_rec["globalId"]],
    )
    loc_a = insts[a]["_migration"]["location"]
    check(
        "A location: grid row 1 / col 2",
        (loc_a["container_global_id"], loc_a["grid_row"], loc_a["grid_col"]) == (grid, 1, 2),
        loc_a,
    )
    loc_b = insts[b]["_migration"]["location"]
    check(
        "B location: image container, marker_index 1",
        loc_b["container_global_id"] == img and loc_b.get("marker_index") == 1,
        loc_b,
    )
    check(
        "D location: workbench", insts[d]["_migration"]["location"]["container_global_id"] == bench
    )
    check(
        "attachment file exported",
        (snap / "attachments" / a / "manual.txt").exists()
        or any((snap / "attachments" / a).glob("*manual.txt")),
        list((snap / "attachments").rglob("*")),
    )
    check(
        "instrument preview image exported",
        (snap / "images" / f"{a}_preview.png").exists(),
        list((snap / "images").glob("*")) if (snap / "images").exists() else None,
    )
    check(
        "instrument template icon exported",
        any((snap / "icons").glob(f"{nt}_icon*")),
        list((snap / "icons").glob("*")) if (snap / "icons").exists() else None,
    )

    snap_t = tmp / "snap_template"
    rc, _, _, err = run(
        "migrate", "export", "--instrument-template", nt, "--output", snap_t, fmt="table"
    )
    blob_t = json.loads((snap_t / "snapshot.json").read_text())
    check(
        "export --instrument-template exports only the template",
        rc == 0
        and [t["globalId"] for t in blob_t["instrument_templates"]] == [nt]
        and not blob_t.get("instruments"),
        err,
    )
    snap_nf = tmp / "snap_nofiles"
    rc, _, _, err = run(
        "migrate", "export", "--instrument", a, "--no-files", "--output", snap_nf, fmt="table"
    )
    check(
        "export --no-files writes no attachments/images/icons",
        rc == 0
        and not any(
            (snap_nf / sub).exists() and any((snap_nf / sub).iterdir())
            for sub in ("attachments", "images", "icons")
        ),
        err,
    )

    # ================================================================== import
    e.section("import --dry-run")
    before = our_instruments()
    rc, _, out, err = migrate_import(snap, "--dry-run")
    check("dry run exits 0", rc == 0, err[-300:])
    check(
        "dry run would reuse the identical template",
        f"would reuse instrument template {nt}" in out,
        out[:400],
    )
    check("dry run would create 4 instruments", out.count("would create instrument:") == 4)
    after = our_instruments()
    check("dry run created nothing", after == before, set(after) - set(before))

    e.section("import")
    rc, m, out, err = migrate_import(snap)
    check(
        "import exits 0 with no errors", rc == 0 and "Migration complete" in out, (err + out)[-500:]
    )
    check("checkpoint removed after success", not (snap / "checkpoint.json").exists())
    check("identical template reused, not copied", m.get(nt) == nt and "↺" in out, m.get(nt))
    check("instrument template not duplicated", our_template_ids() == [nt], our_template_ids())
    new = {g: get(m[g]) for g in (a, b, c, d, c1, grid, img) if g in m}
    check(
        "all instruments and containers were mapped",
        len(new) == 7 and all(new.values()),
        sorted({a, b, c, d, c1, grid, img} - set(m)),
    )
    na, nb, nc, nd = (new.get(g) or {} for g in (a, b, c, d))

    check(
        "A': template, name and description",
        na.get("templateId") == parse_id(nt)
        and na.get("name") == P + "A"
        and na.get("description") == "instrument A",
    )
    check("A': field values", field_values(na) == field_values(src[a]), field_values(na))
    check("A': tags", tags(na) == ["a1", "a2"], tags(na))
    check(
        "A': extra field",
        [(x["name"], x["content"]) for x in na.get("extraFields") or []] == [("Room", "B.204")],
        na.get("extraFields"),
    )
    check(
        "A': barcode",
        [x["data"] for x in na.get("barcodes") or []] == ["BC-A-123"],
        na.get("barcodes"),
    )
    atts = na.get("attachments") or []
    check(
        "A': attachment keeps its original name",
        [x["name"] for x in atts] == ["manual.txt"],
        [x["name"] for x in atts],
    )
    if atts:
        dl = tmp / "dl"
        dl.mkdir()
        run("download", "attachment", atts[0]["id"], "-d", dl, fmt="table")
        got = list(dl.iterdir())
        check(
            "A': attachment content round-trips",
            got and got[0].read_text() == "instrument A manual\n",
            got,
        )
    check("A': preview image set", has_image(na))
    check(
        "A': grid cell row 1 / col 2 in the new grid",
        location(na) == (m[grid], (1, 2)),
        location(na),
    )
    check("B': in the new image container", location(nb)[0] == m[img], location(nb))
    nb_marker = nb.get("parentLocation") or {}
    check(
        "B': on the same marker (coords of marker 2)",
        (nb_marker.get("coordX"), nb_marker.get("coordY")) == marker_coords[1],
        nb_marker,
    )
    check(
        "new image container has the same markers",
        [mc for mc, _ in markers(m[img])] == list(marker_coords),
        markers(m[img]),
    )
    check("C': in the new top container", location(nc)[0] == m[c1], location(nc))
    check("D': on the importer's workbench", location(nd)[0] == bench, location(nd))
    check("D': field values", field_values(nd) == field_values(src[d]), field_values(nd))
    check("B'/C'/D' tags", tags(nb) == ["b1"] and tags(nc) == [] and tags(nd) == [])
    check(
        "new grid and image container nested in the new top container",
        location(new[grid])[0] == m[c1] and location(new[img])[0] == m[c1],
    )
    check(
        "container tags kept after placements",
        tags(new[c1]) == ["c1", "c2"] and tags(new[grid]) == ["g1"],
        (tags(new[c1]), tags(new[grid])),
    )
    grid_content = inv.get_container_by_id(parse_id(m[grid]), include_content=True)
    cells = sorted(
        ((loc.get("content") or {}).get("type"), loc["coordY"], loc["coordX"])
        for loc in grid_content.get("locations") or []
        if loc.get("content")
    )
    check(
        "new grid holds the instrument (1,2) and the subsample (2,1)",
        cells == [("INSTRUMENT", 1, 2), ("SUBSAMPLE", 2, 1)],
        cells,
    )
    src_a = get(a)
    check(
        "source items untouched",
        location(src_a) == (grid, (1, 2))
        and tags(src_a) == ["a1", "a2"]
        and location(get(b))[0] == img,
    )

    e.section("import --skip-instruments")
    before = our_instruments()
    rc, m2, out, err = migrate_import(snap, "--skip-instruments", "--skip-files")
    after = our_instruments()
    check("--skip-instruments exits 0", rc == 0, (err + out)[-300:])
    check("--skip-instruments created no instruments", after == before, set(after) - set(before))
    check("--skip-instruments still created the containers", all(g in m2 for g in (c1, grid, img)))

    e.section("instrument templates: --no-reuse-templates, name collision")
    rc, m3, out, err = migrate_import(snap_t, "--no-reuse-templates")
    copy = m3.get(nt)
    if copy and copy != nt:
        template_ids.append(copy)
    check(
        "--no-reuse-templates creates a copy", rc == 0 and copy and copy != nt, (err + out)[-300:]
    )
    if copy:
        cp = get(copy)
        check(
            "copy has the same field definitions",
            [(f["name"], f["type"]) for f in cp["fields"]]
            == [(f["name"], f["type"]) for f in nt_rec["fields"]],
        )
        check("copy got the icon", (cp.get("iconId") or -1) > 0, cp.get("iconId"))

    blob_d = json.loads((snap_t / "snapshot.json").read_text())
    blob_d["instrument_templates"][0]["description"] = "changed description"
    snap_d = tmp / "snap_template_changed"
    snap_d.mkdir()
    (snap_d / "snapshot.json").write_text(json.dumps(blob_d))
    rc, m4, out, err = migrate_import(snap_d, "--skip-files")
    if m4.get(nt) and m4[nt] != nt:
        template_ids.append(m4[nt])
    check(
        "same name but different definition: warns and creates a new template",
        rc == 0 and m4.get(nt) not in (None, nt) and "differs" in err,
        (err + out)[-300:],
    )

    e.section("checkpoint resume and v2 snapshots")
    (snap_t / "checkpoint.json").write_text(
        json.dumps(
            {
                "id_map": {nt: nt},
                "numeric_map": {},
                "errors": [],
                "reused_templates": [nt],
                "completed_phases": ["templates", "instrument_templates"],
            }
        )
    )
    before = our_template_ids()
    rc, _, out, err = migrate_import(snap_t, "--no-reuse-templates")
    check(
        "resume skips completed instrument_templates phase",
        rc == 0 and "Phase 2 (instrument templates) — skipped" in err,
        err[-300:],
    )
    check("resume created no template", our_template_ids() == before)

    blob_v2 = json.loads((snap / "snapshot.json").read_text())
    blob_v2["schema_version"] = 2
    blob_v2.pop("instruments", None)
    blob_v2.pop("instrument_templates", None)
    snap_v2 = tmp / "snap_v2"
    snap_v2.mkdir()
    (snap_v2 / "snapshot.json").write_text(json.dumps(blob_v2))
    rc, _, out, err = migrate_import(snap_v2, "--dry-run")
    check(
        "v2 snapshot (no instrument keys) dry-runs without a schema warning",
        rc == 0 and "schema" not in err.lower(),
        err[-300:],
    )

finally:
    e.section("cleanup")
    ours = our_instruments()
    if ours:
        rc, _, out, err = run("delete", "instrument", *ours, fmt="table")
        check(f"delete {len(ours)} instrument(s)", rc == 0, err)
    samples = e.find_named(P, "list", "samples", "-q", P)
    for sa in samples:
        for sub in (get(sa) or {}).get("subSamples") or []:
            if bench and location(sub)[0] and not location(sub)[0].startswith("BE"):
                run("move", sub["globalId"], "--target", bench, fmt="table")
    if samples:
        rc, _, out, err = run("delete", "sample", *samples, fmt="table")
        check(f"delete {len(samples)} sample(s)", rc == 0, err)
    # containers: nested ones must go first, so repeat until nothing is left
    for _ in range(5):
        conts = e.find_named(P, "search", P, "--type", "containers")
        if not conts:
            break
        run("delete", "container", *conts, fmt="table")
    check("no containers named " + P + "* left", not conts, conts)
    tids = sorted(set(template_ids + our_template_ids()))
    if tids:
        rc, _, out, err = run("delete", "instrument-template", *tids, fmt="table")
        check(f"delete instrument template(s) {' '.join(tids)}", rc == 0, err)
    left = our_instruments() + e.find_named(P, "list", "samples", "-q", P) + our_template_ids()
    check("nothing named " + P + "* left", not left, left)

sys.exit(e.report())
