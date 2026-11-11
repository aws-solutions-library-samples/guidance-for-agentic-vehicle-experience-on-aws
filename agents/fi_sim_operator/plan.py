"""Work-unit building and cap planning for the simulated fleet operator (spec D10c/D10d).

Pure: no boto3, no AWS calls.  The caller supplies first-seen times and the cap.

Public API
----------
WorkUnit  — a single decision or a ground+book pair (two decisions).
Plan      — ordered list of WorkUnits that fits within the cap.

build_plan(classified, paired_bookings, first_seen, run_at, remaining_cap) -> Plan
    Build the ordered, cap-bounded plan for one fleet run.

Ordering (spec D10c):
    Oldest ``first_seen_at`` first, then p0 before others, then ``finding_id``.
    A unit is eligible only when its primary finding's ``first_seen_at`` is at
    least 20 h before ``run_at``.

Cap (spec D10d):
    A pair counts as two decisions; the plan never exceeds ``remaining_cap``
    and never splits a pair.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional

from agents.fi_sim_operator.select import (
    CLASS_FH_GROUND_BOOK,
    CLASS_RECALL_GROUND_BOOK,
    ClassifiedFinding,
    SKIP_CAP_REACHED,
    SKIP_DECIDED,
    SKIP_EXPIRED,
    SKIP_PAIR_MISSING,
    SKIP_PAIRED,
    SKIP_STALE,
    SKIP_TOO_NEW,
    SKIP_UNCLASSIFIED,
    _is_expired,
    _is_stale,
    _vin,
)

#: A standalone booking that is also some pair's booking (11.3b item 8): the pair
#: runs it, after its grounding; the standalone unit is dropped.
#: Imported from select so select.py can also use it (S8) without a circular dep.
# SKIP_PAIRED is now imported from select above; keep this comment for readers.

#: A candidate is eligible only when first seen this many hours before the run.
_MIN_AGE_HOURS: int = 20

#: Class names that form ground+book pairs (two decisions per unit).
_PAIR_CLASSES: frozenset[str] = frozenset({CLASS_FH_GROUND_BOOK, CLASS_RECALL_GROUND_BOOK})

#: p0 severity (these units are ordered before non-p0 within the same first_seen bucket).
_P0_SEVERITY: str = "p0"


@dataclass(frozen=True)
class WorkUnit:
    """One unit of work: a single decision or a ground+book pair.

    ``findings`` is a 1-tuple for standalone classes or a 2-tuple
    ``(grounding, booking)`` for pair classes.
    """

    findings: tuple[dict, ...]
    class_name: str
    #: first_seen_at of the primary (grounding) finding.
    first_seen_at: str
    #: Number of decision Actions this unit will consume from the cap.
    decision_count: int


@dataclass
class Plan:
    """Result of :func:`build_plan`."""

    units: list[WorkUnit] = field(default_factory=list)
    #: Skip counts by reason (augmented from classified skips + planning skips).
    skip_counts: dict[str, int] = field(default_factory=dict)

    def total_decisions(self) -> int:
        return sum(u.decision_count for u in self.units)

    def _add_skip(self, reason: str) -> None:
        self.skip_counts[reason] = self.skip_counts.get(reason, 0) + 1


def _is_eligible(first_seen_at: str, run_at: str) -> bool:
    """True when first_seen_at is at least 20 h before run_at (spec D10c)."""
    try:
        dt_fs = datetime.datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
        dt_run = datetime.datetime.fromisoformat(run_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return (dt_run - dt_fs).total_seconds() >= _MIN_AGE_HOURS * 3600


def _severity(finding: dict) -> str:
    return finding.get("severity", "")


def _unit_sort_key(unit: WorkUnit) -> tuple:
    """Sort: oldest first_seen first, p0 before non-p0, then finding_id."""
    # first_seen_at ascending, so reverse chronological newest → last
    primary = unit.findings[0]
    is_p0 = 0 if _severity(primary) == _P0_SEVERITY else 1
    fid = primary.get("finding_id", "")
    return (unit.first_seen_at, is_p0, fid)


def build_plan(
    classified: list[ClassifiedFinding],
    paired_bookings: "dict[str, tuple[Optional[dict], Optional[str]]]",
    first_seen: dict[str, str],
    run_at: str,
    remaining_cap: int,
    *,
    decided_ids: "Optional[set[str]]" = None,
) -> Plan:
    """Build the ordered, cap-bounded work plan for one fleet run.

    Parameters
    ----------
    classified:
        Output of :func:`~agents.fi_sim_operator.select.classify_finding` for
        all candidates, including those with skip_reason (they are counted).
    paired_bookings:
        Mapping from a grounding's ``finding_id`` to the ``(booking, reason)``
        tuple returned by :func:`~agents.fi_sim_operator.pairing.pair_booking`.
        ``reason`` is ``None`` on success; any non-None reason means pairing
        failed and its value is counted as the skip reason for that grounding.

        Fix (d) D10 11.3: previously this was ``dict[str, Optional[dict]]``
        (a booking or None). A ``pair_booking`` call that returns
        ``(booking, "disclosure_differs")`` has found a candidate but the
        candidate is not usable — pairing has failed. Treating the returned
        booking as a success would approve a pair whose disclosure directive
        differs. The fix passes the full ``(booking, reason)`` tuple and pairs
        only when ``reason is None``.

        Mutation guard (mutant = pair on a booking whose reason is
        ``"disclosure_differs"``): count the booking as paired → a mismatched
        directive is approved. ``test_plan_disclosure_differs_is_skip`` catches
        this.
    first_seen:
        Mapping from ``finding_id`` to ``first_seen_at`` ISO UTC string
        (from the ledger table ``SEEN#<fleet_id>`` rows).
        Findings absent from this mapping are treated as first-seen now
        (``run_at``) and therefore too new.  For pair units the **booking**'s
        own ``finding_id`` is also looked up here; if absent or < 20 h old,
        the pair is skipped as ``too_new``.
    run_at:
        ISO-8601 UTC string of the run time.
    remaining_cap:
        How many decision Actions this fleet may still make today (already
        counts existing day decisions).
    decided_ids:
        Optional set of ``finding_id`` values already decided.  When provided,
        a paired booking already in this set is skipped as ``decided``.

    Returns
    -------
    Plan
        ``units`` ordered oldest-first, p0-first, finding_id-ascending.
        ``skip_counts`` tallies every skip reason (from pre-classified
        findings and from planning-phase skips such as ``too_new`` and
        ``cap_reached``).
    """
    plan = Plan()

    # ── Tally pre-classified skips ────────────────────────────────────────────
    candidates: list[ClassifiedFinding] = []
    for cf in classified:
        if cf.skip_reason is not None:
            plan._add_skip(cf.skip_reason)
        else:
            candidates.append(cf)

    # ── Build work units for classified candidates ────────────────────────────
    units_all: list[WorkUnit] = []
    for cf in candidates:
        fid = cf.finding.get("finding_id", "")
        fs_at = first_seen.get(fid)

        # Absent from ledger → too new (first time we're seeing it is this run)
        if fs_at is None:
            plan._add_skip(SKIP_TOO_NEW)
            continue

        if not _is_eligible(fs_at, run_at):
            plan._add_skip(SKIP_TOO_NEW)
            continue

        if cf.class_name in _PAIR_CLASSES:
            pair_result = paired_bookings.get(fid)
            if pair_result is None:
                # Not in paired_bookings at all → pair_missing
                plan._add_skip(SKIP_PAIR_MISSING)
                continue
            # Fix (d): unpack (booking, reason); pair only when reason is None.
            booking, pair_reason = pair_result
            if pair_reason is not None:
                # Pairing failed — count the actual reason (e.g. "disclosure_differs",
                # "already_decided", "no_dealer") rather than the generic pair_missing.
                plan._add_skip(pair_reason)
                continue
            if booking is None:
                # Should not happen (reason should be set when booking is None), but
                # fail safely.
                plan._add_skip(SKIP_PAIR_MISSING)
                continue
            # W1: validate the paired booking against the candidate filter
            # (not stale, target_system == "dms", unexpired, undecided, same VIN/fleet)
            # and require its own first-seen to be ≥ 20 h old.
            booking_id = booking.get("finding_id", "")
            b_fs = first_seen.get(booking_id)
            if b_fs is None or not _is_eligible(b_fs, run_at):
                # Booking absent from ledger or first seen < 20 h ago → too_new.
                # Mutation guard P-b: removing this check lets a 10 h-old booking
                # through; test_booking_first_seen_too_new catches it.
                plan._add_skip(SKIP_TOO_NEW)
                continue
            if _is_stale(booking, run_at):
                # Mutation guard P-a: removing this check lets a 30 h-old booking
                # through; test_stale_booking_no_pair catches it.
                plan._add_skip(SKIP_STALE)
                continue
            if _is_expired(booking, run_at):
                plan._add_skip(SKIP_EXPIRED)
                continue
            b_target = (booking.get("recommended_action") or {}).get("target_system", "")
            if b_target != "dms":
                plan._add_skip(SKIP_UNCLASSIFIED)
                continue
            if decided_ids is not None and booking_id in decided_ids:
                plan._add_skip(SKIP_DECIDED)
                continue
            if _vin(booking) != _vin(cf.finding):
                plan._add_skip(SKIP_PAIR_MISSING)
                continue
            if (booking.get("fleet_id") or "") != (cf.finding.get("fleet_id") or ""):
                plan._add_skip(SKIP_PAIR_MISSING)
                continue
            unit = WorkUnit(
                findings=(cf.finding, booking),
                class_name=cf.class_name,
                first_seen_at=fs_at,
                decision_count=2,
            )
        else:
            unit = WorkUnit(
                findings=(cf.finding,),
                class_name=cf.class_name,
                first_seen_at=fs_at,
                decision_count=1,
            )
        units_all.append(unit)

    # ── 11.3b item 8: a booking that is a pair's booking never runs on its own ──
    # Otherwise a recall (or fleet-service) booking could be both its own
    # standalone unit and the second half of a pair, and the standalone unit
    # could book it before its grounding was made. The pair wins; a second pair
    # naming the same booking (after sorting) is dropped too.
    pair_booking_ids = {
        u.findings[1].get("finding_id", "")
        for u in units_all
        if len(u.findings) == 2
    }
    kept: list[WorkUnit] = []
    for u in units_all:
        if len(u.findings) == 1 and u.findings[0].get("finding_id", "") in pair_booking_ids:
            plan._add_skip(SKIP_PAIRED)
            continue
        kept.append(u)
    units_all = kept

    # ── Sort: oldest first_seen, p0 first within same bucket, then finding_id ─
    units_all.sort(key=_unit_sort_key)

    # ── Apply cap (never split a pair) ────────────────────────────────────────
    cap_left = remaining_cap
    used_bookings: set[str] = set()
    for unit in units_all:
        if len(unit.findings) == 2:
            booking_id = unit.findings[1].get("finding_id", "")
            if booking_id in used_bookings:
                plan._add_skip(SKIP_PAIRED)
                continue
        if unit.decision_count > cap_left:
            plan._add_skip(SKIP_CAP_REACHED)
            continue
        plan.units.append(unit)
        cap_left -= unit.decision_count
        if len(unit.findings) == 2:
            used_bookings.add(unit.findings[1].get("finding_id", ""))

    return plan
