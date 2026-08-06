"""
build_cvx_house_style_slide.py — Build a CVX reference architecture slide
that conforms to the AWS Solutions Library house style used in
``~/Downloads/automotive_Data_platform_arch.pptx`` slide 4 (Customer 360
Agentic AI platform).

House style rules (extracted directly from the template):
 * 16:9 canvas (13.33 x 7.5 in)
 * Title text box top-left (~0.3, 0.05, 9.6, 1.0) with two-line title:
   guidance name @ 28pt bold, tagline @ 14pt bold
 * Horizontal separator LINE at y=1.10 spanning full diagram width
 * Vertical divider AUTO_SHAPE (grey fill EAEDED, no border) at x=9.82,
   full height, marking the right-panel boundary
 * Right-panel TEXT_BOX at x=10.18, full height, with 5-6 numbered
   paragraphs (10-11pt Arial regular)
 * Numbered badges (AUTO_SHAPE ovals, 0.30x0.30in, 1-6) placed in two
   places: on the diagram at the specific zone they annotate, AND along
   the right edge of the title area anchoring each right-panel paragraph
 * Named zones = AUTO_SHAPE rectangles with a text label (10-12pt Arial),
   line color = template's neutral gray (7D8998), containing AWS icons
   + captions
 * Cross-account boundary uses dashed pink E7157B border (matches
   template convention for account/governance boundary)
 * AWS icons are rasterized SVGs pulled from the AWS Architecture Icons
   Deck via the existing ``pptx_icon_extractor`` module

Output: ``documentation/diagrams/cvx-reference-architecture-house-style.pptx``
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from typing import Optional

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

# Local module for icon extraction
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from pptx_icon_extractor import (  # noqa: E402
    extract_icon_content_type,
    extract_icon_image,
    find_icon_by_caption,
)

# --- constants -------------------------------------------------------------

REPO_ROOT = Path("~/guidance-for-connected-vehicle-experience-on-aws").expanduser()
OUTPUT_PATH = REPO_ROOT / "documentation" / "diagrams" / "cvx-reference-architecture-house-style-1a.pptx"
TEMPLATE_PATH = Path(
    "~/Downloads/automotive_Data_platform_arch.pptx"
).expanduser()
ICON_DECK_PATH = Path(
    "~/Downloads/Microsoft-PPTx-toolkits_04302026."
    "1ee04f761e1661409056abeb3ca627bb5e336688/"
    "AWS-Architecture-Icons-Deck_For-Light-BG_04282026.pptx"
).expanduser()

# Template's neutral gray for zone borders (from template slide 4)
NEUTRAL_ZONE_LINE = RGBColor(0x7D, 0x89, 0x98)
# Pink E7157B — template's account-boundary color (Central Governance Account)
ACCOUNT_BOUNDARY_COLOR = RGBColor(0xE7, 0x15, 0x7B)
# Sidebar background fill (from template)
SIDEBAR_BG_COLOR = RGBColor(0xEA, 0xED, 0xED)
# Persona maturity convention (same colors used in other CVX project diagrams):
#   green solid  = "Demonstrated in CVX::"
#   amber dashed = "Not yet demonstrated in a reference implementation::"
PERSONA_DEMONSTRATED_COLOR = RGBColor(0x1F, 0x8A, 0x3D)  # green
PERSONA_ROADMAP_COLOR = RGBColor(0xC2, 0x74, 0x0C)       # burnt amber
# Deep navy — used for both badges and connector arrows so the whole
# navigation layer reads as one visual system.
ARROW_COLOR = RGBColor(0x00, 0x2B, 0x36)
# Font
FONT_NAME = "Arial"
# Line widths
STANDARD_LINE_WIDTH = Emu(15875)
ARROW_LINE_WIDTH = Emu(19050)  # slightly heavier than zone borders


# --- SVG rasterization helpers (mirrors drawio_to_pptx.py) -----------------


def rasterize_svg(svg_bytes: bytes, size_px: int = 512) -> bytes:
    """Rasterize SVG bytes to PNG via ``rsvg-convert``."""
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


_ICON_CACHE: dict[str, Optional[bytes]] = {}


def load_icon_png(deck_prs: Presentation, caption: str) -> Optional[bytes]:
    """Look up ``caption`` in the AWS icon deck. Return PNG bytes.

    Applies the substitution convention from ``drawio_to_pptx.py``:
    "Amazon Bedrock Knowledge Base" → "Amazon Bedrock AgentCore" icon
    (since the 04-28-2026 deck ships no distinct KB icon)."""
    lookup = caption
    if "knowledge base" in caption.lower():
        lookup = "Amazon Bedrock AgentCore"
    if "recall" in caption.lower():
        lookup = "AWS Lambda"
    # Nova 2 Sonic is newer than the 04-28-2026 icon release — no distinct
    # icon exists for it. The generic "Amazon Nova" (family) icon IS in the
    # deck (slide 51) BUT it renders as an 8-pointed star/burst that reads
    # ambiguously (bell/notification/alert) at the small size used in this
    # diagram (see visual-inspection findings 2026-07-16). Per the KB→AgentCore
    # fallback convention, ship the AgentCore icon with an accurate
    # "Nova 2 Sonic" text label instead of a plausible-looking wrong icon.
    if ("nova" in caption.lower() and "sonic" in caption.lower()) or \
       caption.lower().strip() in {"amazon nova 2 sonic", "nova 2 sonic"}:
        lookup = "Amazon Bedrock AgentCore"

    if lookup in _ICON_CACHE:
        cached = _ICON_CACHE[lookup]
        if cached is None:
            return None
        return cached

    matches = find_icon_by_caption(deck_prs, lookup, fuzzy=True)
    if not matches:
        # Try an "Amazon Simple Storage Service" alternate for S3
        if lookup.lower() == "amazon s3":
            matches = find_icon_by_caption(
                deck_prs, "Amazon Simple Storage Service", fuzzy=True
            )
    if not matches and lookup == "Amazon Nova":
        # Nova family icon missing — fall through to AgentCore substitute
        # (matches the KB→AgentCore convention). Not reached at 2026-07-16
        # since Nova/Sonic lookups now resolve directly to AgentCore above.
        matches = find_icon_by_caption(
            deck_prs, "Amazon Bedrock AgentCore", fuzzy=True,
        )
    if not matches:
        print(f"[warn] no icon matched for caption {lookup!r}", file=sys.stderr)
        _ICON_CACHE[lookup] = None
        return None

    # Prefer exact caption match; else first fuzzy hit
    target_lo = lookup.lower().strip()
    exact = [m for m in matches if m[2].lower() == target_lo]
    chosen = exact[0] if exact else matches[0]
    pic_shape = chosen[1]
    blob = extract_icon_image(pic_shape)
    ct = extract_icon_content_type(pic_shape)
    if ct == "image/svg+xml":
        png = rasterize_svg(blob, size_px=512)
    else:
        png = blob
    _ICON_CACHE[lookup] = png
    return png


# --- text helpers ----------------------------------------------------------


def set_para_text(paragraph, text: str, *, size: Pt, bold: bool = False,
                  color: Optional[RGBColor] = None, name: str = FONT_NAME) -> None:
    """Assign text and formatting to a single-run paragraph."""
    # Clear existing runs; python-pptx starts with an empty paragraph on new tf.
    for _r in list(paragraph.runs):
        _r.text = ""
    run = paragraph.add_run()
    run.text = text
    run.font.name = name
    run.font.size = size
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def new_paragraph(text_frame, text: str, **kwargs) -> None:
    """Append a new paragraph with the given text and formatting."""
    p = text_frame.add_paragraph()
    set_para_text(p, text, **kwargs)


# --- shape helpers ---------------------------------------------------------


def add_title_box(slide, guidance: str, tagline: str) -> None:
    """Top-left title box, matching template slide 4 layout."""
    left = Inches(0.3)
    top = Inches(0.05)
    width = Inches(9.6)
    height = Inches(1.0)
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = 0
    tf.margin_top = 0
    tf.margin_right = 0
    tf.margin_bottom = 0
    # First paragraph = guidance
    p0 = tf.paragraphs[0]
    set_para_text(p0, guidance, size=Pt(28), bold=True)
    # Second paragraph = tagline
    new_paragraph(tf, tagline, size=Pt(14), bold=True)


def add_horizontal_separator(slide) -> None:
    """Horizontal line at y=1.35, spanning ~9.4in of diagram area.

    Sits BELOW the auto-wrapping title: the guidance name at 28pt bold wraps
    to 2 lines because "Guidance for Connected Vehicle Experience on AWS"
    doesn't fit in 9.6in at 28pt, so the tagline is the *third* line, ending
    at approximately y=1.22 (top=0.05 + 2*0.467in title-line-height +
    1*0.233in tagline-line-height ≈ 1.22). We place the divider at 1.35 to
    give a ~0.13in margin below the tagline text.
    """
    left = Inches(0.3)
    top = Inches(1.35)
    width = Inches(9.4)
    line = slide.shapes.add_connector(1, left, top, left + width, top)  # 1 == STRAIGHT
    line.line.color.rgb = RGBColor(0x7D, 0x89, 0x98)
    line.line.width = Emu(15875)


def add_sidebar_background(slide) -> None:
    """Grey background rectangle for the right narrative panel."""
    left = Inches(9.815)
    top = Inches(0)
    width = Inches(3.52)
    height = Inches(7.5)
    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    bg.fill.solid()
    bg.fill.fore_color.rgb = SIDEBAR_BG_COLOR
    bg.line.fill.background()  # no border
    # No text
    bg.text_frame.text = ""


def add_right_panel(
    slide,
    paragraphs: list[str],
    space_after_pt: float = 3.0,
    font_size_pt: float = 9.0,
) -> None:
    """Right-panel narrative TextBox with numbered paragraphs.

    Each paragraph's first character is a leading digit (matching how the
    template's slide 4 handles numbered blocks). Font 9pt Arial regular by
    default. Uses one paragraph per element (multi-line via wrap), matching
    the template density (~230-300 chars each).

    ``space_after_pt``: paragraph spacing in points. Default 3pt matches
    the 7-paragraph layout; drop to 2pt (or lower) when 8 paragraphs need
    to fit.
    ``font_size_pt``: drop to 8pt when 8 longer paragraphs won't fit at 9pt.
    """
    left = Inches(10.18)
    top = Inches(0.15)
    width = Inches(3.10)
    height = Inches(7.20)
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = Inches(0.05)
    tf.margin_top = Inches(0.05)
    tf.margin_right = Inches(0.05)
    tf.margin_bottom = Inches(0.05)

    for idx, para in enumerate(paragraphs):
        if idx == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        set_para_text(p, para, size=Pt(font_size_pt), bold=False)
        p.space_after = Pt(space_after_pt)


def add_number_badge(slide, digit: int, left: Emu, top: Emu,
                     size: Emu = Inches(0.28),
                     fill_color: Optional[RGBColor] = None) -> None:
    """Add a numbered oval badge (matches template's Oval shapes)."""
    if fill_color is None:
        fill_color = RGBColor(0x00, 0x2B, 0x36)  # deep navy — visible on light bg
    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, left, top, size, size)
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    shape.line.fill.background()  # no border
    tf = shape.text_frame
    tf.margin_left = 0
    tf.margin_top = 0
    tf.margin_right = 0
    tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    set_para_text(p, str(digit), size=Pt(11), bold=True, color=RGBColor(255, 255, 255))


