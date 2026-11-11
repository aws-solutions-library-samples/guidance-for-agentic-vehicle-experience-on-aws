"""Finding-level writer for the FI quality investigation agent — Stage D task 3.3.

Provides:
  - ``AGENT_ID``, ``AGENT_VERSION``, ``FINDING_KIND`` constants
  - ``DailyCapExceeded`` exception
  - ``InvestigationStore`` — in-memory idempotency store for the daily cap
    and the one-live-investigation-per-pattern rule
  - ``build_investigation_finding()`` — builds a population-scoped
    ``quality.investigation`` Finding per contract §§ 2.3–2.8
  - ``_LIVE_STATUSES`` — the investigation statuses that block a new request
  - ``is_live_investigation()`` — the shared live predicate used by both
    ``InvestigationStore`` and the investigations route handler

The validator (task 4.2) and model-call runner (task 4.2) are NOT in this module.

Live-status rule (spec D10: "Start it again")
----------------------------------------------
An investigation whose status is in ``_LIVE_STATUSES`` blocks a new request
for the same pattern: the store returns the existing ``investigation_id``.
An investigation with status ``"failed"`` is NOT live: it admits a new request,
creating a fresh ``investigation_id``.

The statuses considered live are: ``queued``, ``running``, ``completed`` (< 7
days).  A ``"failed"`` investigation — where the model call exhausted retries
— is not live, so the engineer can "Start it again" (spec D10 failure
sentence).

Mapping to the DynamoDB status column (spec D10):
  queued   — request written, not yet picked up by the Tier 2 Lambda
  running  — Tier 2 Lambda is executing the job
  completed — job finished and the Finding is written
  failed   — job error; engineer may request a new investigation

Public API
----------
AGENT_ID       : str — "agent-fi-quality"
AGENT_VERSION  : str — "0.1.0"
FINDING_KIND   : str — "quality.investigation"
POPULATION_ID  : str — "meridian-motors"

DailyCapExceeded
    Raised by InvestigationStore.get_or_create when the daily cap is reached
    and no live investigation exists for the requested pattern.

InvestigationStore(investigations, *, day_count=0)
    get_or_create(pattern_id, requested_by) -> str
        Return the live investigation_id for pattern_id, or create a new one.

build_investigation_finding(investigation_id, pattern_id, bundle,
                            root_cause, options, computed_at, ...) -> dict
    Build a population-scoped quality.investigation Finding.

is_live_investigation(status, requested_at=None) -> bool
    Shared live predicate (spec D10).  Used by InvestigationStore and the
    investigations route handler.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from agents.tier2.writer import compute_finding_key

# ---------------------------------------------------------------------------
# Investigation id shape — shared between the investigations route handler and
# the Tier 2 runner's SQS consumer.
#
# The route creates investigation ids via ``str(uuid.uuid4())``; this regex
# is the single authoritative definition of that shape.  Both the GET handler
# (spec D10 / fix 3: reject lock# / cap# keys) and the SQS consumer (runner
# _run_investigation_sqs: treat any non-matching body as a failed item) import
# and use this constant — neither copies it locally.
#
# Mutation guard: widen the pattern (e.g. accept ``.*``) → the SQS consumer
# test that feeds a bare id validates that the shared pattern is checked.
# ---------------------------------------------------------------------------

#: Compiled UUID regex matching the format produced by ``str(uuid.uuid4())``.
#: Re-exported for the SQS consumer (runner._run_investigation_sqs) and the
#: route handler (lambdas/api-avx-investigations/handler.py).
INVESTIGATION_ID_RE: re.Pattern[str] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Agent identifier — "agent-fi-" prefix puts quality Findings under the
#: iOS route exclusion and push-broker skip (Stage C D13).
AGENT_ID: str = "agent-fi-quality"

#: Semver pinned per artifact write.
AGENT_VERSION: str = "0.1.0"

#: Finding kind for investigation Findings.
FINDING_KIND: str = "quality.investigation"

#: Population id for Meridian Motors (the deployment's maker id).
POPULATION_ID: str = "meridian-motors"

#: TTL in days (contract § 2.8).
_EXPIRY_DAYS: int = 7

#: Daily investigation cap (spec D10).
_DAILY_CAP: int = 20

#: Maximum age (seconds) for a ``running`` row to be considered live.
#: A row older than this limit means the Lambda timed out mid-job
#: (design answer 5: function timeout + 60 s margin; default 900+60=960).
#: Read from env so 4.3's stack and this code share one value.
#:
#: Mutation guard (test_stale_running_is_not_live):
#:   Setting this to 10**9 makes every running row look live → dead
#:   investigations block their patterns forever → test fails.
import os as _os
_STALE_RUNNING_AGE_SECONDS: int = int(_os.environ.get("TIER2_FUNCTION_TIMEOUT_SECONDS", "900")) + 60

#: Maximum age (seconds) for a ``queued`` row to be considered live.
#: A row older than this limit means the Lambda never picked it up
#: (default 2 h = 7 200 s, cycle-3 answer 4; 4.3 pins visibility timeout +
#: function timeout < this limit, e.g. tech.md's 5 400 + 900 = 6 300 < 7 200,
#: so a requeued message's redrive arrives while the row is still live).
#: Read from env so 4.3's stack and this code share one value.
#:
#: Mutation guard (test_stale_queued_is_not_live):
#:   Setting this to 10**9 makes every queued row look live → blocked forever.
_STALE_QUEUED_AGE_SECONDS: int = int(_os.environ.get("TIER2_STALE_QUEUED_AGE_SECONDS", "7200"))

#: Backward-compat alias used by the route handler import.
#: The route imports ``_STALE_AGE_SECONDS`` for its own pin test; it now equals
#: the queued bound (the larger of the two).
_STALE_AGE_SECONDS: int = _STALE_QUEUED_AGE_SECONDS

#: Investigation statuses that block a new request for the same pattern.
#: "failed" is NOT live: it admits a new investigation ("Start it again").
#: This set is the single authoritative definition of "live" for the
#: one-per-pattern rule.  The test test_failed_investigation_admits_new_request
#: pins this: treating "failed" as live would return the old id, not a new one.
#:
#: Valid DynamoDB statuses (spec D10): queued / running / completed / failed.
#: "queued"    — request written, not yet picked up by the Tier 2 Lambda.
#: "running"   — Tier 2 Lambda is executing the job.
#: "completed" — job finished and the Finding is written.
#: "failed"    — job error; admitted as a new request.
#:
#: Fix (task 3.4): the original set contained "pending" and "decided" which are
#: not valid DynamoDB statuses.  A "queued" investigation would not block a
#: repeat because "queued" was absent from the set.  The corrected set matches
#: the actual status column values the route writes and the job reads.
_LIVE_STATUSES: frozenset[str] = frozenset({
    "queued",
    "running",
    "completed",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DailyCapExceeded(Exception):
    """Raised when the daily investigation cap (20) is exhausted."""


# ---------------------------------------------------------------------------
# Shared live predicate (spec D10)
# ---------------------------------------------------------------------------

def is_live_investigation(
    status: str,
    requested_at: Optional[str] = None,
    *,
    _now: Optional[datetime] = None,
) -> bool:
    """Return True iff an investigation is live and blocks a repeat request.

    This is the **single authoritative predicate** for the one-live-investigation-
    per-pattern rule, shared by ``InvestigationStore`` (in-memory, Tier 2 job),
    the investigations route handler (DynamoDB pre-read and ConditionCheck), and
    the SQS runner (stale-``running`` detection on redelivery).

    Rules (spec D10 + G3-cycle1-b C1/C2 fix):
    - ``queued``    → live only if age < ``_STALE_AGE_SECONDS``; older rows are
                      dead (the Lambda timed out before the job started).
    - ``running``   → live only if age < ``_STALE_AGE_SECONDS``; older rows are
                      dead (the Lambda died mid-job).
    - ``completed`` → live if within 7 days of ``requested_at``; if no timestamp
                      is provided (in-memory store), treat as live.
    - ``failed``    → NOT live; admits a new request ("Start it again").
    - any other     → NOT live.

    The age limit for ``queued``/``running`` is chosen to be longer than two
    Lambda timeouts (15 min each) plus the SQS visibility timeout and the
    ``maxReceiveCount`` redrive delay.  A row younger than this bound may still
    have an active Lambda; older than this bound the job is certainly dead.

    Parameters
    ----------
    status:
        The investigation's status string.
    requested_at:
        ISO-8601 UTC timestamp used to measure age for ``queued`` rows.
    _now:
        Injected UTC datetime for testing; defaults to ``datetime.now(timezone.utc)``.

    Mutation guards
    ---------------
    test_failed_investigation_admits_new_request:
        Treating "failed" as live returns the old id; this fails the test.
    test_stale_queued_is_not_live:
        A queued row older than the limit must return False.
    test_stale_running_is_not_live:
        A running row older than the limit must return False.
    test_fresh_queued_is_live:
        A queued row within the limit must return True.
    """
    now = _now or datetime.now(timezone.utc)

    def _age_seconds(ts: Optional[str]) -> Optional[float]:
        if ts is None:
            return None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return (now - dt).total_seconds()
        except (ValueError, TypeError):
            return None

    if status in ("queued", "running"):
        age = _age_seconds(requested_at)
        if age is None:
            # No timestamp (in-memory store): treat as live (safe direction).
            return True
        # Use the appropriate bound: running rows are measured from started_at
        # (the caller passes the right timestamp); queued rows from requested_at.
        # Different bounds per design answer 5 (G3 review cycle 2):
        #   running: function timeout + margin (default 960 s)
        #   queued:  queue stale limit (default 3 600 s = 1 h)
        bound = (
            _STALE_RUNNING_AGE_SECONDS
            if status == "running"
            else _STALE_QUEUED_AGE_SECONDS
        )
        # Past the stale limit → dead.  Mutation guard: removing the age check
        # makes stale rows block new investigations for ever (review-b C1 /
        # review-a2 C3).  test_stale_queued_is_not_live and
        # test_stale_running_is_not_live catch it.
        return age < bound

    if status == "completed":
        if requested_at is None:
            # In-memory store: no timestamp — treat completed as live.
            return True
        age = _age_seconds(requested_at)
        if age is None:
            return True
        return age < _EXPIRY_DAYS * 24 * 3600

    return False  # "failed" or unknown status


# ---------------------------------------------------------------------------
# ISO-8601 helper
# ---------------------------------------------------------------------------

def _iso_add_days(iso: str, days: int) -> str:
    """Add *days* to an ISO-8601 UTC string."""
    import datetime
    dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt + datetime.timedelta(days=days)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Finding builder
# ---------------------------------------------------------------------------

def build_investigation_finding(
    *,
    investigation_id: str,
    pattern_id: str,
    bundle: dict[str, Any],
    root_cause: dict[str, Any],
    options: list[dict[str, Any]],
    computed_at: str,
    confidence: Optional[str] = None,
    inputs_hash: Optional[str] = None,
    detail: str = "",
    tokens_used: int = 0,
    cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Build a population-scoped quality.investigation Finding.

    Task 4.2: on the model path ``detail`` is the model's guarded reasoning (spec
    D11) and ``tokens_used`` / ``cost_usd`` are this investigation's model usage.

    Population-scoped: carries ``scope.population_id`` only; no ``fleet_id``
    at any level (spec D11 / contract § 2.1).

    Invariant (test_investigation_finding_has_no_fleet_id):
        The result must not contain ``fleet_id`` at any level.

    Parameters
    ----------
    investigation_id:
        UUID of the investigation (``subject_key`` and server-set parameter).
    pattern_id:
        ADP fault pattern id (server-set parameter).
    bundle:
        Investigation bundle from ``bundle.build_bundle()`` (no fleet-private
        fields at any depth).
    root_cause:
        Root-cause result from the model or fallback.  Carries:
        ``candidate_id``, ``failure_mode_id``, ``reasoning`` (optional,
        model text — stored but NOT forwarded to the thread writer),
        ``confidence``, ``diagnosis_source``, ``evidence_refs``.
    options:
        Full options evidence list from ``options.build_options_evidence()``.
        Each entry carries ``remedy_option_id``, ``remedy_type``,
        ``parts_cost_per_vehicle``, … ``recommended`` (bool), ``tradeoffs``.
    computed_at:
        ISO-8601 UTC string ("2026-10-05T06:00:00Z").
    confidence:
        Override confidence; defaults to ``root_cause.get("confidence",
        "low")``.
    inputs_hash:
        64-char hex SHA-256 of the input rows.  A 64-char "a" placeholder
        is used when absent (for tests and minimal callers).

    Returns
    -------
    dict
        quality.investigation Finding conforming to finding.schema.json.
    """
    pattern = bundle.get("pattern") or {}
    code_family = pattern.get("code_family", "")
    subject_key = investigation_id

    # ── Derive severity from root_cause and options ──────────────────────────
    failure_mode_id_val = root_cause.get("failure_mode_id", "unknown")
    diagnosis_source = root_cause.get("diagnosis_source", "rules")
    candidate_id = root_cause.get("candidate_id", "")

    recommended_opt = next(
        (o for o in options if o.get("recommended")), None
    )
    recommended_option_id = (
        recommended_opt["remedy_option_id"] if recommended_opt else None
    )
    remedy_type = (recommended_opt.get("remedy_type", "") if recommended_opt else "")

    if failure_mode_id_val == "unknown":
        severity = "routine"
    elif not recommended_opt or remedy_type == "monitor":
        severity = "routine"
    else:
        severity = "soon"

    # ── Headline (§ 2.4) ─────────────────────────────────────────────────────
    cause = _derive_cause(candidate_id, bundle)
    headline = f"Investigation of {code_family}: traces to {cause}"

    # ── confidence ───────────────────────────────────────────────────────────
    effective_confidence = (
        confidence
        or root_cause.get("confidence")
        or ("low" if diagnosis_source == "rules" else "medium")
    )

    # ── finding_key ──────────────────────────────────────────────────────────
    finding_key = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="population_id",
        scope_value=POPULATION_ID,
        finding_kind=FINDING_KIND,
        subject_key=subject_key,
    )

    # ── Evidence entries (§ 2.6) ─────────────────────────────────────────────
    evidence = _build_evidence(
        bundle=bundle,
        root_cause=root_cause,
        options=options,
        computed_at=computed_at,
        investigation_id=investigation_id,
    )

    # ── recommended_action (§ 2.7) ───────────────────────────────────────────
    recommended_action = {
        "action_kind": "record_engineering_decision",
        "target_system": "adp",
        "parameters": {
            "investigation_id": investigation_id,   # server-set
            "pattern_id": pattern_id,               # server-set
            "option_id": recommended_option_id,
            "notes": "",
        },
    }

    finding: dict[str, Any] = {
        "finding_id": f"FIND-{uuid.uuid4()}",
        "finding_key": finding_key,
        "agent_id": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "scope": {"population_id": POPULATION_ID},
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": _iso_add_days(computed_at, _EXPIRY_DAYS),
        "confidence": effective_confidence,
        "finding_kind": FINDING_KIND,
        "headline": headline[:120],
        "detail": detail,
        "evidence": evidence,
        "inputs_hash": inputs_hash or ("a" * 64),
        "tokens_used": tokens_used,
        "cost_usd": cost_usd,
        "audit_trail": [],
        "subject_key": subject_key,
        "recommended_action": recommended_action,
    }
    # No fleet_id anywhere in this dict (invariant).
    return finding


