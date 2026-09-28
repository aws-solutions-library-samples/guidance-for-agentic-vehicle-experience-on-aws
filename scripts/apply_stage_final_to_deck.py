#!/usr/bin/env python3
"""Transplant the five verified stage-slide shape trees into CVX-FCD-merged.pptx
at display positions 12-16 (parts slide30..slide34).

Design decisions, and why:

* Only <p:cSld> is transplanted. The destination's rels are left completely
  untouched. CVX-stage-final.pptx's notesSlide rels are malformed -- they point
  at ../../slides/ and are missing the notesMaster relationship -- so copying
  rels across would import that defect. The merged deck's notesSlide20..24 rels
  are correct and carry notesMaster.
* The source slides carry no image/media rels (only slideLayout7 + their notes),
  so the shape tree is self-contained and needs no rel remapping.
* Speaker notes are PRESERVED, not replaced. The source notes are 94-135 chars
  (just the discovery question); the destination notes are 1183-1976 chars of
  real presenter content. We append the discovery question to the destination.
* Shape ids are renumbered from 2 within each transplanted tree. python-pptx
  emits id="0", which is invalid OOXML and makes PowerPoint silently blank the
  slide.
* Written atomically: the complete zip is built in a .tmp and os.replace()d only
  after every assertion passes. A network fault previously left a half-written
  deck; recovery was rollback, not patch-forward.
"""

import os
import re
import shutil
import sys
import zipfile

DOWNLOADS = os.path.expanduser("~/Downloads")
DEST = os.path.join(DOWNLOADS, "CVX-FCD-merged.pptx")
SRC = os.path.join(DOWNLOADS, "CVX-stage-final.pptx")

# display position -> (destination slide part, destination notes part, source slide part)
MAPPING = [
    ("slide30.xml", "notesSlide20.xml", "slide201.xml"),
    ("slide31.xml", "notesSlide21.xml", "slide202.xml"),
    ("slide32.xml", "notesSlide22.xml", "slide203.xml"),
    ("slide33.xml", "notesSlide23.xml", "slide204.xml"),
    ("slide34.xml", "notesSlide24.xml", "slide205.xml"),
]

DISCOVERY_QUESTIONS = {
    "notesSlide20.xml": "\u2192  Which of your existing channels takes the most pre-purchase questions \u2014 and what happens to those questions today?",
    "notesSlide21.xml": "\u2192  Where in your buying flow does a customer today have to start over in another channel?",
    "notesSlide22.xml": "\u2192  Between order and delivery, how many times does a customer contact you \u2014 and what do they ask?",
    "notesSlide23.xml": "\u2192  Between service visits, what does a customer hear from you that isn\u2019t marketing?",
    "notesSlide24.xml": "\u2192  When a customer reaches a human today, what does that human already know?",
}

CSLD_RE = re.compile(r"<p:cSld(?:\s[^>]*)?>.*?</p:cSld>", re.S)


def _xml_escape(s):
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def next_backup_path():
    n = 1
    while True:
        p = os.path.join(DOWNLOADS, f"CVX-FCD-merged.bak{n}.pptx")
        if not os.path.exists(p):
            return p
        n += 1


def renumber_shape_ids(csld_xml, start=2):
    """Renumber every <p:cNvPr id="..."> sequentially from `start`.

    Guarantees unique, non-zero ids within the slide -- an OOXML requirement
    that python-pptx violates by emitting id="0".
    """
    counter = [start]

    def sub(m):
        cur = counter[0]
        counter[0] += 1
        return f'{m.group(1)}id="{cur}"'

    out = re.sub(r'(<p:cNvPr\s+)id="\d+"', sub, csld_xml)
    return out, counter[0] - start


def append_discovery_question(notes_xml, question):
    """Append a DISCOVERY QUESTION block to the notes body placeholder.

    Locates the <p:sp> whose <p:ph> is type="body", then inserts paragraphs
    before that shape's closing </p:txBody>. Existing notes are untouched.
    """
    sps = list(re.finditer(r"<p:sp>.*?</p:sp>", notes_xml, re.S))
    target = None
    for m in sps:
        if re.search(r'<p:ph\b[^>]*type="body"', m.group(0)):
            target = m
            break
    if target is None:
        raise AssertionError("no body placeholder found in notesSlide")

    sp = target.group(0)
    close = sp.rfind("</p:txBody>")
    if close == -1:
        raise AssertionError("body placeholder has no </p:txBody>")

    block = (
        "<a:p><a:endParaRPr/></a:p>"
        "<a:p><a:r><a:rPr lang=\"en-US\" b=\"1\" dirty=\"0\"/>"
        "<a:t>DISCOVERY QUESTION</a:t></a:r></a:p>"
        "<a:p><a:r><a:rPr lang=\"en-US\" dirty=\"0\"/>"
        f"<a:t>{_xml_escape(question)}</a:t></a:r></a:p>"
    )
    new_sp = sp[:close] + block + sp[close:]
    return notes_xml[: target.start()] + new_sp + notes_xml[target.end() :]


