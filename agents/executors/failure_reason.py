"""Failure-reason builder — spec D14, contract § 10a.

Builds the single operator-facing sentence that explains why an Action did not
go through.  This function is the **one builder**, called in **one place**
(``registry.dispatch()``), so the sentence cannot leak information from an
exception, a response body or caller text.

Design constraints (Fix Group 5, review FG4-contract Critical 1):
- Every parameter is taken from the dispatcher's own typed data; no ``str``
  parameter carries free text.
- The function never raises; input outside the table gets a fixed fallback.
- The fallback itself never echoes any input.
- The schema's ``pattern`` admits only the vocabulary defined here, so a writer
  that bypasses this builder produces a schema-invalid row.
"""
from __future__ import annotations

#: Sentence when nothing matches, or when the input is outside the table.
_FALLBACK = "AVX could not complete this action."

#: Display name pair (capitalized, lowercase) for each target system.
_NAMES: dict[str, tuple[str, str]] = {
    "dms": ("DMS", "DMS"),
    "cms": ("CMS", "CMS"),
    "adp": ("ADP", "ADP"),
    "retail": ("The retail system", "the retail system"),
    "vehicle-command": ("The vehicle", "the vehicle"),
    "ios-owner": ("The owner's app", "the owner's app"),
}

#: Targets that take the target-free fallback sentence regardless of kind.
_TARGET_FREE = frozenset({"chat", "none"})

#: The seven DMS repair-order status words (DMS repair_orders.py vocabulary).
_RO_WORDS = frozenset({
    "Draft", "InProgress", "AwaitingParts",
    "Ready", "Delivered", "Closed", "Invoiced",
})

#: G1–G3 guardrail sentences (single, not G4-case-dependent).
_G1_TO_G3: dict[str, str] = {
    "G1": "Blocked: exempting a safety recall needs a fleet operator.",
    "G2": "Blocked: grounding a vehicle needs a fleet operator.",
    "G3": "Blocked: sending a warranty claim needs a fleet operator.",
}

#: G4 case sentences (all cases except not_completed).
_G4_CASES: dict[str, str] = {
    "not_operator": "Blocked: completing a recall needs a fleet operator.",
    "no_ref": "Blocked: completing a recall needs a service record.",
    "lookup_failed": "Blocked: the service record could not be checked.",
    "not_found": "Blocked: the service record could not be found.",
    "other_vehicle": "Blocked: the service record is for a different vehicle.",
}

_VALID_FAILURE_KINDS = frozenset({
    "target_refused",
    "executor_exhausted_retries",
    "executor_terminal_error",
})


def _valid_status(s: object) -> bool:
    """True iff s is a plain int in [100, 599]."""
    # bool is a subclass of int, so `isinstance(s, int)` alone admits True/False.
    return type(s) is int and 100 <= s <= 599


def _valid_retry_count(n: object) -> bool:
    """True iff n is a plain non-negative int."""
    return type(n) is int and n >= 0


def _attempts(n: int) -> str:
    return "1 attempt" if n == 1 else f"{n} attempts"