# ---------------------------------------------------------------------------
# Evidence builders
# ---------------------------------------------------------------------------

def _derive_cause(candidate_id: str, bundle: dict[str, Any]) -> str:
    """Derive the headline's cause phrase from the chosen candidate."""
    candidates = bundle.get("candidates") or []
    candidate = next(
        (c for c in candidates if c.get("candidate_id") == candidate_id),
        None,
    )
    if candidate is None:
        return "unknown"
    dimension = candidate.get("dimension", "")
    value = candidate.get("value", "")
    return f"{dimension} {value}".strip() or "unknown"


def _build_evidence(
    *,
    bundle: dict[str, Any],
    root_cause: dict[str, Any],
    options: list[dict[str, Any]],
    computed_at: str,
    investigation_id: str,
) -> list[dict[str, Any]]:
    """Build the evidence list for a quality.investigation Finding (§ 2.6)."""
    pattern = bundle.get("pattern") or {}
    candidates = bundle.get("candidates") or []
    failure_modes = bundle.get("failure_modes") or []
    affected_vins = bundle.get("affected_vins") or []
    controls = bundle.get("controls") or []

    evidence: list[dict[str, Any]] = []

    # quality.pattern
    # W4 fix: `affected_count` uses `vehicles_affected` (the authoritative total)
    # not `len(affected_vins)` (which is the sample cap).
    _veh_affected = pattern.get("vehicles_affected")
    _av_count_pattern = (
        int(_veh_affected)
        if _veh_affected is not None
        else len(affected_vins)
    )
    evidence.append({
        "kind": "quality.pattern",
        "source": "adp",
        "source_ref": pattern.get("pattern_id", ""),
        "value": {
            "pattern_id":       pattern.get("pattern_id", ""),
            "code_family":      pattern.get("code_family", ""),
            "dimension":        pattern.get("dimension", ""),
            "value":            pattern.get("value", ""),
            "ratio":            pattern.get("ratio", 0.0),
            "p_value":          pattern.get("p_value", 1.0),
            "method":           pattern.get("method", ""),
            "first_detected":   pattern.get("first_detected", ""),
            "trend":            pattern.get("trend", ""),
            "affected_count":   _av_count_pattern,
        },
        "computed_at": computed_at,
        "provenance": "live",
    })

    # quality.candidates
    evidence.append({
        "kind": "quality.candidates",
        "source": "adp",
        "source_ref": pattern.get("pattern_id", ""),
        "value": [
            {
                "candidate_id": c.get("candidate_id", ""),
                "dimension":    c.get("dimension", ""),
                "value":        c.get("value", ""),
                "ratio":        c.get("ratio", 0.0),
                "p_value":      c.get("p_value", 1.0),
                "rank":         c.get("rank", 1),
            }
            for c in candidates
        ],
        "computed_at": computed_at,
        "provenance": "live",
    })

    # quality.root_cause — carries candidate_id, failure_mode_id, evidence_refs,
    # diagnosis_source, confidence.  Model text (reasoning) is stored here but the
    # thread writer (_build_record_decision_payload) deliberately does not extract it.
    failure_mode_id_val = root_cause.get("failure_mode_id", "unknown")
    # W4 fix: include confidence in root_cause evidence (§ 2.6 requires it).
    evidence.append({
        "kind": "quality.root_cause",
        "source": "agent-fi-quality",
        "source_ref": investigation_id,
        "value": {
            "candidate_id":    root_cause.get("candidate_id", ""),
            "failure_mode_id": failure_mode_id_val,
            "evidence_refs":   root_cause.get("evidence_refs") or [],
            "diagnosis_source": root_cause.get("diagnosis_source", "rules"),
            "confidence":      root_cause.get("confidence", "low"),
            # reasoning is model text — present when the model ran, absent on fallback.
            **(_reasoning_kv(root_cause)),
        },
        "computed_at": computed_at,
        "provenance": "live",
    })

    # quality.failure_mode
    # W4 fix: when the chosen failure_mode_id isn't in the bundle, omit the
    # entry rather than silently naming failure_modes[0].
    if failure_modes:
        chosen_fm = next(
            (fm for fm in failure_modes
             if fm.get("failure_mode_id") == failure_mode_id_val),
            None,  # W4 fix: was failure_modes[0] which names wrong mode
        )
        if chosen_fm:
            evidence.append({
                "kind": "quality.failure_mode",
                "source": "adp",
                "source_ref": chosen_fm.get("failure_mode_id", ""),
                "value": {
                    "failure_mode_id": chosen_fm.get("failure_mode_id", ""),
                    "component":       chosen_fm.get("component", ""),
                    "mechanism":       chosen_fm.get("mechanism", ""),
                    "symptom_codes":   chosen_fm.get("symptom_codes") or [],
                },
                "computed_at": computed_at,
                "provenance": "live",
            })

    # quality.options — numeric fields from corpus; recommended flag from model/fallback.
    if options:
        evidence.append({
            "kind": "quality.options",
            "source": "adp",
            "source_ref": investigation_id,
            "value": options,
            "computed_at": computed_at,
            "provenance": "live",
        })

    # quality.affected_vins
    # W4 fix: count uses vehicles_affected (the authoritative total from fault_patterns),
    # sample entries are dicts with vin + build fields per § 2.6.
    # The configured cap is 50 (§ 2.6 "configured cap").
    if affected_vins or pattern.get("vehicles_affected"):
        _veh_affected = pattern.get("vehicles_affected")
        _av_count = (
            int(_veh_affected)
            if _veh_affected is not None
            else len(affected_vins)
        )
        _SAMPLE_CAP = 50
        evidence.append({
            "kind": "quality.affected_vins",
            "source": "adp",
            "source_ref": pattern.get("pattern_id", ""),
            "value": {
                "count": _av_count,
                # W4 fix: sample entries are dicts with build fields, not bare strings.
                "sample": [
                    {
                        "vin": v.get("vin", ""),
                        "model": v.get("model", ""),
                        "model_year": v.get("model_year", ""),
                        "plant": v.get("plant", ""),
                        "build_month": v.get("build_month", ""),
                        "component_lot": v.get("component_lot", ""),
                    }
                    for v in affected_vins[:_SAMPLE_CAP]
                    if isinstance(v, dict)
                ] + [
                    {"vin": v, "model": "", "model_year": "", "plant": "",
                     "build_month": "", "component_lot": ""}
                    for v in affected_vins[:_SAMPLE_CAP]
                    if isinstance(v, str)
                ][:_SAMPLE_CAP],
            },
            "computed_at": computed_at,
            "provenance": "live",
        })

    # quality.controls
    # W4 fix: controls sample entries are dicts with build fields, not bare strings.
    _SAMPLE_CAP = 50
    if controls:
        evidence.append({
            "kind": "quality.controls",
            "source": "adp",
            "source_ref": pattern.get("pattern_id", ""),
            "value": {
                "count": len(controls),
                "sample": [
                    {
                        "vin": v.get("vin", ""),
                        "model": v.get("model", ""),
                        "model_year": v.get("model_year", ""),
                        "plant": v.get("plant", ""),
                        "build_month": v.get("build_month", ""),
                        "component_lot": v.get("component_lot", ""),
                    }
                    for v in controls[:_SAMPLE_CAP]
                    if isinstance(v, dict)
                ],
            },
            "computed_at": computed_at,
            "provenance": "live",
        })

    # quality.diagnosis_source
    diagnosis_source = root_cause.get("diagnosis_source", "rules")
    evidence.append({
        "kind": "quality.diagnosis_source",
        "source": "agent-fi-quality",
        "source_ref": investigation_id,
        "value": {"diagnosis_source": diagnosis_source},
        "computed_at": computed_at,
        "provenance": "live",
    })

    return evidence


def _reasoning_kv(root_cause: dict[str, Any]) -> dict[str, Any]:
    """Return {reasoning: ...} when model reasoning is present, else {}."""
    reasoning = root_cause.get("reasoning")
    if reasoning and isinstance(reasoning, str):
        return {"reasoning": reasoning[:1200]}
    return {}
