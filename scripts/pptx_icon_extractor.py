"""
pptx_icon_extractor.py — Caption-proximity icon extraction from the AWS
Architecture Icons Deck (Light-BG PPTX, 04-28-2026 release).

The AWS Architecture Icons Deck is an icon reference: each slide is a grid of
Picture shapes with generic names ("Graphic 7", "Graphic 52", ...) sitting
directly above a sibling text box that holds the *actual* service name
("Amazon DynamoDB", "AWS Lambda", ...). There is no service-name attribute on
the Picture shape itself — you must locate the caption text box and infer the
icon by vertical proximity + horizontal alignment.

This module implements that lookup, plus a CLI test harness that verifies
matches exist for the services the CVX diagrams (diagrams 1-3) need.

Usage:
    python3 pptx_icon_extractor.py [PATH_TO_PPTX]

If PATH_TO_PPTX is omitted, defaults to the Light-BG deck in the operator's
~/Downloads/ folder from the 04-30-2026 toolkit drop.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE


# EMU tolerance for "caption is below the picture" (caption top can be
# slightly above pic bottom due to rendering fudge — allow a tiny slack).
_CAPTION_BELOW_TOLERANCE_EMU = 100_000

# The default deck the CVX slide-assembly script will consume.
_DEFAULT_DECK = Path(
    "~/Downloads/Microsoft-PPTx-toolkits_04302026."
    "1ee04f761e1661409056abeb3ca627bb5e336688/"
    "AWS-Architecture-Icons-Deck_For-Light-BG_04282026.pptx"
).expanduser()


IconMatch = Tuple[int, "object", str, int, int, int, int]
# (slide_index, picture_shape, matched_caption_text,
#  picture_left, picture_top, picture_width, picture_height)


def _normalize(text: str) -> str:
    """Collapse whitespace and strip. PPTX runs sometimes contain vertical
    tabs (\\u000B / \\v) as intra-paragraph line breaks — flatten those too."""
    return re.sub(r"\s+", " ", text.replace("\u000B", " ").replace("\v", " ")).strip()


def _horiz_overlap(pic, txt) -> bool:
    """True if picture and caption are roughly horizontally aligned:
    either the picture's horizontal center falls inside the caption's
    horizontal range, or vice versa. This handles both the common case
    (caption wider than icon) and the rare inverse.
    """
    pic_cx = pic.left + pic.width / 2
    txt_cx = txt.left + txt.width / 2
    txt_left, txt_right = txt.left, txt.left + txt.width
    pic_left, pic_right = pic.left, pic.left + pic.width
    return (
        txt_left <= pic_cx <= txt_right
        or pic_left <= txt_cx <= pic_right
    )


def find_icon_by_caption(
    prs: Presentation,
    caption_text: str,
    fuzzy: bool = True,
) -> List[IconMatch]:
    """Scan every slide for text boxes whose text matches ``caption_text``
    (case-insensitive substring when ``fuzzy=True``, exact-normalized match
    otherwise), and for each caption find the nearest Picture shape
    positioned directly ABOVE it (smallest vertical gap, horizontally
    overlapping, within 2× the picture's own height).

    Returns a list of tuples. Multiple pictures with similar-sounding
    captions (e.g. "Amazon DynamoDB" vs "Amazon DynamoDB Accelerator
    (DAX)") produce multiple entries — the caller disambiguates by
    picking the shortest / most-exact caption.
    """
    target = _normalize(caption_text).lower()
    if not target:
        return []

    matches: List[IconMatch] = []

    for slide_idx, slide in enumerate(prs.slides):
        pictures = []
        text_boxes = []
        for shp in slide.shapes:
            # Skip anything with un-set geometry (grouped/off-slide edge cases).
            if shp.left is None or shp.top is None:
                continue
            if shp.width is None or shp.height is None:
                continue
            if shp.shape_type == MSO_SHAPE_TYPE.PICTURE:
                pictures.append(shp)
            elif shp.has_text_frame:
                text_boxes.append(shp)

        for txt in text_boxes:
            raw = txt.text_frame.text
            norm = _normalize(raw)
            if not norm:
                continue
            lo = norm.lower()
            if fuzzy:
                if target not in lo:
                    continue
            else:
                if lo != target:
                    continue

            # Find the picture with the smallest vertical gap above.
            best_pic = None
            best_gap = None
            for pic in pictures:
                pic_bottom = pic.top + pic.height
                # Caption must be below the picture (with a small slack).
                gap = txt.top - pic_bottom
                if gap < -_CAPTION_BELOW_TOLERANCE_EMU:
                    continue
                # Cap gap distance: no runaway matches across the slide.
                if gap > 2 * pic.height:
                    continue
                if not _horiz_overlap(pic, txt):
                    continue
                # Clamp negative gap to 0 for comparison purposes.
                effective = max(gap, 0)
                if best_gap is None or effective < best_gap:
                    best_gap = effective
                    best_pic = pic

            if best_pic is not None:
                matches.append(
                    (
                        slide_idx,
                        best_pic,
                        norm,
                        best_pic.left,
                        best_pic.top,
                        best_pic.width,
                        best_pic.height,
                    )
                )

    return matches


def extract_icon_image(picture_shape) -> bytes:
    """Return the raw image blob for a Picture shape.

    Handles two cases:
      1. Standard raster embed (a:blip r:embed="rIdN") — uses python-pptx's
         built-in ``shape.image.blob``.
      2. SVG-only embed (asvg:svgBlip r:embed="rIdN" inside a:blip/a:extLst)
         — walks the XML to find the SVG relationship and returns the SVG
         bytes directly from the slide part.

    AWS Architecture Icons (2026 release) use case 2: icons are SVG with
    no raster fallback. Callers who need a raster (e.g. to embed in a
    PPTX picture placeholder that can't render SVG on all viewers) must
    rasterize the SVG bytes themselves.

    Returns:
        bytes: the raw image blob. Check ``shape.image.content_type`` for
        raster mimetype; for SVG fallback, mimetype is
        ``image/svg+xml``.
    """
    # Case 1: raster embed present.
    try:
        return picture_shape.image.blob
    except (ValueError, AttributeError):
        pass

    # Case 2: SVG-only via asvg:svgBlip.
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    svg_ns = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"

    svg_blips = picture_shape._element.findall(
        f".//{{{a_ns}}}blip//{{{svg_ns}}}svgBlip"
    )
    if not svg_blips:
        raise ValueError(
            "Picture has neither a raster embed nor an SVG blip extension"
        )
    rid = svg_blips[0].get(f"{{{r_ns}}}embed")
    if not rid:
        raise ValueError("svgBlip element has no r:embed attribute")

    slide_part = picture_shape.part
    rel = slide_part.rels[rid]
    return rel.target_part.blob


def extract_icon_content_type(picture_shape) -> str:
    """Return the MIME content type for the image blob that
    ``extract_icon_image`` will return. Useful when the caller needs to
    dispatch on format (image/png, image/svg+xml, etc.).
    """
    try:
        return picture_shape.image.content_type
    except (ValueError, AttributeError):
        pass

    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    svg_ns = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"

    svg_blips = picture_shape._element.findall(
        f".//{{{a_ns}}}blip//{{{svg_ns}}}svgBlip"
    )
    if not svg_blips:
        raise ValueError(
            "Picture has neither a raster embed nor an SVG blip extension"
        )
    rid = svg_blips[0].get(f"{{{r_ns}}}embed")
    slide_part = picture_shape.part
    return slide_part.rels[rid].target_part.content_type


# ---------------------------------------------------------------------------
# CLI test harness
# ---------------------------------------------------------------------------


# Services the CVX diagrams 1-3 need. The `note` column records any
# expectations from the caller — e.g. a service that may not exist in this
# release. We report the actual finding; we do NOT force a match.
_NEEDED_SERVICES = [
    ("Amazon Connect", ""),
    ("Amazon API Gateway", ""),
    ("AWS Lambda", ""),
    ("Amazon Bedrock AgentCore", ""),
    ("Amazon DynamoDB", "expect DAX/streams as sibling matches — pick shortest"),
    ("Amazon ElastiCache", "may show as 'Amazon ElastiCache' or with a modifier"),
    (
        "Amazon Bedrock Knowledge Bases",
        "may not exist in 04-28-2026 release — report honestly if absent",
    ),
    ("AWS Lake Formation", "under Analytics"),
    ("Amazon Athena", ""),
    ("AWS Glue", ""),
    ("Amazon S3", ""),
    ("Amazon Cognito", ""),
]


def _disambiguate(matches: List[IconMatch], target: str) -> dict:
    """Given all fuzzy matches for a target, decide honestly:

    - Exact caption match on ANY slide → confident (pick first).
      Duplicate hits of the same caption text on multiple slides collapse
      to "found" — pick the first slide.
    - No exact, but ALL fuzzy hits share ONE distinct caption text →
      confident single fuzzy caption.
    - No exact, multiple distinct fuzzy captions → AMBIGUOUS. Never
      auto-pick a "shortest" fuzzy in this case; the shortest may be a
      wholly different service (e.g. "Amazon S3 Glacier" vs a primary
      "Amazon S3" that the deck labels with its full name "Amazon Simple
      Storage Service (Amazon S3)"). Report all candidates and let the
      caller re-query with a more specific caption.

    Returns:
        {
          "status": "found" | "not_found" | "ambiguous",
          "chosen": IconMatch or None,
          "candidates": [ (slide_1idx, caption, w, h), ... ],
          "reason": str,
        }
    """
    target_lo = target.lower().strip()
    all_candidates = sorted(
        [(m[0] + 1, m[2], m[5], m[6]) for m in matches],
        key=lambda c: (len(c[1]), c[0]),
    )

    if not matches:
        return {
            "status": "not_found",
            "chosen": None,
            "candidates": [],
            "reason": (
                f"no caption on any slide contains '{target}' (case-insensitive)"
            ),
        }

    exact = [m for m in matches if m[2].lower() == target_lo]
    if exact:
        return {
            "status": "found",
            "chosen": exact[0],
            "candidates": all_candidates,
            "reason": "exact caption match",
        }

    distinct_captions = {m[2] for m in matches}
    if len(distinct_captions) == 1:
        return {
            "status": "found",
            "chosen": matches[0],
            "candidates": all_candidates,
            "reason": (
                f"single fuzzy caption '{matches[0][2]}' "
                f"(no exact but only one distinct caption in fuzzy hits)"
            ),
        }

    return {
        "status": "ambiguous",
        "chosen": None,
        "candidates": all_candidates,
        "reason": (
            f"no exact caption match; {len(distinct_captions)} distinct fuzzy "
            "captions — caller must re-query with a more specific caption"
        ),
    }


def _report(service: str, note: str, resolution: dict) -> None:
    print(f"\n### {service}")
    if note:
        print(f"    note: {note}")
    status = resolution["status"]
    if status == "not_found":
        print(f"    [NOT FOUND] {resolution['reason']}")
        return
    if status == "ambiguous":
        print(f"    [AMBIGUOUS] {resolution['reason']}")
        print(f"    candidates ({len(resolution['candidates'])}):")
        for slide_1, cap, w, h in resolution["candidates"]:
            print(f"      slide {slide_1:>3}: '{cap}'  (pic {w}x{h} EMU)")
        return
    # found
    chosen = resolution["chosen"]
    slide_idx, _, cap, left, top, w, h = chosen
    print(f"    [OK] slide {slide_idx + 1} (idx {slide_idx})  '{cap}'")
    print(f"         picture: left={left} top={top} w={w} h={h} EMU  "
          f"({w // 9525}x{h // 9525} px @ 96dpi)")
    print(f"         disambiguation: {resolution['reason']}")
    others = [c for c in resolution["candidates"]
              if not (c[0] == slide_idx + 1 and c[1] == cap)]
    if others:
        print(f"    other fuzzy hits ({len(others)}):")
        for slide_1, other_cap, ow, oh in others[:6]:
            print(f"      slide {slide_1:>3}: '{other_cap}'  ({ow}x{oh})")
        if len(others) > 6:
            print(f"      ... and {len(others) - 6} more")


def main(argv: List[str]) -> int:
    deck_path = Path(argv[1]).expanduser() if len(argv) > 1 else _DEFAULT_DECK
    if not deck_path.exists():
        print(f"ERROR: deck not found at {deck_path}", file=sys.stderr)
        return 2

    print(f"Loading deck: {deck_path}")
    prs = Presentation(str(deck_path))
    print(f"  slides: {len(prs.slides)}")
    print(f"  dims:   {prs.slide_width} x {prs.slide_height} EMU")
    print()
    print("=" * 78)
    print(f"Icon-caption match report — {len(_NEEDED_SERVICES)} target services")
    print("=" * 78)

    summary = {"found": 0, "ambiguous": 0, "not_found": 0}
    for service, note in _NEEDED_SERVICES:
        matches = find_icon_by_caption(prs, service, fuzzy=True)
        resolution = _disambiguate(matches, service)
        _report(service, note, resolution)
        summary[resolution["status"]] += 1

    print()
    print("=" * 78)
    print(
        f"Summary: {summary['found']} found, "
        f"{summary['ambiguous']} ambiguous, "
        f"{summary['not_found']} not found "
        f"(of {len(_NEEDED_SERVICES)} services)"
    )
    print("=" * 78)

    return 0 if summary["not_found"] == 0 and summary["ambiguous"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
