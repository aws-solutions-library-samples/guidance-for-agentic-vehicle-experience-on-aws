#!/usr/bin/env python3
"""Add the one-assistant spine to CVX-FCD-merged.pptx.

Three changes:

1. NEW slide after position 11 (THE JOURNEY, END TO END):
   "ONE ASSISTANT. YOUR BRAND. EVERY SURFACE." -- the persona and its reach, closing
   with the loop that carries the loyalty argument.

2. NEW slide after that:
   "TWO WAYS TO BUILD IT. BOTH LAND ON AWS." -- off-the-shelf Alexa Custom Assistant
   vs build-your-own, over a shared foundation.

3. RETITLE position 23. Its current title is "FOUR AGENTS, NOT ONE ASSISTANT", which
   argues the opposite thesis to the new spine. The slide's argument is sound but
   pitched at the engineering layer: the objection it answers is "can't one agent do
   all this?", answered on testability and independent replaceability. Both levels are
   true -- the customer sees ONE assistant; we build FOUR specialists because a single
   monolithic prompt is untestable. The retitle holds both. The four columns are
   untouched.

Package wiring per slide: a slide part, its rels (slideLayout7 + notesSlide), a notes
part, notes rels (notesMaster + slide), two content-type overrides, a
presentation.xml.rels entry, and a sldIdLst entry inserted at the right ordinal.
Written atomically; refuses to write if the sweep fails.
"""

import os
import re
import shutil
import sys
import zipfile

DECK = os.path.expanduser("~/Downloads/cvx-fcd/deck/CVX-FCD-merged.pptx")
BACKUP_DIR = os.path.expanduser("~/Downloads/cvx-fcd/archive/backups")

EMU = 914400
NS = (
    '<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\'?>'
    '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="161E2D"/></a:solidFill>'
    '<a:effectLst/></p:bgPr></p:bg><p:spTree>'
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr/>'
)
TAIL = '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'

FOOTER = ("\u00a9 2026, Amazon Web Services, Inc. or its affiliates. All rights reserved. "
          "Amazon Confidential and Trademark.")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def emu(inches):
    return int(round(inches * EMU))


class Slide:
    """Emits house-style shapes with sequential non-zero ids."""

    def __init__(self):
        self.parts = []
        self._id = 2

    def _next(self):
        i = self._id
        self._id += 1
        return i

    def text(self, name, x, y, w, h, runs, size=1649, face="Amazon Ember Display",
             color="FFFFFF", bold=False, anchor="t", spc=100):
        if isinstance(runs, str):
            runs = [runs]
            
        paras = []
        for line in runs:
            paras.append(
                f'<a:p><a:pPr><a:lnSpc><a:spcPct val="{spc}%"/></a:lnSpc></a:pPr>'
                f'<a:r><a:rPr lang="en-US" sz="{size}" dirty="0" b="{1 if bold else 0}">'
                f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
                f'<a:latin typeface="{face}"/></a:rPr><a:t>{esc(line)}</a:t></a:r></a:p>'
            )
        self.parts.append(
            f'<p:sp><p:nvSpPr><p:cNvPr id="{self._next()}" name="{name}"/>'
            f'<p:cNvSpPr txBox="true"/><p:nvPr/></p:nvSpPr><p:spPr>'
            f'<a:xfrm><a:off x="{emu(x)}" y="{emu(y)}"/>'
            f'<a:ext cx="{emu(w)}" cy="{emu(h)}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
            f'<p:txBody><a:bodyPr wrap="square" anchor="{anchor}" rtlCol="false" '
            f'tIns="0" lIns="0" bIns="0" rIns="0"><a:normAutofit/></a:bodyPr>'
            f'<a:lstStyle/>{"".join(paras)}</p:txBody></p:sp>'
        )

    def rect(self, name, x, y, w, h, fill=None, line=None, lw=9525, dash=None):
        f = (f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>' if fill
             else '<a:noFill/>')
        if line:
            d = f'<a:prstDash val="{dash}"/>' if dash else ''
            ln = (f'<a:ln w="{lw}"><a:solidFill><a:srgbClr val="{line}"/>'
                  f'</a:solidFill>{d}</a:ln>')
        else:
            ln = '<a:ln><a:noFill/></a:ln>'
        # No <p:txBody>: this is a pure graphic. Emitting an empty text body would
        # trip the empty-text-frame guard that catches the 64-chip defect class.
        self.parts.append(
            f'<p:sp><p:nvSpPr><p:cNvPr id="{self._next()}" name="{name}"/>'
            f'<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>'
            f'<a:xfrm><a:off x="{emu(x)}" y="{emu(y)}"/>'
            f'<a:ext cx="{emu(w)}" cy="{emu(h)}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>{f}{ln}</p:spPr></p:sp>'
        )

    def title(self, t):
        self.text("Title", 0.71, 0.48, 18.58, 0.62, t, size=4100,
                  face="Amazon Ember 1 Bold", bold=True)

    def subtitle(self, t):
        self.text("Subtitle", 0.71, 1.06, 18.58, 0.50, t, size=2788,
                  face="Amazon Ember 2")

    def footer(self):
        self.text("Footer", 1.99, 10.62, 7.23, 0.24, FOOTER, size=1050,
                  color="F5F7FF")

    def xml(self):
        return NS + "".join(self.parts) + TAIL


