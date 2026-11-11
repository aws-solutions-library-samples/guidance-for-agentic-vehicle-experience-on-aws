"""Band-table classification for the FI fleet-health agent (spec D9).

This module is deterministic configuration: the band table, severity ordering,
and grounding rules.  The model cannot change severity, grounding, or the needs
list (spec D9).  Any ``model_hint`` argument is accepted and silently ignored.

Public API
----------
classify_needs(open_faults, agent2_findings, pm_tasks, ...) -> NeedsResult

NeedsResult
    severity   : "p0" | "attention" | "soon" | "routine"
    items      : list of "booking" and/or "grounding"
    needs      : list of need dicts (one per input need)

Contract § 1.3 band table
--------------------------
| Most severe need present         | Severity  | Items                         |
|----------------------------------|-----------|-------------------------------|
| open fault band p0               | p0        | ["booking", "grounding"] when |
|                                  |           | flag=True and not grounded    |
| open fault band attention        | attention | ["booking"]                   |
| OR agent2 finding at attention   |           |                               |
| open fault band soon             | soon      | ["booking"]                   |
| OR agent2 finding at soon        |           |                               |
| OR PM task overdue               |           |                               |
| PM task due (and nothing above)  | routine   | ["booking"]                   |

Severity ordering: p0 (0) > attention (1) > soon (2) > routine (3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity ordering — lower number = higher severity
# ---------------------------------------------------------------------------
_SEVERITY_ORDER: dict[str, int] = {
    "p0": 0,
    "attention": 1,
    "soon": 2,
    "routine": 3,
}

_BAND_TO_SEVERITY: dict[str, str] = {
    "p0": "p0",
    "attention": "attention",
    "soon": "soon",
    "routine": "routine",
}


# ---------------------------------------------------------------------------
# NeedsResult
# ---------------------------------------------------------------------------

@dataclass
class NeedsResult:
    """Result of classify_needs.

    severity : str — the most-severe need's severity band.
    items    : list[str] — ["booking"] or ["booking", "grounding"].
    needs    : list[dict] — one entry per need (fault, agent2_finding, pm_task).
    """
    severity: str
    items: List[str] = field(default_factory=list)
    needs: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# classify_needs
# ---------------------------------------------------------------------------

def classify_needs(
    open_faults: list,
    agent2_findings: list,
    pm_tasks: list,
    already_grounded: bool = False,
    fault_grounding_enabled: bool = False,
    model_hint: Optional[dict] = None,  # IGNORED — spec D9
    grounding_reader_fn: Optional[Callable] = None,
) -> NeedsResult:
    """Classify vehicle needs into severity and items (contract § 1.3).

    Parameters
    ----------
    open_faults:
        List of open faults, each a dict with at least ``code`` and ``band``.
    agent2_findings:
        List of Agent 2 findings, each with at least ``severity``.
    pm_tasks:
        List of PM tasks, each with at least ``task_code`` and ``status``
        (``"due"`` or ``"overdue"``).
    already_grounded:
        True when the caller already knows the vehicle has an open grounding.
        Ignored when ``grounding_reader_fn`` is provided.
    fault_grounding_enabled:
        Rollout flag (default False).  When False, no grounding item is produced
        even for p0 faults (spec D9).
    model_hint:
        Accepted for signature compatibility but NEVER read.  Spec D9: the model
        cannot change severity, grounding, or the needs list.
    grounding_reader_fn:
        Optional zero-arg callable returning bool (True = already grounded).
        When provided and ``fault_grounding_enabled`` is True, overrides
        ``already_grounded``.  Any exception from the reader is caught and
        treated as NOT grounded (D8 step 3 fail-closed semantics).
        Mutation guard: catch-and-set-True would suppress grounding items on
        error — the test_groundings_read_error_treated_as_not_grounded test
        catches that mutation.

    Returns
    -------
    NeedsResult
    """
    # ---- Apply grounding_reader_fn override (fail-closed) ----------------
    if grounding_reader_fn is not None and fault_grounding_enabled:
        try:
            result = grounding_reader_fn()
            # Support both (bool, error_count) tuple (from make_groundings_reader)
            # and plain bool (from legacy callers and test helpers).
            if isinstance(result, tuple):
                already_grounded = bool(result[0])
            else:
                already_grounded = bool(result)
        except Exception as exc:  # noqa: BLE001
            # Fail-closed: any read error → treat as not grounded so the
            # grounding item is written.  CMS POST is idempotent (D17).
            # Mutation: set True here → grounding item suppressed → fails.
            log.warning(
                "bands: groundings read error — treating as not grounded: %s", exc
            )
            already_grounded = False

    # ---- Build needs list (model_hint is silently ignored) ----------------
    # W3a: contract §1.6 uses 'severity_band', not 'band', on fault needs.
    # W2: each need carries a deterministic 'need_id' so callers (e.g. 4.1 model)
    #     can reference and defer individual needs (contract §1.7).
    needs: list[dict] = []
    for fault in open_faults:
        code = fault.get("code") or ""
        needs.append({
            "kind": "fault",
            "need_id": f"fault:{code}",        # W2: deterministic id
            "code": code,
            "severity_band": fault.get("band"),  # W3a: contract name
            "last_seen_at": fault.get("last_seen_at"),
            # W9: source so CMS can trace the need origin (contract §1.6)
            "source": "vehicle_fault_status",
            # D6: ADP source column ('generated'|'cms'|None if pre-column schema)
            "fault_source": fault.get("fault_source"),
        })
    for f in agent2_findings:
        fid = f.get("finding_id") or ""
        # W1: carry finding_kind so map_need_to_labor_op can key the config by
        #     the real kind (e.g. "health.battery") not the severity string.
        fkind = f.get("finding_kind") or ""
        raw_sev = f.get("severity")
        # W7 (design answer 4): Agent 2 cannot emit p0 (no directive); cap at attention.
        # Mutation guard: removing this cap → Agent 2 p0 reaches booking with
        # severity='p0' but no directive → test_agent2_p0_mapped_to_attention fails.
        sev_band = "attention" if raw_sev == "p0" else raw_sev
        needs.append({
            "kind": "agent2_finding",
            # G3F3.1 (a1 W4): stable per-condition id — uses finding_kind, not finding_id
            # (which changes on re-mint). Design answer 10: "a2:<finding_kind>".
            # Mutation guard: revert to f"a2:{fid}" → id changes on re-mint →
            # test_agent2_need_id_is_stable_across_remints fails.
            "need_id": f"a2:{fkind}",
            "finding_id": fid,
            "finding_kind": fkind,              # W1: e.g. "health.battery"
            "severity_band": sev_band,
            # W9: source so CMS can trace the need origin (contract §1.6)
            "source": "avx_findings",
        })
    for task in pm_tasks:
        task_code = task.get("task_code") or ""
        needs.append({
            "kind": "pm_task",
            "need_id": f"pm:{task_code}",       # W2: deterministic id
            "task_code": task_code,
            "status": task.get("status"),
            "severity_band": (                  # W3a: contract name for PM too
                "soon" if task.get("status") == "overdue" else "routine"
            ),
            # W9: source so CMS can trace the need origin (contract §1.6)
            "source": "pm_vehicle_status",
        })

    # ---- Determine most-severe severity -----------------------------------
    severity = _most_severe(open_faults, agent2_findings, pm_tasks)

    if severity is None:
        # No actionable needs → return routine with no items (caller should skip)
        return NeedsResult(severity="routine", items=[], needs=needs)

    # ---- Determine items --------------------------------------------------
    items = ["booking"]

    # W7: grounding requires an actual open fault in band 'p0', not merely
    #     severity=='p0' (which could come from an Agent 2 finding at p0 with no
    #     open fault code). An Agent 2 p0 without an open fault code is latent
    #     today; guard it explicitly so the grounding item always carries real
    #     fault_codes from the fault side.
    has_open_p0_faults = any(f.get("band") == "p0" for f in open_faults)
    if (
        has_open_p0_faults
        and fault_grounding_enabled
        and not already_grounded
    ):
        items.append("grounding")

    return NeedsResult(severity=severity, items=items, needs=needs)


def _most_severe(
    open_faults: list,
    agent2_findings: list,
    pm_tasks: list,
) -> Optional[str]:
    """Return the most severe severity string, or None when no needs are present."""
    candidates: list[int] = []

    for fault in open_faults:
        band = fault.get("band", "")
        if band in _SEVERITY_ORDER:
            candidates.append(_SEVERITY_ORDER[band])

    for f in agent2_findings:
        sev = f.get("severity", "")
        # W7 (design answer 4): Agent 2 cannot emit p0; cap at attention.
        if sev == "p0":
            sev = "attention"
        if sev in _SEVERITY_ORDER:
            candidates.append(_SEVERITY_ORDER[sev])

    for task in pm_tasks:
        status = task.get("status", "")
        if status == "overdue":
            candidates.append(_SEVERITY_ORDER["soon"])
        elif status == "due":
            candidates.append(_SEVERITY_ORDER["routine"])

    if not candidates:
        return None

    rank = min(candidates)
    # Reverse-look up the severity string from its rank
    for sev, r in _SEVERITY_ORDER.items():
        if r == rank:
            return sev
    return None  # unreachable
