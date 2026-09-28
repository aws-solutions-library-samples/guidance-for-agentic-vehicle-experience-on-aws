#!/usr/bin/env python3
"""
CVX FCD — rev 12: draw Figure 2 (REFERENCE ARCHITECTURE) natively into position 26.

Spec source: ~/dotkiro/portfolio/figure-briefs.md § "Figure 2 — CVX Agentic Orchestration",
plus the requirements block that was sitting in this slide's placeholder notes.

Requirements honoured:
  - Tier 1 conversational spine running left-to-right
  - Tier 2 lane running in parallel
  - the two lanes VISIBLY MEET at the artifact store  <- the point of the figure
  - NO LLM in the Tier 2 lane (the tier is defined by nobody waiting, not by a model)
  - numbered callouts keyed to the speaker notes (1-9)
  - cross-account boundary marked (dashed = a different AWS account)
  - artifact store carries computed_at, so narration is "as of last Tuesday", not live state

Two deliberate deviations from the brief, both because the brief targets the Implementation
Guide and this is the first-call deck:

  1. NO AWS SERVICE NAMES ON THE FACE. The deck's altitude rule keeps service names to the two
     dedicated service slides. Every box is a capability; the service mapping is in the notes.
  2. NO "target architecture" markers on the specialists. The brief carries an honesty
     constraint for the IG (only agent 2 is deployed). User direction 2026-08-11: "it doesn't
     matter if they are deployed or not, we are presenting a vision, not an implementation."
     That constraint still applies to the IG version of this figure — recorded in the handover
     brief so it is not lost.

Drawing is native shapes, not an image, so it stays editable and crisp at any zoom.
Amber (FF9900) is used semantically throughout: amber == deterministic, not agentic.
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
BACKUP = os.path.expanduser("~/Downloads/cvx-fcd/archive/backups/pre-figure2.pptx")
SLIDE = "slide40.xml"
NOTES = "notesSlide30.xml"

EMU = 914400
def E(inches): return str(int(round(inches * EMU)))

# ---- palette (house) ------------------------------------------------------- #
WHITE, MUTED, GHOST = "FFFFFF", "9BA4B4", "232F3E"
BLUE, AMBER, PALE = "0047AD", "FF9900", "F5F7FF"

FONT_B = "Amazon Ember Display Bold"
FONT_R = "Amazon Ember Display"

# ---- geometry ------------------------------------------------------------- #
X0, X1 = 0.75, 18.85                 # placeholder bounds
W = X1 - X0                          # 18.10

T1_LABEL_Y = 2.20
T1_Y, T1_H = 2.54, 1.58
READ_Y, ARROW_H = 4.20, 0.44
STORE_Y, STORE_H = 4.72, 1.00
WRITE_Y = 5.80
T2_LABEL_Y = 6.32
T2_Y, T2_H = 6.66, 1.58
FOOT_Y, FOOT_H = 8.40, 0.30

GAP = 0.50
T1_N, T2_N = 5, 4
T1_W = (W - GAP * (T1_N - 1)) / T1_N          # 3.22
T2_W = (W - GAP * (T2_N - 1)) / T2_N          # 4.15
T1_X = [X0 + i * (T1_W + GAP) for i in range(T1_N)]
T2_X = [X0 + i * (T2_W + GAP) for i in range(T2_N)]

_id = [100]
def nid():
    _id[0] += 1
    return _id[0]


def rect(name, x, y, w, h, fill, dashed=False):
    """Pure graphic: fill, optional dashed outline. NO <p:txBody> (invariant)."""
    ln = (f'<a:ln w="12700"><a:solidFill><a:srgbClr val="{MUTED}"/></a:solidFill>'
          f'<a:prstDash val="sysDash"/></a:ln>') if dashed else '<a:ln><a:noFill/></a:ln>'
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{nid()}" name="{name}"/><p:cNvSpPr/><p:nvPr/>'
            f'</p:nvSpPr><p:spPr><a:xfrm><a:off x="{E(x)}" y="{E(y)}"/>'
            f'<a:ext cx="{E(w)}" cy="{E(h)}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>{ln}</p:spPr></p:sp>')


def uparrow(name, x, y, h):
    """Vertical line with the arrowhead at the TOP. Pure graphic, no txBody."""
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{nid()}" name="{name}"/><p:cNvSpPr/><p:nvPr/>'
            f'</p:nvSpPr><p:spPr><a:xfrm><a:off x="{E(x)}" y="{E(y)}"/>'
            f'<a:ext cx="0" cy="{E(h)}"/></a:xfrm>'
            f'<a:prstGeom prst="line"><a:avLst/></a:prstGeom>'
            f'<a:ln w="22225"><a:solidFill><a:srgbClr val="{MUTED}"/></a:solidFill>'
            f'<a:headEnd type="triangle" w="med" len="med"/></a:ln></p:spPr></p:sp>')


def esc(s):
    s = re.sub(r"&(?!(amp|lt|gt|quot|apos);)", "&amp;", s)
    return s.replace("<", "&lt;").replace(">", "&gt;")


def para(text, size, colour, font, bold, algn=None, spc_before=0):
    a = f'<a:pPr{f" algn=\"{algn}\"" if algn else ""}>'
    a += '<a:lnSpc><a:spcPct val="100000"/></a:lnSpc>'
    if spc_before:
        a += f'<a:spcBef><a:spcPts val="{spc_before}"/></a:spcBef>'
    a += '</a:pPr>'
    return (f'<a:p>{a}<a:r><a:rPr lang="en-US" sz="{size}" dirty="0" b="{1 if bold else 0}">'
            f'<a:solidFill><a:srgbClr val="{colour}"/></a:solidFill>'
            f'<a:latin typeface="{font}"/></a:rPr><a:t>{esc(text)}</a:t></a:r></a:p>')


def textbox(name, x, y, w, h, paras, anchor="t"):
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{nid()}" name="{name}"/>'
            f'<p:cNvSpPr txBox="true"/><p:nvPr/></p:nvSpPr><p:spPr>'
            f'<a:xfrm><a:off x="{E(x)}" y="{E(y)}"/><a:ext cx="{E(w)}" cy="{E(h)}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
            f'<p:txBody><a:bodyPr wrap="square" anchor="{anchor}" rtlCol="false" '
            f'tIns="0" lIns="0" bIns="0" rIns="0"><a:normAutofit/></a:bodyPr>'
            f'<a:lstStyle/>{"".join(paras)}</p:txBody></p:sp>')


def card(idx, name, x, y, w, h, num, heading, sub, amber=False, dashed=False):
    """A capability card: ghost fill, optional amber top rule, number + heading + sub."""
    out = [rect(f"Card_{name}", x, y, w, h, GHOST, dashed=dashed)]
    if amber:
        out.append(rect(f"AmberRule_{name}", x, y, w, 0.045, AMBER))
    pad = 0.20
    out.append(textbox(
        f"CardTxt_{name}", x + pad, y + pad + (0.05 if amber else 0), w - 2 * pad, h - 2 * pad,
        [para(f"{num}", 1300, MUTED, FONT_R, False),
         para(heading, 1400, AMBER if amber else WHITE, FONT_B, True, spc_before=200),
         para(sub, 1300, MUTED, FONT_R, False, spc_before=400)]))
    return out


# --------------------------------------------------------------------------- #
# content
# --------------------------------------------------------------------------- #
TIER1 = [
    ("Surfaces", "1", "YOUR SURFACES",
     "Your app, your website, your contact centre, the vehicle itself.", False, False),
    ("Assistant", "2", "ONE ASSISTANT",
     "Your brand, one front door \u2014 whichever surface they arrive on.", False, False),
    ("Route", "3", "UNDERSTAND AND ROUTE",
     "Works out what is being asked, hands it on, keeps the thread.", False, False),
    ("Specialists", "4", "SPECIALISTS",
     "Energy and charging \u00b7 vehicle health \u00b7 ownership cost \u00b7 household mobility.",
     False, False),
    ("Seams", "5", "DETERMINISTIC SEAMS",
     "Safety triage, credit, payment, disclosure. Not agentic \u2014 on purpose.", True, False),
]

TIER2 = [
    ("Schedule", "6", "ON A SCHEDULE",
     "Runs overnight, one pass per component.", False, False),
    ("DataPlatform", "7", "THE DATA PLATFORM, AT SOURCE",
     "Read in place, never copied. The only step that leaves your account.", False, True),
    ("Reason", "8", "REASONS ACROSS FLEET AND HISTORY",
     "Multi-step work measured in minutes, not milliseconds.", False, False),
    ("NoModel", "9", "NO MODEL IN THIS LANE",
     "The judgement here is deterministic code. Deliberate, not a gap.", True, False),
]


def build_shapes():
    s = []
    # lane labels
    s.append(textbox("LaneLabel_T1", X0, T1_LABEL_Y, W, 0.26, [para(
        "TIER 1 \u00b7 THE CONVERSATIONAL SPINE \u2014 latency-bound. A customer is waiting.",
        1300, MUTED, FONT_R, False)]))
    s.append(textbox("LaneLabel_T2", X0, T2_LABEL_Y, W, 0.26, [para(
        "TIER 2 \u00b7 SCHEDULED REASONING \u2014 nobody is waiting. This is where the expensive "
        "thinking happens.", 1300, MUTED, FONT_R, False)]))

    # cards + connecting arrows
    for i, (nm, num, head, sub, amber, dashed) in enumerate(TIER1):
        s += card(i, nm, T1_X[i], T1_Y, T1_W, T1_H, num, head, sub, amber, dashed)
        if i < T1_N - 1:
            s.append(textbox(f"Flow_T1_{i}", T1_X[i] + T1_W, T1_Y + T1_H / 2 - 0.22, GAP, 0.44,
                             [para("\u2192", 1800, MUTED, FONT_R, False, algn="ctr")]))
    for i, (nm, num, head, sub, amber, dashed) in enumerate(TIER2):
        s += card(i, nm, T2_X[i], T2_Y, T2_W, T2_H, num, head, sub, amber, dashed)
        if i < T2_N - 1:
            s.append(textbox(f"Flow_T2_{i}", T2_X[i] + T2_W, T2_Y + T2_H / 2 - 0.22, GAP, 0.44,
                             [para("\u2192", 1800, MUTED, FONT_R, False, algn="ctr")]))

    # the artifact store — the hinge, and the only 0047AD fill on the slide
    s.append(rect("ArtifactStore", X0, STORE_Y, W, STORE_H, BLUE))
    s.append(textbox("StoreTxt", X0 + 0.30, STORE_Y + 0.14, W - 0.60, STORE_H - 0.28, [
        para("THE ARTIFACT STORE \u2014 where the two lanes meet", 1800, WHITE, FONT_B, True,
             algn="ctr"),
        para("Every finding carries when it ran, how confident it is, and what it was based on. "
             "So the assistant says \u201cas of last Tuesday\u201d rather than implying live state.",
             1300, PALE, FONT_R, False, algn="ctr", spc_before=300)]))

    # the two arrows that make the meeting visible
    read_x = T1_X[3] + T1_W / 2                       # under SPECIALISTS
    write_x = T2_X[3] + T2_W / 2                      # above NO MODEL IN THIS LANE
    s.append(uparrow("Arrow_Read", read_x, READ_Y + 0.04, ARROW_H - 0.08))
    s.append(textbox("ArrowLbl_Read", X0 + 4.0, READ_Y + 0.09, read_x - (X0 + 4.0) - 0.18, 0.30,
                     [para("read inside the turn, in milliseconds", 1300, WHITE, FONT_R,
                           False, algn="r")]))
    s.append(uparrow("Arrow_Write", write_x, WRITE_Y + 0.04, ARROW_H - 0.08))
    s.append(textbox("ArrowLbl_Write", X0 + 6.0, WRITE_Y + 0.09, write_x - (X0 + 6.0) - 0.18, 0.30,
                     [para("written overnight, with nobody waiting", 1300, WHITE, FONT_R,
                           False, algn="r")]))

    # footnote: the seam rule + the dashed-border legend
    s.append(textbox("FigFootnote", X0, FOOT_Y, W, FOOT_H, [para(
        "Agents propose. Deterministic seams decide. A broker owns delivery \u2014 channel, consent, "
        "timing.     Amber = deterministic, not agentic.     Dashed = a different AWS account; "
        "everything else runs in yours.", 1300, MUTED, FONT_R, False)]))
    return s


NOTES_TEXT = """REFERENCE ARCHITECTURE \u2014 Figure 2, drawn natively 2026-08-11 (rev 10).

