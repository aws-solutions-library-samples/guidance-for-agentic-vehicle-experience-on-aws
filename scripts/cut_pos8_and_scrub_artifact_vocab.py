#!/usr/bin/env python3
"""
CVX FCD — rev 10: cut position 8, and take artifact-contract vocabulary off two faces.

User decisions 2026-08-11:
  (1) "yep, kill it"   -> delete position 8, the 'From Generative AI to Agentic AI' divider.
  (3) "yes, remove"    -> take artifact-contract field vocabulary off the slide faces.

Note on (3): computed_at / inputs_hash were NEVER on a slide face — they appear only in
speaker notes (positions 17, 23, 28), which is where the altitude rule puts them. The two
actual on-face occurrences are `evidence[]` (position 21) and the field-list
`timestamp · confidence · evidence` (position 23). Those are what this script rewords.
Notes are deliberately left alone.

Cutting a slide is the reverse of inserting one, and every step matters:
  1. drop <p:sldId> from ppt/presentation.xml
  2. drop <Relationship> from ppt/_rels/presentation.xml.rels
  3. drop ppt/slides/slide15.xml and its _rels
  4. drop ppt/notesSlides/notesSlide36.xml and its _rels  (verified not shared)
  5. drop both <Override> entries from [Content_Types].xml
  6. KEEP all media — all three images on this slide are used by other slides.

Verification: assert the exact removed/changed set, run structural_sweep, then os.replace.
"""

import hashlib
import os
import re
import shutil
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apply_stage_final_to_deck import structural_sweep  # noqa: E402

DECK = os.path.expanduser("~/Downloads/cvx-fcd/deck/CVX-FCD-merged.pptx")
BACKUP = os.path.expanduser("~/Downloads/cvx-fcd/archive/backups/pre-cut-pos8.pptx")

CUT_POSITION = 8

# --- (3) face rewordings: (slide part, shape name, old run text, new run text) ------ #
FACE_EDITS = [
    (
        "slide7.xml",
        "Band_HOW CVX CONSUMES IT",
        "Every agent finding carries evidence[] and provenance tracing back to a named source",
        "Every agent finding carries its own evidence and provenance tracing back to a named source",
    ),
    (
        "slide36.xml",
        "TextBox 9",
        "\u2192  Records the finding: timestamp \u00b7 confidence \u00b7 evidence",
        "\u2192  Records when it ran, how confident it is, and what it saw",
    ),
]


def slide_order(z):
    pres = z.read("ppt/presentation.xml").decode()
    rels = z.read("ppt/_rels/presentation.xml.rels").decode()
    r2t = {}
    for el in re.findall(r"<Relationship\b[^>]*/?>", rels):
        i = re.search(r'Id="([^"]+)"', el)
        t = re.search(r'Target="([^"]+)"', el)
        if i and t:
            r2t[i.group(1)] = t.group(1)
    rids = re.findall(r'<p:sldId\b[^>]*r:id="([^"]+)"', pres)
    return [r2t[r].split("/")[-1] for r in rids], rids