def add_top_of_panel_badges(slide, count: int, y_positions_in: list[float]) -> None:
    """Add badges along right edge of title/panel divider, one per paragraph.

    ``y_positions_in`` is a list of floats (in inches) giving each badge's top.
    """
    left = Inches(9.93)
    for idx, y in enumerate(y_positions_in, start=1):
        add_number_badge(slide, idx, left, Inches(y))


def _add_small_label(
    slide,
    text: str,
    left_in: float,
    top_in: float,
    width_in: float,
    height_in: float,
    *,
    size: Pt = Pt(8),
    bold: bool = False,
    italic: bool = False,
    align_center: bool = False,
    color: Optional[RGBColor] = None,
) -> None:
    """Small utility text box for arrow labels, sub-box captions, and
    inline annotations. Zero margins so short strings sit tight in a
    small rectangle.
    """
    tb = slide.shapes.add_textbox(
        Inches(left_in), Inches(top_in), Inches(width_in), Inches(height_in),
    )
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(0.02)
    tf.margin_top = Inches(0.01)
    tf.margin_right = Inches(0.02)
    tf.margin_bottom = Inches(0.01)
    tf.vertical_anchor = MSO_ANCHOR.TOP
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER if align_center else PP_ALIGN.LEFT
    run = p.add_run()
    run.text = text
    run.font.name = FONT_NAME
    run.font.size = size
    run.font.bold = bold
    run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color


def add_arrow(
    slide,
    x1_in: float,
    y1_in: float,
    x2_in: float,
    y2_in: float,
    *,
    dashed: bool = False,
    color: Optional[RGBColor] = None,
    width_emu: int = 19050,
) -> object:
    """Add a straight line with a triangle arrowhead at (x2, y2).

    python-pptx's ``add_connector`` produces a naked line; we add the
    ``<a:tailEnd>`` element directly in the XML to get a rendered
    arrowhead compatible with PowerPoint and LibreOffice.

    Arrowhead attributes (OOXML CT_LineEndProperties): ``type``, ``w``
    (width sm/med/lg), ``len`` (length sm/med/lg). Note: the attribute
    for length is ``len``, NOT ``h``. Using ``h`` silently fails in
    LibreOffice (invalid attribute is dropped, arrowhead renders at
    default med-size which is visually thin on a dashed line). Fixed
    2026-07-16 after visual-inspection findings.
    """
    if color is None:
        color = ARROW_COLOR
    line = slide.shapes.add_connector(
        1,  # 1 == STRAIGHT connector type
        Inches(x1_in), Inches(y1_in),
        Inches(x2_in), Inches(y2_in),
    )
    line.line.color.rgb = color
    line.line.width = Emu(width_emu)
    if dashed:
        line.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    # XML: append <a:tailEnd type="triangle" w="lg" len="lg"/> to <a:ln>
    # Schema order for CT_LineProperties: fill → dash → join → headEnd →
    # tailEnd. tailEnd MUST come after prstDash; python-pptx's
    # dash_style setter added prstDash above, so appending here is
    # schema-correct.
    from pptx.oxml.ns import qn
    from lxml import etree
    ln_elem = line.line._get_or_add_ln()
    for existing in ln_elem.findall(qn("a:tailEnd")):
        ln_elem.remove(existing)
    tail = etree.SubElement(ln_elem, qn("a:tailEnd"))
    tail.set("type", "triangle")
    tail.set("w", "lg")
    tail.set("len", "lg")
    return line