# --------------------------------------------------------------------------
# Slide A -- ONE ASSISTANT. YOUR BRAND. EVERY SURFACE.
# --------------------------------------------------------------------------
def build_one_assistant():
    s = Slide()
    s.title("ONE ASSISTANT. YOUR BRAND. EVERY SURFACE.")
    s.subtitle("The customer asks once, in their own words \u2014 and never has to know "
               "which system answers.")

    # left: the persona
    s.rect("AccentRule", 0.71, 2.16, 1.40, 0.04, fill="FF9900")
    s.text("Persona", 0.71, 2.46, 4.90, 0.90, "\u201cHey Meridian.\u201d",
           size=3200, face="Amazon Ember Display Bold", bold=True)
    s.text("PersonaBody", 0.71, 3.62, 4.90, 2.40, [
        "One assistant, wearing your brand \u2014 not a new destination the customer "
        "has to be taught to visit.",
        "",
        "It carries what it already knows about them from one surface to the next.",
    ], size=1649)
    s.text("PersonaNote", 0.71, 6.40, 4.90, 0.90,
           "Fleet: the same assistant answers the driver in the cab and the "
           "operator at the desk.", size=1300, color="9BA4B4")

    # right: four surfaces
    surfaces = [
        ("YOUR APP", "Voice and text, in the app they already have."),
        ("YOUR WEBSITE", "The same answers on the surface they arrive at first."),
        ("YOUR CONTACT CENTRE", "Voice and chat, with the human getting full context on escalation."),
        ("IN THE VEHICLE", "Your own wake word and voice, through Alexa Custom Assistant."),
    ]
    x0, pitch, w = 6.40, 3.15, 2.85
    for i, (head, body) in enumerate(surfaces):
        x = x0 + i * pitch
        s.rect(f"Hair{i}", x, 2.16, w, 0.02, fill="9BA4B4")
        # Amber, not 0047AD: AWS blue on the 161E2D background reads as dimmer than
        # the white headings, which inverts the intent of highlighting this one.
        s.text(f"SurfHead{i}", x, 2.40, w, 0.60, head, size=1800,
               face="Amazon Ember Display Bold", bold=True,
               color="FF9900" if i == 3 else "FFFFFF")
        s.text(f"SurfBody{i}", x, 3.16, w, 2.20, body, size=1649)

    # the loop -- the loyalty payoff
    s.rect("LoopBand", 0.71, 7.60, 18.58, 1.90, fill="232F3E")
    s.text("LoopHead", 1.05, 7.90, 17.90, 0.40, "THE LOOP THAT MAKES IT PAY",
           size=1800, face="Amazon Ember Display Bold", bold=True, color="FF9900")
    s.text("LoopChain", 1.05, 8.44, 17.90, 0.50, [
        "\u201cFind me a new car.\u201d   \u2192   \u201cWhere\u2019s my build?\u201d   "
        "\u2192   \u201cWhy is that light on?\u201d   \u2192   "
        "\u201cWhat\u2019s my trade-in worth?\u201d"
    ], size=1649)
    s.text("LoopClose", 1.05, 8.98, 17.90, 0.40,
           "\u2014 and the answer to the last one begins the first one again.",
           size=1649, color="9BA4B4")

    s.text("Kicker", 0.71, 9.76, 18.58, 0.50,
           "Three touchpoints in five years becomes one continuous relationship. "
           "That is what loyalty is made of.",
           size=1800, face="Amazon Ember Display Bold", bold=True)
    s.footer()
    return s.xml()


