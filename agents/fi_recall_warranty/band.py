"""Band table for recall severity and items (spec D9).

The table is deterministic configuration; the model cannot change the result.
``model_hint`` is accepted for forward-compatibility but is never read.
Unknown ``recall_class`` or ``match_basis`` raise ``ValueError`` (fail closed).
"""
from __future__ import annotations

from typing import Any, NamedTuple, Optional


class BandResult(NamedTuple):
    severity: str
    items: list  # new list each call — callers may mutate freely

    @property
    def urgency(self) -> str:
        """Map severity to urgency per spec D8 rules fallback.

        p0 → this_week; attention (safety, not do_not_drive) → this_month;
        soon / routine → next_visit.
        """
        if self.severity == "p0":
            return "this_week"
        if self.severity == "attention":
            return "this_month"
        return "next_visit"


# Module-level band table: (recall_class, do_not_drive, match_basis) -> (severity, items)
# Row key: (recall_class, do_not_drive_flag, match_basis)
# do_not_drive_flag=None means "either" (wildcard when do_not_drive is True)
_DEFAULT_BAND_TABLE: tuple[tuple, ...] = (
    # Row 1: safety + do_not_drive + either basis -> p0, [ground, book]
    ("safety",     True,  None,          "p0",        ["ground", "book"]),
    # Row 2: safety + not do_not_drive + CONFIRMED -> p0, [ground, book]
    ("safety",     False, "CONFIRMED",   "p0",        ["ground", "book"]),
    # Row 3: safety + not do_not_drive + POPULATION -> attention, [book]
    ("safety",     False, "POPULATION",  "attention", ["book"]),
    # Row 4: non_safety + CONFIRMED -> soon, [book]
    ("non_safety", False, "CONFIRMED",   "soon",      ["book"]),
    # Row 5: non_safety + POPULATION -> routine, [book]
    ("non_safety", False, "POPULATION",  "routine",   ["book"]),
)

_VALID_CLASSES = frozenset({"safety", "non_safety"})
_VALID_BASES = frozenset({"CONFIRMED", "POPULATION"})


def classify(
    recall_class: str,
    do_not_drive: bool,
    match_basis: str,
    model_hint: Optional[Any] = None,  # accepted, never read
    *,
    _table: tuple[tuple, ...] = _DEFAULT_BAND_TABLE,
) -> BandResult:
    """Return the deterministic severity and items for a recall classification.

    Parameters
    ----------
    recall_class:   "safety" or "non_safety".
    do_not_drive:   True when the recall is a do-not-drive notice.
    match_basis:    "CONFIRMED" or "POPULATION".
    model_hint:     Ignored; accepted so callers can pass model output safely.

    Raises
    ------
    ValueError if recall_class or match_basis are not in the valid sets.
    """
    if recall_class not in _VALID_CLASSES:
        raise ValueError(f"Unknown recall_class {recall_class!r}; expected one of {sorted(_VALID_CLASSES)}")
    if match_basis not in _VALID_BASES:
        raise ValueError(f"Unknown match_basis {match_basis!r}; expected one of {sorted(_VALID_BASES)}")
    # G3-9: a real bool only. Athena returns strings, and "false" is truthy, so a string here
    # would ground every vehicle; the selection layer must parse it, and this refuses if not.
    if not isinstance(do_not_drive, bool):
        raise ValueError(f"do_not_drive must be a bool, got {type(do_not_drive).__name__}")

    for row_class, row_dnd, row_basis, severity, items in _table:
        # Row 1 wildcard: do_not_drive=True matches regardless of match_basis and (G3-9)
        # regardless of class: a do-not-drive notice is never classified below p0.
        if row_dnd is True and do_not_drive:
            return BandResult(severity=severity, items=list(items))
        if row_class != recall_class:
            continue
        if row_dnd is False and not do_not_drive and row_basis == match_basis:
            return BandResult(severity=severity, items=list(items))

    raise ValueError(
        f"No band row matched recall_class={recall_class!r}, "
        f"do_not_drive={do_not_drive!r}, match_basis={match_basis!r}"
    )