def add_short_gap_arrow(
    slide,
    left_in: float,
    top_in: float,
    width_in: float = 0.14,
    height_in: float = 0.14,
    *,
    dashed: bool = False,
    color: Optional[RGBColor] = None,
) -> object:
    """Add a small filled RIGHT_ARROW shape.

    Used for arrows across narrow inter-zone gaps (~0.10-0.20"), where a
    line-plus-arrowhead is too thin to read. Filled arrow shapes render
    reliably at small sizes in both PowerPoint and LibreOffice.
    """
    if color is None:
        color = ARROW_COLOR
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RIGHT_ARROW,
        Inches(left_in), Inches(top_in),
        Inches(width_in), Inches(height_in),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    if dashed:
        shape.line.color.rgb = color
        shape.line.dash_style = MSO_LINE_DASH_STYLE.DASH
        shape.line.width = Emu(9525)
    else:
        shape.line.fill.background()
    return shape


def add_persona_column(
    slide,
    left_in: float,
    top_in: float,
    width_in: float,
    height_in: float,
) -> None:
    """Add the persona column with two maturity-tagged sub-groupings.

    Uses the exact phrasing from the IG's personas-and-tenancy.adoc
    (anchor pt-six-personas) — 'Demonstrated in CVX::' and 'Not yet
    demonstrated in a reference implementation::'. Do NOT paraphrase.

    Green solid border for the 'Demonstrated' subgroup;
    amber dashed for the 'Not yet demonstrated' subgroup.

    Implementation note: text is placed in overlaid text boxes rather
    than inside the AutoShape rectangles' own text frames, because
    LibreOffice's PPTX rendering drops mid-frame paragraphs when the
    frame mixes bold-plus-colored labels with plain body paragraphs
    (observed 2026-07-16). Overlaid text boxes render reliably.
    """
    # --- Outer container: transparent, neutral gray border ---
    outer = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(left_in), Inches(top_in),
        Inches(width_in), Inches(height_in),
    )
    outer.fill.background()
    outer.line.color.rgb = NEUTRAL_ZONE_LINE
    outer.line.width = STANDARD_LINE_WIDTH
    # Zone-style small header inside the outer container's own text frame.
    tf = outer.text_frame
    tf.margin_left = Inches(0.05)
    tf.margin_top = Inches(0.03)
    tf.margin_right = Inches(0.05)
    tf.margin_bottom = Inches(0.03)
    tf.vertical_anchor = MSO_ANCHOR.TOP
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    set_para_text(
        p, "Personas", size=Pt(11), bold=False, color=NEUTRAL_ZONE_LINE,
    )

    # --- Green subgroup rectangle (border only, no text) ---
    green_top = top_in + 0.38
    green_h = 1.80
    green_box = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(left_in + 0.06), Inches(green_top),
        Inches(width_in - 0.12), Inches(green_h),
    )
    green_box.fill.background()
    green_box.line.color.rgb = PERSONA_DEMONSTRATED_COLOR
    green_box.line.width = STANDARD_LINE_WIDTH
    green_box.text_frame.text = ""  # ensure no auto-content

    # Green subgroup label + persona names as separate text boxes.
    inner_left = left_in + 0.11
    inner_w = width_in - 0.22
    _add_small_label(
        slide, "Demonstrated in CVX::",
        left_in=inner_left, top_in=green_top + 0.04,
        width_in=inner_w, height_in=0.28,
        size=Pt(7.5), bold=True,
        color=PERSONA_DEMONSTRATED_COLOR,
    )
    _add_small_label(
        slide, "fleet_driver",
        left_in=inner_left, top_in=green_top + 0.42,
        width_in=inner_w, height_in=0.18, size=Pt(8),
    )
    _add_small_label(
        slide, "service_advisor",
        left_in=inner_left, top_in=green_top + 0.62,
        width_in=inner_w, height_in=0.18, size=Pt(8),
    )
    _add_small_label(
        slide, "technician",
        left_in=inner_left, top_in=green_top + 0.82,
        width_in=inner_w, height_in=0.18, size=Pt(8),
    )
    _add_small_label(
        slide, "(fleet_driver = default)",
        left_in=inner_left, top_in=green_top + 1.10,
        width_in=inner_w, height_in=0.18, size=Pt(6.5), italic=True,
        color=PERSONA_DEMONSTRATED_COLOR,
    )

    # --- Amber (dashed) subgroup rectangle (border only, no text) ---
    orange_top = green_top + green_h + 0.10
    orange_h = height_in - (orange_top - top_in) - 0.05
    orange_box = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(left_in + 0.06), Inches(orange_top),
        Inches(width_in - 0.12), Inches(orange_h),
    )
    orange_box.fill.background()
    orange_box.line.color.rgb = PERSONA_ROADMAP_COLOR
    orange_box.line.width = STANDARD_LINE_WIDTH
    orange_box.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    orange_box.text_frame.text = ""

    # Orange subgroup label wraps to ~4 lines; give it 0.68in height.
    _add_small_label(
        slide, "Not yet demonstrated in a reference implementation::",
        left_in=inner_left, top_in=orange_top + 0.04,
        width_in=inner_w, height_in=0.68,
        size=Pt(7.5), bold=True,
        color=PERSONA_ROADMAP_COLOR,
    )
    _add_small_label(
        slide, "consumer",
        left_in=inner_left, top_in=orange_top + 0.78,
        width_in=inner_w, height_in=0.18, size=Pt(8),
    )
    _add_small_label(
        slide, "retail_advisor",
        left_in=inner_left, top_in=orange_top + 0.98,
        width_in=inner_w, height_in=0.18, size=Pt(8),
    )
    _add_small_label(
        slide, "vehicle_owner",
        left_in=inner_left, top_in=orange_top + 1.18,
        width_in=inner_w, height_in=0.18, size=Pt(8),
    )


def add_zone(
    slide,
    label: str,
    left_in: float,
    top_in: float,
    width_in: float,
    height_in: float,
    *,
    line_color: RGBColor = NEUTRAL_ZONE_LINE,
    dash_style: Optional[int] = None,
    label_size: Pt = Pt(11),
) -> object:
    """Add a named zone rectangle with a small label in the top-left."""
    left = Inches(left_in)
    top = Inches(top_in)
    width = Inches(width_in)
    height = Inches(height_in)
    zone = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    zone.fill.background()  # transparent fill
    zone.line.color.rgb = line_color
    zone.line.width = STANDARD_LINE_WIDTH
    if dash_style is not None:
        zone.line.dash_style = dash_style
    tf = zone.text_frame
    tf.margin_left = Inches(0.08)
    tf.margin_top = Inches(0.05)
    tf.margin_right = Inches(0.05)
    tf.margin_bottom = Inches(0.05)
    tf.vertical_anchor = MSO_ANCHOR.TOP
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    set_para_text(p, label, size=label_size, bold=False, color=line_color)
    return zone


def add_icon(
    slide,
    icon_png: bytes,
    left_in: float,
    top_in: float,
    size_in: float = 0.45,
) -> None:
    """Insert a rasterized AWS icon."""
    stream = io.BytesIO(icon_png)
    slide.shapes.add_picture(
        stream, Inches(left_in), Inches(top_in),
        width=Inches(size_in), height=Inches(size_in),
    )


