"""Deterministic validator for recall and warranty claim tool outputs (spec D8, task 4.1).

Fail-closed throughout: any input not satisfying all constraints raises ValidationError.
"""
from __future__ import annotations

from typing import Any, Optional


class ValidationError(Exception):
    """Raised when model tool output violates a deterministic validation rule."""


# ---------------------------------------------------------------------------
# RecallValidator
# ---------------------------------------------------------------------------
_P0_ALLOWED_URGENCY: frozenset[str] = frozenset({"this_week"})
_SAFETY_FORBIDDEN_URGENCY: frozenset[str] = frozenset({"next_visit"})
_VALID_URGENCY: frozenset[str] = frozenset({"this_week", "this_month", "next_visit"})
_VALID_CONFIDENCE: frozenset[str] = frozenset({"high", "medium", "low"})
_VALID_PROCEED: frozenset[str] = frozenset({"file", "insufficient_evidence"})

# Keys that must never appear in either tool output (decisions.md call 48).
_RECALL_PLAN_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"dealer_id", "combine_with", "urgency", "detail", "evidence_refs", "confidence"}
)
_CLAIM_DRAFT_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"narrative", "proceed", "detail", "evidence_refs", "confidence"}
)


class RecallValidator:
    """Validate recall plan fields: dealer, combine_with, urgency (spec D8 / call 49)."""

    def __init__(
        self,
        *,
        candidate_dealer_ids: list[str],
        bundle_ids: Optional[list[str]] = None,
        allowed_severities: Optional[list[str]] = None,
        recall_class: Optional[str] = None,
    ) -> None:
        self._candidates: list[str] = list(candidate_dealer_ids)
        self._bundle: list[str] = list(bundle_ids) if bundle_ids is not None else []
        self._severities: list[str] = (
            list(allowed_severities) if allowed_severities is not None else []
        )
        # None treated as "safety" per decisions.md call 49.
        self._recall_class: str = recall_class if recall_class is not None else "safety"

    def validate(
        self,
        dealer_id: str,
        combine_with: Optional[list[str]],
        urgency: Optional[str],
    ) -> None:
        """Validate dealer_id, combine_with, and urgency.

        Raises ValidationError on any violation.  All checks are fail-closed:
        an empty candidate list rejects every dealer; an empty bundle rejects
        any non-empty combine_with.
        """
        # --- dealer check -------------------------------------------------
        if dealer_id not in self._candidates:
            raise ValidationError(
                f"dealer_id {dealer_id!r} is not in candidate list {self._candidates!r}"
            )

        # --- combine_with check -------------------------------------------
        if combine_with:
            for cid in combine_with:
                if cid not in self._bundle:
                    raise ValidationError(
                        f"combine_with id {cid!r} is not in bundle {self._bundle!r}"
                    )

        # --- urgency check ------------------------------------------------
        if urgency is not None:
            self._check_urgency(urgency)

    def _check_urgency(self, urgency: str) -> None:
        if "p0" in self._severities:
            # p0: only this_week is allowed.
            if urgency not in _P0_ALLOWED_URGENCY:
                raise ValidationError(
                    f"urgency {urgency!r} is not allowed for p0; "
                    f"only {sorted(_P0_ALLOWED_URGENCY)} are allowed"
                )
            return
        # Safety (non-p0): next_visit is forbidden.
        if self._recall_class == "safety":
            if urgency in _SAFETY_FORBIDDEN_URGENCY:
                raise ValidationError(
                    f"urgency {urgency!r} is not allowed for safety recalls "
                    f"(forbidden set: {sorted(_SAFETY_FORBIDDEN_URGENCY)})"
                )
        # non_safety: any urgency is accepted (no further check needed).


