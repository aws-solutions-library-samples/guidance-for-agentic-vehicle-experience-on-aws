#!/usr/bin/env python3
"""
CVX FCD — rev 9: speaker notes for the closing proof slides, plus two repairs.

Four changes, all minimal-diff:

  1. Position 30 (customer reference wall, 9 named AWS customers) — notes rewritten
     from 189 chars to a full CLAIM / EVIDENCE / WHAT-NOT-TO-PROMISE block, modelled
     on position 31's Ferrari notes which already do this well.
  2. Position 32 (customer examples with figures) — same treatment, and surfaces the
     contradiction that its notes said "(appendix — not presented)" while the slide
     sits in the main deck before the closing slide.
  3. Position 25 (one assistant / four specialists) — RESTORES two blocks that the
     rev-8 retitle dropped when it rewrote these notes wholesale: a WHAT NOT TO
     PROMISE and the TECHNICAL DETAIL tool-identifier mapping. Phase reference is
     now by slide NAME, not ordinal, because ordinals have shifted twice.
  4. Position 8 (section divider) — deletes a stale "© 2023 ... AWS Confidential."
     footer text box that overlaps the correct 2026 house footer by ~0.9in. Isolated
     to this one slide (swept: 1 of 48).

Why notes and not slide faces: the altitude rule says AWS service names and
implementation identifiers belong in notes, and attribution risk is a presenter
problem, not an audience one.

Verification: per-part SHA-256 (expect exactly 4 changed parts), then the reusable
structural_sweep from apply_stage_final_to_deck.py, then atomic os.replace.
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
BACKUP = os.path.expanduser("~/Downloads/cvx-fcd/archive/backups/pre-proof-notes.pptx")


# --------------------------------------------------------------------------- #
# Notes content
# --------------------------------------------------------------------------- #

NOTES_POS30 = """CUSTOMER REFERENCES — nine published AWS case studies.

DO NOT READ this list aloud. Say: "The names behind the outcomes you just saw \u2014 all published case studies." Then move on. This wall is meant to be seen, not recited.

CLAIM: automotive brands are already in production with AWS on exactly the customer-experience surfaces this deck has described \u2014 shopping and price intelligence, configurators, aftersales, personalisation, sales enablement, payments, and predictive maintenance.

EVIDENCE: all nine are published AWS case studies, indexed at aws.amazon.com/solutions/case-studies/. Every name on this slide maps to a public study. Nothing here is under NDA and nothing here needs hedging.

WHAT EACH IS GOOD FOR, if the room asks:
\u2022 CarGurus \u2014 AI price intelligence in car shopping. Reach for this on Discover.
\u2022 Audi \u2014 more than 60% compute cost saving on the car-configurator backend. Good for Acquire, and the best answer in the deck to "what does it cost to run?"
\u2022 Mercedes-Benz \u2014 global cloud transformation of the aftersales application landscape. Good for Service, and for "this is not a pilot."
\u2022 Ferrari \u2014 generative AI for personalisation and production efficiency. Full slide follows; its numbers and its caveat are in that slide's notes.
\u2022 Cox Automotive \u2014 Claude in Amazon Bedrock for car sales. Use when asked which model.
\u2022 Aston Martin Lagonda \u2014 sales increase using AI on AWS, delivered with Data Reply. Use for the partner-delivery question.
\u2022 TrueCar \u2014 automated ETL and improved personalisation. Pairs with the data-foundation slide.
\u2022 MAN Truck &amp; Bus \u2014 scalable payment system. Good for fleet, and for Acquire's money seam.
\u2022 Motability Operations \u2014 predictive-maintenance pilot. Good for fleet \u2014 and say "pilot", because that is what the study says.

WHAT NOT TO PROMISE \u2014 the most important line on this slide:
None of these nine is a CVX deployment. They validate the pattern; they are not reference implementations of the architecture in this deck. If asked "did they build it this way?", say: "These are AWS automotive customers in production, on the same services and the same problems. The reference architecture we showed you is how we would assemble it for you." Never imply a named customer is running CVX.