def add_icon_caption(
    slide,
    caption: str,
    left_in: float,
    top_in: float,
    width_in: float = 1.4,
    height_in: float = 0.35,
    size: Pt = Pt(9),
) -> None:
    """Add a caption text under an icon."""
    tb = slide.shapes.add_textbox(
        Inches(left_in), Inches(top_in), Inches(width_in), Inches(height_in),
    )
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = 0
    tf.margin_top = 0
    tf.margin_right = 0
    tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    set_para_text(p, caption, size=size, bold=False)


def add_icon_with_caption(
    slide,
    deck_prs: Presentation,
    caption: str,
    center_x_in: float,
    icon_top_in: float,
    icon_size_in: float = 0.45,
    caption_width_in: float = 1.4,
    caption_size: Pt = Pt(9),
    caption_override: Optional[str] = None,
    skip_caption: bool = False,
) -> None:
    """Add an AWS icon + centered caption below it. Fails hard on missing icon.

    ``caption_override``: if provided, the visible caption text is replaced by
    this string (icon lookup still uses ``caption``).
    ``skip_caption``: if True, do not draw a caption text box (used when the
    caller wants to render a manual caption at a different position).
    """
    icon_png = load_icon_png(deck_prs, caption)
    if icon_png is None:
        raise RuntimeError(f"No icon available for caption {caption!r}")
    icon_left = center_x_in - icon_size_in / 2
    add_icon(slide, icon_png, icon_left, icon_top_in, size_in=icon_size_in)
    if skip_caption:
        return
    caption_text = caption_override if caption_override is not None else caption
    caption_left = center_x_in - caption_width_in / 2
    caption_top = icon_top_in + icon_size_in + 0.03
    add_icon_caption(
        slide, caption_text, caption_left, caption_top,
        width_in=caption_width_in, size=caption_size,
    )


# --- slide assembly --------------------------------------------------------


def strip_all_slides(prs: Presentation) -> None:
    """Remove all slides from a presentation while preserving master/layouts."""
    sldIdLst = prs.slides._sldIdLst  # noqa: SLF001
    for sldId in list(sldIdLst):
        rId = sldId.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        prs.part.drop_rel(rId)
        sldIdLst.remove(sldId)


