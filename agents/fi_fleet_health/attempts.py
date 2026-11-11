"""Attempt rules for fleet-health findings (spec D9, contract § 6).

Mirrors Stage C's agents/fi_recall_warranty/attempts.py but for the two fleet-
health subject forms: ``service/<n>`` (booking) and ``ground/<n>`` (grounding).
Both subject forms use the same severity-based timing rules (contract § 6).

Public API
----------
should_retry_health(prev_status, severity, days_since_decision,
                    attempt=0, subject_segment=None) -> bool
build_max_attempts_item(vin, attempt, subject_segment) -> dict

Attempt cap: MAX_ATTEMPTS = 5.  After 5 attempts an informational item is
written (no ``recommended_action``, headline from _INFORMATIONAL_HEADLINE).

Timing windows (contract § 6):
  p0 / attention  : rejected → retry after ≥ 14 days
  soon / routine  : rejected → retry after ≥ 30 days
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Module-level constants (overridable by argument for tests)
# ---------------------------------------------------------------------------
MAX_ATTEMPTS: int = 5
REJECTED_HIGH_SEVERITY_DAYS: int = 14  # p0, attention
REJECTED_LOW_SEVERITY_DAYS: int = 30   # soon, routine

# Statuses that trigger an immediate retry
_IMMEDIATE_RETRY = frozenset({"cancelled", "failed", "refused"})

# Statuses that never trigger a retry
_NO_RETRY = frozenset({"stalled", "blocked", "executed", "completed", "in_progress",
                        "dismissed", "unknown"})

# High-severity levels that use the 14-day window
_HIGH_SEVERITY = frozenset({"p0", "attention"})

_INFORMATIONAL_HEADLINE = "Bookings keep failing — manual follow-up needed."


def should_retry_health(
    prev_status: str,
    severity: str,
    days_since_decision: int,
    attempt: int = 0,
    subject_segment: Optional[str] = None,
    *,
    _max_attempts: int = MAX_ATTEMPTS,
    _high_days: int = REJECTED_HIGH_SEVERITY_DAYS,
    _low_days: int = REJECTED_LOW_SEVERITY_DAYS,
) -> bool:
    """Return True if a next attempt should be written for this vehicle/subject.

    Parameters
    ----------
    prev_status:         One of: cancelled, failed, refused, rejected, stalled,
                         blocked, executed, completed.
    severity:            "p0", "attention", "soon", "routine".
    days_since_decision: Days elapsed since the decision was made.
    attempt:             The current (just-resolved) attempt number; 0 = unknown.
    subject_segment:     "service" or "ground" — both use the same rules.

    Mutation guards:
    - Ignore attempt → cap never fires → True at attempt=5, test fails.
    - Treat attention as 30-day → True at day 13, test fails.
    """
    # Cap check (spec D9: max 5 attempts per subject)
    if attempt >= _max_attempts:
        return False

    if prev_status in _IMMEDIATE_RETRY:
        return True

    if prev_status == "rejected":
        threshold = _high_days if severity in _HIGH_SEVERITY else _low_days
        return days_since_decision >= threshold

    # blocked, stalled, executed, completed, in_progress, dismissed, unknown
    return False


def build_max_attempts_item(
    vin: str,
    attempt: int,
    subject_segment: str = "service",
) -> dict:
    """Return the informational item written after MAX_ATTEMPTS are exhausted.

    The item carries no ``recommended_action`` (D9).  Severity is
    ``"informational"`` (matching the schema enum).

    Mutation guard (test_after_5_attempts_informational_item_written):
    severity="soon" → test fails.
    """
    return {
        "vin": vin,
        "attempt": attempt,
        "subject_segment": subject_segment,
        "subject_key": f"{subject_segment}/{attempt}",
        "severity": "informational",
        "headline": _INFORMATIONAL_HEADLINE,
        # No recommended_action — informational only.
    }