DO NOT ADD BMW OR STELLANTIS TO THIS SLIDE. They appear on the two-paths slide as Alexa Custom Assistant references, which is a different category: they are Alexa/ACA references, not AWS customer references, and they are not published AWS case studies. Conflating the two would put the one unsupportable claim on an otherwise fully-sourced slide.

Motability is a pilot in its own case study. Do not upgrade it to production."""


NOTES_POS32 = """CUSTOMER EXAMPLES \u2014 three further published studies, these ones carrying figures.

STATUS CONTRADICTION TO RESOLVE BEFORE PRESENTING: these notes previously read "(appendix \u2014 not presented)", but this slide currently sits in the main deck immediately before the closing slide. Decide which it is \u2014 either present it as the numbers-bearing companion to the reference wall, or move it behind the appendix divider. Do not leave it as a slide the presenter has no plan for.

IF PRESENTED \u2014 CLAIM: these customer-experience outcomes are measurable, not merely qualitative.
\u2022 Mitsubishi Motors \u2014 world's first cinematic 3D driving configurator. 5x leads and 2x ROI against the previous 2D image-based configurator. This is the strongest single number in the deck for Discover and Acquire.
\u2022 Volkswagen Middle East \u2014 aftersales retention and loyalty. 80% conversion on targeted customers; 12% accuracy on customer behaviour patterns for upsell, price sensitivity and loyalty. Good for Own and Service, and it substantiates the loyalty argument that closes the one-assistant slide.
\u2022 Hyundai \u2014 digital showroom on Amazon.com: configure a vehicle, estimate a price, search local dealer inventory. Use this when asked about channels the OEM does not own.

EVIDENCE: all three are published AWS case studies. Quote the figures exactly as written \u2014 5x, 2x, 80%, 12%. Do not round them, re-base them, or combine them.

WHAT NOT TO PROMISE:
The 12% is behaviour-pattern ACCURACY, not a conversion or revenue figure. It has been misread as "12% uplift" \u2014 it is not. If you are unsure what a figure is measuring, cite the case study rather than the number.
The 5x and 2x are both stated against that customer's own prior 2D configurator, not against an industry baseline. Say "versus their previous configurator."
As with the reference wall: none of these is a CVX deployment. Pattern validation, not a reference implementation.

DISCOVERY QUESTION, if the room is engaged: "Which of these outcomes would matter most to you in the first year?" It turns a proof slide into a scoping conversation, which is the point of a first call."""


# Appended to the existing rev-8 notes on position 25, restoring dropped content.
RESTORE_POS25 = """

WHAT NOT TO PROMISE: do not imply all four agents land in a first engagement. The phased approach on the "HOW WE'D START" slide is the honest answer \u2014 the first phase grounds the Own-stage assistant, and the full four-agent deployment comes later. (Restored 2026-08-11: this block and the two below were dropped when these notes were rewritten for the one-assistant retitle. Phases and slides are referenced by NAME here, not by ordinal, because deck ordinals have now shifted twice.)

TECHNICAL DETAIL \u2014 the implementation identifiers behind each column, for a technical Q&amp;A only. These are deliberately absent from the slide face per the altitude rule; speaker notes are the sanctioned place for them.
Energy &amp; Charging: energy_summary \u00b7 charging_history
Vehicle Health: triage classifier \u00b7 diy_repair_advisor \u00b7 book \u00b7 service_network
Ownership Cost: service_history \u00b7 coverage data \u00b7 partner API
Household Mobility: offers_lookup \u00b7 variant_compare \u00b7 ota_history \u00b7 lead_capture

FLEET VARIANT: agent 4 (Household Mobility Concierge) becomes replacement-cycle and utilisation planning for fleet operators. This is already on the slide face; the note is here so the presenter can expand on it."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def xml_escape(s):
    """Escape for XML text content. '&' first, and never double-escape entities."""
    s = re.sub(r"&(?!(amp|lt|gt|quot|apos);)", "&amp;", s)
    return s.replace("<", "&lt;").replace(">", "&gt;")