def build_slide(prs: Presentation, deck_prs: Presentation) -> None:
    """Build the single CVX reference architecture slide."""
    # Template ships only 1 layout ("Blank") — use index 0.
    blank_layout = prs.slide_layouts[0]
    slide = prs.slides.add_slide(blank_layout)

    # --- background elements (order matters: bottom-first) ---
    add_sidebar_background(slide)
    add_horizontal_separator(slide)

    # --- title ---
    add_title_box(
        slide,
        guidance="Guidance for Connected Vehicle Experience on AWS",
        tagline="Grounded, cross-account agentic responses across the ownership journey",
    )

    # --- right-panel narrative (8 paragraphs) ---
    # Paragraph 8 (new) covers the dual-runtime split and persona routing.
    # 8pt font + 2pt spacing lets 8 paragraphs fit in the 7.20in panel.
    paragraphs = [
        "CVX is the composition layer that binds live operational state from Connected Mobility (CMS) with governed analytical data products from the Automotive Data Platform (ADP). CVX holds no vehicle database, KB, or lake — it fuses live and analytical data per-session and dispatches per-turn tool calls.",
        "At session initialization, the persona loader runs one partition-scoped cross-account Athena query against ADP's Lake-Formation-governed data products, serializes results to JSON, and seeds AgentCore Memory. This one-time Athena hop takes ~6-7s and never repeats within a session.",
        "After init, tool dispatch reads only from Memory (sub-100ms exact-match filters on persona / vehicle / customer IDs) or from Bedrock KB Retrieve (filtered by source_category). Athena is never re-queried per turn — the cost & latency invariant that keeps conversations fast.",
        "The cross-account authorization boundary is enforced by AWS Lake Formation cross-account grants (per-database GetDataAccess) and by the Bedrock KB resource policy (CVX IAM principals authorized for Retrieve). CVX receives session-scoped S3 tokens only; ADP owns the lake, catalog, and KMS keys.",
        "CMS provides live operational state via Amazon ElastiCache (Redis LKS — sub-ms telemetry snapshots read once at session init) and via Amazon DynamoDB for service-history writes and NHTSA recalls (through the recalls Lambda). All CMS reads use Lambda-backed APIs.",
        "Graceful degradation: if Memory seeding fails, affected tools return structured \"not yet available\" messages and the session continues. If KB Retrieve fails, DTC tools fall back to local rules. If Redis LKS is unavailable, triage defaults to conservative P1.",
        "Terminal tools (book, finance_qualify, payment_initiate, reservation_handoff) call systems OUTSIDE AWS via webhook or API — ticket sinks (SQS / Salesforce / ServiceNow), dealer DMS / OEM finance (Reynolds, CDK Global, Tekion, Dealertrack, Fleetio, Whip Around, Stripe/Plaid — illustrative, v3 direct-DMS write is roadmap), and Own-stage subscription/CRM (Zuora, Salesforce Service Cloud — illustrative). Not yet demonstrated in a reference implementation:: a Sell/Trade-in marketplace handoff (illustrative: Amazon Autos) — no CVX v1 code path exists for this. CVX does not own or write directly into any of these partner systems.",
        "One supervisor code base runs two modes: bidi voice (`BidiNovaSonicModel`, Amazon Connect) and text/HTTP (`BedrockModel`, API Gateway). Only transport and model class differ. Demonstrated in CVX:: `fleet_driver`, `service_advisor`, `technician`. Not yet demonstrated in a reference implementation:: `consumer`, `retail_advisor`, `vehicle_owner`.",
    ]
    add_right_panel(slide, paragraphs, space_after_pt=2.0, font_size_pt=8.0)

    # --- 8 numbered badges on right panel divider (one per paragraph) ---
    # Recomputed for 8-paragraph layout at 8pt / 2pt spacing.
    # Line-height ~0.14in at 8pt, ~52 chars/line in a 3.00in text column:
    #   P1 302c → 6 lines (0.84in) top=0.20
    #   P2 279c → 6 lines (0.84in) top=1.07
    #   P3 273c → 6 lines (0.84in) top=1.94
    #   P4 300c → 6 lines (0.84in) top=2.81
    #   P5 262c → 5 lines (0.70in) top=3.68
    #   P6 254c → 5 lines (0.70in) top=4.41
    #   P7 305c → 6 lines (0.84in) top=5.14
    #   P8 410c → 8 lines (1.12in) top=6.01
    panel_badge_y = [0.15, 1.02, 1.89, 2.76, 3.63, 4.36, 5.09, 5.96]
    add_top_of_panel_badges(slide, count=8, y_positions_in=panel_badge_y)

    # ============= DIAGRAM AREA =============
    #
    # Redesigned layout (2026-07-16) — adds a persona column on the far
    # left and splits the AgentCore Runtime box into two sub-boxes (bidi
    # voice / text HTTP) inside Zone 2. Diagram area is 0.30-9.65 wide.
    #
    # ==== -1a RESTRUCTURE (2026-07-16) ====
    # Per user request: (1) ADP Data Zone moves from a right-side column
    # to a BOTTOM BAND spanning horizontally under AgentCore Runtime, and
    # (2) External Partners moves from a strip under AgentCore Runtime to
    # a FAR-RIGHT VERTICAL COLUMN mirroring the Personas column's own
    # construction (same pattern: outer neutral-gray container, badge at
    # top). AgentCore Runtime widens into the space ADP vacated on the
    # right, since it no longer shares that horizontal band with ADP.
    #
    #   Personas:      x=0.30-1.50 (w=1.20)   y=1.50-6.85 (h=5.35)  [unchanged]
    #   Zone 1:        x=1.60-2.85 (w=1.25)   y=1.50-3.85 (Ingress) [unchanged]
    #   Zone 3:        x=1.60-2.85 (w=1.25)   y=4.00-6.85 (CMS)     [unchanged]
    #   Zone 2:        x=2.95-8.30 (w=5.35)   y=1.50-5.20 (AgentCore — WIDER, SHORTER)
    #   Zone 4:        x=2.95-8.30 (w=5.35)   y=5.30-6.85 (h=1.55) (cross-account — NOW BOTTOM BAND)
    #     Zone 5:      x=3.05-8.20 (w=5.15)   y=5.55-6.75 (h=1.20) (ADP Data Zone, nested)
    #   Zone 6 (NEW):  x=8.40-9.65 (w=1.25)   y=1.50-6.85 (h=5.35) (External Partners — FAR-RIGHT COLUMN)
    #
    # Inside Zone 2 (unchanged sub-box geometry, just more headroom to
    # the right since Zone 2's right edge moved from 6.25 to 8.30):
    #   Sub-box A (bidi):   x=3.05-4.60 (w=1.55)   y=1.85-3.25 (h=1.40)
    #   Sub-box B (text):   x=4.70-6.20 (w=1.50)   y=1.85-3.25 (h=1.40)
    #   Shared invariant caption:                  y=3.30-3.52
    #   AgentCore Memory icon + caption:           y=3.60-4.35
    #   Session init / per-turn caption block:     y=4.45-4.98
    #   Never-re-queries invariant + badge 6:      y=5.05-5.25 (compressed —
    #     Zone 2 now ends at y=5.20, was 5.80, so this block loses ~0.55in
    #     of headroom; text kept single-line to fit.)

    # --- Personas column (unchanged from prior pass) ---
    add_persona_column(
        slide, left_in=0.30, top_in=1.50,
        width_in=1.20, height_in=5.35,
    )
    add_number_badge(slide, 8, Inches(1.15), Inches(1.42))

    # --- Zone 1: CVX Ingress ---
    add_zone(slide, "CVX Ingress",
             left_in=1.60, top_in=1.50, width_in=1.25, height_in=2.35,
             label_size=Pt(10))
    add_number_badge(slide, 1, Inches(1.50), Inches(1.42))
    add_icon_with_caption(
        slide, deck_prs, "Amazon Connect",
        center_x_in=2.225, icon_top_in=1.85, icon_size_in=0.50,
        caption_width_in=1.20, caption_size=Pt(9),
    )
    add_icon_with_caption(
        slide, deck_prs, "Amazon API Gateway",
        center_x_in=2.225, icon_top_in=3.00, icon_size_in=0.50,
        caption_width_in=1.20, caption_size=Pt(9),
    )

    # --- Zone 3: CMS Foundation ---
    add_zone(slide, "CMS Foundation",
             left_in=1.60, top_in=4.00, width_in=1.25, height_in=2.85,
             label_size=Pt(10))
    add_number_badge(slide, 5, Inches(2.72), Inches(3.90))
    add_icon_with_caption(
        slide, deck_prs, "Amazon ElastiCache",
        center_x_in=2.225, icon_top_in=4.25, icon_size_in=0.45,
        caption_width_in=1.20, caption_size=Pt(8),
        caption_override="Amazon ElastiCache (Redis LKS)",
    )
    add_icon_with_caption(
        slide, deck_prs, "Amazon DynamoDB",
        center_x_in=2.225, icon_top_in=5.15, icon_size_in=0.45,
        caption_width_in=1.20, caption_size=Pt(8),
    )
    add_icon_with_caption(
        slide, deck_prs, "AWS Lambda",
        center_x_in=2.225, icon_top_in=6.05, icon_size_in=0.40,
        caption_width_in=1.20, caption_size=Pt(8),
        caption_override="AWS Lambda (Recalls)",
    )

    # --- Zone 2: AgentCore Runtime & Memory (WIDER: 2.95-8.30, w=5.35;
    #     SHORTER: 1.50-5.20, h=3.70 — see restructure note above) ---
    add_zone(slide, "AgentCore Runtime & Memory",
             left_in=2.95, top_in=1.50, width_in=5.35, height_in=3.70)
    add_number_badge(slide, 2, Inches(2.85), Inches(1.40))

    # Sub-box A: Bidirectional Voice (bidi) — AgentCore + Nova 2 Sonic
    # (unchanged geometry — extra width goes to the right of sub-box B,
    # not between A and B, so the two sub-boxes still read as a pair)
    add_zone(
        slide, "AgentCore Runtime — Bidi Voice",
        left_in=3.05, top_in=1.85, width_in=1.55, height_in=1.40,
        line_color=NEUTRAL_ZONE_LINE, label_size=Pt(7.5),
    )
    add_icon_with_caption(
        slide, deck_prs, "Amazon Bedrock AgentCore",
        center_x_in=3.40, icon_top_in=2.15, icon_size_in=0.40,
        caption_width_in=0.75, caption_size=Pt(6.5),
        caption_override="AgentCore",
    )
    # Nova 2 Sonic — uses the Amazon Nova family icon (see load_icon_png).
    add_icon_with_caption(
        slide, deck_prs, "Amazon Nova 2 Sonic",
        center_x_in=4.25, icon_top_in=2.15, icon_size_in=0.40,
        caption_width_in=0.85, caption_size=Pt(6.5),
        caption_override="Nova 2 Sonic",
    )
    # Transport / model-class caption at bottom of sub-box A
    _add_small_label(
        slide, "WebSocket • BidiNovaSonicModel",
        left_in=3.08, top_in=2.98, width_in=1.50, height_in=0.20,
        size=Pt(6.5), italic=True, align_center=True,
    )

    # Sub-box B: Text / HTTP — AgentCore only
    add_zone(
        slide, "AgentCore Runtime — Text / HTTP",
        left_in=4.70, top_in=1.85, width_in=1.50, height_in=1.40,
        line_color=NEUTRAL_ZONE_LINE, label_size=Pt(7.5),
    )
    add_icon_with_caption(
        slide, deck_prs, "Amazon Bedrock AgentCore",
        center_x_in=5.45, icon_top_in=2.15, icon_size_in=0.40,
        caption_width_in=1.20, caption_size=Pt(7),
        caption_override="AgentCore",
    )
    _add_small_label(
        slide, "HTTP unary req / resp • BedrockModel",
        left_in=4.72, top_in=2.98, width_in=1.46, height_in=0.20,
        size=Pt(6.5), italic=True, align_center=True,
    )

    # Shared caption below both sub-boxes (unchanged position — the
    # extra Zone 2 width is to the RIGHT of this caption's 3.15in span,
    # so it stays where it was, just with more empty zone whitespace
    # to its right now)
    _add_small_label(
        slide,
        "Same supervisor code + tool catalog; differ in transport + model class.",
        left_in=3.05, top_in=3.28, width_in=3.15, height_in=0.22,
        size=Pt(7.5), italic=True, align_center=True,
    )

    # AgentCore Memory — shared by both sub-boxes, centered below them
    add_icon_with_caption(
        slide, deck_prs, "Amazon Bedrock AgentCore",
        center_x_in=4.60, icon_top_in=3.62, icon_size_in=0.50,
        caption_width_in=1.60, caption_size=Pt(9),
        caption_override="AgentCore Memory",
    )
    add_number_badge(slide, 3, Inches(5.55), Inches(4.15))

    # Session-init / per-turn caption block — placed in the whitespace
    # ABOVE the A6 arrow's diagonal path, not beside it. A6 runs from
    # (6.20, 2.55) to (7.35, 5.85), slope ≈2.87; at y=3.55 the arrow's
    # x is already 6.55, so a caption box spanning x=6.20-8.15 (as
    # originally placed) put its right two-thirds directly in the
    # arrow's path — this was the root cause of the overlap seen in
    # visual inspection, not a collision with badge 3 as first assumed.
    # Fix: narrow the box to end at x=6.50 (clear of the line for the
    # whole y=3.55-4.85 range, since the line's minimum x in that band
    # is 6.55) and raise it to sit entirely above y=3.30 where the
    # shared "Same supervisor code..." caption ends, using the vertical
    # space Sub-box B's right side + the narrow gap above the arrow's
    # start point (6.20, 2.55) — reflowed as two shorter lines at
    # narrower width instead of one wide block.
    session_caption = slide.shapes.add_textbox(
        Inches(6.35), Inches(1.85), Inches(1.55), Inches(1.35),
    )
    tf = session_caption.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(0.03)
    tf.margin_top = Inches(0.03)
    tf.margin_right = Inches(0.03)
    tf.margin_bottom = Inches(0.03)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    set_para_text(
        p,
        "Session init: one-time Athena→Memory seed (~6-7s).",
        size=Pt(8),
        bold=True,
    )
    new_paragraph(
        tf,
        "Per turn: Memory / KB reads only (sub-100ms).",
        size=Pt(8),
        bold=True,
    )

    # Never-re-queries invariant + badge 6 — placed below Memory,
    # compressed to fit above Zone 2's new bottom edge at y=5.20.
    invariant_text = slide.shapes.add_textbox(
        Inches(3.35), Inches(4.55), Inches(4.70), Inches(0.45),
    )
    tf = invariant_text.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(0.03)
    tf.margin_top = Inches(0.02)
    tf.margin_right = Inches(0.03)
    tf.margin_bottom = Inches(0.02)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    set_para_text(
        p,
        "Never re-queries Athena within a session — cost/latency invariant.",
        size=Pt(7.5),
        bold=False,
    )
    add_number_badge(slide, 6, Inches(3.03), Inches(4.58))

    # --- Zone 6: External Partners & Booking Sinks — RESTRUCTURED as a
    #     far-right VERTICAL COLUMN mirroring the Personas column
    #     construction (outer neutral-gray container spanning the full
    #     zone height, badge at top, content stacked vertically inside).
    #     Previously a horizontal strip under AgentCore Runtime; moved
    #     per user request 2026-07-16. ---
    add_zone(
        slide, "External Partners — outside AWS",
        left_in=8.40, top_in=1.50, width_in=1.25, height_in=5.35,
        line_color=NEUTRAL_ZONE_LINE,
        dash_style=MSO_LINE_DASH_STYLE.DASH,
        label_size=Pt(8),
    )
    # Badge 7 at top-right, mirroring badge 8's top-right placement in
    # the Personas column on the opposite side of the canvas.
    add_number_badge(slide, 7, Inches(9.35), Inches(1.42))

    add_icon_with_caption(
        slide, deck_prs, "Amazon API Gateway",
        center_x_in=9.025, icon_top_in=1.95, icon_size_in=0.38,
        caption_width_in=1.10, caption_size=Pt(7.5),
        caption_override="Webhook / API",
    )
    # Ticket-sink text block, stacked vertically below the icon (column
    # layout — narrow width forces short line-wraps, same density
    # pattern as the Personas column's persona-name stacks).
    _add_small_label(
        slide, "Ticket sink:",
        left_in=8.46, top_in=2.55, width_in=1.13, height_in=0.16,
        size=Pt(7.5), bold=True,
    )
    _add_small_label(
        slide, "SQS / Salesforce / ServiceNow",
        left_in=8.46, top_in=2.72, width_in=1.13, height_in=0.42,
        size=Pt(7),
    )
    _add_small_label(
        slide, "Dealer DMS / OEM finance:",
        left_in=8.46, top_in=3.30, width_in=1.13, height_in=0.30,
        size=Pt(7.5), bold=True,
    )
    _add_small_label(
        slide, "Reynolds, CDK Global, Tekion, Stripe/Plaid",
        left_in=8.46, top_in=3.62, width_in=1.13, height_in=0.55,
        size=Pt(7),
    )
    _add_small_label(
        slide, "(illustrative, not direct)",
        left_in=8.46, top_in=4.22, width_in=1.13, height_in=0.30,
        size=Pt(6.5), italic=True,
    )

    # --- Extended partner content (2026-07-16) ---
    # Sourced from the-customer-journey.adoc Service stage (v1/v2/v3
    # phased DMS booking table) and Own stage (proactive-engagement
    # partner integrations). Both extend the existing Dealer DMS / OEM
    # finance list rather than duplicate it — Dealertrack, Fleetio,
    # Whip Around are the v3-roadmap DMS names not yet on the diagram;
    # v3 direct-DMS-write is explicitly roadmap in the IG's phased
    # table, so this line carries the same "illustrative, not direct"
    # framing as the existing Reynolds/CDK Global/Tekion/Stripe-Plaid
    # line above it, not a new maturity tier.
    _add_small_label(
        slide, "+ v3 DMS (roadmap):",
        left_in=8.46, top_in=4.58, width_in=1.13, height_in=0.16,
        size=Pt(7.5), bold=True,
    )
    _add_small_label(
        slide, "Dealertrack, Fleetio, Whip Around",
        left_in=8.46, top_in=4.75, width_in=1.13, height_in=0.42,
        size=Pt(7),
    )

    # Own stage (proactive engagement): subscription activation +
    # CRM-routed outbound contact. The IG marks both partners
    # illustrative — "the specific partner webhook contracts remain
    # tenant-dependent" — same illustrative framing as above, just a
    # different functional pipeline (Flink→MSK→Lambda→Connect, which
    # IS functional; only the named partners are illustrative).
    _add_small_label(
        slide, "Subscription / CRM:",
        left_in=8.46, top_in=5.25, width_in=1.13, height_in=0.16,
        size=Pt(7.5), bold=True,
    )
    _add_small_label(
        slide, "Zuora, Salesforce Service Cloud",
        left_in=8.46, top_in=5.42, width_in=1.13, height_in=0.42,
        size=Pt(7),
    )
    _add_small_label(
        slide, "(illustrative, not direct)",
        left_in=8.46, top_in=5.86, width_in=1.13, height_in=0.20,
        size=Pt(6.5), italic=True,
    )

    # Maturity-convention pivot: everything above this point is either
    # functional-and-demonstrated or the IG's "illustrative, not
    # direct" framing for a functional pipeline with a swappable named
    # partner. Amazon Autos below is different in kind — the IG states
    # explicitly "This remains a forward-looking concept and is not
    # part of CVX v1. No CVX v1 code path exists" for the Sell/Trade-in
    # stage. Uses the established amber-dashed roadmap sub-box pattern
    # (same convention as the Personas column's own roadmap subgroup)
    # to visually separate it from the functional/illustrative content
    # above, rather than blending it into the same list.
    # Sized from actual wrap behavior, not estimated: at 6.5pt in a
    # 1.09in-wide column, "Not yet demonstrated in a reference
    # implementation::" (52 chars) wraps to 3 lines (~0.39in at 6.5pt
    # line-height ≈0.13in/line), and the Amazon Autos description
    # (shortened below to fit) wraps to 2 lines (~0.26in). Outer box
    # grown to 0.68in to hold both without overflow, with its bottom
    # edge at 6.14+0.68=6.82 — 0.03in inside Zone 6's actual bottom
    # edge at 6.85 (found via direct visual inspection: the previous
    # 0.62in box let text overflow past its own border because both
    # labels' height_in values were smaller than their real wrapped
    # height, not because the box itself was mispositioned).
    roadmap_partner_box = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Inches(8.44), Inches(6.14), Inches(1.17), Inches(0.68),
    )
    roadmap_partner_box.fill.background()
    roadmap_partner_box.line.color.rgb = PERSONA_ROADMAP_COLOR
    roadmap_partner_box.line.width = STANDARD_LINE_WIDTH
    roadmap_partner_box.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    roadmap_partner_box.text_frame.text = ""
    _add_small_label(
        slide, "Not yet demonstrated in a reference implementation::",
        left_in=8.49, top_in=6.17, width_in=1.09, height_in=0.40,
        size=Pt(6.5), bold=True, color=PERSONA_ROADMAP_COLOR,
    )
    # Shortened from "Amazon Autos (marketplace handoff, Sell/Trade-in)"
    # (50 chars, wraps to 3 lines) to fit in the remaining 0.28in of
    # box height alongside the label above. Marketplace-handoff /
    # Sell-Trade-in context is already carried in the right panel's
    # paragraph 7 in full; this column only needs the partner name.
    _add_small_label(
        slide, "Amazon Autos (marketplace)",
        left_in=8.49, top_in=6.58, width_in=1.09, height_in=0.22,
        size=Pt(6.5), color=PERSONA_ROADMAP_COLOR,
    )

    # Right-arrow connector from Zone 2 (Runtime, right edge) → Zone 6
    # (External Partners column, left edge). Horizontal now, not
    # vertical, since Zone 6 moved from below to beside Zone 2.
    partner_arrow = slide.shapes.add_shape(
        MSO_SHAPE.RIGHT_ARROW,
        Inches(8.31), Inches(2.75), Inches(0.08), Inches(0.14),
    )
    partner_arrow.fill.solid()
    partner_arrow.fill.fore_color.rgb = ARROW_COLOR
    partner_arrow.line.fill.background()

    # --- Zone 4: Cross-account boundary (dashed pink) — RESTRUCTURED
    #     as a BOTTOM BAND spanning horizontally under AgentCore
    #     Runtime (same x-span as Zone 2: 2.95-8.30), rather than a
    #     right-side column. Moved per user request 2026-07-16. ---
    add_zone(
        slide,
        "[cross-account boundary] — ADP Account",
        left_in=2.95, top_in=5.30, width_in=5.35, height_in=1.55,
        line_color=ACCOUNT_BOUNDARY_COLOR,
        dash_style=MSO_LINE_DASH_STYLE.DASH,
        label_size=Pt(10),
    )
    add_number_badge(slide, 4, Inches(2.85), Inches(5.20),
                     fill_color=ACCOUNT_BOUNDARY_COLOR)

    # --- Zone 5: ADP Data Zone (nested inside Zone 4, now a single
    #     horizontal row of 5 icons instead of 3 stacked rows, since
    #     the band is wide (5.15in) but short (1.20in)). ---
    add_zone(
        slide, "ADP Data Zone",
        left_in=3.05, top_in=5.55, width_in=5.15, height_in=1.20,
        line_color=NEUTRAL_ZONE_LINE,
        label_size=Pt(9),
    )
    # Single row, 5 icons evenly spaced across x=3.55-7.65 (5 slots at
    # 0.90in spacing, matching the 0.38in icon size used in the Zone-6
    # column so icon sizing reads consistently across the whole diagram
    # at this zoom level).
    adp_row_y = 5.85
    adp_centers = [3.75, 4.65, 5.55, 6.45, 7.35]
    add_icon_with_caption(
        slide, deck_prs, "AWS Lake Formation",
        center_x_in=adp_centers[0], icon_top_in=adp_row_y, icon_size_in=0.38,
        caption_width_in=0.85, caption_size=Pt(6.5),
    )
    add_icon_with_caption(
        slide, deck_prs, "Amazon Athena",
        center_x_in=adp_centers[1], icon_top_in=adp_row_y, icon_size_in=0.38,
        caption_width_in=0.85, caption_size=Pt(6.5),
    )
    add_icon_with_caption(
        slide, deck_prs, "AWS Glue",
        center_x_in=adp_centers[2], icon_top_in=adp_row_y, icon_size_in=0.38,
        caption_width_in=0.85, caption_size=Pt(6.5),
    )
    add_icon_with_caption(
        slide, deck_prs, "Amazon Simple Storage Service",
        center_x_in=adp_centers[3], icon_top_in=adp_row_y, icon_size_in=0.38,
        caption_width_in=0.85, caption_size=Pt(6.5),
        caption_override="Amazon S3",
    )
    add_icon_with_caption(
        slide, deck_prs, "Amazon Bedrock Knowledge Base",
        center_x_in=adp_centers[4], icon_top_in=adp_row_y, icon_size_in=0.38,
        caption_width_in=0.90, caption_size=Pt(6.5),
    )

    # ============= ARROWS (additions §3, adapted for -1a restructure) =========
    # Arrows sit ON TOP of the shapes drawn above. Two visual styles:
    #   solid  = session-init flows (one-time, expensive)
    #   dashed = per-turn flows (repeated, cheap, in-region)
    # Colors: deep navy for all arrows regardless of style; the dash
    # pattern alone conveys the init-vs-per-turn distinction.
    #
    # We intentionally do NOT draw per-sub-box routing arrows (Connect →
    # Sub-box A, API Gateway → Sub-box B) or sub-box → Memory arrows.
    # The sub-box titles and shared caption ("Same supervisor code +
    # tool catalog...") plus paragraph 8 convey the routing story more
    # cleanly than a web of arrows crossing through the sub-boxes.

    # A1. Personas → Zone 1 CVX Ingress (short-gap right-arrow shape).
    #     Anchor mid-height between Connect (voice) and API Gateway (text)
    #     rows so a single arrow reads as "personas → ingress" rather than
    #     six-per-persona routing.
    add_short_gap_arrow(slide, left_in=1.51, top_in=2.63, width_in=0.13, height_in=0.14)

    # A2. Zone 1 CVX Ingress → Zone 2 AgentCore Runtime (short-gap
    #     right-arrow at same mid-height as A1). One arrow captures the
    #     'ingress feeds runtime' relationship; the bidi-vs-text routing
    #     distinction lives in the sub-box titles and paragraph 8.
    add_short_gap_arrow(slide, left_in=2.86, top_in=2.63, width_in=0.13, height_in=0.14)

    # A5. AgentCore Memory → Athena (session-init hop, solid, long).
    #     RETARGETED for -1a: Athena moved from the right-side ADP zone
    #     (was center_x=8.80, y=2.40-2.85) to the bottom band's icon row
    #     (now center_x=4.65, icon_top=5.85, size=0.38 → top-center at
    #     x=4.65, y=5.85). The arrow now travels DOWNWARD from Memory
    #     into the bottom band, instead of rightward into a side column.
    #     ORIGIN: bottom-center of AgentCore Memory icon (Memory at
    #     center_x=4.60, top=3.62, size=0.50 → bottom-center at
    #     x=4.60, y=4.12).
    #     TARGET: Athena top-center in the bottom band (center_x=4.65,
    #     icon_top=5.85 → x=4.65, y=5.85).
    #     Trajectory: near-vertical (slope 0.05/1.73 ≈ 0.03, essentially
    #     straight down with a 0.05in rightward drift). Passes through
    #     the "Never re-queries..." invariant text band (y=4.55-5.00,
    #     x=3.35-8.05) and the Zone 2 bottom border (y=5.20) before
    #     entering Zone 4's title band (y=5.20-5.45) and reaching Zone
    #     5's icon row. Crosses the dashed pink Zone 4 top border once
    #     at y=5.30, which visually reinforces the cross-account nature
    #     of this specific hop — same semantic as the previous layout's
    #     single boundary-crossing.
    add_arrow(slide, x1_in=4.60, y1_in=4.12, x2_in=4.65, y2_in=5.85)
    # Label positioned to the right of the arrow's midpoint, in the
    # whitespace of the invariant-text band (y=4.55-5.00 has text only
    # out to x=8.05, but this label sits below that text's own line at
    # y=4.70, clear of both the invariant text above and Zone 4's title
    # below at y=5.30).
    _add_small_label(
        slide, "session init only, ~6-7s",
        left_in=4.85, top_in=4.85, width_in=1.65, height_in=0.16,
        size=Pt(7), italic=True, color=ARROW_COLOR, align_center=True,
    )

    # A6. AgentCore Runtime → Bedrock KB (per-turn, dashed, sub-100ms).
    #     RETARGETED for -1a: KB moved from the right-side ADP zone (was
    #     center_x=8.00, icon_top=5.05) to the bottom band's icon row,
    #     rightmost position (now center_x=7.35, icon_top=5.85).
    #     ORIGIN: right edge of Sub-box B (Text/HTTP), since this is the
    #     runtime-to-KB per-turn read path (Sub-box B right edge x=6.20,
    #     mid-height y=2.55).
    #     TARGET: KB top-center in the bottom band (center_x=7.35,
    #     icon_top=5.85 → x=7.35, y=5.85).
    add_arrow(
        slide, x1_in=6.20, y1_in=2.55, x2_in=7.35, y2_in=5.78,
        dashed=True,
    )
    # Label placement: the line's equation is x = 6.20 + (y-2.55)/2.874.
    # At y=3.35 (a safe height inside Zone 2's whitespace, clear of both
    # Sub-box B above and the session-init caption box to the right,
    # which now sits at x=6.35-7.90/y=1.85-3.20), the line's x is 6.48.
    # Placing the label's LEFT edge at 6.55 (0.07in right of the line at
    # that height) keeps it beside the line rather than straddling it.
    _add_small_label(
        slide, "per-turn",
        left_in=6.55, top_in=3.30, width_in=1.10, height_in=0.16,
        size=Pt(7), italic=True, color=ARROW_COLOR, align_center=True,
    )
    _add_small_label(
        slide, "reads only",
        left_in=6.55, top_in=3.46, width_in=1.10, height_in=0.16,
        size=Pt(7), italic=True, color=ARROW_COLOR, align_center=True,
    )

    # A7. AgentCore Runtime → CMS Redis LKS (session-init read).
    #     Unaffected by the -1a restructure — CMS Foundation (Zone 3)
    #     and AgentCore Runtime (Zone 2) did not move relative to each
    #     other; only Zone 2's RIGHT edge moved (6.25→8.30). Geometry
    #     unchanged from the prior pass.
    #
    #     Label clarified 2026-07-16 to state the Lambda-fronting fact
    #     ("All CMS reads use Lambda-backed APIs" per
    #     building-on-cms-and-adp.adoc) directly in the arrow label,
    #     rather than via a 4th icon. A dedicated icon + fan-out arrow
    #     was tried first and produced real overlap defects (Lambda
    #     icon colliding with the "CMS Foundation" zone label, its
    #     caption running into the ElastiCache icon, badge 5 colliding
    #     with the new "init: LKS" label) — found via direct visual
    #     inspection. Per the debug-discipline "guard-stacking is a
    #     smell" rule, the fix is to step back to a simpler
    #     representation (a label-only correction) rather than keep
    #     nudging coordinates around an already-crowded 1.25in-wide
    #     zone. The mechanism is already stated in full in the right
    #     panel's paragraph 5 ("All CMS reads use Lambda-backed
    #     APIs"); the diagram's job is a lightweight correction, not a
    #     redraw.
    add_short_gap_arrow(slide, left_in=2.86, top_in=4.42, width_in=0.13, height_in=0.14)
    _add_small_label(
        slide, "init: LKS (Lambda)", left_in=2.83, top_in=4.20,
        width_in=0.85, height_in=0.18, size=Pt(6.5), italic=True,
        color=ARROW_COLOR,
    )

    # A8. AgentCore Runtime → CMS DynamoDB / Recalls (per-turn, dashed).
    #     Geometry reverted to the y=5.10 fix from the prior pass
    #     (aligned with DynamoDB's icon-row top, clear of Zone 4's
    #     y=5.30 top edge). Label clarified with the same "(Lambda)"
    #     suffix as A7, same reasoning.
    add_arrow(
        slide, x1_in=2.85, y1_in=5.10, x2_in=3.00, y2_in=5.10,
        dashed=True,
    )
    _add_small_label(
        slide, "per-turn: DDB, Recalls (Lambda)",
        left_in=3.05, top_in=5.02, width_in=1.55, height_in=0.28,
        size=Pt(6.5), italic=True, color=ARROW_COLOR,
    )


def main() -> int:
    if not TEMPLATE_PATH.exists():
        print(f"ERROR: template not found at {TEMPLATE_PATH}", file=sys.stderr)
        return 2
    if not ICON_DECK_PATH.exists():
        print(f"ERROR: icon deck not found at {ICON_DECK_PATH}", file=sys.stderr)
        return 2

    # Verify rsvg-convert is on PATH.
    try:
        subprocess.run(
            ["rsvg-convert", "--version"], check=True, capture_output=True,
        )
    except FileNotFoundError:
        print("ERROR: rsvg-convert not found on PATH. Install via 'brew install librsvg'.",
              file=sys.stderr)
        return 3

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"Loading template: {TEMPLATE_PATH}")
    prs = Presentation(str(TEMPLATE_PATH))
    print(f"Loading icon deck: {ICON_DECK_PATH}")
    deck_prs = Presentation(str(ICON_DECK_PATH))

    print("Stripping template slides")
    strip_all_slides(prs)

    print("Building CVX house-style slide")
    build_slide(prs, deck_prs)

    print(f"Saving: {OUTPUT_PATH}")
    prs.save(str(OUTPUT_PATH))
    print(f"Done. {len(prs.slides)} slide(s) written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