# ---------------------------------------------------------------------------
# validate_recall_plan
# ---------------------------------------------------------------------------
def validate_recall_plan(
    tool_input: dict[str, Any],
    *,
    candidate_dealer_ids: list[str],
    bundle_ids: list[str],
    severity: str,
    recall_class: Optional[str],
    evidence_kinds: list[str],
) -> None:
    """Validate a record_recall_plan tool response (decisions.md calls 48–49).

    Checks exact key set, types, enums, lengths, dealer/combine/urgency via
    RecallValidator, and evidence_refs membership.

    Raises ValidationError on any violation.
    """
    _check_exact_keys(tool_input, _RECALL_PLAN_REQUIRED_KEYS)

    dealer_id = tool_input["dealer_id"]
    combine_with = tool_input["combine_with"]
    urgency = tool_input["urgency"]
    detail = tool_input["detail"]
    evidence_refs = tool_input["evidence_refs"]
    confidence = tool_input["confidence"]

    # Types
    _require_str(dealer_id, "dealer_id")
    _require_list_of_str(combine_with, "combine_with")
    _require_str(urgency, "urgency")
    _require_str_len(detail, "detail", 1, 600)
    _require_list_of_str(evidence_refs, "evidence_refs")
    _require_str(confidence, "confidence")

    # Enums
    if urgency not in _VALID_URGENCY:
        raise ValidationError(
            f"urgency {urgency!r} is not one of {sorted(_VALID_URGENCY)}"
        )
    if confidence not in _VALID_CONFIDENCE:
        raise ValidationError(
            f"confidence {confidence!r} is not one of {sorted(_VALID_CONFIDENCE)}"
        )

    # evidence_refs membership
    _check_evidence_refs(evidence_refs, evidence_kinds)

    # Delegate dealer / combine_with / urgency to RecallValidator.
    v = RecallValidator(
        candidate_dealer_ids=candidate_dealer_ids,
        bundle_ids=bundle_ids,
        allowed_severities=[severity] if severity else [],
        recall_class=recall_class,
    )
    v.validate(dealer_id=dealer_id, combine_with=combine_with, urgency=urgency)


# ---------------------------------------------------------------------------
# validate_claim_draft
# ---------------------------------------------------------------------------
def validate_claim_draft(
    tool_input: dict[str, Any],
    *,
    evidence_kinds: list[str],
) -> None:
    """Validate a record_claim_draft tool response (decisions.md call 48).

    Raises ValidationError on any violation.
    """
    _check_exact_keys(tool_input, _CLAIM_DRAFT_REQUIRED_KEYS)

    narrative = tool_input["narrative"]
    proceed = tool_input["proceed"]
    detail = tool_input["detail"]
    evidence_refs = tool_input["evidence_refs"]
    confidence = tool_input["confidence"]

    _require_str_len(narrative, "narrative", 1, 1000)
    _require_str(proceed, "proceed")
    _require_str_len(detail, "detail", 1, 600)
    _require_list_of_str(evidence_refs, "evidence_refs")
    _require_str(confidence, "confidence")

    if proceed not in _VALID_PROCEED:
        raise ValidationError(
            f"proceed {proceed!r} is not one of {sorted(_VALID_PROCEED)}"
        )
    if confidence not in _VALID_CONFIDENCE:
        raise ValidationError(
            f"confidence {confidence!r} is not one of {sorted(_VALID_CONFIDENCE)}"
        )

    _check_evidence_refs(evidence_refs, evidence_kinds)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _check_exact_keys(obj: dict[str, Any], required: frozenset[str]) -> None:
    """Raise ValidationError if obj has extra or missing keys."""
    present = frozenset(obj.keys())
    extra = present - required
    if extra:
        raise ValidationError(
            f"tool output contains unexpected keys {sorted(extra)}; "
            f"only {sorted(required)} are allowed"
        )
    missing = required - present
    if missing:
        raise ValidationError(
            f"tool output is missing required keys {sorted(missing)}"
        )


def _require_str(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValidationError(
            f"{field} must be a string, got {type(value).__name__!r}"
        )


def _require_str_len(value: Any, field: str, min_len: int, max_len: int) -> None:
    _require_str(value, field)
    if not (min_len <= len(value) <= max_len):
        raise ValidationError(
            f"{field} length {len(value)} is outside [{min_len}, {max_len}]"
        )


def _require_list_of_str(value: Any, field: str) -> None:
    if not isinstance(value, list):
        raise ValidationError(
            f"{field} must be a list, got {type(value).__name__!r}"
        )
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValidationError(
                f"{field}[{i}] must be a string, got {type(item).__name__!r}"
            )


def _check_evidence_refs(refs: list[str], kinds: list[str]) -> None:
    kind_set = frozenset(kinds)
    for ref in refs:
        if ref not in kind_set:
            raise ValidationError(
                f"evidence_refs entry {ref!r} is not a known evidence kind; "
                f"allowed: {sorted(kind_set)}"
            )