def slide_order(z):
    pres = z.read("ppt/presentation.xml").decode()
    rels = z.read("ppt/_rels/presentation.xml.rels").decode()
    r2t = {}
    for el in re.findall(r"<Relationship\b[^>]*/?>", rels):
        i = re.search(r'Id="([^"]+)"', el)
        t = re.search(r'Target="([^"]+)"', el)
        if i and t:
            r2t[i.group(1)] = t.group(1)
    ids = re.findall(r'<p:sldId\b[^>]*r:id="([^"]+)"', pres)
    return [r2t[i].split("/")[-1] for i in ids]


def notes_part_for(z, slide_part):
    rels = z.read(f"ppt/slides/_rels/{slide_part}.rels").decode()
    m = re.search(r'Target="\.\./notesSlides/(notesSlide\d+\.xml)"', rels)
    if not m:
        raise SystemExit(f"{slide_part} has no notesSlide relationship")
    return m.group(1)


def body_sp(xml):
    """Return the <p:sp> element that is the notes body placeholder."""
    for sp in re.findall(r"<p:sp>.*?</p:sp>", xml, re.S):
        if re.search(r'<p:ph[^>]*type="body"', sp):
            return sp
    raise SystemExit("no body placeholder found in notesSlide")


# Verbatim paragraph patterns lifted from notesSlide51.xml, which renders correctly.
# Positions 30/32 originally stored notes as ONE run with literal newlines, which is
# unusual OOXML — PowerPoint may collapse those to spaces and render the whole block
# as a single unbroken paragraph in the presenter pane. Real paragraphs instead.
PARA = '<a:p><a:r><a:rPr lang="en-US" dirty="0"/><a:t>{}</a:t></a:r></a:p>'
SPACER = "<a:p><a:endParaRPr/></a:p>"


def build_paras(text):
    """Turn plain text into a paragraph sequence; blank lines become spacers."""
    out = []
    for line in text.split("\n"):
        out.append(SPACER if not line.strip() else PARA.format(xml_escape(line)))
    return "".join(out)


def _sp_para_span(sp):
    """Character span of the paragraph sequence inside a body placeholder."""
    first = sp.find("<a:p>")
    last = sp.rfind("</a:p>")
    if first == -1 or last == -1:
        raise SystemExit("body placeholder has no paragraphs")
    return first, last + len("</a:p>")


def set_notes_text(xml, new_text):
    """Replace the whole paragraph sequence of the notes body placeholder."""
    sp = body_sp(xml)
    a, b = _sp_para_span(sp)
    new_sp = sp[:a] + build_paras(new_text) + sp[b:]
    assert new_sp != sp, "notes body substitution was a no-op"
    return xml.replace(sp, new_sp, 1)


def append_notes_text(xml, extra_text):
    """Append paragraphs after the existing ones, leaving current content intact."""
    sp = body_sp(xml)
    _, b = _sp_para_span(sp)
    new_sp = sp[:b] + build_paras(extra_text) + sp[b:]
    assert new_sp != sp, "notes append was a no-op"
    return xml.replace(sp, new_sp, 1)


def get_notes_text(xml):
    """Concatenate all run text in the notes body placeholder (for reporting)."""
    return " ".join(re.findall(r"<a:t>(.*?)</a:t>", body_sp(xml), re.S))