# --------------------------------------------------------------------------
# Slide B -- TWO WAYS TO BUILD IT. BOTH LAND ON AWS.
# --------------------------------------------------------------------------
def build_two_ways():
    s = Slide()
    s.title("TWO WAYS TO BUILD IT. BOTH LAND ON AWS.")
    s.subtitle("The choice changes the front door, not the brain behind it.")

    rows = [
        ("WAKE WORD, SPEECH",
         "Amazon\u2019s stack, with your own custom wake word.",
         "Yours to build \u2014 including the wake word itself."),
        ("VOICE AND PERSONALITY",
         "Customisable within the platform.",
         "Entirely yours, with model choice at two layers."),
        ("TIME TO MARKET",
         "Faster. Amazon positions it on reduced investment and complexity.",
         "Longer, in exchange for full control."),
        ("AUTOMOTIVE-GRADE IN-CABIN",
         "A proven path \u2014 shipped by BMW and Stellantis.",
         "Yours to solve."),
        ("WHERE CVX SITS",
         "The domain brain behind the assistant.",
         "CVX is the assistant."),
    ]

    cols = [
        (0.71, "OFF THE SHELF", "Alexa Custom Assistant", "0047AD"),
        (10.35, "BUILD YOUR OWN", "Nova Sonic + your reasoning model on Bedrock", "FF9900"),
    ]
    cw = 8.30
    for cx, head, sub, accent in cols:
        s.rect(f"Acc{head[:3]}", cx, 2.10, 1.40, 0.04, fill=accent)
        s.text(f"ColHead{head[:3]}", cx, 2.36, cw, 0.50, head, size=2400,
               face="Amazon Ember Display Bold", bold=True)
        s.text(f"ColSub{head[:3]}", cx, 2.92, cw, 0.44, sub, size=1649, color="9BA4B4")

    s.rect("Divider", 9.98, 2.10, 0.02, 5.90, fill="9BA4B4")

    y = 3.66
    for label, left, right in rows:
        s.text(f"Lbl{label[:6]}", 0.71, y, 8.30, 0.30, label, size=1300,
               face="Amazon Ember Display Bold", bold=True, color="9BA4B4")
        s.text(f"L{label[:6]}", 0.71, y + 0.32, 8.30, 0.60, left, size=1649)
        s.text(f"R{label[:6]}", 10.35, y, 8.30, 0.30, label, size=1300,
               face="Amazon Ember Display Bold", bold=True, color="9BA4B4")
        s.text(f"RV{label[:6]}", 10.35, y + 0.32, 8.30, 0.60, right, size=1649)
        y += 0.90

    # shared foundation
    s.rect("SharedBand", 0.71, 8.34, 18.58, 1.28, fill="232F3E")
    s.text("SharedHead", 1.05, 8.60, 17.90, 0.36, "SHARED FOUNDATION, EITHER WAY",
           size=1800, face="Amazon Ember Display Bold", bold=True, color="FF9900")
    s.text("SharedBody", 1.05, 9.06, 17.90, 0.44,
           "Reasoning grounded on your own content  \u00b7  deterministic seams for "
           "safety, money and disclosure  \u00b7  your data stays yours.", size=1649)

    s.text("Kicker2", 0.71, 9.80, 18.58, 0.46,
           "Buy the front door or build it. The brain, the grounding and the "
           "guardrails are the same either way.",
           size=1800, face="Amazon Ember Display Bold", bold=True)
    s.footer()
    return s.xml()


