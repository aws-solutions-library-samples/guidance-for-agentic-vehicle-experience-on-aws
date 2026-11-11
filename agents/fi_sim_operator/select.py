"""Candidate filter and class assignment for the simulated fleet operator (spec D10b).

Pure: no boto3, no AWS calls.  Every DDB-dependent set is passed in by the caller.

Public API
----------
classify_finding(finding, decided_ids, now_iso, hold_vins,
                 grounded_vins, recall_pending_groundings) -> ClassifiedFinding
    Classify a Finding from the by_fleet scan into one of the five classes or
    assign a skip reason.

Constants (exported for tests)
-------------------------------
FH_AGENT, RW_AGENT
CLASS_FH_GROUND_BOOK, CLASS_FH_BOOK
CLASS_RECALL_GROUND_BOOK, CLASS_RECALL_BOOK, CLASS_WARRANTY_FILE
SKIP_* constants
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Mapping, Optional

from agents.executors.simulated_operator import SIMULATED_KINDS

# ── agent identifiers (used in class assignment) ─────────────────────────────
FH_AGENT: str = "agent-fi-fleet-health"
RW_AGENT: str = "agent-fi-recall-warranty"

# ── class names ──────────────────────────────────────────────────────────────
CLASS_FH_GROUND_BOOK: str = "fh_ground_book"
CLASS_FH_BOOK: str = "fh_book"
CLASS_RECALL_GROUND_BOOK: str = "recall_ground_book"
CLASS_RECALL_BOOK: str = "recall_book"
CLASS_WARRANTY_FILE: str = "warranty_file"

_ALL_CLASSES: frozenset[str] = frozenset({
    CLASS_FH_GROUND_BOOK,
    CLASS_FH_BOOK,
    CLASS_RECALL_GROUND_BOOK,
    CLASS_RECALL_BOOK,
    CLASS_WARRANTY_FILE,
})

# ── skip reasons (spec D10b) ──────────────────────────────────────────────────
SKIP_NOT_IN_SCOPE_KIND: str = "not_in_scope_kind"
SKIP_NEEDS_REVIEW: str = "needs_review"
SKIP_NOT_SAFETY_RECALL: str = "not_safety_recall"
SKIP_P0_NOT_GROUNDED: str = "p0_not_grounded"
SKIP_PAIR_MISSING: str = "pair_missing"
SKIP_DISCLOSURE_DIFFERS: str = "disclosure_differs"
SKIP_HELD: str = "held"
SKIP_STALE: str = "stale"
SKIP_EXPIRED: str = "expired"
SKIP_DECIDED: str = "decided"
SKIP_TOO_NEW: str = "too_new"
SKIP_CAP_REACHED: str = "cap_reached"
SKIP_UNCLASSIFIED: str = "unclassified"
#: Exported here so plan.py can import it without a circular dependency.
SKIP_PAIRED: str = "paired"

# How old a Finding's computed_at may be before it is considered stale.
_STALE_HOURS: int = 26

# A valid VIN is exactly 17 characters.
_VIN_LENGTH: int = 17

# Expected target_system per action_kind (spec D10, executor routing).
_EXPECTED_TARGET: dict[str, str] = {
    "ground_vehicle": "cms",
    "book_fleet_service": "dms",
    "book_recall_service": "dms",
    "submit_warranty_claim": "dms",
}


@dataclass(frozen=True)
class ClassifiedFinding:
    """Result of classifying one candidate Finding."""

    finding: dict
    #: One of the five class names, or ``None`` when skipped.
    class_name: Optional[str]
    #: Skip reason when class_name is ``None``.
    skip_reason: Optional[str]


# ── internal helpers ──────────────────────────────────────────────────────────

def _fleet_id(finding: dict) -> Optional[str]:
    """Return fleet_id from top-level attr or scope.fleet_id."""
    top = finding.get("fleet_id")
    if isinstance(top, str) and top:
        return top
    scope = finding.get("scope")
    if isinstance(scope, dict):
        fid = scope.get("fleet_id")
        if isinstance(fid, str) and fid:
            return fid
    return None


def _vin(finding: dict) -> Optional[str]:
    """Return scope.vin or None."""
    scope = finding.get("scope")
    if isinstance(scope, dict):
        v = scope.get("vin")
        if isinstance(v, str):
            return v
    return None


def _is_expired(finding: dict, now_iso: str) -> bool:
    """True when expires_at is missing, malformed, naive, or <= now (fail closed).

    Fix (b) D10 11.3: parse UTC rather than comparing strings. String comparison
    fails for second-precision ``Z``-suffixed timestamps: ``"…:00Z" > "…:00.400Z"``
    because ord('Z')=90 > ord('.')=46, so a recommendation that expired 400 ms ago
    is incorrectly treated as live. Missing or malformed → expired (fail closed), as
    the auto-approver does for its own expiry check (F1.5 S2).

    Mutation guard (mutant = old string comparison): a string-comparison implementation
    treats a missing expires_at as live (returns False) — test_missing_expires_at_is_expired
    catches this because the finding would then not be skipped for expiry.
    """
    ea = finding.get("expires_at")
    if not isinstance(ea, str) or not ea:
        return True  # missing → expired (fail closed)
    try:
        dt_ea = datetime.datetime.fromisoformat(ea.replace("Z", "+00:00"))
        dt_now = datetime.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        # TypeError is raised when one is naive and the other is offset-aware.
        return dt_ea <= dt_now
    except (ValueError, AttributeError, TypeError):
        return True  # malformed or naive → expired (fail closed)


def _is_stale(finding: dict, now_iso: str) -> bool:
    """True when computed_at is more than 26 h before now_iso (spec D10b).

    S1: catch TypeError as well as ValueError — a naive computed_at string (no
    timezone) raises TypeError on the subtraction ``dt_now - dt_computed`` when
    dt_now is offset-aware.  Treat it as stale (fail-closed), as _is_expired does.
    """
    computed = finding.get("computed_at")
    if not isinstance(computed, str) or not computed:
        return True  # malformed → treat as stale (fail-closed)
    try:
        dt_computed = datetime.datetime.fromisoformat(computed.replace("Z", "+00:00"))
        dt_now = datetime.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return (dt_now - dt_computed).total_seconds() > _STALE_HOURS * 3600
    except (ValueError, TypeError):
        return True


def _evidence_values(finding: dict, kind: str) -> list[dict]:
    """The ``value`` of every evidence entry whose ``kind`` is ``kind``.

    Reads the PERSISTED shape (``{"kind": "recall.campaign", "value": {...}, "vin",
    "provenance", ...}``), the only shape the stand-in ever sees: it reads Findings from
    ``vsa-{stage}-avx-findings``. The agents build entries in memory as
    ``{"recall.campaign": {...}}`` (``fi_recall_warranty/evidence.py``); the Finding
    writer stores them as ``kind``/``value``. The first pass read the in-memory shape,
    so on staging every recall Finding skipped as ``not_safety_recall`` and every claim
    as ``needs_review`` (architect check against live Findings, prompt 287). A value
    that isn't a dict doesn't count.
    """
    return [
        e["value"] for e in finding.get("evidence", []) or []
        if isinstance(e, dict) and e.get("kind") == kind and isinstance(e.get("value"), dict)
    ]


def _fault_need_codes(parameters: dict) -> frozenset[str]:
    """The codes of a booking's ``fault:<code>`` needs (``need_id``)."""
    codes = set()
    for need in parameters.get("needs") or []:
        if isinstance(need, dict):
            nid = need.get("need_id")
            if isinstance(nid, str) and nid.startswith("fault:") and nid[6:]:
                codes.add(nid[6:])
    return frozenset(codes)