def structural_sweep(path):
    """Every invariant we have learned the hard way, asserted on the real file."""
    problems = []
    z = zipfile.ZipFile(path)

    names = z.namelist()
    if names[0] != "[Content_Types].xml":
        problems.append(f"[Content_Types].xml is not first entry (got {names[0]})")
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        problems.append(f"duplicate zip entries: {sorted(dupes)}")
    if z.testzip() is not None:
        problems.append("zip integrity check failed")

    ct = z.read("[Content_Types].xml").decode("utf8")
    slides = sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
    notes = sorted(n for n in names if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n))
    for part in slides + notes:
        if f'PartName="/{part}"' not in ct:
            problems.append(f"no content-type override for {part}")

    # notes wiring: no orphans, no dangling refs, no notes part shared by >1 slide
    referenced = {}
    for s in slides:
        rp = f"ppt/slides/_rels/{os.path.basename(s)}.rels"
        if rp not in names:
            continue
        r = z.read(rp).decode("utf8")
        for tgt in re.findall(r'Target="([^"]+notesSlide\d+\.xml)"', r):
            if "../../" in tgt:
                problems.append(f"{s}: notes rel wrong depth ({tgt})")
            base = os.path.basename(tgt)
            referenced.setdefault(base, []).append(s)
    for base, refs in referenced.items():
        if f"ppt/notesSlides/{base}" not in names:
            problems.append(f"dangling notes ref {base} from {refs}")
        if len(refs) > 1:
            problems.append(f"notes part {base} shared by {refs}")
    for n in notes:
        if os.path.basename(n) not in referenced:
            problems.append(f"orphaned notes part {n}")
        nr = f"ppt/notesSlides/_rels/{os.path.basename(n)}.rels"
        if nr not in names:
            problems.append(f"{n}: missing rels")
        else:
            r = z.read(nr).decode("utf8")
            if "notesMaster" not in r:
                problems.append(f"{n}: rels missing notesMaster")

    # shape ids + background, per slide
    empty_frames = {}
    for s in slides:
        x = z.read(s).decode("utf8")
        ids = re.findall(r'<p:cNvPr\s+id="(\d+)"', x)
        if "0" in ids:
            problems.append(f"{s}: has cNvPr id=0")
        if len(ids) != len(set(ids)):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            problems.append(f"{s}: duplicate shape ids {dup}")
        # <p:bg> must be FIRST child of <p:cSld> when present. It is legitimately
        # absent on the light-themed slides (slide13/slide14), which render light
        # via their own full-coverage fills and must never receive a dark bg.
        m = CSLD_RE.search(x)
        if m and "<p:bg>" in m.group(0):
            inner = re.sub(r"^<p:cSld(?:\s[^>]*)?>", "", m.group(0))
            if not inner.lstrip().startswith("<p:bg>"):
                problems.append(f"{s}: <p:bg> present but not first child of <p:cSld>")

        # An empty text frame is only a defect when it belongs to a shape meant to
        # hold text. Pure geometry legitimately has one: the journey-band pill sits
        # behind its own label, and the icon zone is a dashed border with a separate
        # caption shape. Anything else with an empty frame is the 64-chip defect
        # regressing, so allowlist by name rather than by count.
        offenders = []
        for sp in re.findall(r"<p:sp>.*?</p:sp>", x, re.S):
            tb = re.search(r"<p:txBody>.*?</p:txBody>", sp, re.S)
            if not tb:
                continue
            if "".join(re.findall(r"<a:t>(.*?)</a:t>", tb.group(0), re.S)).strip():
                continue
            nm = re.search(r'<p:cNvPr[^>]*name="([^"]*)"', sp)
            nm = nm.group(1) if nm else "(unnamed)"
            if nm.startswith(("D_Pill_", "IconPlaceholder_")):
                continue
            offenders.append(nm)
        if offenders:
            empty_frames[os.path.basename(s)] = offenders

    # every sldIdLst r:id resolvable
    pres = z.read("ppt/presentation.xml").decode("utf8")
    prels = z.read("ppt/_rels/presentation.xml.rels").decode("utf8")
    known = set(re.findall(r'Id="([^"]+)"', prels))
    for rid in re.findall(r'<p:sldId[^>]*r:id="([^"]+)"', pres):
        if rid not in known:
            problems.append(f"sldIdLst references unresolvable {rid}")

    z.close()
    return problems, empty_frames