# --------------------------------------------------------------------------
NOTES_A = """CLAIM: One forward-facing assistant, wearing the OEM's brand, reachable on every surface the customer already uses \u2014 and the journey closes into a loop rather than ending at a funnel.

WHY THIS IS THE SPINE: Position 9 argues the relationship shape inverts \u2014 three touchpoints in five years becomes continuous, and silence is why loyalty fails. A named, persistent assistant is the MECHANISM that makes that concrete rather than abstract. Without a persona, "continuous relationship" is a claim. With one, it is a thing the customer can name.

THE LOOP IS THE LOYALTY ARGUMENT: "Find me a new car" through to "What's my trade-in worth" and back to the first question again. That is a loop, not a funnel, and the loop is what pays.

STATUS \u2014 PRECISION FOR THIS ROOM, NOT FOR THE SLIDE:
\u2022 Web, mobile app (voice and text) and contact centre are wired and running: /assistant/chat, the Nova Sonic bidi runtime, Amazon Connect contact flows.
\u2022 True in-cabin is the architectural extension. There is no head-unit surface built today, and there is no CVX-to-Alexa-Custom-Assistant integration in the codebase.
\u2022 There is no wake word today. The app is push-to-talk. Nova Sonic bidi is a streaming session, not keyword spotting.
Do not claim in-cabin as shipped. Claim it as reach, which is what the slide says.

SCOPE GUARD: In-cabin via Alexa Custom Assistant is DECK SCOPE ONLY. It is deliberately NOT in the CES / re:Invent Zone 2 build, and it must NOT appear in the Aug 14 design doc. User direction 2026-08-11: "that's out of scope for the build/CES, but probably in scope for this deck."

NAMING: The assistant takes the OEM's name. "Hey Meridian" is correct because Meridian is the OEM \u2014 the same pattern as Hey Mercedes or Hey BMW. Meridian is the instance, not the product name. For a different OEM the assistant wears their brand. This is the proof of position 22, WHOSE BRAND WHOSE DATA WHOSE CUSTOMER. The assistant name is tenant configuration, alongside brand.

LIKELY OBJECTION: "We already have an assistant."
ANSWER: Then this is the brain behind it, not a replacement for it. The next slide is precisely that choice.

DISCOVERY QUESTION
\u2192  If a customer asked your assistant a question it could not answer today, where would that question go?"""

NOTES_B = """CLAIM: Two routes to a branded in-vehicle assistant \u2014 adopt Alexa Custom Assistant, or build your own on Nova Sonic with your choice of reasoning model. Both run on AWS. The choice changes the front door, not the brain.

EVIDENCE \u2014 ALEXA CUSTOM ASSISTANT, ALL PUBLISHED AND ATTRIBUTABLE TO AMAZON:
\u2022 ACA lets an automaker create an assistant with its own skills, personality, voice and wake word (Amazon developer documentation, Smart Vehicles).
\u2022 As of Amazon's June 2026 developer post, ACA is built on the Alexa+ architecture \u2014 over 70 large language models \u2014 with agentic capabilities powered by Amazon Bedrock on AWS. So both columns sit on the same substrate; this is one story, not two.
\u2022 BMW was the first OEM to deliver voice technology on ACA, powering its Intelligent Personal Assistant (December 2024). FCA / Stellantis implemented ACA inside Uconnect.
\u2022 Amazon documents Alexa operating alongside third-party assistants on the same device, so a custom wake word and "Alexa" are not mutually exclusive.

ATTRIBUTION CARE: BMW and Stellantis are Alexa references, not AWS customer references. Do not present them beside the 13 named AWS customers in the proof section. Attribute to Amazon / Alexa if asked.

THE ARCHITECTURAL POINT WORTH MAKING OUT LOUD: on the ACA path we never build a wake word. Alexa owns the wake word, the speech recognition and the speech synthesis. CVX supplies the domain reasoning, the grounding on the OEM's own content, the tool orchestration and the deterministic seams. That also moves the latency problem: the roughly one-second voice budget stops being ours, because ACA owns the speech loop.

MODEL CHOICE AT TWO LAYERS is the build-your-own advantage, and it is a real Bedrock differentiator: you pick the speech-to-speech model and you pick the reasoning model independently. Note the distinction if a technical audience presses \u2014 Nova Sonic is the speech-to-speech model; the reasoning runs on a separate model (Claude Sonnet in CVX today).

STATUS: only the right-hand column is demonstrable today. Build-your-own is what exists \u2014 Nova Sonic bidi, push-to-talk, plus text and contact centre. There is no ACA integration in the codebase. If asked to show in-cabin, show the app and describe ACA as the path.

LIKELY OBJECTION: "Which one should we pick?"
ANSWER: If time to market and automotive-grade voice matter most, adopt. If the assistant is the differentiator you want to own, build. The work underneath \u2014 grounding on your content, the deterministic seams, keeping your data yours \u2014 is the same either way, which means the decision is reversible for longer than it looks.

DISCOVERY QUESTION
\u2192  Is your in-vehicle assistant something you want to own, or something you want to work?"""

