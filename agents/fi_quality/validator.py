"""Validators for the investigation agent's two model calls (spec D10 step 4, task 4.2).

Both replies are untrusted.  Every id must come from the bundle; the text fields have length
caps and no control characters; nothing numeric exists in either tool (contract §§ 9.2-9.3).
Error messages name the rule, never the rejected value, so a retry prompt never echoes
model output.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Iterable

VALID_CONFIDENCE: tuple[str, ...] = ("high", "medium", "low")
REASONING_MAX_CHARS: int = 1200
TRADEOFFS_MAX_CHARS: int = 600
MIN_OPTIONS: int = 2
MAX_OPTIONS: int = 4
MAX_EVIDENCE_REFS: int = 10
#: Spec D10 step 3: an option is a remedy option of the chosen failure mode, or ``monitor``.
MONITOR_OPTION_ID: str = "monitor"
UNKNOWN_FAILURE_MODE: str = "unknown"
ROOT_CAUSE_FIELDS: frozenset[str] = frozenset(
    {"candidate_id", "failure_mode_id", "evidence_refs", "reasoning", "confidence"})
DESIGN_OPTIONS_FIELDS: frozenset[str] = frozenset({"options"})
OPTION_FIELDS: frozenset[str] = frozenset({"remedy_option_id", "tradeoffs", "recommended"})


class ValidationError(Exception):
    """Raised when a model reply breaks a rule."""


def _text_ok(value: Any, max_chars: int) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= max_chars
            and not any(unicodedata.category(c) == "Cc" and c not in ("\n", "\t")
                        for c in value))


class InvestigationValidator:
    """Checks root-cause and design-option replies against one investigation bundle."""

    def __init__(self, bundle: dict[str, Any]) -> None:
        self._candidate_ids = frozenset(
            c.get("candidate_id") for c in bundle.get("candidates") or []
            if isinstance(c, dict) and c.get("candidate_id"))
        self._options_by_fm: dict[str, frozenset[str]] = {}
        for fm in bundle.get("failure_modes") or []:
            if not isinstance(fm, dict) or not fm.get("failure_mode_id"):
                continue
            self._options_by_fm[fm["failure_mode_id"]] = frozenset(
                o.get("remedy_option_id") for o in fm.get("remedy_options") or []
                if isinstance(o, dict) and o.get("remedy_option_id"))

    def allowed_option_ids(self, failure_mode_id: str) -> frozenset[str]:
        """The chosen failure mode's remedy option ids plus ``monitor``."""
        return self._options_by_fm.get(failure_mode_id, frozenset()) | {MONITOR_OPTION_ID}

    # -- spec D10 step 4, the id rules (the 1.4 item 8 tests call these) -----------------

    def validate_root_cause(self, candidate_id: Any, failure_mode_id: Any) -> None:
        if not isinstance(candidate_id, str) or candidate_id not in self._candidate_ids:
            raise ValidationError("candidate_id is not one of the bundle's candidates")
        if not isinstance(failure_mode_id, str) or (
                failure_mode_id != UNKNOWN_FAILURE_MODE
                and failure_mode_id not in self._options_by_fm):
            raise ValidationError("failure_mode_id is neither a bundle failure mode nor unknown")

    def validate_design_options(self, failure_mode_id: Any, options: Any) -> None:
        if not isinstance(failure_mode_id, str) or failure_mode_id not in self._options_by_fm:
            raise ValidationError("design options need a failure mode from the bundle")
        if not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            raise ValidationError(f"give {MIN_OPTIONS} to {MAX_OPTIONS} options")
        allowed = self.allowed_option_ids(failure_mode_id)
        seen: set[str] = set()
        recommended = 0
        for option in options:
            if not isinstance(option, dict) or set(option) != OPTION_FIELDS:
                raise ValidationError(
                    "each option must have exactly remedy_option_id, tradeoffs, recommended")
            oid = option["remedy_option_id"]
            if not isinstance(oid, str) or oid not in allowed:
                raise ValidationError("an option is not a remedy option of the failure mode")
            if oid in seen:
                raise ValidationError("an option appears twice")
            seen.add(oid)
            if not _text_ok(option["tradeoffs"], TRADEOFFS_MAX_CHARS):
                raise ValidationError(
                    f"tradeoffs must be 1 to {TRADEOFFS_MAX_CHARS} characters of plain text")
            if option["recommended"] is True:
                recommended += 1
            elif option["recommended"] is not False:
                raise ValidationError("recommended must be true or false")
        if recommended != 1:
            raise ValidationError("exactly one option must be recommended")

    # -- the whole tool replies -----------------------------------------------------------

    def validate_root_cause_input(self, tool_input: Any, *,
                                  evidence_kinds: Iterable[str]) -> dict[str, Any]:
        if not isinstance(tool_input, dict) or set(tool_input) != ROOT_CAUSE_FIELDS:
            raise ValidationError("the tool input must have exactly the fields "
                                  + ", ".join(sorted(ROOT_CAUSE_FIELDS)))
        self.validate_root_cause(tool_input["candidate_id"], tool_input["failure_mode_id"])
        if not _text_ok(tool_input["reasoning"], REASONING_MAX_CHARS):
            raise ValidationError(
                f"reasoning must be 1 to {REASONING_MAX_CHARS} characters of plain text")
        refs = tool_input["evidence_refs"]
        if (not isinstance(refs, list) or len(refs) > MAX_EVIDENCE_REFS
                or not all(isinstance(r, str) for r in refs)
                or set(refs) - frozenset(evidence_kinds)):
            raise ValidationError("evidence_refs must name only the listed evidence kinds")
        if tool_input["confidence"] not in VALID_CONFIDENCE:
            raise ValidationError("confidence must be high, medium or low")
        return {k: tool_input[k] for k in sorted(ROOT_CAUSE_FIELDS)}

    def validate_design_options_input(self, tool_input: Any, *,
                                      failure_mode_id: str) -> list[dict[str, Any]]:
        if not isinstance(tool_input, dict) or set(tool_input) != DESIGN_OPTIONS_FIELDS:
            raise ValidationError("the tool input must have exactly the field options")
        self.validate_design_options(failure_mode_id, tool_input["options"])
        return [dict(o) for o in tool_input["options"]]