def main():
    if not os.path.exists(DECK):
        raise SystemExit(f"deck not found: {DECK}")
    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    shutil.copy(DECK, BACKUP)
    print(f"backup -> {BACKUP}")

    zin = zipfile.ZipFile(DECK)
    names = zin.namelist()
    order, rids = slide_order(zin)
    if len(order) != 48:
        raise SystemExit(f"expected 48 slides, found {len(order)}")

    part8 = order[CUT_POSITION - 1]
    rid8 = rids[CUT_POSITION - 1]
    print(f"\ncutting position {CUT_POSITION}: {part8} (r:id={rid8})")

    # Sanity: confirm it really is the divider we mean, not a mis-indexed slide.
    body8 = zin.read(f"ppt/slides/{part8}").decode()
    assert "Generative AI to Agentic AI" in body8, (
        f"{part8} is not the expected divider — refusing to delete"
    )

    rels8 = zin.read(f"ppt/slides/_rels/{part8}.rels").decode()
    m = re.search(r"notesSlides/(notesSlide\d+\.xml)", rels8)
    notes8 = m.group(1) if m else None
    print(f"  notes part: {notes8}")

    # Guard: notes part must not be shared (invariant 7).
    if notes8:
        for p in order:
            if p == part8:
                continue
            try:
                if notes8 in zin.read(f"ppt/slides/_rels/{p}.rels").decode():
                    raise SystemExit(f"{notes8} is shared with {p} — refusing to delete it")
            except KeyError:
                pass

    drop = {f"ppt/slides/{part8}", f"ppt/slides/_rels/{part8}.rels"}
    if notes8:
        drop.add(f"ppt/notesSlides/{notes8}")
        rn = f"ppt/notesSlides/_rels/{notes8}.rels"
        if rn in names:
            drop.add(rn)
    for d in sorted(drop):
        assert d in names, f"part to drop is not in the package: {d}"
    print(f"  dropping {len(drop)} parts: {sorted(x.split('/')[-1] for x in drop)}")

    edited = {}

    # --- 1. presentation.xml: remove the sldId ---------------------------------- #
    pres = zin.read("ppt/presentation.xml").decode()
    sld_el = re.search(r'<p:sldId id="\d+" r:id="' + re.escape(rid8) + r'"\s*/>', pres)
    assert sld_el, f"no <p:sldId> for {rid8}"
    assert pres.count(sld_el.group(0)) == 1
    npres = pres.replace(sld_el.group(0), "", 1)
    assert npres.count("<p:sldId ") == 47, f"expected 47 sldIds, got {npres.count('<p:sldId ')}"
    edited["ppt/presentation.xml"] = npres.encode()
    print(f"  presentation.xml: removed {sld_el.group(0)}")

    # --- 2. presentation.xml.rels: remove the relationship ---------------------- #
    prels = zin.read("ppt/_rels/presentation.xml.rels").decode()
    rel_el = None
    for el in re.findall(r"<Relationship\b[^>]*/>", prels):
        if re.search(r'Id="' + re.escape(rid8) + r'"', el):
            rel_el = el
            break
    assert rel_el, f"no <Relationship> for {rid8}"
    assert f"slides/{part8}" in rel_el, f"{rid8} does not target {part8}: {rel_el}"
    assert prels.count(rel_el) == 1
    edited["ppt/_rels/presentation.xml.rels"] = prels.replace(rel_el, "", 1).encode()
    print(f"  presentation.xml.rels: removed {rid8}")

    # --- 3. [Content_Types].xml: remove both overrides -------------------------- #
    ct = zin.read("[Content_Types].xml").decode()
    nct = ct
    for target in [f"/ppt/slides/{part8}"] + ([f"/ppt/notesSlides/{notes8}"] if notes8 else []):
        ov = re.search(r'<Override PartName="' + re.escape(target) + r'"[^>]*/>', nct)
        assert ov, f"no Override for {target}"
        assert nct.count(ov.group(0)) == 1
        nct = nct.replace(ov.group(0), "", 1)
        print(f"  [Content_Types].xml: removed Override {target}")
    edited["[Content_Types].xml"] = nct.encode()

    # --- (3) face rewordings ---------------------------------------------------- #
    print("\ntaking artifact-contract vocabulary off the faces:")
    for part, shape, old, new in FACE_EDITS:
        key = f"ppt/slides/{part}"
        assert key not in drop, f"{part} is being deleted; cannot also edit it"
        x = edited.get(key, zin.read(key)).decode()
        sps = [sp for sp in re.findall(r"<p:sp>.*?</p:sp>", x, re.S)
               if re.search(r'name="' + re.escape(shape) + r'"', sp)]
        assert len(sps) == 1, f"{part}: expected 1 shape named {shape!r}, found {len(sps)}"
        sp = sps[0]
        assert sp.count(old) == 1, f"{part}/{shape}: old text not found exactly once"
        nsp = sp.replace(old, new, 1)
        assert x.count(sp) == 1
        edited[key] = x.replace(sp, nsp, 1).encode()
        print(f"  {part} :: {shape}")
        print(f"      - {old}")
        print(f"      + {new}")

    # --- write atomically ------------------------------------------------------- #
    before = {n: hashlib.sha256(zin.read(n)).hexdigest() for n in names}
    tmp = DECK + ".tmp"
    ct_name = "[Content_Types].xml"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr(ct_name, edited.get(ct_name, zin.read(ct_name)))
        for n in names:
            if n == ct_name or n in drop:
                continue
            zout.writestr(n, edited.get(n, zin.read(n)))
    zin.close()

    # --- verify ----------------------------------------------------------------- #
    zchk = zipfile.ZipFile(tmp)
    after_names = set(zchk.namelist())
    removed = set(before) - after_names
    assert removed == drop, f"removed set wrong\n  got:      {sorted(removed)}\n  expected: {sorted(drop)}"
    changed = sorted(
        n for n in after_names if hashlib.sha256(zchk.read(n)).hexdigest() != before[n]
    )
    expected_changed = sorted(set(edited) - drop)
    assert changed == expected_changed, (
        f"changed set wrong\n  got:      {changed}\n  expected: {expected_changed}"
    )
    new_order, _ = slide_order(zchk)
    assert len(new_order) == 47, f"expected 47 slides, got {len(new_order)}"
    assert part8 not in new_order
    # media must survive
    assert any(n.startswith("ppt/media/") for n in after_names)
    zchk.close()

    print(f"\nremoved parts:  {sorted(x.split('/')[-1] for x in removed)}")
    print(f"changed parts:  {[c.split('/')[-1] for c in changed]}")

    problems, empty_frames = structural_sweep(tmp)
    print("structural sweep problems:", problems or "none")
    if problems:
        os.unlink(tmp)
        raise SystemExit("sweep failed — deck NOT written")

    os.replace(tmp, DECK)
    print(f"\nwrote {DECK}  (48 -> 47 slides)")


if __name__ == "__main__":
    main()