NOTES_23_NEW = """CLAIM: The customer talks to one assistant. Behind it, four purpose-built agents cover the Own and Service stages \u2014 each with a defined trigger, inputs, outputs and services.

WHY BOTH HALVES MATTER: these are two different layers and both are true. At the EXPERIENCE layer there is one assistant, because a customer should never have to work out which specialist they need. At the ARCHITECTURE layer there are four, because one monolithic prompt attempting all of this would be slower, harder to test and harder to maintain. The supervisor pattern is what lets a single front door sit on top of specialised agents.

NOTE ON SCOPE: these four cover Own and Service. The one assistant spans all five stages; do not let the four columns imply the assistant only does two stages.

EVIDENCE: all four agents are deployed in the CVX platform. The trigger / inputs / outputs / services breakdown follows the ADP deck idiom. The coverage seam between agent 2 (which asks) and agent 3 (which decides) is a designed deterministic boundary.

LIKELY OBJECTION: "So is it one agent or four?"
ANSWER: One from where the customer sits, four from where the engineer sits. That is the point of the supervisor pattern rather than an evasion \u2014 purpose-built agents with defined scope are testable, auditable and independently replaceable, and the customer never pays for that in complexity.

Agents propose. Deterministic seams decide. The broker delivers."""


def next_backup():
    n = 1
    while True:
        p = os.path.join(BACKUP_DIR, f"pre-one-assistant-{n}.pptx")
        if not os.path.exists(p):
            return p
        n += 1


def notes_xml(body):
    paras = "".join(
        f'<a:p><a:r><a:rPr lang="en-US" dirty="0"/><a:t>{esc(l)}</a:t></a:r></a:p>'
        if l.strip() else '<a:p><a:endParaRPr/></a:p>'
        for l in body.split("\n")
    )
    return (
        '<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\'?>'
        '<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
        '<p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        '<p:nvPr><p:ph type="sldImg" idx="2"/></p:nvPr></p:nvSpPr><p:spPr/></p:sp>'
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes Placeholder 2"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        '<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/>'
        f'<p:txBody><a:bodyPr/><a:lstStyle/>{paras}</p:txBody></p:sp>'
        '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>'
    )


SLIDE_REL = (
    '<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\'?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/slideLayout" Target="../slideLayouts/slideLayout7.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/notesSlide" Target="../notesSlides/{notes}"/></Relationships>'
)
NOTES_REL = (
    '<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\'?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/notesMaster" Target="../notesMasters/notesMaster1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/slide" Target="../slides/{slide}"/></Relationships>'
)