WHAT THIS SLIDE IS FOR: it answers "how does this actually hang together?" without becoming a service inventory. Walk it in flow order \u2014 the bottom lane runs first, overnight; the top lane is what the customer experiences.

THE ONE RELATIONSHIP TO LAND: the two lanes meet at the artifact store. Tier 2 writes it overnight with nobody waiting. Tier 1 reads it inside the turn with a customer waiting. If the room takes away only that, they have understood the architecture.

WALKING THE NUMBERS
1  Your surfaces \u2014 app, web, contact centre, vehicle. Nothing new for the customer to learn.
2  One assistant \u2014 your brand, one front door.
3  Understand and route \u2014 works out the ask, hands it to a specialist, keeps the thread.
4  Specialists \u2014 energy and charging, vehicle health, ownership cost, household mobility.
5  Deterministic seams \u2014 amber means NOT agentic, on purpose: safety triage, credit, payment, disclosure.
6  On a schedule \u2014 overnight, one pass per component.
7  The data platform, at source \u2014 read in place, never copied. The dashed border marks the only step that leaves your account.
8  Reasons across fleet and history \u2014 minutes, not milliseconds.
9  No model in this lane \u2014 amber again. The judgement here is deterministic code.

WHY NO MODEL IN TIER 2, if asked: the tier is defined by nobody waiting, not by a model being present. Putting a brain icon there would teach the wrong lesson.