def main():
    if not os.path.exists(DEST):
        sys.exit(f"missing {DEST}")
    if not os.path.exists(SRC):
        sys.exit(f"missing {SRC}")

    backup = next_backup_path()
    shutil.copy2(DEST, backup)
    print(f"backup  -> {os.path.basename(backup)}")

    zsrc = zipfile.ZipFile(SRC)
    zdst = zipfile.ZipFile(DEST)

    # Build replacement payloads before writing anything.
    new_parts = {}
    for dest_slide, dest_notes, src_slide in MAPPING:
        src_xml = zsrc.read(f"ppt/slides/{src_slide}").decode("utf8")
        dst_xml = zdst.read(f"ppt/slides/{dest_slide}").decode("utf8")

        sm = CSLD_RE.search(src_xml)
        dm = CSLD_RE.search(dst_xml)
        assert sm, f"no cSld in source {src_slide}"
        assert dm, f"no cSld in dest {dest_slide}"

        csld, n_shapes = renumber_shape_ids(sm.group(0))

        inner = re.sub(r"^<p:cSld(?:\s[^>]*)?>", "", csld)
        assert inner.lstrip().startswith("<p:bg>"), (
            f"{src_slide}: <p:bg> must be first child of <p:cSld>"
        )

        merged = dst_xml[: dm.start()] + csld + dst_xml[dm.end() :]
        new_parts[f"ppt/slides/{dest_slide}"] = merged.encode("utf8")

        notes_xml = zdst.read(f"ppt/notesSlides/{dest_notes}").decode("utf8")
        before = len("".join(re.findall(r"<a:t>(.*?)</a:t>", notes_xml, re.S)))
        notes_new = append_discovery_question(notes_xml, DISCOVERY_QUESTIONS[dest_notes])
        after = len("".join(re.findall(r"<a:t>(.*?)</a:t>", notes_new, re.S)))
        assert after > before, f"{dest_notes}: notes did not grow"
        new_parts[f"ppt/notesSlides/{dest_notes}"] = notes_new.encode("utf8")

        print(
            f"  {dest_slide:<13} <- {src_slide:<13} "
            f"{n_shapes:>3} shapes | notes {before} -> {after} chars"
        )

    # Atomic rewrite. [Content_Types].xml first, no duplicate entries.
    tmp = DEST + ".tmp"
    written = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        ct = "[Content_Types].xml"
        out.writestr(zdst.getinfo(ct), zdst.read(ct))
        written.add(ct)
        for item in zdst.infolist():
            if item.filename in written:
                continue
            data = new_parts.get(item.filename)
            out.writestr(item, zdst.read(item.filename) if data is None else data)
            written.add(item.filename)

    zsrc.close()
    zdst.close()

    problems, empty = structural_sweep(tmp)

    targets = {m[0] for m in MAPPING}
    target_empty = {k: v for k, v in empty.items() if k in targets}
    if target_empty:
        problems.append(
            f"target slides have unexpected empty text frames: {target_empty}"
        )

    # Positively assert the 64-chip defect is gone: the old grid used chips named
    # Chip_* / Rectangle* holding no text, plus the 'WHAT THIS LOOKS LIKE' heading
    # that sat above them.
    zc = zipfile.ZipFile(tmp)
    for part in sorted(targets):
        x = zc.read(f"ppt/slides/{part}").decode("utf8")
        if "WHAT THIS LOOKS LIKE" in x:
            problems.append(f"{part}: old 'WHAT THIS LOOKS LIKE' heading still present")
        stale = [
            n
            for n in re.findall(r'<p:cNvPr[^>]*name="([^"]*)"', x)
            if n.startswith(("Chip_", "Rectangle"))
        ]
        if stale:
            problems.append(f"{part}: stale chip shapes remain {stale}")
    zc.close()

    if problems:
        os.remove(tmp)
        print("\nFAILED -- deck NOT modified:")
        for p in problems:
            print("  !", p)
        sys.exit(1)

    os.replace(tmp, DEST)

    print("\nstructural sweep: clean")
    print("positions 12-16: no unexpected empty text frames, no stale chips")
    if empty:
        print(f"  (unexpected empty frames elsewhere in deck, untouched: {empty})")
    print(f"wrote   -> {os.path.basename(DEST)}")


if __name__ == "__main__":
    main()