def main():
    z = zipfile.ZipFile(DECK)
    names = z.namelist()

    # ---- discover free part numbers / ids -------------------------------
    smax = max(int(m.group(1)) for n in names
               if (m := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", n)))
    nmax = max(int(m.group(1)) for n in names
               if (m := re.fullmatch(r"ppt/notesSlides/notesSlide(\d+)\.xml", n)))
    prels = z.read("ppt/_rels/presentation.xml.rels").decode()
    rmax = max(int(m) for m in re.findall(r'Id="rId(\d+)"', prels))
    pres = z.read("ppt/presentation.xml").decode()
    idmax = max(int(m) for m in re.findall(r'<p:sldId id="(\d+)"', pres))

    new = [
        (f"slide{smax+1}.xml", f"notesSlide{nmax+1}.xml", f"rId{rmax+1}", idmax + 1,
         build_one_assistant(), NOTES_A),
        (f"slide{smax+2}.xml", f"notesSlide{nmax+2}.xml", f"rId{rmax+2}", idmax + 2,
         build_two_ways(), NOTES_B),
    ]

    payload = {}
    for slide, notes, rid, sid, sxml, ntxt in new:
        payload[f"ppt/slides/{slide}"] = sxml.encode()
        payload[f"ppt/slides/_rels/{slide}.rels"] = SLIDE_REL.format(notes=notes).encode()
        payload[f"ppt/notesSlides/{notes}"] = notes_xml(ntxt).encode()
        payload[f"ppt/notesSlides/_rels/{notes}.rels"] = NOTES_REL.format(slide=slide).encode()

    # ---- content types --------------------------------------------------
    ct = z.read("[Content_Types].xml").decode()
    adds = []
    for slide, notes, *_ in new:
        adds.append(f'<Override PartName="/ppt/slides/{slide}" ContentType='
                    '"application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')
        adds.append(f'<Override PartName="/ppt/notesSlides/{notes}" ContentType='
                    '"application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>')
    ct = ct.replace("</Types>", "".join(adds) + "</Types>")
    payload["[Content_Types].xml"] = ct.encode()

    # ---- presentation rels ----------------------------------------------
    radds = "".join(
        f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/slide" Target="slides/{slide}"/>'
        for slide, _n, rid, *_ in new
    )
    payload["ppt/_rels/presentation.xml.rels"] = \
        prels.replace("</Relationships>", radds + "</Relationships>").encode()

    # ---- sldIdLst: insert after position 11 -----------------------------
    entries = re.findall(r'<p:sldId[^>]*/>', pres)
    assert len(entries) == 46, f"expected 46 sldId entries, found {len(entries)}"
    anchor = entries[10]           # position 11, zero-indexed
    ins = "".join(f'<p:sldId id="{sid}" r:id="{rid}"/>' for _s, _n, rid, sid, *_ in new)
    assert pres.count(anchor) == 1, "position-11 sldId entry is not unique"
    payload["ppt/presentation.xml"] = pres.replace(anchor, anchor + ins).encode()

    # ---- retitle position 23 (slide38) ----------------------------------
    s38 = z.read("ppt/slides/slide38.xml").decode()
    before = s38
    s38 = s38.replace(
        "<a:t>FOUR AGENTS, NOT ONE ASSISTANT</a:t>",
        "<a:t>ONE ASSISTANT, FOUR SPECIALISTS BEHIND IT</a:t>")
    s38 = s38.replace(
        "<a:t>Each with a defined trigger, inputs, outputs, and services.</a:t>",
        "<a:t>The customer talks to one. Four purpose-built agents do the work.</a:t>")
    assert s38 != before, "slide38 retitle matched nothing"
    assert "FOUR AGENTS, NOT ONE ASSISTANT" not in s38, "old title survived"
    payload["ppt/slides/slide38.xml"] = s38.encode()
    payload["ppt/notesSlides/notesSlide28.xml"] = notes_xml(NOTES_23_NEW).encode()

    # ---- atomic write ---------------------------------------------------
    backup = next_backup()
    shutil.copy2(DECK, backup)
    print(f"backup -> {os.path.basename(backup)}")

    tmp = DECK + ".tmp"
    written = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        first = "[Content_Types].xml"
        out.writestr(first, payload[first])
        written.add(first)
        for item in z.infolist():
            if item.filename in written:
                continue
            data = payload.get(item.filename)
            out.writestr(item, z.read(item.filename) if data is None else data)
            written.add(item.filename)
        for name, data in payload.items():
            if name not in written:
                out.writestr(name, data)
                written.add(name)
    z.close()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from apply_stage_final_to_deck import structural_sweep
    problems, empty = structural_sweep(tmp)
    targets = {f"slide{smax+1}.xml", f"slide{smax+2}.xml"}
    unexpected = {k: v for k, v in empty.items() if k in targets}
    if unexpected:
        problems.append(f"new slides have unexpected empty text frames: {unexpected}")

    if problems:
        os.remove(tmp)
        print("\nFAILED -- deck NOT modified:")
        for p in problems:
            print("  !", p)
        sys.exit(1)

    os.replace(tmp, DECK)
    print("structural sweep: clean")
    for slide, notes, rid, sid, *_ in new:
        print(f"  + {slide} / {notes} / {rid} / sldId {sid}")
    print("  ~ slide38.xml retitled; notesSlide28.xml rewritten")


if __name__ == "__main__":
    main()