WHY THE SEAMS ARE AMBER: amber marks deterministic. Where a seam carries a safety, money or disclosure property, the phrase carrying it is narrated verbatim \u2014 the model does not paraphrase it.

SERVICES BEHIND EACH BOX, if a technical audience asks. Deliberately NOT on the slide face, per the deck's altitude rule \u2014 service names live on the two dedicated service slides.
1-2  surfaces: iOS and web clients; Amazon Connect for voice and chat.
3    route: API Gateway with a Cognito authorizer; supervisor on Amazon Bedrock AgentCore Runtime.
4    specialists: worker agents on AgentCore; Claude Sonnet for reasoning; Nova Sonic on the voice path; AgentCore Memory for session context.
5    seams: deterministic classifiers and partner APIs \u2014 not model calls.
6    schedule: Amazon EventBridge Scheduler.
7    data platform: cross-account Amazon Athena into the data platform account, over governed data products.
8-9  reasoning: AWS Lambda, deterministic.
     artifact store: Amazon DynamoDB.

VOICE LATENCY, if asked: the voice path assumes tool calls return in roughly a second, which is exactly why the expensive reasoning sits in the bottom lane rather than the top. The latency budget is a design input, not a limitation to apologise for.

WHAT NOT TO PROMISE: do not present this as a fixed end state. It is the shape of the architecture and it lands in phases \u2014 the HOW WE'D START slide carries the sequencing. This is a vision of the architecture, not a deployment inventory.

