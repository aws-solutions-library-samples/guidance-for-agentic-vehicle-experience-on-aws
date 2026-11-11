"""Service-plan validator for the FI fleet-health agent (spec D8 step 7, task 4.1).

The model's ``record_service_plan`` reply is untrusted input. Nothing the model writes
reaches a Finding until every check here passes; the checks are re-run server-side even
though the Converse tool schema carries the same enums, because a tool schema is a request
to the model, not a guarantee.

Checks (contract § 9.1):
  - the reply is an object with exactly the six tool fields, each of the right type;
  - ``dealer_id`` is one of the candidate rooftops;
  - ``urgency`` is one the band allows (``p0``: ``this_week`` only);
  - ``defer_need_ids`` are unique, name only deferrable needs in the bundle (never a
    ``p0`` or ``attention`` need) and leave at least one need on the booking;
  - ``detail`` is 1-600 characters, has no control characters, and, on a ``p0`` item,
    says nothing about driving or safety (the stop-driving directive is shown verbatim
    beside it; detail text must not compete with it);
  - ``evidence_refs`` name only evidence kinds the item carries;
  - ``confidence`` is ``high``, ``medium`` or ``low``.

Error messages name the rule, never the rejected value, so a retry prompt built from them
never echoes model output back to the model.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Optional

VALID_URGENCIES: tuple[str, ...] = ("this_week", "this_month", "next_visit")
VALID_CONFIDENCE: tuple[str, ...] = ("high", "medium", "low")
#: Needs in these bands are never deferred (spec D8 step 6).
NON_DEFERRABLE_BANDS: frozenset[str] = frozenset({"p0", "attention"})
DETAIL_MAX_CHARS: int = 600
MAX_EVIDENCE_REFS: int = 10
TOOL_FIELDS: frozenset[str] = frozenset(
    {"dealer_id", "urgency", "defer_need_ids", "detail", "evidence_refs", "confidence"}
)

#: Words a p0 item's detail may not use (defence in depth beside the system prompt; the
#: authoritative stop-driving text is ``disclosure_txt``, copied byte for byte).
_P0_FORBIDDEN_WORDS = re.compile(r"\b(driv\w*|safe\w*|safety)\b", re.IGNORECASE)


class ValidationError(Exception):
    """Raised when a model-proposed service plan breaks a rule."""


def allowed_urgencies(severity: Optional[str]) -> tuple[str, ...]:
    """Urgencies the band allows: ``p0`` → ``this_week`` only, any other band → all three.

    Matches the approve route's check (contract § 4; decisions 2026-10-05 item 10).
    """
    if severity == "p0":
        return ("this_week",)
    return VALID_URGENCIES


def band_urgency(severity: Optional[str]) -> str:
    """The rules path's urgency for a band: ``this_week`` for ``p0``, else ``this_month``."""
    return "this_week" if severity == "p0" else "this_month"


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _has_control_chars(text: str) -> bool:
    return any(
        unicodedata.category(ch) == "Cc" and ch not in ("\n", "\t") for ch in text
    )


class ServicePlanValidator:
    """Validates a model-proposed service plan for one booking item (spec D8 step 7)."""

    def __init__(
        self,
        candidate_dealer_ids: Iterable[str],
        severity: str,
        non_deferrable_need_ids: Optional[Iterable[str]] = None,
        deferrable_need_ids: Optional[Iterable[str]] = None,
    ) -> None:
        self._candidates = frozenset(candidate_dealer_ids)
        self._severity = severity
        self._non_deferrable = frozenset(non_deferrable_need_ids or ())
        # None means "any id not in the non-deferrable set may be deferred" (the 1.4 red
        # tests construct the validator without the bundle's full need list).
        self._deferrable = (
            frozenset(deferrable_need_ids) if deferrable_need_ids is not None else None
        )

    # -- the three rules spec D8 step 7 names ------------------------------------------

    def validate(self, dealer_id: Any, defer_need_ids: Any, urgency: Any) -> None:
        """Rooftop, deferral and urgency rules.  Raises :class:`ValidationError`."""
        if not isinstance(dealer_id, str) or dealer_id not in self._candidates:
            raise ValidationError("dealer_id is not one of the candidate rooftops")
        if not isinstance(urgency, str) or urgency not in allowed_urgencies(self._severity):
            raise ValidationError("urgency is not allowed for this item's band")
        if not _is_str_list(defer_need_ids):
            raise ValidationError("defer_need_ids must be a list of need ids")
        deferred = set(defer_need_ids)
        if len(deferred) != len(defer_need_ids):
            raise ValidationError("defer_need_ids repeats a need id")
        if deferred & self._non_deferrable:
            raise ValidationError("defer_need_ids names a p0 or attention need")
        if self._deferrable is not None:
            if deferred - self._deferrable:
                raise ValidationError("defer_need_ids names a need that is not in the bundle")
            all_needs = self._deferrable | self._non_deferrable
            if all_needs and deferred >= all_needs:
                raise ValidationError("a booking must keep at least one need")

    # -- the whole tool reply -----------------------------------------------------------

    def validate_tool_input(
        self, tool_input: Any, *, evidence_kinds: Iterable[str]
    ) -> dict[str, Any]:
        """Validate a whole ``record_service_plan`` reply; return a clean copy of it."""
        if not isinstance(tool_input, dict):
            raise ValidationError("the tool input must be an object")
        if set(tool_input) != TOOL_FIELDS:
            raise ValidationError(
                "the tool input must have exactly the fields "
                + ", ".join(sorted(TOOL_FIELDS))
            )
        detail = tool_input["detail"]
        if not isinstance(detail, str) or not detail.strip():
            raise ValidationError("detail must be non-empty text")
        if len(detail) > DETAIL_MAX_CHARS:
            raise ValidationError(f"detail must be at most {DETAIL_MAX_CHARS} characters")
        if _has_control_chars(detail):
            raise ValidationError("detail contains control characters")
        if self._severity == "p0" and _P0_FORBIDDEN_WORDS.search(detail):
            raise ValidationError(
                "detail on a stop-driving item must not mention driving or safety"
            )
        refs = tool_input["evidence_refs"]
        kinds = frozenset(evidence_kinds)
        if not _is_str_list(refs) or len(refs) > MAX_EVIDENCE_REFS:
            raise ValidationError("evidence_refs must be a short list of evidence kinds")
        if set(refs) - kinds:
            raise ValidationError("evidence_refs names an evidence kind the item does not carry")
        confidence = tool_input["confidence"]
        if not isinstance(confidence, str) or confidence not in VALID_CONFIDENCE:
            raise ValidationError("confidence must be high, medium or low")
        self.validate(
            dealer_id=tool_input["dealer_id"],
            defer_need_ids=tool_input["defer_need_ids"],
            urgency=tool_input["urgency"],
        )
        return {
            "dealer_id": tool_input["dealer_id"],
            "urgency": tool_input["urgency"],
            "defer_need_ids": list(tool_input["defer_need_ids"]),
            "detail": detail,
            "evidence_refs": list(refs),
            "confidence": confidence,
        }
