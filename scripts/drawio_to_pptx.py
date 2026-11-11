#!/usr/bin/env python3
"""
drawio_to_pptx.py — Automated slide assembly for the CVX reference-architecture
diagrams (Diagrams 1-3).

Reads the 3 drawio sources at
``~/guidance-for-connected-vehicle-experience-on-aws/documentation/diagrams/
cvx-{1-orchestration,2-data-foundation,3-escalation}.drawio``,
extracts primary boxes / arrows / boundaries / badges, and produces a 3-slide
PowerPoint deck with real AWS icons pulled from the AWS-Architecture-Icons-Deck
via the verified ``pptx_icon_extractor`` module.

Output:
    ~/guidance-for-connected-vehicle-experience-on-aws/documentation/diagrams/
    cvx-reference-architecture.pptx  (3 slides, 16:9 widescreen)

Design notes
------------
* Coordinate transform is per-diagram: we compute the tight bounding box of all
  rendered content and fit it into the slide's content area (below the title
  bar) with uniform scaling. Aspect ratio is preserved — letterboxing on the
  axis where the drawio canvas doesn't match 16:9 is expected and intentional.
* Icon sourcing: the extractor returns SVG bytes (AWS 2026 icons are SVG-only,
  no raster fallback). We rasterize via ``rsvg-convert`` (librsvg) to PNG at
  512x512 and embed as pictures. python-pptx has no first-class SVG picture
  API, so raster is the pragmatic path.
* Icon substitution: "Amazon Bedrock Knowledge Bases" is not present in the
  04-28-2026 icon deck; we substitute the "Amazon Bedrock AgentCore" icon but
  keep the on-slide label accurate. Documented in ``SERVICE_KEYWORDS`` below.
* This is a first-pass automated placement — visual refinement by hand in
  PowerPoint is expected. We do NOT attempt pixel-perfect fidelity to drawio's
  arrow routing; straight-line connectors trimmed to box edges are used.

Usage
-----
::

    python3 -m pip install -r scripts/requirements-diagrams.txt
    python3 scripts/drawio_to_pptx.py
    python3 scripts/drawio_to_pptx.py --output /tmp/preview.pptx

Dependencies are declared in ``scripts/requirements-diagrams.txt`` (python-pptx,
lxml, defusedxml) — deliberately separate from the repo-root
``requirements.txt``, which is the AgentCore runtime deployment manifest. Also
requires ``rsvg-convert`` (librsvg) on PATH for SVG rasterization.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# defusedxml, not xml.etree — `.drawio` files are untrusted input from the
# reader's perspective: they may be hand-authored, pulled from a shared drive,
# or exported by a third-party tool. The stdlib parser resolves external
# entities and DTDs, which makes it vulnerable to XXE / billion-laughs.
# defusedxml.ElementTree is a drop-in replacement with those disabled.
# See issues/2026-08-03-cvx-probe-critical-triage/report.md (semgrep
# use-defused-xml, use-defused-xml-parse).
from defusedxml import ElementTree as ET

# lxml is used ONLY to build OXML elements for python-pptx's shape tree
# (`etree.SubElement`, one call site) — it never parses input, so it carries no
# XXE surface here. Do not "harden" this import with a custom parser; there is
# nothing for a parser to guard.
#
# The 2026-08-03 probe reported bandit B410 ("etree untrusted XML") on this
# import. That check no longer exists: B410 was removed from bandit upstream
# (absent from the blacklist in 1.8.6, which ships B401-B409/B411-B415) because
# importing lxml is not itself a finding. The pragma below is retained only so
# older bandit versions stay quiet; it is a no-op on current bandit.
from lxml import etree  # nosec B410 — element construction only, no parsing
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_CONNECTOR_TYPE, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

# Local module — must be on sys.path when running from scripts/.
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from pptx_icon_extractor import (  # noqa: E402
    extract_icon_content_type,
    extract_icon_image,
    find_icon_by_caption,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path("~/guidance-for-connected-vehicle-experience-on-aws").expanduser()
DIAGRAMS_DIR = REPO_ROOT / "documentation" / "diagrams"
DECK_PATH = Path(
    "~/Downloads/Microsoft-PPTx-toolkits_04302026."
    "1ee04f761e1661409056abeb3ca627bb5e336688/"
    "AWS-Architecture-Icons-Deck_For-Light-BG_04282026.pptx"
).expanduser()

DIAGRAMS = [
    ("cvx-1-orchestration.drawio", "orchestration"),
    ("cvx-2-data-foundation.drawio", "data-foundation"),
    ("cvx-3-escalation.drawio", "escalation"),
]

OUTPUT = DIAGRAMS_DIR / "cvx-reference-architecture.pptx"

# 16:9 widescreen: 13.333in x 7.5in.
SLIDE_W_EMU = 12_192_000
SLIDE_H_EMU = 6_858_000

# Layout inside each slide.
MARGIN_LR = 228_600            # 0.25"
TITLE_TOP = 228_600            # 0.25"
TITLE_H = 500_000              # ~0.55"
SUBTITLE_TOP = 750_000         # ~0.82"
SUBTITLE_H = 280_000           # ~0.31"
SEPARATOR_TOP = 1_050_000      # ~1.15"
SEPARATOR_H = 25_000           # ~0.03" (thin orange bar)

CONTENT_TOP = 1_140_000        # below separator
CONTENT_LEFT = MARGIN_LR
CONTENT_RIGHT = SLIDE_W_EMU - MARGIN_LR
CONTENT_BOTTOM = SLIDE_H_EMU - 150_000
CONTENT_W = CONTENT_RIGHT - CONTENT_LEFT
CONTENT_H = CONTENT_BOTTOM - CONTENT_TOP

# Target icon side on the slide — ~0.85".
ICON_SIZE_EMU = 780_000

# EMU per point (for line widths). 12700 EMU = 1 pt.
EMU_PT = 12_700

# ---------------------------------------------------------------------------
# Caption / icon mapping
#
# Order matters — most specific keywords first. Each entry maps a lowercased
# substring of the drawio container's label text to the *exact* caption to
# look up in the AWS icon deck.
# ---------------------------------------------------------------------------

SERVICE_KEYWORDS: List[Tuple[str, str]] = [
    ("amazon connect", "Amazon Connect"),
    ("api gateway", "Amazon API Gateway"),
    ("amazon cognito", "Amazon Cognito"),
    ("cognito", "Amazon Cognito"),
    # Runtime / Memory / KB all substitute to AgentCore icon (KB intentionally,
    # since the 04-28-2026 deck doesn't ship a distinct "Knowledge Bases" icon
    # — label stays accurate, icon is the visual stand-in).
    ("bedrock agentcore runtime", "Amazon Bedrock AgentCore"),
    ("bedrock agentcore memory", "Amazon Bedrock AgentCore"),
    ("bedrock agentcore", "Amazon Bedrock AgentCore"),
    ("bedrock knowledge base", "Amazon Bedrock AgentCore"),
    ("knowledge base", "Amazon Bedrock AgentCore"),
    ("lambda", "AWS Lambda"),
    ("elasticache", "Amazon ElastiCache"),
    ("redis lks", "Amazon ElastiCache"),
    ("redis", "Amazon ElastiCache"),
    ("dynamodb", "Amazon DynamoDB"),
    ("centralized data repository", "AWS Lake Formation"),
    ("lake formation", "AWS Lake Formation"),
    ("amazon athena", "Amazon Athena"),
    ("aws glue", "AWS Glue"),
    # S3 is under its canonical deck caption; the extractor's disambiguation
    # will pick the first match (slide 154) per the verified mapping.
    ("amazon s3", "Amazon Simple Storage Service (Amazon S3)"),
    ("amazon simple storage", "Amazon Simple Storage Service (Amazon S3)"),
]

# Labels we always render as person-shape (not service icon).
PERSON_KEYWORDS = ("customer", "human agent", "tier-2")

# Value prefixes that indicate an account / region boundary.
BOUNDARY_VALUE_PREFIXES = (
    "CVX account", "ADP account", "CMS foundation",
)

# Value prefixes for named groupings that are NOT boundaries (e.g., CMS
# foundation is filled but not dashed).
GROUPING_VALUE_PREFIXES = ("CMS foundation",)

# Cell ids we always skip (title chrome, legend, arrow labels, nested icon /
# sublist decorations that our own render replaces).
# Note: this pattern is applied to the cell's own id AND every ancestor's id
# via ``_ancestor_should_skip`` so that children of a skipped container (e.g.
# ``subtitle-text`` living under ``title-group``) are transitively skipped.
SKIP_ID_PATTERNS = re.compile(
    r"^(title-|subtitle-|legend-|line-styles-|arrow-)"
    r"|(-icon|-sublist|-separator)$"
)

# ---------------------------------------------------------------------------
# drawio parsing
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    id: str
    parent: str
    value: str            # HTML-stripped, whitespace-collapsed label
    raw_value: str        # original label (may be HTML)
    style: str
    x: float
    y: float
    w: float
    h: float
    is_vertex: bool
    is_edge: bool
    source: Optional[str] = None
    target: Optional[str] = None
    abs_x: float = 0.0    # computed absolute (parent-chain-summed) x
    abs_y: float = 0.0


def _strip_html(s: str) -> str:
    """Drop tags, decode common entities, collapse whitespace/newlines to
    a single-line plain string. drawio labels are HTML fragments (`<div>`,
    `<br>`, `&nbsp;`, `&#xa;`, etc.); we need the text-only rendering."""
    if not s:
        return ""
    text = re.sub(r"<[^>]+>", " ", s)
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&apos;": "'",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    text = re.sub(r"&#x[0-9a-fA-F]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"[\r\n\t\xa0]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_drawio(path: Path) -> Tuple[Dict[str, Cell], Dict[str, str]]:
    """Return ``(cells_by_id, meta)`` where ``meta`` has ``page_w``, ``page_h``,
    ``title``, ``subtitle`` extracted from the well-known title-text and
    subtitle-text cells."""
    tree = ET.parse(path)
    root = tree.getroot()
    diagram = root.find("diagram")
    if diagram is None:
        raise ValueError(f"{path}: no <diagram> element")
    model = diagram.find("mxGraphModel")
    if model is None:
        raise ValueError(f"{path}: no <mxGraphModel>")

    meta = {
        "page_w": float(model.get("pageWidth", "2000")),
        "page_h": float(model.get("pageHeight", "1200")),
        "title": "",
        "subtitle": "",
    }

    cells: Dict[str, Cell] = {}
    for c in model.iter("mxCell"):
        cid = c.get("id")
        if cid is None or cid in ("0", "1"):
            continue
        parent = c.get("parent", "1")
        raw_value = c.get("value", "") or ""
        value = _strip_html(raw_value)
        style = c.get("style", "") or ""
        geom = c.find("mxGeometry")

        def _f(attr: str) -> float:
            if geom is None:
                return 0.0
            v = geom.get(attr, "0") or "0"
            try:
                return float(v)
            except ValueError:
                return 0.0

        cells[cid] = Cell(
            id=cid,
            parent=parent,
            value=value,
            raw_value=raw_value,
            style=style,
            x=_f("x"),
            y=_f("y"),
            w=_f("width"),
            h=_f("height"),
            is_vertex=c.get("vertex") == "1",
            is_edge=c.get("edge") == "1",
            source=c.get("source"),
            target=c.get("target"),
        )

        if cid == "title-text":
            meta["title"] = value
        elif cid == "subtitle-text":
            meta["subtitle"] = value

    # Compute absolute positions by summing along the parent chain until we
    # hit "0" or "1" (mxGraphModel roots). This handles the drawio convention
    # where child cell geometry is relative to its immediate container.
    for cell in cells.values():
        ax, ay = cell.x, cell.y
        p = cell.parent
        seen = {cell.id}
        while p and p in cells and p not in seen:
            parent_cell = cells[p]
            ax += parent_cell.x
            ay += parent_cell.y
            seen.add(p)
            p = parent_cell.parent
        cell.abs_x = ax
        cell.abs_y = ay

    return cells, meta


# ---------------------------------------------------------------------------
# Cell classification
# ---------------------------------------------------------------------------


def _is_boundary(c: Cell) -> bool:
    # Dashed outline + no fill = classic drawio account/region boundary.
    if "dashPattern=8 4" in c.style and "fillColor=none" in c.style:
        return True
    if any(c.value.startswith(v) for v in BOUNDARY_VALUE_PREFIXES):
        # Grouping (CMS foundation) is technically a boundary-like container
        # but rendered filled — handled separately.
        if any(c.value.startswith(g) for g in GROUPING_VALUE_PREFIXES):
            return False
        return True
    return False


def _is_grouping(c: Cell) -> bool:
    return any(c.value.startswith(g) for g in GROUPING_VALUE_PREFIXES)


def _is_person(c: Cell) -> bool:
    v = c.value.lower()
    return any(term in v for term in PERSON_KEYWORDS)


def _is_tag(c: Cell) -> bool:
    return "shape=step" in c.style


def _is_badge(c: Cell) -> bool:
    return c.id.startswith("badge-")


def _ancestor_should_skip(c: Cell, cells: Dict[str, Cell]) -> bool:
    """Return True if the cell OR any ancestor in its parent chain has an id
    matching the skip pattern. Handles cases where a leaf value cell (e.g.
    ``subtitle-text``) lives inside a container we skip (``title-group``)."""
    p = c.parent
    seen = {c.id}
    while p and p in cells and p not in seen:
        if SKIP_ID_PATTERNS.search(p):
            return True
        seen.add(p)
        p = cells[p].parent
    return False


def _is_primary(c: Cell, cells: Dict[str, Cell]) -> bool:
    """A primary shape that gets rendered (not skipped)."""
    if not c.is_vertex:
        return False
    if SKIP_ID_PATTERNS.search(c.id):
        # Badges do get rendered — SKIP pattern doesn't match ``badge-*``.
        return False
    if _ancestor_should_skip(c, cells):
        return False
    if c.w <= 0 or c.h <= 0:
        return False
    if not c.value and not _is_boundary(c):
        return False
    return True


def classify(c: Cell, cells: Dict[str, Cell]) -> str:
    """Return one of: ``boundary`` ``grouping`` ``badge`` ``tag`` ``person``
    ``service`` ``skip``. Order-sensitive — earlier checks win."""
    if not _is_primary(c, cells):
        # Badges pass _is_primary=False when their id matches SKIP; but the
        # pattern doesn't include ``badge-``, so we re-check here to keep
        # the classifier surface stable.
        if _is_badge(c) and c.w > 0 and c.h > 0 and c.value:
            return "badge"
        return "skip"
    if _is_badge(c):
        return "badge"
    if _is_boundary(c):
        return "boundary"
    if _is_grouping(c):
        return "grouping"
    if _is_tag(c):
        return "tag"
    if _is_person(c):
        return "person"
    return "service"


def match_service_caption(label: str) -> Optional[str]:
    """Match a container label to a deck caption via substring keywords."""
    lo = label.lower()
    for keyword, caption in SERVICE_KEYWORDS:
        if keyword in lo:
            return caption
    return None


# ---------------------------------------------------------------------------
# Icon lookup & rasterization
# ---------------------------------------------------------------------------


_ICON_CACHE: Dict[str, Optional[bytes]] = {}


def rasterize_svg(svg_bytes: bytes, size_px: int = 512) -> bytes:
    """Rasterize SVG → PNG via ``rsvg-convert``. Square target with aspect
    preserved via ``--keep-aspect-ratio``."""
    proc = subprocess.run(
        [
            "rsvg-convert",
            "-w", str(size_px), "-h", str(size_px),
            "--keep-aspect-ratio",
            "-f", "png",
        ],
        input=svg_bytes,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def load_icon_png(deck_prs, caption: str) -> Optional[bytes]:
    """Look up ``caption`` in the deck. Return rasterized PNG bytes or None
    if not found. Cache both hits and misses to avoid rescanning."""
    if caption in _ICON_CACHE:
        return _ICON_CACHE[caption]

    matches = find_icon_by_caption(deck_prs, caption, fuzzy=True)
    if not matches:
        _ICON_CACHE[caption] = None
        return None

    # Prefer exact caption match; else first fuzzy hit (spec: S3 uses first
    # match by slide index — the extractor already iterates slides in order).
    target_lo = caption.lower().strip()
    exact = [m for m in matches if m[2].lower() == target_lo]
    chosen = exact[0] if exact else matches[0]
    pic_shape = chosen[1]
    blob = extract_icon_image(pic_shape)
    ct = extract_icon_content_type(pic_shape)
    if ct == "image/svg+xml":
        png = rasterize_svg(blob, size_px=512)
    else:
        png = blob
    _ICON_CACHE[caption] = png
    return png


# ---------------------------------------------------------------------------
# Coordinate transform (drawio pixel space → slide EMU)
# ---------------------------------------------------------------------------


@dataclass
class Transform:
    """Scale-and-fit transform from drawio absolute pixel coordinates to
    slide EMU. Aspect is preserved (letterbox on the deficit axis)."""
    src_min_x: float
    src_min_y: float
    scale: float           # EMU per drawio pixel
    dst_x_offset: int      # EMU
    dst_y_offset: int      # EMU

    def x(self, sx: float) -> int:
        return int(self.dst_x_offset + (sx - self.src_min_x) * self.scale)

    def y(self, sy: float) -> int:
        return int(self.dst_y_offset + (sy - self.src_min_y) * self.scale)

    def dim(self, sv: float) -> int:
        return int(sv * self.scale)


def compute_transform(cells: Dict[str, Cell]) -> Transform:
    """Compute a Transform that fits the union bounding box of all
    rendered content into the content area."""
    xs: List[float] = []
    ys: List[float] = []
    for c in cells.values():
        if classify(c, cells) == "skip":
            continue
        xs.append(c.abs_x)
        ys.append(c.abs_y)
        xs.append(c.abs_x + c.w)
        ys.append(c.abs_y + c.h)
    if not xs:
        raise ValueError("no renderable cells in diagram")
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    src_w = max_x - min_x
    src_h = max_y - min_y

    scale = min(CONTENT_W / src_w, CONTENT_H / src_h)
    dst_w = int(src_w * scale)
    dst_h = int(src_h * scale)
    x_off = CONTENT_LEFT + (CONTENT_W - dst_w) // 2
    y_off = CONTENT_TOP + (CONTENT_H - dst_h) // 2
    return Transform(
        src_min_x=min_x, src_min_y=min_y,
        scale=scale, dst_x_offset=x_off, dst_y_offset=y_off,
    )


# ---------------------------------------------------------------------------
# Color / style helpers
# ---------------------------------------------------------------------------


def _hex_to_rgb(hexstr: str) -> RGBColor:
    hexstr = hexstr.lstrip("#")
    return RGBColor(
        int(hexstr[0:2], 16),
        int(hexstr[2:4], 16),
        int(hexstr[4:6], 16),
    )


def _style_color(style: str, key: str) -> Optional[RGBColor]:
    m = re.search(rf"\b{key}=#([0-9A-Fa-f]{{6}})\b", style)
    if not m:
        return None
    return _hex_to_rgb(m.group(1))


def _add_line_arrowhead(line_shape) -> None:
    """Append an ``<a:tailEnd/>`` element to the connector's line so it
    renders with a triangular arrowhead. python-pptx doesn't expose this
    via a property, so we manipulate the underlying XML directly."""
    ln = line_shape.line._get_or_add_ln()
    # Remove any existing tailEnd so we don't append duplicates.
    for existing in ln.findall(qn("a:tailEnd")):
        ln.remove(existing)
    tail = etree.SubElement(
        ln, qn("a:tailEnd"),
        attrib={"type": "triangle", "w": "med", "len": "med"},
    )
    del tail  # placate linters


# ---------------------------------------------------------------------------
# Slide rendering
# ---------------------------------------------------------------------------


def add_title_bar(slide, title_text: str, subtitle_text: str) -> None:
    """Title + subtitle + orange separator line matching the drawio look."""
    tx_w = SLIDE_W_EMU - 2 * MARGIN_LR
    if title_text:
        tb = slide.shapes.add_textbox(
            Emu(MARGIN_LR), Emu(TITLE_TOP), Emu(tx_w), Emu(TITLE_H)
        )
        tf = tb.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run()
        r.text = title_text
        r.font.size = Pt(24)
        r.font.bold = True
        r.font.name = "Helvetica"
        r.font.color.rgb = _hex_to_rgb("232F3E")
    if subtitle_text:
        sb = slide.shapes.add_textbox(
            Emu(MARGIN_LR), Emu(SUBTITLE_TOP), Emu(tx_w), Emu(SUBTITLE_H)
        )
        tf = sb.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run()
        r.text = subtitle_text
        r.font.size = Pt(12)
        r.font.name = "Helvetica"
        r.font.color.rgb = _hex_to_rgb("545B64")
    # Orange accent bar.
    line = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(MARGIN_LR), Emu(SEPARATOR_TOP), Emu(tx_w), Emu(SEPARATOR_H),
    )
    line.fill.solid()
    line.fill.fore_color.rgb = _hex_to_rgb("FF9900")
    line.line.fill.background()


def add_boundary(slide, c: Cell, t: Transform) -> None:
    """Dashed rounded-rect representing an account / region boundary."""
    x, y = t.x(c.abs_x), t.y(c.abs_y)
    w, h = t.dim(c.w), t.dim(c.h)
    rect = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    rect.fill.background()
    stroke = _style_color(c.style, "strokeColor") or _hex_to_rgb("232F3E")
    rect.line.color.rgb = stroke
    rect.line.width = Emu(EMU_PT * 2)
    rect.line.dash_style = MSO_LINE_DASH_STYLE.DASH

    tf = rect.text_frame
    tf.margin_left = Emu(140_000)
    tf.margin_top = Emu(80_000)
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run()
    r.text = c.value
    r.font.size = Pt(11)
    r.font.bold = True
    r.font.name = "Helvetica"
    r.font.color.rgb = stroke


def add_grouping(slide, c: Cell, t: Transform) -> None:
    """Solid-filled grouping swimlane (e.g., 'CMS foundation ...')."""
    x, y = t.x(c.abs_x), t.y(c.abs_y)
    w, h = t.dim(c.w), t.dim(c.h)
    rect = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    fill = _style_color(c.style, "fillColor") or _hex_to_rgb("F5F5F5")
    stroke = _style_color(c.style, "strokeColor") or _hex_to_rgb("666666")
    rect.fill.solid()
    rect.fill.fore_color.rgb = fill
    rect.line.color.rgb = stroke
    rect.line.width = Emu(EMU_PT)
    tf = rect.text_frame
    tf.margin_left = Emu(140_000)
    tf.margin_top = Emu(80_000)
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run()
    r.text = c.value
    r.font.size = Pt(11)
    r.font.bold = True
    r.font.name = "Helvetica"
    r.font.color.rgb = stroke


def add_service_box(slide, c: Cell, t: Transform, deck_prs,
                    missing_icons: List[str]) -> None:
    """Rounded rectangle + icon (from deck) + label. If no icon can be
    matched, records a miss and renders label-only."""
    caption = match_service_caption(c.value)
    x, y = t.x(c.abs_x), t.y(c.abs_y)
    w, h = t.dim(c.w), t.dim(c.h)

    rect = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    fill = _style_color(c.style, "fillColor") or _hex_to_rgb("FFFFFF")
    stroke = _style_color(c.style, "strokeColor") or _hex_to_rgb("232F3E")
    rect.fill.solid()
    rect.fill.fore_color.rgb = fill
    rect.line.color.rgb = stroke
    rect.line.width = Emu(EMU_PT * 2)

    # Icon: constant physical size, clamped so it fits in the box.
    icon_side = min(ICON_SIZE_EMU, max(int(w * 0.35), Emu(400_000)),
                    max(int(h * 0.55), Emu(400_000)))
    icon_left = x + Emu(150_000)
    icon_top = y + Emu(140_000)
    icon_placed = False
    icon_note = ""

    if caption:
        png = load_icon_png(deck_prs, caption)
        if png:
            slide.shapes.add_picture(
                io.BytesIO(png), icon_left, icon_top,
                width=icon_side, height=icon_side,
            )
            icon_placed = True
            # Note KB substitution honestly (label stays accurate).
            if ("knowledge base" in c.value.lower()
                    and caption == "Amazon Bedrock AgentCore"):
                icon_note = ("substituted 'Amazon Bedrock AgentCore' icon "
                             "(no distinct KB icon in 04-28-2026 deck)")
        else:
            missing_icons.append(
                f"[{c.id}] label='{c.value}' caption='{caption}' "
                "→ not found in deck (placeholder used)"
            )
    else:
        missing_icons.append(
            f"[{c.id}] label='{c.value}' → no service keyword matched "
            "(no icon placed)"
        )

    # `icon_note` is Optional[str]; this is an idiomatic None/empty check, not a
    # string-constant-as-boolean defect. The rule targets code that relies on a
    # non-empty literal always being truthy. See
    # issues/2026-08-03-cvx-probe-critical-triage/report.md.
    # nosemgrep: no-strings-as-booleans
    if icon_note:
        missing_icons.append(
            f"[{c.id}] label='{c.value}' → {icon_note}"
        )

    # Label placement: to the right of the icon if we placed one, otherwise
    # centered.
    label_left = (icon_left + icon_side + Emu(100_000)) if icon_placed else x + Emu(150_000)
    label_top = y + Emu(140_000)
    label_w = x + w - label_left - Emu(120_000)
    label_h = min(h - Emu(200_000), int(icon_side))
    if label_w > Emu(200_000):
        tb = slide.shapes.add_textbox(label_left, label_top, label_w, label_h)
        tf = tb.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.TOP
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run()
        r.text = c.value
        r.font.size = Pt(11)
        r.font.bold = True
        r.font.name = "Helvetica"
        r.font.color.rgb = stroke


def add_person_box(slide, c: Cell, t: Transform) -> None:
    """Rounded rect background with a simple person icon (head + torso)
    and label at the bottom. Used for Customer and Human Agent boxes."""
    x, y = t.x(c.abs_x), t.y(c.abs_y)
    w, h = t.dim(c.w), t.dim(c.h)
    rect = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    rect.fill.solid()
    rect.fill.fore_color.rgb = _hex_to_rgb("F5F5F5")
    rect.line.color.rgb = _hex_to_rgb("666666")
    rect.line.width = Emu(EMU_PT)

    # Head (oval) — 40% of the min dimension.
    head_side = min(int(w * 0.35), int(h * 0.35), Emu(500_000))
    hx = x + (w - head_side) // 2
    hy = y + Emu(220_000)
    head = slide.shapes.add_shape(MSO_SHAPE.OVAL, hx, hy, head_side, head_side)
    head.fill.solid()
    head.fill.fore_color.rgb = _hex_to_rgb("232F3E")
    head.line.fill.background()

    # Torso (trapezoid-ish rounded rectangle underneath).
    torso_w = int(head_side * 1.5)
    torso_h = int(head_side * 0.6)
    tx = x + (w - torso_w) // 2
    ty = hy + head_side + Emu(20_000)
    if ty + torso_h < y + h - Emu(300_000):
        torso = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, tx, ty, torso_w, torso_h
        )
        torso.fill.solid()
        torso.fill.fore_color.rgb = _hex_to_rgb("232F3E")
        torso.line.fill.background()

    # Label at bottom.
    lb = slide.shapes.add_textbox(
        x, y + h - Emu(360_000), w, Emu(320_000),
    )
    tf = lb.text_frame
    tf.margin_left = tf.margin_right = 0
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = c.value
    r.font.size = Pt(11)
    r.font.bold = True
    r.font.name = "Helvetica"
    r.font.color.rgb = _hex_to_rgb("333333")


def add_tag(slide, c: Cell, t: Transform) -> None:
    """Off-diagram tag ('→ Diagram 2', etc.) — a right-pointing chevron."""
    x, y = t.x(c.abs_x), t.y(c.abs_y)
    w, h = t.dim(c.w), t.dim(c.h)
    # Some drawio tags face west (direction=east flips the step shape).
    face_east = "direction=east" not in c.style
    shape_type = MSO_SHAPE.PENTAGON if face_east else MSO_SHAPE.CHEVRON
    shape = slide.shapes.add_shape(shape_type, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = _hex_to_rgb("F5F5F5")
    shape.line.color.rgb = _hex_to_rgb("999999")
    shape.line.width = Emu(EMU_PT)
    tf = shape.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = tf.margin_right = Emu(80_000)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = c.value
    r.font.size = Pt(10)
    r.font.bold = True
    r.font.name = "Helvetica"
    r.font.color.rgb = _hex_to_rgb("232F3E")


def add_badge(slide, c: Cell, t: Transform) -> None:
    x, y = t.x(c.abs_x), t.y(c.abs_y)
    w, h = t.dim(c.w), t.dim(c.h)
    shape_type = MSO_SHAPE.OVAL if "ellipse" in c.style else MSO_SHAPE.ROUNDED_RECTANGLE
    badge = slide.shapes.add_shape(shape_type, x, y, w, h)
    badge.fill.solid()
    badge.fill.fore_color.rgb = _hex_to_rgb("007CBD")
    badge.line.fill.background()
    tf = badge.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.word_wrap = False
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = c.value or ""
    r.font.size = Pt(9)
    r.font.bold = True
    r.font.name = "Helvetica"
    r.font.color.rgb = _hex_to_rgb("FFFFFF")


def _edge_from_center(cx: float, cy: float, tx: float, ty: float,
                      half_w: float, half_h: float) -> Tuple[float, float]:
    """Point on the axis-aligned rectangle border (centered at cx,cy with
    given half-extents) along the ray from (cx,cy) toward (tx,ty). Used
    to terminate arrows at box borders rather than centers."""
    dx = tx - cx
    dy = ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    if dx == 0:
        return cx, cy + (half_h if dy > 0 else -half_h)
    if dy == 0:
        return cx + (half_w if dx > 0 else -half_w), cy
    r = min(half_w / abs(dx), half_h / abs(dy))
    return cx + dx * r, cy + dy * r


def add_arrows(slide, cells: Dict[str, Cell], t: Transform) -> int:
    """Draw straight-line connectors (with triangular arrowheads) between
    each edge cell's source and target, trimmed to box borders. Colors
    and dashed style are lifted from the drawio edge style. Returns count."""
    count = 0
    for c in cells.values():
        if not c.is_edge:
            continue
        if not c.source or not c.target:
            continue
        s = cells.get(c.source)
        d = cells.get(c.target)
        if s is None or d is None:
            continue
        # Compute centers in EMU.
        sw = t.dim(s.w)
        sh = t.dim(s.h)
        dw = t.dim(d.w)
        dh = t.dim(d.h)
        sx = t.x(s.abs_x) + sw / 2
        sy = t.y(s.abs_y) + sh / 2
        dx = t.x(d.abs_x) + dw / 2
        dy = t.y(d.abs_y) + dh / 2

        p1x, p1y = _edge_from_center(sx, sy, dx, dy, sw / 2, sh / 2)
        p2x, p2y = _edge_from_center(dx, dy, sx, sy, dw / 2, dh / 2)

        line = slide.shapes.add_connector(
            MSO_CONNECTOR_TYPE.STRAIGHT,
            int(p1x), int(p1y), int(p2x), int(p2y),
        )
        stroke = _style_color(c.style, "strokeColor") or _hex_to_rgb("545B64")
        line.line.color.rgb = stroke
        line.line.width = Emu(EMU_PT * 2)
        if "dashed=1" in c.style:
            line.line.dash_style = MSO_LINE_DASH_STYLE.DASH
        _add_line_arrowhead(line)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Per-slide orchestration
# ---------------------------------------------------------------------------


def render_slide(prs, drawio_path: Path, deck_prs,
                 missing_icons: List[str]) -> Dict[str, int]:
    """Render one slide from one drawio file. Returns per-category counts."""
    cells, meta = parse_drawio(drawio_path)
    t = compute_transform(cells)

    blank_layout = prs.slide_layouts[6]  # standard blank layout
    slide = prs.slides.add_slide(blank_layout)

    counts = {
        "boundaries": 0, "groupings": 0, "services": 0, "persons": 0,
        "tags": 0, "arrows": 0, "badges": 0,
    }

    add_title_bar(slide, meta["title"], meta["subtitle"])

    # Order: boundaries (back) → groupings → arrows (behind boxes) → boxes →
    # badges (in front of arrows so numbers are readable).
    for c in cells.values():
        if classify(c, cells) == "boundary":
            add_boundary(slide, c, t)
            counts["boundaries"] += 1
    for c in cells.values():
        if classify(c, cells) == "grouping":
            add_grouping(slide, c, t)
            counts["groupings"] += 1

    counts["arrows"] = add_arrows(slide, cells, t)

    for c in cells.values():
        cls = classify(c, cells)
        if cls == "service":
            add_service_box(slide, c, t, deck_prs, missing_icons)
            counts["services"] += 1
        elif cls == "person":
            add_person_box(slide, c, t)
            counts["persons"] += 1
        elif cls == "tag":
            add_tag(slide, c, t)
            counts["tags"] += 1

    for c in cells.values():
        if classify(c, cells) == "badge":
            add_badge(slide, c, t)
            counts["badges"] += 1

    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble CVX diagrams into a pptx deck.")
    parser.add_argument("--deck", default=str(DECK_PATH),
                        help="Source AWS icon deck (pptx).")
    parser.add_argument("--output", default=str(OUTPUT),
                        help="Output pptx path.")
    args = parser.parse_args()

    deck_path = Path(args.deck).expanduser()
    output_path = Path(args.output).expanduser()

    if not deck_path.exists():
        print(f"ERROR: icon deck not found at {deck_path}", file=sys.stderr)
        return 2

    # Verify rsvg-convert is on PATH.
    try:
        subprocess.run(["rsvg-convert", "--version"],
                       capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ERROR: rsvg-convert not found on PATH — "
              "install via `brew install librsvg`", file=sys.stderr)
        return 3

    print(f"Loading icon deck: {deck_path.name}")
    deck_prs = Presentation(str(deck_path))
    print(f"  ({len(deck_prs.slides)} slides)")

    print("Building output presentation")
    prs = Presentation()
    prs.slide_width = Emu(SLIDE_W_EMU)
    prs.slide_height = Emu(SLIDE_H_EMU)

    missing_icons: List[str] = []
    slide_counts: List[Tuple[str, Dict[str, int]]] = []

    for fname, short in DIAGRAMS:
        src = DIAGRAMS_DIR / fname
        if not src.exists():
            print(f"  ERROR: missing {src}", file=sys.stderr)
            return 4
        print(f"  rendering: {short} ({fname})")
        counts = render_slide(prs, src, deck_prs, missing_icons)
        print(f"    counts: {counts}")
        slide_counts.append((short, counts))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))
    print(f"\nWrote: {output_path}")

    # Verification: re-open and count shapes per slide.
    print("\nVerification: re-opening output")
    check = Presentation(str(output_path))
    if len(check.slides) != 3:
        print(f"  ERROR: expected 3 slides, got {len(check.slides)}",
              file=sys.stderr)
        return 5
    for i, slide in enumerate(check.slides):
        # Split into pictures vs. non-pictures for a quick sanity check.
        pics = 0
        others = 0
        for shp in slide.shapes:
            try:
                if shp.shape_type is not None and shp.shape_type == 13:
                    pics += 1
                else:
                    others += 1
            except Exception:
                others += 1
        print(f"  slide {i + 1}: {len(slide.shapes)} shape(s) "
              f"({pics} picture(s), {others} other)")

    if missing_icons:
        print(f"\nIcon-lookup notes ({len(missing_icons)}):")
        for m in missing_icons:
            print(f"  - {m}")
    else:
        print("\nAll service boxes matched an icon.")

    print("\nSummary:")
    print(f"  Output: {output_path}")
    print(f"  Slides: {len(check.slides)}")
    for short, counts in slide_counts:
        summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        print(f"    - {short}: {summary}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