DISCOVERY QUESTION: "Which of these two lanes is the one you are missing today?" Most OEMs have some version of the top lane and nothing at all in the bottom one."""


def main():
    shutil.copy(DECK, BACKUP)
    print(f"backup -> {BACKUP}")
    zin = zipfile.ZipFile(DECK)
    names = zin.namelist()

    # ---- slide: drop placeholder, inject figure ---------------------------- #
    key = f"ppt/slides/{SLIDE}"
    x = zin.read(key).decode()
    used = {int(i) for i in re.findall(r'<p:cNvPr id="(\d+)"', x)}
    assert max(used) < 100, f"id space collides with 100+: {sorted(used)}"

    ph = [sp for sp in re.findall(r"<p:sp>.*?</p:sp>", x, re.S)
          if 'name="FigurePlaceholder"' in sp]
    assert len(ph) == 1, f"expected 1 FigurePlaceholder, found {len(ph)}"
    x2 = x.replace(ph[0], "", 1)
    assert "FigurePlaceholder" not in x2

    shapes = build_shapes()
    anchor = "</p:spTree>"
    assert x2.count(anchor) == 1
    x2 = x2.replace(anchor, "".join(shapes) + anchor, 1)

    new_ids = {int(i) for i in re.findall(r'<p:cNvPr id="(\d+)"', x2)}
    assert len(new_ids) == len(re.findall(r'<p:cNvPr id="(\d+)"', x2)), "duplicate shape ids"
    assert 0 not in new_ids, "id=0 present (invalid OOXML)"
    print(f"  figure: {len(shapes)} shapes injected; placeholder removed")
    print(f"  shape ids now: {min(new_ids)}..{max(new_ids)} ({len(new_ids)} unique)")

    # ---- notes: replace the placeholder brief ------------------------------ #
    nkey = f"ppt/notesSlides/{NOTES}"
    n = zin.read(nkey).decode()
    body = [sp for sp in re.findall(r"<p:sp>.*?</p:sp>", n, re.S)
            if re.search(r'<p:ph[^>]*type="body"', sp)]
    assert len(body) == 1
    sp = body[0]
    a, b = sp.find("<a:p>"), sp.rfind("</a:p>") + len("</a:p>")
    assert a != -1 and b != -1
    paras = "".join(
        '<a:p><a:endParaRPr/></a:p>' if not ln.strip()
        else f'<a:p><a:r><a:rPr lang="en-US" dirty="0"/><a:t>{esc(ln)}</a:t></a:r></a:p>'
        for ln in NOTES_TEXT.split("\n"))
    nsp = sp[:a] + paras + sp[b:]
    n2 = n.replace(sp, nsp, 1)
    assert "PLACEHOLDER" not in n2 and "not yet delivered" not in n2
    print(f"  notes: placeholder brief replaced ({len(NOTES_TEXT)} chars)")

    edited = {key: x2.encode(), nkey: n2.encode()}

    # ---- write + verify ---------------------------------------------------- #
    before = {k: hashlib.sha256(zin.read(k)).hexdigest() for k in names}
    tmp = DECK + ".tmp"
    ct = "[Content_Types].xml"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
        zo.writestr(ct, zin.read(ct))
        for k in names:
            if k == ct:
                continue
            zo.writestr(k, edited.get(k, zin.read(k)))
    zin.close()

    zc = zipfile.ZipFile(tmp)
    after = {k: hashlib.sha256(zc.read(k)).hexdigest() for k in zc.namelist()}
    assert set(after) == set(before), "part set changed"
    changed = sorted(k for k in before if before[k] != after[k])
    assert changed == sorted(edited), f"changed set wrong: {changed}"
    assert len(re.findall(r"<p:sldId\b", zc.read("ppt/presentation.xml").decode())) == 47
    zc.close()
    print(f"\nchanged parts: {[c.split('/')[-1] for c in changed]}")

    problems, empty = structural_sweep(tmp)
    # Only flag empty text frames this build INTRODUCED. Baseline from the pre-change file:
    # slide40 already carried an empty container rect ("Rectangle 5") before we touched it.
    _, base_empty = structural_sweep(BACKUP)
    baseline = set(base_empty.get(SLIDE, []))
    if SLIDE in empty:
        allowed = baseline | {e for e in empty[SLIDE]
                              if e.startswith(("Card_", "AmberRule_", "Arrow_", "ArtifactStore"))}
        unexpected = set(empty[SLIDE]) - allowed
        if unexpected:
            problems.append(f"{SLIDE} NEW unexpected empty frames: {sorted(unexpected)}")
        print(f"  empty frames on {SLIDE}: {len(empty[SLIDE])} "
              f"({len(baseline)} pre-existing, rest are intended pure graphics)")
    print("structural sweep problems:", problems or "none")
    if problems:
        os.unlink(tmp)
        raise SystemExit("sweep failed — deck NOT written")

    os.replace(tmp, DECK)
    print(f"wrote {DECK}")


if __name__ == "__main__":
    main()