# ── public API ────────────────────────────────────────────────────────────────

def classify_finding(
    finding: dict,
    decided_ids: set[str],
    now_iso: str,
    hold_vins: frozenset[str],
    grounded_codes: "Mapping[str, frozenset[str]]",
    recall_pending_groundings: set[tuple[str, str]],
) -> ClassifiedFinding:
    """Classify a Finding into one of the five classes or return a skip reason.

    Parameters
    ----------
    finding:
        A Finding dict from the ``by_fleet`` DDB scan, including top-level
        ``fleet_id`` for GSI compatibility.
    decided_ids:
        Set of ``finding_id`` values for which a decision Action already
        exists (ConsistentRead GetItem on the actions table).
    now_iso:
        Current run time as ISO-8601 UTC string (e.g. ``"2026-11-06T07:10:00Z"``).
    hold_vins:
        VINs that must never be decided (from ``OperatorConfig.hold_vins``).
    grounded_codes:
        ``{vin: frozenset(fault codes)}`` from the fleet's OPEN FI fault grounding
        rows in CMS (``reason == "fault"``, made by FI: ``findingId`` and
        ``actionId`` present), the union over a VIN's rows. A p0
        ``book_fleet_service`` Finding is ``fh_book`` only when every one of its
        ``fault:<code>`` needs' codes is in its VIN's set (11.3b item 7: a car
        grounded for one fault isn't grounded for another); otherwise
        ``p0_not_grounded``. A p0 booking with no fault need is ``p0_not_grounded``.
    recall_pending_groundings:
        Set of ``(vin, campaign_id)`` pairs for which a pending
        ``ground_vehicle`` / recall Finding exists and hasn't been decided.
        A ``book_recall_service`` Finding whose pair is pending should not be
        classified as a standalone ``recall_book``.

    Returns
    -------
    ClassifiedFinding
        ``class_name`` is one of the five class constants; ``skip_reason`` is
        ``None``.  OR ``class_name`` is ``None`` and ``skip_reason`` names why.
    """
    def _skip(reason: str) -> ClassifiedFinding:
        return ClassifiedFinding(finding=finding, class_name=None, skip_reason=reason)

    def _classify(cls: str) -> ClassifiedFinding:
        return ClassifiedFinding(finding=finding, class_name=cls, skip_reason=None)

    finding_id = finding.get("finding_id", "")

    # ── 1. Pre-filter: must have recommended_action ───────────────────────────
    rec = finding.get("recommended_action")
    if not isinstance(rec, dict):
        return _skip(SKIP_NOT_IN_SCOPE_KIND)

    action_kind = rec.get("action_kind", "")
    target_system = rec.get("target_system", "")
    parameters = rec.get("parameters") or {}

    # ── 2. Action kind must be in SIMULATED_KINDS (allowlist) ─────────────────
    if action_kind not in SIMULATED_KINDS:
        return _skip(SKIP_NOT_IN_SCOPE_KIND)

    # ── 3. Not expired ────────────────────────────────────────────────────────
    if _is_expired(finding, now_iso):
        return _skip(SKIP_EXPIRED)

    # ── 4. Not already decided ────────────────────────────────────────────────
    if finding_id in decided_ids:
        return _skip(SKIP_DECIDED)

    # ── 5. Not stale (computed_at within 26 h of run) ─────────────────────────
    if _is_stale(finding, now_iso):
        return _skip(SKIP_STALE)

    # ── 6. VIN must be valid (17 chars) and not held ──────────────────────────
    vin = _vin(finding)
    if not isinstance(vin, str) or len(vin) != _VIN_LENGTH:
        return _skip(SKIP_UNCLASSIFIED)
    if vin in hold_vins:
        return _skip(SKIP_HELD)

    # ── 7. target_system must match the expected for this kind ────────────────
    expected_target = _EXPECTED_TARGET.get(action_kind)
    if target_system != expected_target:
        return _skip(SKIP_UNCLASSIFIED)

    # ── 8. Class assignment (allowlist — only these five) ─────────────────────
    agent_id = finding.get("agent_id", "")
    severity = finding.get("severity", "")

    # ── 8a. fh_ground_book: FH agent, ground_vehicle with fault_codes ─────────
    if agent_id == FH_AGENT and action_kind == "ground_vehicle":
        fault_codes = parameters.get("fault_codes")
        if isinstance(fault_codes, list) and fault_codes:
            return _classify(CLASS_FH_GROUND_BOOK)
        # ground_vehicle from FH with no fault_codes → unclassified
        return _skip(SKIP_UNCLASSIFIED)

    # ── 8b. fh_book: FH agent, book_fleet_service ─────────────────────────────
    if agent_id == FH_AGENT and action_kind == "book_fleet_service":
        if severity != "p0":
            return _classify(CLASS_FH_BOOK)
        # p0 booking: an open FI fault grounding must already hold every fault
        # code the booking is for (the D3 seam).
        need_codes = _fault_need_codes(parameters)
        if need_codes and need_codes <= grounded_codes.get(vin, frozenset()):
            return _classify(CLASS_FH_BOOK)
        return _skip(SKIP_P0_NOT_GROUNDED)

    # ── 8c. recall_ground_book: RW agent, ground_vehicle with recall_id ───────
    if agent_id == RW_AGENT and action_kind == "ground_vehicle":
        recall_id = parameters.get("recall_id")
        if isinstance(recall_id, str) and recall_id:
            # Must have exactly one safety recall.campaign evidence entry
            campaign_entries = _evidence_values(finding, "recall.campaign")
            if len(campaign_entries) == 1:
                camp = campaign_entries[0]
                if (camp.get("recall_class") == "safety"
                        and camp.get("campaign_id") == recall_id):
                    return _classify(CLASS_RECALL_GROUND_BOOK)
            return _skip(SKIP_NOT_SAFETY_RECALL)
        return _skip(SKIP_UNCLASSIFIED)

    # ── 8d. recall_book: RW agent, book_recall_service, safety, matching campaign ─
    if agent_id == RW_AGENT and action_kind == "book_recall_service":
        campaign_entries = _evidence_values(finding, "recall.campaign")
        if len(campaign_entries) != 1:
            return _skip(SKIP_NOT_SAFETY_RECALL)
        camp = campaign_entries[0]
        if camp.get("recall_class") != "safety":
            return _skip(SKIP_NOT_SAFETY_RECALL)
        # W2: the evidence campaign_id must equal the Action's recall_id (as
        # recall_ground_book requires).  A booking whose evidence names a different
        # campaign than the action is requesting must not be auto-approved.
        recall_id = parameters.get("recall_id")
        if not isinstance(recall_id, str) or not recall_id:
            return _skip(SKIP_NOT_SAFETY_RECALL)
        if camp.get("campaign_id") != recall_id:
            return _skip(SKIP_NOT_SAFETY_RECALL)
        # S8: a booking held back by a pending grounding counts as paired (not
        # unclassified), so skip counts read correctly.
        if (vin, recall_id) in recall_pending_groundings:
            return _skip(SKIP_PAIRED)
        return _classify(CLASS_RECALL_BOOK)

    # ── 8e. warranty_file: RW agent, submit_warranty_claim, approved verdict ───
    if agent_id == RW_AGENT and action_kind == "submit_warranty_claim":
        verdict_entries = _evidence_values(finding, "warranty.verdict")
        if len(verdict_entries) != 1:
            return _skip(SKIP_NEEDS_REVIEW)
        verdict_val = verdict_entries[0].get("verdict")
        if verdict_val == "approved":
            return _classify(CLASS_WARRANTY_FILE)
        return _skip(SKIP_NEEDS_REVIEW)

    # ── 9. Nothing matched → unclassified ─────────────────────────────────────
    return _skip(SKIP_UNCLASSIFIED)