def drop_stale_footer(xml):
    """Remove the <p:sp> whose text carries the stale 2023 / AWS Confidential footer."""
    targets = [
        sp
        for sp in re.findall(r"<p:sp>.*?</p:sp>", xml, re.S)
        if "\u00a9 2023" in sp or "AWS Confidential" in sp
    ]
    if len(targets) != 1:
        raise SystemExit(f"expected exactly 1 stale footer shape, found {len(targets)}")
    sp = targets[0]
    # Guard: never delete something that also carries the correct house footer.
    assert "Amazon Confidential and Trademark" not in sp, (
        "refusing to delete a shape that also carries the house footer"
    )
    return xml.replace(sp, "", 1)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    if not os.path.exists(DECK):
        raise SystemExit(f"deck not found: {DECK}")

    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    shutil.copy(DECK, BACKUP)
    print(f"backup -> {BACKUP}")

    zin = zipfile.ZipFile(DECK)
    names = zin.namelist()
    order = slide_order(zin)
    if len(order) != 48:
        raise SystemExit(f"expected 48 slides, found {len(order)}")

    s8, s25, s30, s32 = order[7], order[24], order[29], order[31]
    n25 = notes_part_for(zin, s25)
    n30 = notes_part_for(zin, s30)
    n32 = notes_part_for(zin, s32)
    print(f"pos  8 slide  = {s8}   (stale footer removal)")
    print(f"pos 25 notes  = {n25}  (restore dropped blocks)")
    print(f"pos 30 notes  = {n30}  (author reference-wall notes)")
    print(f"pos 32 notes  = {n32}  (author examples notes)")

    if len({n25, n30, n32}) != 3:
        raise SystemExit("notes parts are not distinct — refusing to write")

    before = {n: hashlib.sha256(zin.read(n)).hexdigest() for n in names}
    edited = {}

    # --- 1/2. Author notes for positions 30 and 32 -------------------------- #
    for part, text, label in (
        (n30, NOTES_POS30, "pos30"),
        (n32, NOTES_POS32, "pos32"),
    ):
        x = zin.read(f"ppt/notesSlides/{part}").decode()
        old_len = len(get_notes_text(x))
        nx = set_notes_text(x, text)
        edited[f"ppt/notesSlides/{part}"] = nx.encode()
        print(f"  {label}: notes {old_len} -> {len(text)} chars")

    # --- 3. Restore blocks dropped from position 25 ------------------------- #
    x25 = zin.read(f"ppt/notesSlides/{n25}").decode()
    cur = get_notes_text(x25)
    if "TECHNICAL DETAIL" in cur:
        raise SystemExit("pos25 notes already contain TECHNICAL DETAIL — already restored?")
    # Append paragraphs; existing content is left byte-identical.
    nx25 = append_notes_text(x25, RESTORE_POS25)
    edited[f"ppt/notesSlides/{n25}"] = nx25.encode()
    new25 = get_notes_text(nx25)
    assert cur in new25 or cur.split(" ")[0] in new25, "existing pos25 notes were disturbed"
    print(f"  pos25: notes {len(cur)} -> {len(new25)} chars (restored, existing text intact)")

    # --- 4. Drop the stale overlapping footer on position 8 ----------------- #
    x8 = zin.read(f"ppt/slides/{s8}").decode()
    nx8 = drop_stale_footer(x8)
    assert "\u00a9 2023" not in nx8 and "AWS Confidential" not in nx8
    assert "Amazon Confidential and Trademark" in nx8, "house footer must survive"
    edited[f"ppt/slides/{s8}"] = nx8.encode()
    print(f"  pos8: stale footer shape removed ({len(x8)} -> {len(nx8)} bytes)")

    # --- write atomically -------------------------------------------------- #
    tmp = DECK + ".tmp"
    ct = "[Content_Types].xml"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr(ct, zin.read(ct))  # must be first entry
        for n in names:
            if n == ct:
                continue
            zout.writestr(n, edited.get(n, zin.read(n)))
    zin.close()

    # --- verify: exactly the 4 intended parts changed ---------------------- #
    zchk = zipfile.ZipFile(tmp)
    after = {n: hashlib.sha256(zchk.read(n)).hexdigest() for n in zchk.namelist()}
    zchk.close()

    if set(after) != set(before):
        raise SystemExit("part set changed — refusing to write")
    changed = sorted(n for n in before if before[n] != after[n])
    print("\nchanged parts:", changed)
    if set(changed) != set(edited):
        os.unlink(tmp)
        raise SystemExit(f"unexpected change set\n  got:      {changed}\n  expected: {sorted(edited)}")

    # structural_sweep returns (problems, empty_frames). empty_frames is
    # informational — the original deck is full of legitimately empty decorative
    # Freeform/AutoShape/Rectangle frames. Fail only on real problems, but do
    # assert the slide we edited did not acquire one.
    problems, empty_frames = structural_sweep(tmp)
    if s8 in empty_frames:
        problems.append(f"{s8} gained empty text frames: {empty_frames[s8]}")
    print("structural sweep problems:", problems or "none")
    if problems:
        os.unlink(tmp)
        raise SystemExit("sweep failed — deck NOT written")

    os.replace(tmp, DECK)
    print(f"\nwrote {DECK}")


if __name__ == "__main__":
    main()