def failure_reason(
    *,
    target_system: object,
    failure_kind: object = None,
    error_status: object = None,
    retry_count: object = 0,
    guardrail: object = None,
    guardrail_case: object = None,
    ro_status: object = None,
) -> str:
    """Build one operator-facing sentence explaining why an Action did not go
    through.

    All parameters are keyword-only so the caller cannot accidentally pass a
    free-text value positionally.

    Parameters
    ----------
    target_system:
        The ``Action.target_system``.
    failure_kind:
        One of ``"target_refused"``, ``"executor_exhausted_retries"``,
        ``"executor_terminal_error"``, or ``None``.
    error_status:
        The HTTP status the target returned, or ``None``.  Must be a plain
        ``int`` in [100, 599]; anything else is treated as absent.
    retry_count:
        Total attempts made.  Must be a plain non-negative ``int``; anything
        else is treated as outside the table → fallback.
    guardrail:
        ``"G1"``–``"G4"`` when the block was a guardrail, else ``None``.
    guardrail_case:
        G4 sub-case: ``"not_operator" | "no_ref" | "lookup_failed" |
        "not_found" | "other_vehicle" | "not_completed"``, or ``None``.
    ro_status:
        The DMS repair-order status word (for ``guardrail_case="not_completed"``
        only).

    Returns
    -------
    str
        A sentence from the § 10a vocabulary. Never raises; never echoes free
        text from any parameter.
    """
    # --- validate scalar inputs that affect every branch ---
    if error_status is not None and not _valid_status(error_status):
        return _FALLBACK
    if not _valid_retry_count(retry_count):
        return _FALLBACK

    # --- guardrail branch (rows 1-9) ---------------------------------------
    if guardrail is not None:
        # A guardrail block is always failure_kind=target_refused with no status.
        if failure_kind != "target_refused" or error_status is not None:
            return _FALLBACK

        if guardrail in _G1_TO_G3:
            # G1–G3 take no case or ro_status.
            if guardrail_case is not None or ro_status is not None:
                return _FALLBACK
            return _G1_TO_G3[guardrail]

        if guardrail == "G5":
            # G5: engineering decisions need a product engineer — contract § 10a row 9a.
            if guardrail_case is not None or ro_status is not None:
                return _FALLBACK
            return "Blocked: engineering decisions need a product engineer."

        if guardrail != "G4":
            # Unknown guardrail value.
            return _FALLBACK

        # G4 cases.
        if guardrail_case == "not_completed":
            word = ro_status if ro_status in _RO_WORDS else "unknown"
            return f"Blocked: the service record is not complete (status {word})."

        if guardrail_case in _G4_CASES:
            # not_completed is handled above; the others take no ro_status.
            if ro_status is not None:
                return _FALLBACK
            return _G4_CASES[guardrail_case]

        # Unknown G4 case.
        return _FALLBACK

    # --- no guardrail; but guardrail_case or ro_status present → fallback ---
    if guardrail_case is not None or ro_status is not None:
        return _FALLBACK

    # --- target-free targets (chat, none) → always fallback ----------------
    if target_system in _TARGET_FREE:
        return _FALLBACK

    # --- unknown target → fallback ------------------------------------------
    if target_system not in _NAMES:
        return _FALLBACK

    # --- validate failure_kind here (after guardrail checks) ----------------
    if failure_kind not in _VALID_FAILURE_KINDS:
        return _FALLBACK

    cap, low = _NAMES[target_system]  # type: ignore[index]

    # --- row 11 / 12-13: target_refused ------------------------------------
    if failure_kind == "target_refused":
        # Row 11: Stage A's CmsExecutor — no status, no refusal code.
        if target_system == "cms" and error_status is None:
            return "CMS cannot carry out this action yet."
        if error_status is not None:
            return f"{cap} refused the request (HTTP {error_status})."
        return f"{cap} refused the request."

    # --- rows 14-16: executor_exhausted_retries -----------------------------
    if failure_kind == "executor_exhausted_retries":
        if retry_count == 0:
            # Row 16: ran out of time before any attempt (no status expected).
            # If a status is somehow present with 0 attempts, that's incoherent
            # — fall through to the fallback.
            if error_status is not None:
                return _FALLBACK
            return f"AVX ran out of time before it could contact {low}."
        if error_status is not None:
            # Row 14: target answered (5xx / rate-limit / etc.).
            return f"{cap} was unavailable (HTTP {error_status}) after {_attempts(retry_count)}."
        # Row 15: no answer (connection error, timeout).
        return f"{cap} did not respond after {_attempts(retry_count)}."

    # --- rows 17-18: executor_terminal_error --------------------------------
    if failure_kind == "executor_terminal_error":
        if error_status is not None:
            return f"{cap} did not accept the request (HTTP {error_status})."
        return f"AVX could not complete the request to {low}."

    # Should be unreachable (failure_kind was validated above), but be safe.
    return _FALLBACK  # pragma: no cover
