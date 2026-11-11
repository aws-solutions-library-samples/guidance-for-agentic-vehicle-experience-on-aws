"""
Mock-provenance guard for Tier 2 Findings — Spec 1a, inherited by Spec 1b Agent 3.

Spec reference: specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md § M14 /
parent PRD § M14.

When to fire:
    ANY evidence entry carries provenance="mocked"
    AND the Finding surfaces a monetary figure
    → watermark disclosure_txt as illustrative (default) OR raise if REFUSE mode.

"Surfaces a monetary figure" is detected two ways, both structural (no prose reading):
    1. finding_kind is in _MONETARY_FINDING_KINDS (exact frozenset; membership check only)
    2. disclosure_txt is already present and non-empty (monetary schema guard populates it)

Either condition is sufficient — the guard fires if EITHER is true when mocked provenance
is present. This covers cases where the schema guard added disclosure_txt but the kind
is novel (future proofing for Spec 2 additions).

Not coupled to Agent 2 specifics. Agent 2 has no mocked seams in production; this module
exists so Spec 1b's Agent 3 can import it without modification.

Render modes (controlled by the ``mode`` parameter to ``render_finding``):
    "watermark" (default): append an ILLUSTRATIVE_WATERMARK note to disclosure_txt
                           (or add it if absent). Does not raise.
    "refuse":              raise ProvenanceGuardError.

Public surface (pinned by agents/tier2/tests/test_mock_provenance_guard.py):

    render_finding(finding: dict, *, mode: str = "watermark") -> dict
    ProvenanceGuardError (subclass of ValueError)
    _MONETARY_FINDING_KINDS (frozenset)
    ILLUSTRATIVE_WATERMARK (str)
    _has_mocked_provenance(finding: dict) -> bool
    _is_monetary_finding(finding: dict) -> bool
"""

# ---------------------------------------------------------------------------
# Constants — the sole source of truth for this module's boundaries.
# These are exact membership sets; no substring checks, no regex, no prose.
# ---------------------------------------------------------------------------

_MONETARY_FINDING_KINDS: frozenset = frozenset({
    "coverage.warranty",
    "coverage.recall",
    "energy.cost",
    "loyalty.points",
    "loyalty.expiry",
})
"""Finding kinds that carry a monetary figure surfaced to the user.
Sourced verbatim from the schema's allOf[1].if.properties.finding_kind.enum.
Spec 1a Agent 2 emits none of these; Spec 1b Agent 3 emits coverage.* and loyalty.*.
Any finding_kind in this set MUST carry disclosure_txt per the schema's own allOf guard;
the provenance guard is a second, orthogonal layer for the mocked-provenance case."""

ILLUSTRATIVE_WATERMARK: str = (
    "Note: one or more evidence entries for this finding are from a mocked data source. "
    "Monetary figures shown are illustrative and should not be relied upon for financial decisions."
)
"""Appended to disclosure_txt (or set as disclosure_txt if absent) when the watermark
mode fires. Verbatim copy surfaced to the iOS renderer."""

_VALID_MODES: frozenset = frozenset({"watermark", "refuse"})
"""Render modes accepted by render_finding(). Exact membership; no normalisation."""


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class ProvenanceGuardError(ValueError):
    """Raised by render_finding() in refuse mode when mocked provenance is detected
    in a monetary Finding.

    Inherits from ValueError so callers that catch ValueError get this too.
    """


# ---------------------------------------------------------------------------
# Internal detection helpers — kept separate so each property is measurable
# independently (lesson: 'covered by construction' is a claim, not a measurement).
# ---------------------------------------------------------------------------


def _has_mocked_provenance(finding: dict) -> bool:
    """Return True iff any evidence entry carries provenance="mocked".

    Iterates every entry; short-circuits on first match.
    Returns False for findings with an empty evidence list.

    The check is an exact string equality on the 'provenance' field value —
    NOT a substring test, NOT a regex, NOT a case-insensitive comparison.
    A value of 'Mocked', 'MOCKED', or 'mocked ' would return False.
    """
    for entry in finding.get("evidence", []):
        if entry.get("provenance") == "mocked":
            return True
    return False


def _is_monetary_finding(finding: dict) -> bool:
    """Return True iff the Finding surfaces a monetary figure.

    Two structural signals, either sufficient:
    1. finding_kind is a member of _MONETARY_FINDING_KINDS (exact set membership).
    2. disclosure_txt is present and non-empty (the schema's allOf monetary guard
       populates it; its presence is the authoritative signal that monetary copy was
       already attached by the writer).

    No prose reading. No substring search of headline or detail fields.
    """
    kind = finding.get("finding_kind", "")
    if kind in _MONETARY_FINDING_KINDS:
        return True
    disclosure = finding.get("disclosure_txt", "")
    if isinstance(disclosure, str) and disclosure.strip():
        return True
    return False


# ---------------------------------------------------------------------------
# Public render function
# ---------------------------------------------------------------------------


def render_finding(finding: dict, *, mode: str = "watermark") -> dict:
    """Apply the mock-provenance guard and return the (possibly modified) Finding.

    This is the render-path entry point. The guard fires when BOTH:
      - at least one evidence entry has provenance="mocked"  (_has_mocked_provenance)
      - the Finding surfaces a monetary figure               (_is_monetary_finding)

    When the guard does not fire, the finding is returned unchanged.

    Parameters
    ----------
    finding:
        A Finding dict. Not validated against the full JSON Schema here —
        the caller is responsible for schema-valid input.
    mode:
        "watermark" (default): annotate disclosure_txt and return.
        "refuse": raise ProvenanceGuardError.

    Returns
    -------
    dict
        The finding, possibly with disclosure_txt added or extended (watermark mode).

    Raises
    ------
    ProvenanceGuardError
        In refuse mode when the guard condition is met.
    ValueError
        If mode is not a member of _VALID_MODES.
    """
    if mode not in _VALID_MODES:
        raise ValueError(
            f"mode {mode!r} is not valid. Must be one of: {sorted(_VALID_MODES)}"
        )

    if not (_has_mocked_provenance(finding) and _is_monetary_finding(finding)):
        # Guard does not fire — return unchanged.
        return finding

    # Guard fires.
    if mode == "refuse":
        raise ProvenanceGuardError(
            "Finding surfacing a monetary figure has mocked-provenance evidence; "
            "rendering refused. Set mode='watermark' to allow with an illustrative note."
        )

    # Watermark mode: attach or extend disclosure_txt.
    result = dict(finding)  # shallow copy — do not mutate the caller's dict
    existing = result.get("disclosure_txt", "")
    if isinstance(existing, str) and existing.strip():
        result["disclosure_txt"] = existing.rstrip() + "\n\n" + ILLUSTRATIVE_WATERMARK
    else:
        result["disclosure_txt"] = ILLUSTRATIVE_WATERMARK
    return result
