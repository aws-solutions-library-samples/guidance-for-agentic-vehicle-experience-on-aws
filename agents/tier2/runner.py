"""
Substrate-agnostic entrypoint the EventBridge Scheduler invokes — spec T3.2.

Substrate: **Plan B** (Lambda + Strands + Bedrock), per ``decisions.md``
§ 2026-08-03.  The substrate decision is *confined to this wrapper*: the
reasoning code (``agents/health/``) is substrate-agnostic, and a later
migration to Plan A (AgentCore managed harness) is a one-file swap here —
nothing else changes.

Invocation shape (Lambda handler):

    def lambda_handler(event: dict, context) -> dict

EventBridge Scheduler passes a freeform JSON payload via the Target's
``Input`` field.  The runner reads:

    event["stage"]           str  — "staging" | "prod"  (required)
    event.get("vins")        list[str] | None  — explicit VIN batch; if absent
                             selection resolves the batch via the cursor mechanism
    event.get("component")   str | None — default "battery" (S1-only)

**Batch, never per-VIN fan-out** (spec § D2).  One invocation processes a
batch; the EventBridge Scheduler has one rule per stage and fires once daily.
A per-VIN schedule at fleet scale is a cost and quota problem, not a design.

**Selection** (spec § D3, 2026-08-10): when ``vins`` is not provided, the runner
calls ``selection.resolve_batch(source, cursor_store, component, limit)`` to
determine the concrete VIN list for this run.  The cursor persists across runs,
advancing the window through the priority pool.  An explicitly-passed ``vins``
argument bypasses selection entirely — the cursor is not read or written.

**Per-component batch limits** (spec § D4, amended 2026-08-10): the batch size
differs across components because rows-per-VIN varies 224× (tires 240 rows/VIN,
battery 100, brakes ~1).  A single global cap sized for the worst component
(tires) would cap brakes at 156 VINs/night when brakes could process 2,000
within the same row ceiling.  Three separate env vars, one per component:
``TIER2_MAX_VINS_TIRES``, ``TIER2_MAX_VINS_BATTERY``, ``TIER2_MAX_VINS_BRAKES``.
There is no global fallback that applies one number to all three — that is the
specific defect spec § D4 rejects.

Budget cap (spec § D6) is honoured **in the loop**.  On breach, ``BudgetExceeded``
carries ``exc.partial_finding`` — accessed programmatically, *never* via ``%r``
or ``repr(exc)`` because ``repr=False`` was set on that field to prevent VIN
leakage into logs.  A capped run writes a well-formed partial artifact and
emits an abort record rather than discarding all work.

S1 wires the battery component only (tires are now unblocked as of 2026-08-04 — see
``agents.health.adp_tire_source.AdpTireSource``).  Tires are structurally
supported; the gate re-opened when ADP deployed the product.

Injectable interfaces for unit-testability without AWS:
  - ``source``       — any object with ``fetch_findings(vins)`` and
                       ``select_vins(cursor, limit)``; defaults to component source
  - ``writer``       — any object with a ``put(artifact: dict) -> None`` method;
                       defaults to ``ArtifactWriter`` for production use
  - ``cursor_store`` — any object with ``get_cursor`` / ``put_cursor`` methods;
                       defaults to a DynamoDB-backed store in production
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
from typing import Any, Callable, Optional, Protocol

from agents.tier2.budget import BudgetCap, BudgetExceeded
from agents.tier2.contract import AGENT_ID_BY_COMPONENT as _contract_agent_ids, VALID_COMPONENTS
from agents.tier2.observability import RunObserver, emit_metrics, emit_run_record_heartbeat
from agents.tier2.owner_resolver import AssignedDriverDirectory
from agents.tier2.owner_resolver import StandingFacts
from agents.tier2.owner_resolver import mask_vin as _mask_vin
from agents.tier2.owner_resolver import resolve_standing_facts
from agents.tier2.snapshot_freshness import (
    read_snapshot_dates as _read_snapshot_dates,
    require_fresh as _require_fresh,
    StaleSnapshotError,
    SNAPSHOT_TABLES as _SNAPSHOT_TABLES,
    empty_keys as _empty_keys,
)

log = logging.getLogger(__name__)


def _assigned_driver_directory(stage: str, *, ddb_client: Any) -> AssignedDriverDirectory:
    """The run's vehicle → assigned-driver directory (issue 2026-10-07, owner_id).

    One per Lambda invocation, never module-scoped: it caches the drivers table for
    the run, so a module-level instance would serve a reassigned driver until the
    container recycled.

    ``CMS_USER_POOL_ID`` is the CMS pool (the stack passes ``voiceAuthUserPoolId``).
    Unset or blank leaves owner resolution off: every Finding is written with no
    ``owner_id``, which denies rather than guesses. The Cognito client takes its
    region from the pool id (``us-west-2_…``), which is where the pool lives.
    """
    import boto3

    pool_id = os.environ.get("CMS_USER_POOL_ID", "").strip()
    drivers_table = os.environ.get("CMS_DRIVERS_TABLE", f"cms-{stage}-storage-drivers")
    if not pool_id:
        log.warning("owner_resolver: event=driver_directory_unconfigured reason=no_cms_user_pool_id")
    cognito_client = (
        boto3.client("cognito-idp", region_name=pool_id.split("_", 1)[0]) if pool_id else None
    )
    return AssignedDriverDirectory(
        ddb_client=ddb_client,
        drivers_table=drivers_table,
        cognito_client=cognito_client,
        user_pool_id=pool_id,
    )

# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------

#: Default component for production runs.
_DEFAULT_COMPONENT = "battery"

#: Default wall-clock budget.  Operator can override via Lambda env var.
_DEFAULT_MAX_WALL_SECONDS = float(os.environ.get("TIER2_MAX_WALL_SECONDS", "540.0"))

#: Legacy global token cap — read only by callers that pass an explicit
#: ``max_tokens`` kwarg (unit tests mostly).  Production invocations
#: resolve the cap per-component via :func:`_component_max_tokens` instead:
#: tires at 156 VINs × ~450 tokens/VIN comfortably exceeds any 50,000-token
#: shared cap, which is what aborted the 2026-10-04 tires and brakes runs
#: (69,564 and 65,555 tokens against a 50,000 limit).
#:
#: Keeping this constant lets existing tests that pass ``max_tokens=...``
#: continue to work; the Lambda handler explicitly selects the per-component
#: value, so the number here does not affect production.
_DEFAULT_MAX_TOKENS = int(os.environ.get("TIER2_MAX_TOKENS", "100000"))

# Per-component VIN caps (spec § D4, amended 2026-08-10).
#
# Rows-per-VIN varies 224× across products (tires 240, battery 100, brakes ~1),
# so a single global cap sized for the worst component (tires at 156) would
# artificially limit brakes to 156/night when brakes could safely process 2,000
# within the same 50,000-row ceiling.  Three separate env vars, one per component.
#
# There is NO single shared fallback that maps to all three — that is the specific
# defect spec § D4 rejects, and test_tier2_alarm_wiring_guard.py pins it.
#
# Binding constraints (per Group 1 measurements, 2026-08-10):
#   tires  → 156  rows-bound: 156 × 240 = 37,440  (25.1% headroom under 50,000)
#   battery→ 600  not rows-bound since the 2026-10-04 dedup (one row per VIN, <= 600 rows);
#                  raised from 375 by stage D 6.3 so the ~4,143-VIN managed pool is covered in
#                  7 nights; its 600 s wall is measured by 6.3 item 1 (375 VINs took ~45 s)
#   brakes → 2000 write-path-bound: ~2,140 rows (well under ceiling); bounded by
#                  sequential conditional PutItem + agent_fn calls within 300s
_MAX_VINS_TIRES   = int(os.environ.get("TIER2_MAX_VINS_TIRES",   "156"))
_MAX_VINS_BATTERY = int(os.environ.get("TIER2_MAX_VINS_BATTERY", "600"))
_MAX_VINS_BRAKES  = int(os.environ.get("TIER2_MAX_VINS_BRAKES",  "2000"))

# Per-component token caps (defect D3 fix, 2026-10-04 — resized in review
# cycle 1 after measurement correction).
#
# Spec § D6 defines a token cap "per run" to bound cost; the prior shape
# applied a single ``TIER2_MAX_TOKENS`` to every component.  Per-VIN token
# cost measured on the stateless path (C1 + C2 fixed) is approximately:
#   SYSTEM_PROMPT ~1,083 tokens + user prompt ~200 + reply ~150 ≈ 1,445
# tokens per LLM-called VIN.  The 2026-10-04 figures the pre-fix review
# cycle 1 sizing rested on (69,564 and 65,555) were inflated meter
# artifacts, not real usage — see review-cycle1.md Critical #2.
#
# After the needs-judgment filter (C5), routine components do not reach
# the LLM, so each component's cap is sized for its "worst case" fleet
# composition: the proportion of VINs that genuinely need judgment.
#
# Mapping (defaults — operator can override via env var):
#   tires:   156 VINs × 1,500 tokens × 1.5 headroom ≈ 350,000
#            Even if every VIN is replacement-flagged (worst case), the
#            cap covers the full batch with 50% drift room.
#   battery: 375 VINs × 1,500 tokens × 1.5 headroom ≈ 850,000
#            Covers full batch even if every battery is degraded.
#            Unchanged when the batch went to 600 (stage D 6.3): the shared
#            TIER2_MAX_LLM_CALLS_PER_BATCH=100 breaker in _default_agent_fn bounds any
#            batch at ~100 × 1,500 = 150,000 tokens, so 850,000 still covers it.
#   brakes:  2,000-VIN batch; C4 bypasses every C0161 record (both
#            active and resolved), and C5 skips routine (resolved,
#            no-DTC) findings.  Review cycle 3 (2026-10-04) corrected
#            the "1-5% active rate" premise cycles 1-2 rested on:
#            selection only picks ``outcome IN ('parts_pending',
#            'follow_up_required')``, so EVERY selected brake VIN is
#            non-routine and the C5 needs-judgment filter removes none
#            of them.  The 100-call circuit breaker (C5) therefore
#            fires on every nightly brakes run.  Sizing: 100 LLM-called
#            VINs × 1,500 tokens × 2 headroom = 300,000.  Combined with
#            the TIER2_MAX_LLM_CALLS_PER_BATCH=100 circuit breaker, the
#            cap is also an upper bound: 100 calls × 1,500 tokens =
#            150,000, so 300,000 doubles the circuit-breaker ceiling.
#            Wall time, not tokens, is the binding dimension for brakes.
#
# There is no shared fallback — a new component must get its own cap
# (same policy as ``_selection_limit_for_component``).
_MAX_TOKENS_TIRES   = int(os.environ.get("TIER2_MAX_TOKENS_TIRES",   "350000"))
_MAX_TOKENS_BATTERY = int(os.environ.get("TIER2_MAX_TOKENS_BATTERY", "850000"))
_MAX_TOKENS_BRAKES  = int(os.environ.get("TIER2_MAX_TOKENS_BRAKES",  "300000"))

# Per-component VIN caps for the fleet-health service component (spec D8, task 3.8).
# ``service`` selects managed VINs first (managed-first selection) and is bounded by the
# same row-count ceiling as the other ADP components.  Default 500 covers a typical
# managed fleet at ~6 needs/VIN well under the 50,000-row ceiling.
# ``investigation`` is SQS-triggered, not selection-based; the cap is a no-op guard so
# the component is present in both per-component helpers without a dead branch.
_MAX_VINS_SERVICE      = int(os.environ.get("TIER2_MAX_VINS_SERVICE",      "500"))
_MAX_VINS_INVESTIGATION = int(os.environ.get("TIER2_MAX_VINS_INVESTIGATION",  "0"))

# Per-component token budgets (spec D8, task 3.8).
# ``service`` calls the model only for the service-plan validator (task 4.1); the rules
# path (this task) is zero-cost.  Budget at 500 VINs × 1,500 tokens × 1.5 headroom.
# ``investigation`` has its own Bedrock calls (task 4.2); budget at 2 calls × 50,000 tokens.
_MAX_TOKENS_SERVICE      = int(os.environ.get("TIER2_MAX_TOKENS_SERVICE",      "1125000"))
_MAX_TOKENS_INVESTIGATION = int(os.environ.get("TIER2_MAX_TOKENS_INVESTIGATION", "100000"))

#: Minimum remaining Lambda time (seconds) required to START a new investigation record.
#: Below this threshold the record is reported as a batch item failure rather than started.
#: Two model calls × 150 s MODEL_CALL_RESERVE_S + Athena bundle reads.  (task 4.3 D10)
#: Mutation guard: lowering to 0 → TestDeadlineBoundary.test_below_margin_record_fails passes
#: with remaining_ms=299_000, which must fail.
_INVESTIGATION_START_MARGIN_S = 300.0

#: Athena result-page ceiling.  Exceeding this raises AthenaResultTooLargeError
#: rather than returning a silently-truncated set (spec Fix Group 6a, T6a.1).
_DEFAULT_MAX_ROWS = int(os.environ.get("TIER2_MAX_ROWS_PER_QUERY", "50000"))


def _selection_limit_for_component(component: str) -> int:
    """Return the per-component VIN batch limit for selection.

    Spec § D4 (amended 2026-08-10): the batch size is per-component because
    rows-per-VIN varies 224× across products.  There is no global fallback —
    falling through to a shared default would silently reinstate the defect
    this spec fixes.

    Raises
    ------
    ValueError
        If ``component`` is not a recognised member of ``VALID_COMPONENTS``.
        This surfaces a missing branch at startup rather than silently selecting
        the wrong number of VINs.
    """
    if component == "tires":
        return _MAX_VINS_TIRES
    if component == "battery":
        return _MAX_VINS_BATTERY
    if component == "brakes":
        return _MAX_VINS_BRAKES
    if component == "service":
        return _MAX_VINS_SERVICE
    if component == "investigation":
        # SQS-driven; no Athena selection.  Return the env-var value (0 by default)
        # as a non-operational guard so a new component does not fall to the raise.
        return _MAX_VINS_INVESTIGATION
    # Guard: if a new component is added to VALID_COMPONENTS without a corresponding
    # limit here, the run fails loudly rather than defaulting to any shared number.
    raise ValueError(
        f"No per-component VIN batch limit defined for component {component!r}. "
        f"Add a TIER2_MAX_VINS_{{COMPONENT}} env var and a branch in "
        f"_selection_limit_for_component().  Valid components: {sorted(VALID_COMPONENTS)}."
    )


def _component_max_tokens(component: str) -> int:
    """Return the per-component token budget for one run.

    Defect D3 fix (2026-10-04): the pre-fix shape applied a single 50,000-token
    cap to every component.  The two early figures that motivated the resize
    (69,564 tokens on 8 tires VINs; 65,555 on 9 brakes VINs) were later shown
    to be ~75x-inflated meter artifacts, not actual usage — the shared Agent
    was accumulating lifetime-cumulative token counts (review cycle-1 C2).
    After C1 + C2 the measured per-call cost on the stateless path is
    SYSTEM_PROMPT ~1,083 + user prompt ~200 + reply ~150 ≈ 1,445 tokens.
    Caps are sized to the per-component batch, with headroom for drift and
    the C5 circuit-breaker ceiling (100 LLM calls * ~1,500 tokens).
    One env var per component mirrors the per-component VIN cap
    (``_selection_limit_for_component``).

    There is no shared fallback — a new component must declare its own cap.
    A missing branch surfaces at startup rather than silently applying the
    wrong number.

    Raises
    ------
    ValueError
        If ``component`` is not a recognised member of ``VALID_COMPONENTS``.
    """
    if component == "tires":
        return _MAX_TOKENS_TIRES
    if component == "battery":
        return _MAX_TOKENS_BATTERY
    if component == "brakes":
        return _MAX_TOKENS_BRAKES
    if component == "service":
        return _MAX_TOKENS_SERVICE
    if component == "investigation":
        return _MAX_TOKENS_INVESTIGATION
    raise ValueError(
        f"No per-component token budget defined for component {component!r}. "
        f"Add a TIER2_MAX_TOKENS_{{COMPONENT}} env var and a branch in "
        f"_component_max_tokens().  Valid components: {sorted(VALID_COMPONENTS)}."
    )

#: Default TTL: 8 days from now (in seconds).  Superseded findings age out
#: rather than accumulating — spec § D4.
_TTL_SECONDS = 8 * 24 * 60 * 60

#: Agent version tag, embedded in every artifact.
AGENT_VERSION = "tier2-health-v0.1.0"

#: Agent ID for health run records (contract § 8, spec D10).
#: CMS joins the ``agent_component`` pk on this value when rendering "last ran at".
#: Derived from ``contract.AGENT_ID_BY_COMPONENT`` (test-pinned, call 57).
HEALTH_AGENT_ID: str = _contract_agent_ids["battery"]

#: Agent ID for the FI cost component's run records (``agent-fi-cost#cost``, contract § 8;
#: spec 2026-10-02-fi-cost-stage-b D6).  Equal to ``agents.fi_cost.finding_keys.AGENT_ID``
#: (test-pinned); derived from the map so a rename propagates everywhere (call 57).
FI_COST_AGENT_ID: str = _contract_agent_ids["cost"]

#: Recall/warranty agent id (test-pinned); derived from the map (call 57).
FI_RECALL_WARRANTY_AGENT_ID: str = _contract_agent_ids["recall"]

#: Default model id for recall/warranty model calls.
_FI_RW_DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"

#: Bedrock client timeouts for recall/warranty — same as cost (brief S4).
_FI_RW_BEDROCK_READ_TIMEOUT_S = 30
_FI_RW_BEDROCK_CONNECT_TIMEOUT_S = 5

#: Max model-call caps (0–500) from env; defaults mirror fi_recall_warranty.runner.
_FI_RECALL_DEFAULT_CAP = 60
_FI_WARRANTY_DEFAULT_CAP = 30
_FI_RW_CAP_MAX = 500

#: All possible recall evidence kinds (constant; test-pinned against build_recall_evidence).
#: Includes recall.confirming_signal (only present on CONFIRMED items) so the validator
#: always accepts a valid reference regardless of match_basis.
_RECALL_EVIDENCE_KINDS: list[str] = [
    "recall.campaign",
    "recall.rule",
    "recall.match_basis",
    "recall.confirming_signal",
    "recall.item",
    "recall.rooftops",
    "recall.position",
    "vehicle.open_items",
    "recall.plan",
    "recall.diagnosis_source",
]


def _allowed_urgencies_for_severity(severity: str) -> list[str]:
    """Urgencies the validator accepts for a given recall band severity (call 49)."""
    if severity == "p0":
        return ["this_week"]
    if severity == "attention":
        return ["this_week", "this_month"]
    return ["this_week", "this_month", "next_visit"]


def _parse_rw_cap(raw: str, *, source: str) -> int:
    """Parse a recall/warranty model-call cap env-var value; raise on malformed."""
    stripped = raw.strip()
    if not stripped.lstrip("0123456789") == "" or stripped == "":
        raise ValueError(f"{source}: must be an integer 0–{_FI_RW_CAP_MAX}")
    val = int(stripped)
    if not 0 <= val <= _FI_RW_CAP_MAX:
        raise ValueError(f"{source}: must be an integer 0–{_FI_RW_CAP_MAX}")
    return val

#: Fleet-health agent id (spec 2026-10-05-fi-fleet-health-stage-d D8 / task 3.8).
#: Equal to ``agents.fi_fleet_health.write.AGENT_ID`` (test-pinned); derived from the
#: contract map (call 57), so the runner imports nothing from fi_fleet_health at load.
FI_FLEET_HEALTH_AGENT_ID: str = _contract_agent_ids["service"]

#: Quality/investigation agent id (spec 2026-10-05-fi-fleet-health-stage-d D10 / task 3.8).
#: Equal to ``agents.fi_quality.write.AGENT_ID`` (test-pinned); derived from the map.
FI_QUALITY_AGENT_ID: str = _contract_agent_ids["investigation"]

#: Rebalancing agent id (spec 2026-10-06-fi-rebalancing-stage-e G4-D10).
#: Derived from the contract map; run records keyed ``agent-fi-rebalancing#rebalance``.
FI_REBALANCING_AGENT_ID: str = _contract_agent_ids["rebalance"]
# Upper bound for FI_REBAL_MAX_MODEL_CALLS: values above are clamped (T1.3 item 9).
# Module-level so the scanner in test_policy_no_literals does not flag it as a
# policy-value literal inside _run_rebalance_from_env.
_REBAL_MAX_MODEL_CALLS_CEILING: int = 100

# ---------------------------------------------------------------------------
# Derived semver for the AVX Finding schema
# ---------------------------------------------------------------------------
# The AVX Finding schema requires ``agent_version`` to match
# ``^[0-9]+\.[0-9]+\.[0-9]+$`` (plain semver, no prefix).  AGENT_VERSION
# carries a ``tier2-health-v`` prefix so that the legacy health-findings
# consumers can identify the producer; we extract the numeric part here.
#
# This extraction RAISES AT IMPORT if it cannot parse, so a mis-formed
# AGENT_VERSION is a hard startup failure rather than a silent per-VIN
# exception swallowed inside an ``except Exception`` block.  That
# per-VIN swallow is the exact defect this fixes (Critical C1, Fix Group 17
# review-group16-cycle1.md).
#
# Mutation guard: change the regex to require a prefix → ImportError at
# startup catches it.  Use a value like "9.9.9" → tests asserting the
# semver tracks AGENT_VERSION fail.
import re as _re

def _extract_semver(version_tag: str) -> str:
    """Extract the semver portion from a version tag like 'tier2-health-v0.1.0'.

    Raises
    ------
    ValueError
        If the tag does not contain a plain ``MAJOR.MINOR.PATCH`` suffix.
        Raised at module import time so a mis-formed tag is a hard failure,
        not a per-VIN exception swallowed inside a try/except.
    """
    _match = _re.search(r"(\d+\.\d+\.\d+)$", version_tag)
    if not _match:
        raise ValueError(
            f"Cannot derive a semver from AGENT_VERSION={version_tag!r}. "
            "The tag must end with MAJOR.MINOR.PATCH (e.g. 'tier2-health-v0.1.0'). "
            "Fix AGENT_VERSION or update the semver extraction regex."
        )
    return _match.group(1)

#: Plain semver consumed by the AVX Finding schema (``^[0-9]+\\.[0-9]+\\.[0-9]+$``).
#: Derived from AGENT_VERSION at import time — never falls back silently.
AVX_AGENT_VERSION: str = _extract_semver(AGENT_VERSION)


# ---------------------------------------------------------------------------
# Source / writer protocols (for injection and double-testing)
# ---------------------------------------------------------------------------


class FindingSource(Protocol):
    """Duck-type protocol for a component data-source.

    Implementations: ``agents.health.adp_battery_source.AdpBatterySource``,
    ``agents.health.adp_tire_source.AdpTireSource``, and
    ``agents.health.adp_brake_source.AdpBrakeSource``.
    """

    def fetch_findings(self, vins: Optional[list[str]]) -> list[dict[str, Any]]:
        """Return normalised ADP rows for the given VIN batch (or all VINs if None)."""
        ...

    def select_vins(
        self, *, cursor: Optional[str], limit: int
    ) -> tuple[list[str], Optional[str]]:
        """Return up to *limit* VINs after *cursor*, plus the next cursor value.

        Used by ``selection.resolve_batch`` — not called directly from the runner.
        """
        ...


class ArtifactWriterProtocol(Protocol):
    """Duck-type protocol for the DynamoDB artifact writer.

    **Addendum D2 (2026-09-18)**: the primary write path for Findings is
    :meth:`write_finding`, which implements the four-step GSI-query / in-place-update
    algorithm.  :meth:`put` is retained for cursor-state and partial-artifact writes
    that do not go through the D2 algorithm.
    """

    def write_finding(
        self,
        finding: dict[str, Any],
        *,
        utc_now_fn: Any = None,
        standing: Any = None,
    ) -> dict[str, Any]:
        """Write a Finding using the D2 in-place-update algorithm.

        Returns ``{"action": "put" | "update" | "noop", "finding_id": str}``.

        Both ``run_agent()`` (deterministic path) and ``run_health_agent()``
        (Strands path) route their artifact writes through this method — they
        produce ``artifact_fields`` dicts; the runner assembles a D2-conforming
        Finding and calls this.

        Addendum D2 algorithm:
        1. Query ``finding_key`` GSI for a live (non-expired) row.
        2. If live row exists → ``UpdateItem`` (preserve ``finding_id``).
        3. If no live row → ``PutItem`` (new ``finding_id``).
        Retry-safety (same ``inputs_hash`` within one execution) is a no-op.
        """
        ...

    def put(self, artifact: dict[str, Any]) -> bool:
        """Unconditionally write *artifact*.

        D2 note: under Addendum D2 this is called by the D2 algorithm after
        the write decision is made.  No ConditionExpression is set.
        Returns True always.
        """
        ...

    def put_raw(self, item: dict[str, Any]) -> None:
        """Unconditionally write *item* (used for cursor state, not artifacts)."""
        ...


class CursorStoreProtocol(Protocol):
    """Duck-type protocol for per-component cursor persistence.

    The production implementation wraps ``ArtifactWriter.put_raw`` so the cursor
    lives in the existing ``vsa-{stage}-health-findings`` table under
    ``pk="CURSOR#{component}", sk="STATE"``.  Tests inject a simple in-memory double.
    """

    def get_cursor(self, component: str) -> Optional[dict[str, Any]]:
        """Return the stored cursor item for *component*, or None if absent."""
        ...

    def put_cursor(self, component: str, item: dict[str, Any]) -> None:
        """Persist *item* as the cursor for *component* (unconditional overwrite)."""
        ...


# ---------------------------------------------------------------------------
# Core run logic (injectable source + writer for unit tests)
# ---------------------------------------------------------------------------


def run(
    stage: str,
    *,
    source: FindingSource,
    writer: ArtifactWriterProtocol,
    vins: Optional[list[str]] = None,
    component: str = _DEFAULT_COMPONENT,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    max_wall_seconds: float = _DEFAULT_MAX_WALL_SECONDS,
    agent_fn: Optional[Callable[[str, list[dict]], dict]] = None,
    run_id: Optional[str] = None,
    cursor_store: Optional[CursorStoreProtocol] = None,
    cloudwatch_client: Optional[Any] = None,
    avx_writer: Optional[ArtifactWriterProtocol] = None,
    owner_resolver: Optional[Callable[[str], Any]] = None,
    run_record_writer: Optional[Callable] = None,
) -> dict[str, Any]:
    """Execute one Tier 2 health-agent run and return a summary dict.

    This function is the *substrate-agnostic* core.  The Lambda handler
    ``lambda_handler`` calls it after wiring the real source and writer;
    unit tests call it with injected doubles so no AWS access is needed.

    Parameters
    ----------
    stage:
        Deployment stage, e.g. ``"staging"`` or ``"prod"``.
    source:
        A :class:`FindingSource` that fetches ADP rows for the component.
        Must implement ``fetch_findings(vins)`` and ``select_vins(cursor, limit)``.
    writer:
        An :class:`ArtifactWriterProtocol` that persists artifacts.
        Must implement ``put(artifact)`` and ``put_raw(item)``.
    vins:
        Explicit VIN batch.  If provided, selection is bypassed entirely and the
        cursor is not read or written.  If ``None``, ``cursor_store`` and the
        per-component batch limit are used to resolve the batch via selection.
    component:
        Health component — ``"battery"``, ``"tires"``, or ``"brakes"``.
        Must be a member of ``contract.VALID_COMPONENTS``.
    max_tokens:
        Hard token ceiling for this run.  Defaults to the env-var value.
    max_wall_seconds:
        Hard wall-clock ceiling in seconds.  Defaults to the env-var value.
    agent_fn:
        Callable ``(vin, rows) -> artifact_fields_dict`` that performs judgment.
        In production this is wired to the Strands-based health agent.  In unit
        tests it can be a simple lambda.  If ``None``, the production agent is
        imported lazily to keep the Lambda cold-start shallow when testing.
    run_id:
        Stable identifier for this invocation — used in the observability
        record.  Defaults to a UTC ISO timestamp.
    cursor_store:
        Injected cursor-store for selection.  Required when ``vins`` is ``None``
        (selection path).  If ``None`` and ``vins`` is also ``None``, a
        ``_NullCursorStore`` is used that starts from the beginning of the pool
        every run (safe but non-advancing — for tests that don't exercise selection).
    cloudwatch_client:
        Injected boto3 CloudWatch client (or any object with ``put_metric_data``).
        When provided, three metrics (``VinsSelected``, ``ArtifactsWritten``,
        ``DuplicatesSkipped``) are emitted in one ``PutMetricData`` call at run end.
        When ``None``, metric emission is skipped (safe default for tests and for
        any caller that does not need metrics).  A ``PutMetricData`` failure is
        **logged and swallowed** — it never fails the run.
    avx_writer:
        Optional :class:`ArtifactWriterProtocol` pointed at the AVX findings
        table (``vsa-{stage}-avx-findings``).  When provided, the runner
        additionally calls ``avx_writer.write_finding()`` after each successful
        legacy write, using the T16.1 shape transform.  Failures are caught and
        logged as a distinct ``avx_write_failed`` signal — the nightly health run
        never aborts due to an AVX write error.  When ``None``, the AVX write is
        silently skipped (backward-compatible default for existing tests that do
        not exercise the AVX path).

    Returns
    -------
    dict
        Run summary: ``status`` (``"completed"`` or ``"aborted"``), ``stage``,
        ``component``, ``artifacts_written``, ``rows_read``, ``tokens_used``,
        ``abort_reason`` (``None`` or string), ``vins_selected`` (int — number of
        VINs resolved by selection, 0 when an explicit ``vins`` list was passed).
    """
    if run_id is None:
        run_id = _utc_now()

    obs = RunObserver(run_id=run_id, stage=stage, component=component)
    cap = BudgetCap(max_tokens=max_tokens, max_wall_seconds=max_wall_seconds)

    # Lazy-import the production agent only when no test double is provided.
    if agent_fn is None:
        # W3 cycle-2 (2026-10-04): thread the BudgetCap through so the Strands
        # agent_fn's circuit breaker is time-aware, not call-count-only.  At
        # the observed 4-6.6 s per Bedrock call, the 100-call ceiling alone
        # could blow a 600 s wall budget on a slow run.
        agent_fn = _default_agent_fn(cap)

    # --- resolve VIN batch --------------------------------------------------
    # When the caller passes an explicit vins list, bypass selection entirely.
    # The cursor is NOT read or written.  This preserves the pre-selection
    # behavior so all existing tests pass unchanged.
    #
    # When vins is None, call selection.resolve_batch() to determine the concrete
    # batch for this run.  This advances the cursor and respects the priority pool.
    # The resolved batch is then passed to source.fetch_findings() — reusing the
    # explicit-VIN path rather than duplicating the fetch logic.
    vins_selected: int = 0  # recorded in summary for Group 4 VinsSelected metric
    _managed_select_errors: int = 0  # W3: managed-query fallback count for the run record
    # Stage D U7: the 7-day managed-coverage fallback record selection writes when the managed
    # pool is larger than the cap (issue 2026-10-22-battery-fallback-record).
    _managed_coverage: Optional[dict[str, Any]] = None

    if vins is None:
        # Import lazily to keep the module loadable when selection.py is absent
        # (e.g. during isolated unit tests that always pass explicit vins).
        from agents.tier2 import selection as _selection

        # Use the injected cursor_store, or a no-op store that always starts fresh.
        effective_cursor_store: CursorStoreProtocol = (
            cursor_store if cursor_store is not None else _NullCursorStore()
        )

        # Per-component batch limit — no global fallback (spec § D4).
        selection_limit = _selection_limit_for_component(component)

        # W3: collect managed-query fallback count so it reaches the run record
        # and can be alarmed on (4.3 adds the alarm).
        _selection_run_stats: dict[str, Any] = {}
        resolved_vins = _selection.resolve_batch(
            source=source,
            cursor_store=effective_cursor_store,
            component=component,
            limit=selection_limit,
            run_stats=_selection_run_stats,
        )
        vins_selected = len(resolved_vins)
        # W3: forward managed_select_errors from selection into the observation
        # summary so it reaches the run record and metric layer.
        _managed_select_errors = _selection_run_stats.get("managed_select_errors", 0)
        _managed_coverage = _selection_run_stats.get("managed_coverage")
        # Pass the resolved concrete list through the existing explicit-VIN path.
        # An empty resolved list is valid (pool exhausted or empty) — fetch returns
        # no rows and the run completes normally with artifacts_written=0.
        vins = resolved_vins if resolved_vins else []
        log.info(
            "runner: selection resolved %d VINs for component=%s stage=%s",
            vins_selected,
            component,
            stage,
        )

    # --- fetch batch --------------------------------------------------------
    # Batch fetch — never per-VIN fan-out (spec § D2).
    cap.check()
    rows = source.fetch_findings(vins)
    obs.record_rows_read(len(rows))

    if not rows:
        log.warning("runner: no rows returned from source component=%s stage=%s", component, stage)
        record = obs.finish()
        summary = _summary(record)
        summary["vins_selected"] = vins_selected
        summary["managed_select_errors"] = _managed_select_errors
        if _managed_coverage is not None:
            summary["managed_coverage"] = _managed_coverage
        summary.update(_breaker_summary(agent_fn))
        emit_metrics(
            cloudwatch_client,
            stage,
            component,
            vins_selected=vins_selected,
            artifacts_written=record.artifacts_written,
            duplicates_skipped=record.duplicates_skipped,
            avx_write_failures=record.avx_write_failures,
            owner_integrity_degraded=record.owner_integrity_degraded,
        )
        # G3F3.1 (a2 W2): forward managed_select_errors to the run record at all exits.
        _emit_run_record(run_record_writer, component=component, summary=summary,
                         agent_version=AVX_AGENT_VERSION, stage=stage,
                         cloudwatch_client=cloudwatch_client,
                         extra=_selection_record_extra(summary))
        return summary

    # --- group rows by VIN --------------------------------------------------
    # rows are plain dicts; assume each has a "vin" key (set by component source).
    vin_rows: dict[str, list[dict]] = {}
    for row in rows:
        # The uppercase "VIN" fallback is unreachable with the three sources that
        # exist: all of them normalise to lowercase "vin" at their own boundary.
        # It is retained as a cheap guard for a future source that forgets to,
        # and is called out here so the next reader does not mistake it for
        # evidence that some source emits uppercase. Review Cycle 12 Suggestion 1.
        vin = row.get("vin") or row.get("VIN") or ""
        if not vin:
            continue
        vin_rows.setdefault(vin, []).append(row)

    if not vin_rows:
        log.warning(
            "runner: rows present but none carry a VIN column; skipping component=%s",
            component,
        )
        record = obs.finish()
        summary = _summary(record)
        summary["vins_selected"] = vins_selected
        summary["managed_select_errors"] = _managed_select_errors
        if _managed_coverage is not None:
            summary["managed_coverage"] = _managed_coverage
        summary.update(_breaker_summary(agent_fn))
        emit_metrics(
            cloudwatch_client,
            stage,
            component,
            vins_selected=vins_selected,
            artifacts_written=record.artifacts_written,
            duplicates_skipped=record.duplicates_skipped,
            avx_write_failures=record.avx_write_failures,
            owner_integrity_degraded=record.owner_integrity_degraded,
        )
        _emit_run_record(run_record_writer, component=component, summary=summary,
                         agent_version=AVX_AGENT_VERSION, stage=stage,
                         cloudwatch_client=cloudwatch_client,
                         extra=_selection_record_extra(summary))
        return summary

    # --- per-VIN judgment loop (still a batch: one invocation, N VINs) ------
    last_partial: Optional[str] = None

    # W3 cycle-3 (2026-10-04 Critical): the Strands agent_fn's time-aware
    # breaker needs to know how many VINs still have writes left to do so
    # it can reserve wall time for them.  Snapshot the batch size here and
    # decrement before each iteration (see loop below).  The agent_fn
    # exposes a mutable ``remaining_vins`` attribute; when the injected
    # agent_fn does not (test doubles), setattr still succeeds but is
    # ignored by the breaker.
    _vins_left = len(vin_rows)
    _agent_fn_remaining = getattr(agent_fn, "remaining_vins", None)

    try:
        for vin, vin_row_list in vin_rows.items():
            # Check budget *before* each unit of work (spec § D6: in the loop).
            cap.check()

            # Tell the agent_fn how many VINs (including this one) still
            # have writes to do.  The breaker reserves
            # ``remaining_vins × TIER2_WALL_PER_VIN_WRITE_SECONDS`` ON TOP
            # OF the per-call safety margin, so a slow brakes batch stops
            # calling the LLM in time for the deterministic path to finish
            # all remaining writes within the wall budget.
            if _agent_fn_remaining is not None:
                _agent_fn_remaining["n"] = _vins_left

            # Perform judgment — this is the Strands-agent call.
            artifact_fields = agent_fn(vin, vin_row_list)

            # Decrement AFTER the call: the breaker needs to see the current
            # VIN's write still counted during the call itself.
            _vins_left -= 1

            # Accumulate token usage if the agent returned it.
            tokens = artifact_fields.pop("_tokens_used", 0)
            cost = artifact_fields.pop("_cost_usd", 0.0)
            if tokens:
                cap.record_tokens(tokens)
                obs.record_tokens(tokens, cost_usd=cost)

            # Build and write the artifact.  F11.2: _build_artifact returns
            # (artifact, owner_id); owner_id is deliberately NOT on the artifact
            # (schema-validated, hashed) and travels alongside it to the writer,
            # which projects it onto the persisted DDB item only.
            #
            # W1 (Fix Group 20): an owner-integrity failure costs the PROJECTION,
            # not the Finding. Accept 4 forbids writing a *mismatched* owner; it
            # does not ask us to drop the health finding. The earlier `continue`
            # here also discarded the legacy artifact and the observability
            # records, so a single transient DDB blip on either of the two reads
            # silently cost a vehicle its whole nightly assessment. Re-building
            # with owner_resolver=None makes no network call and yields the same
            # artifact — inputs_hash is unaffected by the projection, which
            # test_inputs_hash_is_identical_with_and_without_the_projection pins.
            try:
                artifact, projected_standing = _build_artifact(
                    vin, component, vin_row_list, artifact_fields,
                    tokens_used=tokens, cost_usd=cost,
                    owner_resolver=owner_resolver,
                )
            except OwnerProjectionIntegrityError as _owner_exc:
                # event= first so the countable token survives CloudWatch
                # truncation (same convention as avx_write_failed below).
                log.warning(
                    "runner: event=owner_integrity_degraded vin_hash=%s component=%s "
                    "stage=%s detail=%s",
                    _mask_vin(vin),
                    component,
                    stage,
                    str(_owner_exc)[:120],
                )
                obs.record_owner_integrity_degraded()
                artifact, projected_standing = _build_artifact(
                    vin, component, vin_row_list, artifact_fields,
                    tokens_used=tokens, cost_usd=cost,
                    owner_resolver=None,
                )

            # LEGACY Agent 2 artifact path — deliberately NOT the D2 write_finding()
            # path. Reverted 2026-09-18 after a live staging deploy proved the
            # routing was premature; see
            # issues/2026-09-18-d2-writer-wired-before-its-table-exists/.
            #
            # write_finding() Queries a `by_finding_key` GSI and PutItems an
            # AVX Finding keyed on `finding_id`. This table
            # (HEALTH_FINDINGS_TABLE = vsa-{stage}-health-findings) is keyed on
            # pk/sk and has NO GSIs, so the D2 path cannot work against it: the
            # Query fails on IAM first, and even granted it would fail on the
            # missing index, and even indexed the PutItem would fail for missing
            # pk/sk. The two artifacts are different shapes, not one shape behind
            # a missing grant.
            #
            # The D2 writer (agents/tier2/writer.py) is correct and
            # mutation-verified; it simply has no table yet. Group 3 creates
            # vsa-{stage}-avx-findings WITH the finding_key GSI, and wires
            # write_finding() to THAT table. Re-pointing this line at
            # write_finding() before that table exists breaks every nightly run.
            wrote_new = writer.put(artifact)
            if wrote_new:
                obs.record_artifact_written()
            else:
                obs.record_duplicate_skipped()

            # ── AVX dual-write (T16.2) ───────────────────────────────────────
            # After the legacy write commits, additionally write an AVX Finding
            # to the avx-findings table via the T16.1 shape transform.
            # Failures are caught and logged as a distinct ``avx_write_failed``
            # field — the nightly health run must not abort on an AVX write error
            # (spec.md:512 requires the legacy path to remain unchanged).
            # The signal is observable and countable: ``log.warning`` emits
            # ``event=avx_write_failed`` as a structured field that CloudWatch
            # Logs Insights can count per run, per component, per stage.  NOT
            # folded into an existing counter.
            if avx_writer is not None:
                try:
                    from agents.tier2.finding_transform import health_artifact_to_finding
                    avx_finding = health_artifact_to_finding(artifact)
                    avx_writer.write_finding(avx_finding, standing=projected_standing)
                except Exception as _avx_exc:  # noqa: BLE001
                    # Put event= first so the countable token is never truncated
                    # mid-message in CloudWatch (Suggestion from review-group16-cycle1.md).
                    # Use a stable SHA-256 digest for the VIN fragment so the same VIN
                    # produces the same fragment across Lambda invocations (Python's
                    # built-in hash() is per-process randomised — W2, Fix Group 17).
                    _vin_frag = hashlib.sha256(vin.encode()).hexdigest()[:8]
                    log.warning(
                        "runner: event=avx_write_failed vin_hash=%s component=%s stage=%s "
                        "error=%s error_detail=%s",
                        _vin_frag,
                        component,
                        stage,
                        type(_avx_exc).__name__,
                        str(_avx_exc)[:80],
                    )
                    obs.record_avx_write_failed()

            # Update the partial-finding snapshot so BudgetExceeded carries
            # the most-recently completed finding if we breach on the *next*
            # iteration.  Never log this — it is owner-facing text.
            last_partial = artifact_fields.get("finding", "")
            cap.set_partial_finding(last_partial)

            # Check again after the call catches a breach on the just-completed
            # token accumulation (spec § D6 comment in budget.py usage pattern).
            cap.check()

    except BudgetExceeded as exc:
        # Access partial_finding programmatically — never via %r or repr(exc)
        # because repr=False was set on that field to guard against VIN leakage.
        partial = exc.partial_finding  # may be None if no work completed
        log.warning(
            "runner: budget exceeded dimension=%s used=%s limit=%s; writing partial",
            exc.dimension,
            exc.used,
            exc.limit,
        )
        if partial:
            # Write a partial artifact for the most-recently completed VIN so
            # the run is not a total loss.  This is a best-effort write; if the
            # writer also fails, we let that error propagate.
            partial_artifact = _build_partial_artifact(component, partial, last_partial)
            try:
                wrote_new = writer.put(partial_artifact)
                if wrote_new:
                    obs.record_artifact_written()
                else:
                    obs.record_duplicate_skipped()
            except Exception:  # noqa: BLE001
                log.warning("runner: could not write partial artifact after budget breach")

        record = obs.finish(abort_reason=str(exc))
        summary = _summary(record)
        summary["vins_selected"] = vins_selected
        summary["managed_select_errors"] = _managed_select_errors
        if _managed_coverage is not None:
            summary["managed_coverage"] = _managed_coverage
        summary.update(_breaker_summary(agent_fn))
        emit_metrics(
            cloudwatch_client,
            stage,
            component,
            vins_selected=vins_selected,
            artifacts_written=record.artifacts_written,
            duplicates_skipped=record.duplicates_skipped,
            avx_write_failures=record.avx_write_failures,
            owner_integrity_degraded=record.owner_integrity_degraded,
        )
        _emit_run_record(run_record_writer, component=component, summary=summary,
                         agent_version=AVX_AGENT_VERSION, stage=stage,
                         cloudwatch_client=cloudwatch_client,
                         extra=_selection_record_extra(summary))
        return summary

    record = obs.finish()
    summary = _summary(record)
    summary["vins_selected"] = vins_selected
    summary["managed_select_errors"] = _managed_select_errors
    if _managed_coverage is not None:
        summary["managed_coverage"] = _managed_coverage
    summary.update(_breaker_summary(agent_fn))
    emit_metrics(
        cloudwatch_client,
        stage,
        component,
        vins_selected=vins_selected,
        artifacts_written=record.artifacts_written,
        duplicates_skipped=record.duplicates_skipped,
        avx_write_failures=record.avx_write_failures,
        owner_integrity_degraded=record.owner_integrity_degraded,
    )
    _emit_run_record(run_record_writer, component=component, summary=summary,
                     agent_version=AVX_AGENT_VERSION, stage=stage,
                     cloudwatch_client=cloudwatch_client,
                     extra=_selection_record_extra(summary))
    return summary


# ---------------------------------------------------------------------------
# Lambda handler (Plan B — substrate wrapper)
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entrypoint invoked by EventBridge Scheduler.

    This is the *only* Plan-B-specific surface in the codebase.  Swapping to
    Plan A (AgentCore managed harness) means replacing this function with an
    ``@app.entrypoint``-decorated handler; everything called by ``run()`` is
    unchanged.

    Expected EventBridge Scheduler ``Input`` payload::

        {
          "stage": "staging",           # required
          "component": "battery",       # optional, default "battery"
          "vins": ["VIN1", "VIN2", …]   # optional; absent = full fleet batch
        }

    Returns a summary dict compatible with EventBridge Scheduler's success /
    failure detection (non-exception = success; raised exception = failure →
    retry policy applies).
    """
    # ── SQS event: investigation component ─────────────────────────────────
    # EventBridge Scheduler passes a plain JSON object (event["stage"]).  SQS
    # wraps each message in an envelope with a top-level "Records" list whose
    # items carry eventSource == "aws:sqs".  The investigation handler NEVER
    # raises: per-item failures are collected into batchItemFailures so the
    # Lambda returns success and SQS handles the redrive.
    if "Records" in event:
        records = event["Records"]
        if records and records[0].get("eventSource") == "aws:sqs":
            return _run_investigation_sqs(records, context)
        # Non-SQS Records (e.g. DynamoDB Streams) are not expected here; fall
        # through to the Scheduler path which will fail on a missing "stage".
        log.warning("runner: lambda_handler received Records but not aws:sqs; falling through")

    stage = event.get("stage") or os.environ.get("VSA_STAGE", "staging")
    component = event.get("component", _DEFAULT_COMPONENT)
    vins: Optional[list[str]] = event.get("vins")

    log.info("runner: lambda_handler invoked stage=%s component=%s", stage, component)

    if component == "diagnostics":
        return _run_diagnostics_from_env(stage)

    if component == "cost":
        return _run_cost_from_env(stage, event, context)

    if component == "recall":
        return _run_recall_from_env(stage, event, context)

    if component == "warranty":
        return _run_warranty_from_env(stage, event, context)

    if component == "service":
        return _run_service_from_env(stage)

    if component == "rebalance":
        return _run_rebalance_from_env(stage)

    # Wire the production source, writer, and cursor_store.  All are imported
    # lazily so unit tests that only call ``run()`` with injected doubles don't
    # trigger AWS SDK imports or environment variable requirements.
    #
    # Per-component batch limit: resolved by _selection_limit_for_component()
    # from the per-component env vars.  Passed to _production_dependencies so
    # the source's max_vins kwarg is set to the right value for this component
    # (used when the source is called without selection, e.g. from tests that
    # pass an explicit vins list AND in the underlying source constructor).
    selection_limit = _selection_limit_for_component(component)
    source, writer, avx_writer = _production_dependencies(
        stage,
        component,
        max_vins=selection_limit,
        max_rows=_DEFAULT_MAX_ROWS,
    )

    # Production cursor store: wraps the existing ArtifactWriter for
    # unconditional writes to the health-findings table.
    cursor_store = _make_production_cursor_store(writer)

    # Production CloudWatch client for metric emission (spec § D5).
    # Injected here so tests can pass None (no-op) without AWS access.
    # Use DYNAMODB_REGION (already in stack env block) — both DynamoDB and
    # CloudWatch run in the same Lambda region (us-west-2 for staging).
    import boto3  # noqa: F811 — boto3 already imported above in this scope
    lambda_region = os.environ.get("DYNAMODB_REGION", "us-west-2")
    cw_client = boto3.client("cloudwatch", region_name=lambda_region)

    # ── F11.2: VIN → standing-facts resolver ─────────────────────────────────
    # Bound to the CMS vehicle registry (fleetId, vehicleId) and, for owner_id,
    # the CMS drivers table plus the CMS pool (issue 2026-10-07: owner_id is the
    # assigned driver's sub, not the purchaser).  The table name is consumed fully
    # resolved from CMS_VEHICLES_TABLE (the stack is the source of truth and
    # this is the same contract variable lambdas/shared/cms_client_real.py
    # reads); nothing is derived from parts here.  The registry lives in the
    # Lambda's own region and account for every stage deployed to date; the
    # stack's IAM grant is what actually constrains which table is reachable.
    #
    # Degrading to owner_resolver=None when the variable is unset would silently
    # disable the projection and read as "no owners exist" — indistinguishable
    # from a fleet with no assigned drivers.  So the default is explicit and the
    # resolver is always wired; unresolvable VINs are the resolver's own
    # fail-closed path, which is observable in logs.
    cms_vehicles_table = os.environ.get(
        "CMS_VEHICLES_TABLE", f"cms-{stage}-storage-vehicles"
    )
    cms_ddb_client = boto3.client("dynamodb", region_name=lambda_region)
    drivers = _assigned_driver_directory(stage, ddb_client=cms_ddb_client)

    def owner_resolver(vin: str) -> StandingFacts:
        return resolve_standing_facts(
            vin,
            ddb_client=cms_ddb_client,
            vehicles_table=cms_vehicles_table,
            drivers=drivers,
        )

    return run(
        stage=stage,
        source=source,
        writer=writer,
        vins=vins,
        component=component,
        max_tokens=_component_max_tokens(component),
        cursor_store=cursor_store,
        cloudwatch_client=cw_client,
        avx_writer=avx_writer,
        owner_resolver=owner_resolver,
    )


# ---------------------------------------------------------------------------
# diagnostics component: active CMS DTCs (spec 2026-09-25-avx-own-vehicle-findings T4.2)
# ---------------------------------------------------------------------------


class DiagnosticsRunError(RuntimeError):
    """Raised after a diagnostics run in which at least one Finding write failed.

    Raised at the END of the run, so every other vehicle's Finding is still written; the
    Lambda error is what reaches the run-failure alarm and the scheduler's retry.
    """


def _live_row_is_adp_written(avx_writer: Any, vin: str, component: str, now_str: str) -> bool:
    """Return True if the finding_key for (vin, component) has a live row NOT from diagnostics.

    A row is from diagnostics when at least one evidence entry has ``kind == "cms.dtc.active"``.
    An ADP row has no such entry. This is the W3 (ADP overlap) guard (review Cycle 1).

    Uses the avx_writer's DDB client and table to run the query without an extra injected
    dependency; both are already present on the writer.
    """
    from agents.tier2.writer import _query_live_row, compute_finding_key

    finding_key = compute_finding_key(
        agent_id="agent-2-vehicle-health",
        scope_field="vin",
        scope_value=vin,
        finding_kind=f"health.{component}",
    )
    client = getattr(avx_writer, "dynamodb_client", None)
    table = getattr(avx_writer, "table_name", None)
    if client is None or not table:
        # Without the table the guard cannot run; writing anyway could overwrite a
        # longer-lived ADP Finding, so fail this group rather than skip the check.
        raise TypeError("avx_writer must expose dynamodb_client and table_name")
    live = _query_live_row(
        finding_key=finding_key,
        table_name=table,
        dynamodb_client=client,
        now_str=now_str,
    )
    if live is None:
        return False
    evidence = live.get("evidence") or []
    return not any(e.get("kind") == "cms.dtc.active" for e in evidence)


def _redirect_adp_overlaps(
    groups: "list[Any]", avx_writer: Any, now_str: str
) -> "list[Any]":
    """Redirect DTCs from ADP-occupied components into health.diagnostics.

    For each group whose component is NOT "diagnostics", if the finding_key
    for (vin, component) already has a live row that was not written by diagnostics
    (no evidence entry with ``kind='cms.dtc.active'``), the group is replaced by
    a contribution to that VIN's health.diagnostics group instead.

    Groups already assigned to "diagnostics" pass through unchanged. Redirected
    groups are merged with any pre-existing diagnostics group for the same VIN.

    W3 fix (review Cycle 1): prevents diagnostics from overwriting an 8-day ADP
    Finding with a 30-minute one on the shared battery/brakes/tires finding_keys.
    """
    from agents.health.cms_dtc_source import DiagnosticsGroup

    # Separate non-diagnostics groups that need the overlap check.
    result: list[Any] = []
    redirected_dtcs: dict[str, list] = {}  # vin -> merged dtcs list

    for group in groups:
        if group.component == "diagnostics":
            result.append(group)
            continue
        if _live_row_is_adp_written(avx_writer, group.vin, group.component, now_str):
            log.info(
                "runner: event=adp_overlap_redirect vin_hash=%s component=%s dtcs=%s "
                "action=redirect_to_health.diagnostics",
                _mask_vin(group.vin), group.component,
                ",".join(d["code"] for d in group.dtcs),
            )
            redirected_dtcs.setdefault(group.vin, []).extend(group.dtcs)
        else:
            result.append(group)

    # Merge redirected DTCs into existing diagnostics groups or create new ones.
    for vin, dtcs in redirected_dtcs.items():
        # Find the existing diagnostics group for this VIN, if any.
        diag_group = next(
            (g for g in result if g.vin == vin and g.component == "diagnostics"), None
        )
        if diag_group is not None:
            diag_group.dtcs.extend(dtcs)
            diag_group.dtcs.sort(key=lambda d: d["code"])
        else:
            new_group = DiagnosticsGroup(vin=vin, component="diagnostics", dtcs=sorted(dtcs, key=lambda d: d["code"]))
            result.append(new_group)

    return result


def run_diagnostics(
    stage: str,
    *,
    source: Any,
    avx_writer: Any,
    owner_resolver: Optional[Callable[[str], Any]] = None,
    cloudwatch_client: Optional[Any] = None,
    run_id: Optional[str] = None,
    run_record_writer: Optional[Callable] = None,
) -> dict[str, Any]:
    """One ``diagnostics`` run: every vehicle with an active CMS DTC (a P0 code makes its group a p0 Finding
    carrying the directive verbatim, spec 2026-11-05-avx-proactive-p0-alert).

    Differs from :func:`run` on purpose:

    * **AVX table only.** No legacy ``health-findings`` artifact: its only reader is the
      supervisor's ``health_findings`` tool, which reads the three ADP components, and an
      8-day legacy row for a 30-minute fact would outlive it.
    * **No selection cursor and no agent call.** Every vehicle is covered on every run
      (spec D4 Revised), and the text is deterministic (``cms_dtc_source.build_fields``).
    * **Short TTL with keep-alive.** ``DIAGNOSTICS_TTL_SECONDS`` and
      ``write_finding(refresh_on_noop=True)``, so an unchanged fault keeps one card and a
      cleared one expires.
    """
    from agents.health.cms_dtc_source import DIAGNOSTICS_TTL_SECONDS, DiagnosticsReadError
    from agents.tier2.finding_transform import health_artifact_to_finding

    component = "diagnostics"
    if run_id is None:
        run_id = _utc_now()
    obs = RunObserver(run_id=run_id, stage=stage, component=component)
    failures = 0

    # W4: fetch_groups raises DiagnosticsReadError when any VIN's DTC query fails.
    # Carry the partial groups (which succeeded) so we still write those cards.
    try:
        groups = source.fetch_groups()
    except DiagnosticsReadError as exc:
        groups = exc.groups  # process the VINs that DID succeed
        for failed_vin in exc.failed_vins:
            failures += 1
            obs.record_avx_write_failed()
            log.warning(
                "runner: event=dtc_read_failed vin_hash=%s stage=%s",
                _mask_vin(failed_vin), stage,
            )

    obs.record_rows_read(sum(len(g.dtcs) for g in groups))

    # W3: redirect codes for battery/brakes/tires whose finding_key already has
    # a live ADP-written row (evidence has no cms.dtc.active entry) into the
    # health.diagnostics group for that VIN instead of overwriting the ADP row.
    now_str = _utc_now()
    groups = _redirect_adp_overlaps(groups, avx_writer, now_str)

    vins_selected = len({g.vin for g in groups})

    for group in groups:
        fields = group.agent_fields()
        try:
            try:
                artifact, standing = _build_artifact(
                    group.vin, group.component, group.dtcs, fields,
                    owner_resolver=owner_resolver,
                    inputs_hash=group.inputs_hash, ttl_seconds=DIAGNOSTICS_TTL_SECONDS,
                )
            except OwnerProjectionIntegrityError:
                obs.record_owner_integrity_degraded()
                artifact, standing = _build_artifact(
                    group.vin, group.component, group.dtcs, fields,
                    owner_resolver=None,
                    inputs_hash=group.inputs_hash, ttl_seconds=DIAGNOSTICS_TTL_SECONDS,
                )
            finding = health_artifact_to_finding(artifact, ttl_seconds=DIAGNOSTICS_TTL_SECONDS)
            result = avx_writer.write_finding(finding, standing=standing, refresh_on_noop=True)
            if result.get("action") in ("put", "update"):
                obs.record_artifact_written()
            else:
                obs.record_duplicate_skipped()
        except Exception as exc:  # noqa: BLE001 — one vehicle must not stop the others
            failures += 1
            obs.record_avx_write_failed()
            log.warning(
                "runner: event=avx_write_failed vin_hash=%s component=%s kind=%s stage=%s "
                "error=%s error_detail=%s",
                _mask_vin(group.vin), component, group.component, stage,
                type(exc).__name__, str(exc)[:80],
            )

    record = obs.finish(abort_reason=f"{failures} write(s) failed" if failures else None)
    summary = _summary(record)
    summary["vins_selected"] = vins_selected
    emit_metrics(
        cloudwatch_client,
        stage,
        component,
        vins_selected=vins_selected,
        artifacts_written=record.artifacts_written,
        duplicates_skipped=record.duplicates_skipped,
        avx_write_failures=record.avx_write_failures,
        owner_integrity_degraded=record.owner_integrity_degraded,
    )
    if failures:
        _emit_run_record(run_record_writer, component=component, summary=summary,
                         agent_version=AVX_AGENT_VERSION, stage=stage,
                         cloudwatch_client=cloudwatch_client)
        raise DiagnosticsRunError(f"{failures} of {len(groups)} diagnostics Finding write(s) failed")
    _emit_run_record(run_record_writer, component=component, summary=summary,
                     agent_version=AVX_AGENT_VERSION, stage=stage,
                     cloudwatch_client=cloudwatch_client)
    return summary


def _run_diagnostics_from_env(stage: str) -> dict[str, Any]:
    """Production wiring for :func:`run_diagnostics` from the stack's environment."""
    import boto3

    from agents.health.cms_dtc_source import CmsDtcSource
    from agents.tier2.artifact_writer import ArtifactWriter

    region = os.environ.get("DYNAMODB_REGION", "us-west-2")
    ddb_client = boto3.client("dynamodb", region_name=region)
    cms_vehicles_table = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{stage}-storage-vehicles")
    source = CmsDtcSource(
        dtc_table=os.environ.get("CMS_DTC_HISTORY_TABLE", f"cms-{stage}-storage-dtc-history"),
        # The registry supplies each Finding's VIN (issue 2026-09-28-diagnostics-vin-taken-
        # from-dtc-rows); the same table the owner resolver below reads.
        vehicles_table=cms_vehicles_table,
        ddb_client=ddb_client,
        ddb_resource=boto3.resource("dynamodb", region_name=region),
    )
    avx_writer = ArtifactWriter(
        table_name=os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{stage}-avx-findings"),
        dynamodb_client=ddb_client,
    )

    drivers = _assigned_driver_directory(stage, ddb_client=ddb_client)

    def owner_resolver(vin: str) -> StandingFacts:
        return resolve_standing_facts(vin, ddb_client=ddb_client, vehicles_table=cms_vehicles_table,
                                      drivers=drivers)

    return run_diagnostics(
        stage,
        source=source,
        avx_writer=avx_writer,
        owner_resolver=owner_resolver,
        cloudwatch_client=boto3.client("cloudwatch", region_name=region),
    )


# ---------------------------------------------------------------------------
# cost component: FI cost agent (spec 2026-10-02-fi-cost-stage-b D6, task 4.1)
# ---------------------------------------------------------------------------


class CostRunError(RuntimeError):
    """Raised after a cost run in which at least one Finding write failed.

    Raised at the END of the run, after the run record, so every other vehicle's
    Finding is written; the Lambda error reaches the run-failure alarm. Lambda retries
    an asynchronous invocation that errors twice: a retry skips the vehicles already
    written, re-diagnoses the ones whose write failed, and takes vehicles the failed run
    deferred (decisions 2026-10-05 "Group 4 review cycle 1" item 1).
    """


#: SSM parameter holding ``max_diagnoses_per_run`` (seeded from
#: ``deployment/config/avx-policy-defaults.json``). ``{stage}`` is substituted.
FI_COST_MAX_DIAGNOSES_PARAM = "/vsa/{stage}/avx/fi-cost/max-diagnoses-per-run"

#: Bedrock client timeouts for the cost component. ``agents.fi_cost.agent``'s
#: DIAGNOSIS_RESERVE_S assumes this read timeout; no botocore retries, because the
#: agent's own retry (one, then the rules fallback) is the retry policy (D6 step 5).
_FI_COST_BEDROCK_READ_TIMEOUT_S = 30
_FI_COST_BEDROCK_CONNECT_TIMEOUT_S = 5


def _parse_max_diagnoses(value: Any, *, source: str) -> int:
    """An integer 0..ceiling from an override or the policy value, else ``ValueError``."""
    from agents.fi_cost.agent import MAX_DIAGNOSES_CEILING

    if isinstance(value, bool):
        raise ValueError(f"{source}: max_diagnoses_per_run must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ValueError(f"{source}: max_diagnoses_per_run must be an integer")
        value = int(text)
    if not isinstance(value, int) or not 0 <= value <= MAX_DIAGNOSES_CEILING:
        raise ValueError(
            f"{source}: max_diagnoses_per_run must be an integer from 0 to "
            f"{MAX_DIAGNOSES_CEILING}"
        )
    return value


def resolve_max_diagnoses_per_run(stage: str, event: dict[str, Any], ssm_client: Any) -> int:
    """The run's cap: a one-run event override, else the policy value, else the default.

    The event field ``max_diagnoses_per_run`` is the one-run override task 5.3 item 2
    uses; a malformed override raises, so a typo never runs with an unintended cap. An
    absent or unreadable policy value falls back to the built-in default with a
    warning, as the executors' policy reads do: a missing parameter must not stop the
    run. A present but malformed policy value also falls back, logged at ERROR.
    """
    from agents.fi_cost.agent import DEFAULT_MAX_DIAGNOSES_PER_RUN

    if "max_diagnoses_per_run" in event:
        return _parse_max_diagnoses(event["max_diagnoses_per_run"], source="event override")
    name = FI_COST_MAX_DIAGNOSES_PARAM.replace("{stage}", stage)
    try:
        raw = ssm_client.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 — absent or unreachable: use the default
        log.warning(
            "runner: event=fi_cost_policy_default component=cost param=%s error=%s "
            "using=%d", name, type(exc).__name__, DEFAULT_MAX_DIAGNOSES_PER_RUN,
        )
        return DEFAULT_MAX_DIAGNOSES_PER_RUN
    try:
        return _parse_max_diagnoses(raw, source=name)
    except ValueError:
        log.error(
            "runner: event=fi_cost_policy_malformed component=cost param=%s using=%d",
            name, DEFAULT_MAX_DIAGNOSES_PER_RUN,
        )
        return DEFAULT_MAX_DIAGNOSES_PER_RUN


def _cost_record_fields(stats: dict[str, Any]) -> dict[str, Any]:
    """The cost component's counters carried into its run record (contract § 8 additive)."""
    return {
        "data_month": stats.get("data_month"),
        "vins_selected": stats.get("anomalies_selected", 0),
        "model_calls": stats.get("model_calls", 0),
        "deferred": stats.get("deferred", 0),
        "tokens_used": stats.get("tokens_used", 0),
        "cost_usd": stats.get("cost_usd", 0.0),
        "write_failures": stats.get("write_failures", 0),
        "rules_fallbacks": stats.get("rules_fallbacks", 0),
        "guardrail_interventions": stats.get("guardrail_interventions", 0),
    }


def run_cost(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    bedrock_client: Any,
    avx_writer: Any,
    max_diagnoses_per_run: int,
    remaining_seconds: Callable[[], float],
    owner_resolver: Optional[Callable[[str], Any]] = None,
    cloudwatch_client: Optional[Any] = None,
    run_id: Optional[str] = None,
    run_record_writer: Optional[Callable] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    """One ``cost`` run (spec D6): ``agents.fi_cost.agent.run_cost_loop`` plus the run's
    record (``agent-fi-cost#cost``), metrics and heartbeat.

    * **AVX table only**, as diagnostics: no legacy health-findings artifact.
    * **One run record per run**, written on every exit: completed, aborted by an
      Athena error or the wall clock (re-raised so the Lambda errors), or completed
      with failed writes (recorded as aborted, then :class:`CostRunError`).
    """
    from agents.fi_cost.agent import AGENT_VERSION as FI_COST_AGENT_VERSION
    from agents.fi_cost.agent import run_cost_loop
    from agents.tier2.writer import _query_live_row

    component = "cost"
    if run_id is None:
        run_id = _utc_now()
    obs = RunObserver(run_id=run_id, stage=stage, component=component)

    def lookup_live(finding_key: str) -> Optional[dict[str, Any]]:
        return _query_live_row(
            finding_key=finding_key,
            table_name=avx_writer.table_name,
            dynamodb_client=avx_writer.dynamodb_client,
            now_str=_utc_now(),
        )

    def write(finding: dict[str, Any], standing: Any) -> dict[str, Any]:
        return avx_writer.write_finding(finding, standing=standing)

    try:
        stats = run_cost_loop(
            stage=stage,
            athena_client=athena_client,
            workgroup=workgroup,
            results_s3_path=results_s3_path,
            bedrock_client=bedrock_client,
            write_finding=write,
            lookup_live=lookup_live,
            resolve_standing=owner_resolver,
            max_diagnoses_per_run=max_diagnoses_per_run,
            remaining_seconds=remaining_seconds,
            now=now,
        )
    except Exception as exc:
        # The exception's text can carry a VIN (input validation); its type cannot.
        partial = getattr(exc, "cost_stats", None)
        if partial:
            # Findings written before the abort are real writes; the record says so.
            for _ in range(partial.get("anomalies_written", 0) + partial.get("sell_timing_written", 0)):
                obs.record_artifact_written()
            obs.record_tokens(partial.get("tokens_used", 0), cost_usd=partial.get("cost_usd", 0.0))
        record = obs.finish(abort_reason=type(exc).__name__)
        summary = _summary(record)
        summary["vins_selected"] = (partial or {}).get("anomalies_selected", 0)
        emit_metrics(cloudwatch_client, stage, component,
                     vins_selected=summary["vins_selected"],
                     artifacts_written=0, duplicates_skipped=0)
        # What ran before the abort (the data month, model calls already billed) is
        # recorded too, so an aborted record never under-reports a run's spend.
        _emit_run_record(run_record_writer, component=component, summary=summary,
                         agent_version=FI_COST_AGENT_VERSION, stage=stage,
                         cloudwatch_client=cloudwatch_client,
                         agent_id=FI_COST_AGENT_ID,
                         extra=_cost_record_fields(partial) if partial else None)
        raise

    for _ in range(stats["anomalies_written"] + stats["sell_timing_written"]):
        obs.record_artifact_written()
    for _ in range(stats["duplicates_skipped"] + stats["sell_timing_skipped"]
                   + stats["anomalies_noop"]):
        obs.record_duplicate_skipped()
    for _ in range(stats["write_failures"]):
        obs.record_avx_write_failed()
    for _ in range(stats["owner_integrity_degraded"]):
        obs.record_owner_integrity_degraded()
    obs.record_tokens(stats["tokens_used"], cost_usd=stats["cost_usd"])

    failures = stats["write_failures"]
    record = obs.finish(abort_reason=f"{failures} write(s) failed" if failures else None)
    summary = _summary(record)
    summary["cost"] = dict(stats)
    summary["vins_selected"] = stats["anomalies_selected"]
    emit_metrics(
        cloudwatch_client,
        stage,
        component,
        vins_selected=stats["anomalies_selected"],
        artifacts_written=record.artifacts_written,
        duplicates_skipped=record.duplicates_skipped,
        avx_write_failures=record.avx_write_failures,
        owner_integrity_degraded=record.owner_integrity_degraded,
    )
    _emit_run_record(run_record_writer, component=component, summary=summary,
                     agent_version=FI_COST_AGENT_VERSION, stage=stage,
                     cloudwatch_client=cloudwatch_client,
                     agent_id=FI_COST_AGENT_ID, extra=_cost_record_fields(stats))
    if failures:
        raise CostRunError(f"{failures} cost Finding write(s) failed")
    return summary


def _run_cost_from_env(stage: str, event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Production wiring for :func:`run_cost` from the stack's environment."""
    import time as _time

    import boto3
    from botocore.config import Config

    from agents.tier2.artifact_writer import ArtifactWriter

    # The guardrail is part of D6 step 5. Without it every model reply would fail its
    # ApplyGuardrail call and fall back to rules; fail the run loudly instead.
    guardrail_id = os.environ.get("FI_COST_GUARDRAIL_ID", "")
    guardrail_version = os.environ.get("FI_COST_GUARDRAIL_VERSION", "")
    if not guardrail_id or not guardrail_version:
        log.error("runner: event=fi_cost_guardrail_unset component=cost "
                  "(FI_COST_GUARDRAIL_ID / FI_COST_GUARDRAIL_VERSION not set)")
        raise RuntimeError("FI_COST_GUARDRAIL_ID and FI_COST_GUARDRAIL_VERSION must be set")

    region = os.environ.get("DYNAMODB_REGION", "us-west-2")
    adp_region = os.environ.get("ADP_REGION", "us-east-1")
    workgroup = os.environ.get("ATHENA_WORKGROUP", f"cvx-{stage}-analytics")
    results_bucket = os.environ.get("ATHENA_RESULTS_BUCKET", "")
    if not results_bucket:
        raise RuntimeError("ATHENA_RESULTS_BUCKET must be set")
    cms_vehicles_table = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{stage}-storage-vehicles")

    ddb_client = boto3.client("dynamodb", region_name=region)
    avx_writer = ArtifactWriter(
        table_name=os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{stage}-avx-findings"),
        dynamodb_client=ddb_client,
    )
    bedrock_client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            read_timeout=_FI_COST_BEDROCK_READ_TIMEOUT_S,
            connect_timeout=_FI_COST_BEDROCK_CONNECT_TIMEOUT_S,
            # total_max_attempts counts the first request; botocore's max_attempts counts
            # retries, so max_attempts=1 would send two (Group 4 review cycle 2 W1).
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )

    drivers = _assigned_driver_directory(stage, ddb_client=ddb_client)

    def owner_resolver(vin: str) -> StandingFacts:
        return resolve_standing_facts(vin, ddb_client=ddb_client, vehicles_table=cms_vehicles_table,
                                      drivers=drivers)

    # The run's clock is the shorter of the Lambda's remaining time and the Tier 2 wall
    # budget (TIER2_MAX_WALL_SECONDS, 600 s): the shared run-duration alarm fires at
    # 600 s, and a cost run that respects the budget never pages it on a normal day
    # (Group 4 review cycle 1). Work the budget cuts short is deferred to the next run.
    budget_deadline = _time.monotonic() + _DEFAULT_MAX_WALL_SECONDS
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        def remaining_seconds() -> float:
            return min(context.get_remaining_time_in_millis() / 1000.0,
                       budget_deadline - _time.monotonic())
    else:
        def remaining_seconds() -> float:
            return budget_deadline - _time.monotonic()

    return run_cost(
        stage,
        athena_client=boto3.client("athena", region_name=adp_region),
        workgroup=workgroup,
        results_s3_path=f"s3://{results_bucket}/tier2/fi-cost/",
        bedrock_client=bedrock_client,
        avx_writer=avx_writer,
        max_diagnoses_per_run=resolve_max_diagnoses_per_run(
            stage, event, boto3.client("ssm", region_name=region)
        ),
        remaining_seconds=remaining_seconds,
        owner_resolver=owner_resolver,
        cloudwatch_client=boto3.client("cloudwatch", region_name=region),
    )


def _rw_record_fields(stats: dict[str, Any], *, rows_selected: int,
                      select_refused: int = 0,
                      model_stats: Optional[Any] = None) -> dict[str, Any]:
    """Recall/warranty counters carried into the run record (write_run_record names them)."""
    fields = {"vins_selected": rows_selected,
              "rows_refused": int(stats.get("rows_refused", 0)) + select_refused}
    for name in ("grounding_read_errors", "book_items_without_rooftops", "rows_without_terms"):
        if name in stats:
            fields[name] = int(stats[name])
    # Model-path counters (S4); zero when rules-only.
    ms = model_stats
    fields["model_calls"] = ms.model_calls if ms else 0
    fields["tokens_used"] = (ms.input_tokens + ms.output_tokens) if ms else 0
    fields["cost_usd"] = ms.cost_usd() if ms else 0.0
    fields["rules_fallbacks"] = ms.rules_fallbacks if ms else 0
    fields["guardrail_interventions"] = ms.guardrail_interventions if ms else 0
    fields["deferred"] = int(stats.get("deferred", 0))
    return fields


def run_recall(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    ddb_client: Any,
    vehicles_table: str,
    fleet_locations_table: str,
    groundings_table: str,
    actions_table: str,
    avx_writer: Any,
    owner_resolver: Optional[Callable[[str], Any]] = None,
    cloudwatch_client: Optional[Any] = None,
    run_id: Optional[str] = None,
    run_record_writer: Optional[Callable] = None,
    now: Optional[datetime.datetime] = None,
    bedrock_client: Optional[Any] = None,
    remaining_seconds: Optional[Callable[[], float]] = None,
    model_call_cap: Optional[int] = None,
    model_id: str = _FI_RW_DEFAULT_MODEL_ID,
    guardrail_id: str = "",
    guardrail_version: str = "",
) -> dict[str, Any]:
    """One ``recall`` run: ``agents.fi_recall_warranty.recall.run_recall_loop`` plus
    a run record keyed ``agent-fi-recall-warranty#recall`` on every exit."""
    from agents.fi_recall_warranty import attempts as rw_attempts
    from agents.fi_recall_warranty import recall as rw_recall
    from agents.fi_recall_warranty import selection as rw_selection
    from agents.fi_recall_warranty.writer import AGENT_VERSION as _FI_RW_AGENT_VERSION
    from agents.tier2.writer import _query_live_row

    component = "recall"
    if run_id is None:
        run_id = _utc_now()
    obs = RunObserver(run_id=run_id, stage=stage, component=component)
    today = (now or datetime.datetime.now(datetime.timezone.utc)).date()
    computed_at = _utc_now()

    def lookup_live(finding_key: str) -> Optional[dict[str, Any]]:
        return _query_live_row(
            finding_key=finding_key,
            table_name=avx_writer.table_name,
            dynamodb_client=avx_writer.dynamodb_client,
            now_str=_utc_now(),
        )

    def write(finding: dict[str, Any], standing: Any, refresh_on_noop: bool = False,
              update_on_same_hash: bool = False) -> dict[str, Any]:
        return avx_writer.write_finding(finding, standing=standing,
                                        refresh_on_noop=refresh_on_noop,
                                        update_on_same_hash=update_on_same_hash)

    def vehicle_ids_fn(vin: str):
        return rw_selection.lookup_vehicle_cms_ids(vin, ddb_client=ddb_client,
                                                   vehicles_table=vehicles_table)

    def locations_fn(fleet_id: str):
        return rw_selection.lookup_fleet_locations(fleet_id, ddb_client=ddb_client,
                                                   fleet_locations_table=fleet_locations_table)

    def grounded_fn(vehicle_id: str):
        return rw_selection.check_grounded(vehicle_id, ddb_client=ddb_client,
                                           groundings_table=groundings_table)

    def history_fn(vin: str):
        return rw_attempts.read_attempt_history(vin, ddb_client=ddb_client,
                                                findings_table=avx_writer.table_name,
                                                actions_table=actions_table)

    # Model path: shared VIN-items cache + plan_fn closure (S4).
    model_stats: Optional[Any] = None
    plan_fn_recall: Optional[Callable] = None
    open_items_fn_recall: Optional[Callable] = None
    if bedrock_client is not None:
        from agents.fi_recall_warranty import llm as rw_llm
        from agents.fi_recall_warranty import runner as _rw_runner

        model_stats = rw_llm.ModelStats()
        _vin_items_cache: dict[str, list] = {}

        def _get_vin_items(vin: str) -> list:
            if vin not in _vin_items_cache:
                _vin_items_cache[vin] = rw_attempts.read_vin_findings(
                    vin, ddb_client=ddb_client, findings_table=avx_writer.table_name)
            return _vin_items_cache[vin]

        def history_fn(vin: str):  # noqa: F811 — shadows the rules-only fn above
            items = _get_vin_items(vin)
            return rw_attempts.attempt_history_from_items(
                items, now=datetime.datetime.now(datetime.timezone.utc),
                ddb_client=ddb_client, actions_table=actions_table)

        def open_items_fn_recall(vin: str) -> list:
            items = _get_vin_items(vin)
            return rw_attempts.open_items_from_items(items, now_iso=computed_at)

        _ms = model_stats  # captured for closure

        def plan_fn_recall(*, row, item_type, band_result, rooftops, origin, bundle):
            severity = band_result.severity
            allowed = _allowed_urgencies_for_severity(severity)
            item_context = {
                "campaign_id": row.get("campaign_id"),
                "recall_class": row.get("recall_class"),
                "do_not_drive": row.get("do_not_drive"),
                "park_outside": row.get("park_outside"),
                "match_basis": row.get("match_basis"),
                "severity": severity,
                "allowed_urgencies": allowed,
                "candidates": [
                    {"dealer_id": r["dealer_id"],
                     "distance_miles": r.get("distance_miles"),
                     "in_stock": r.get("in_stock")}
                    for r in rooftops
                ],
                "bundle": [
                    {"finding_id": b["finding_id"],
                     "finding_kind": b["finding_kind"],
                     "headline": b.get("headline", "")}
                    for b in bundle
                ],
                "remedy_available_from": row.get("remedy_available_from"),
                # What evidence_refs may name; without it the model guesses and the
                # first attempt fails validation (architect check, Group 4).
                "evidence_kinds": list(_RECALL_EVIDENCE_KINDS),
            }

            def rules_fn(exc):
                from agents.fi_recall_warranty.recall import _rules_plan_or_empty
                return _rules_plan_or_empty(rooftops, band_result, item_type)

            return rw_llm.plan_booking(
                bedrock_client=bedrock_client,
                item_context=item_context,
                candidate_dealer_ids=[r["dealer_id"] for r in rooftops],
                bundle_ids=[b["finding_id"] for b in bundle],
                severity=severity,
                recall_class=row.get("recall_class", "safety"),
                evidence_kinds=_RECALL_EVIDENCE_KINDS,
                rules_fn=rules_fn,
                stats=_ms,
                model_id=model_id,
                guardrail_id=guardrail_id,
                guardrail_version=guardrail_version,
            )

    try:
        recall_rows, select_refused = rw_selection.select_unremedied_recalls(
            stage, athena_client=athena_client, workgroup=workgroup,
            results_s3_path=results_s3_path)
        stock_rows = rw_selection.select_parts_stock(
            stage, athena_client=athena_client, workgroup=workgroup,
            results_s3_path=results_s3_path)
        stats = rw_recall.run_recall_loop(
            recall_rows=recall_rows, stock_rows=stock_rows,
            today=today, computed_at=computed_at,
            vehicle_ids_fn=vehicle_ids_fn, locations_fn=locations_fn,
            grounded_fn=grounded_fn, history_fn=history_fn,
            standing_fn=owner_resolver, lookup_live=lookup_live,
            write_finding=write, plan_fn=plan_fn_recall,
            open_items_fn=open_items_fn_recall,
            model_call_cap=model_call_cap,
            remaining_seconds=remaining_seconds,
        )
    except Exception as exc:
        if model_stats is not None:
            obs.record_tokens(model_stats.input_tokens + model_stats.output_tokens,
                              cost_usd=model_stats.cost_usd())
        record = obs.finish(abort_reason=type(exc).__name__)
        summary = _summary(record)
        emit_metrics(cloudwatch_client, stage, component, vins_selected=0,
                     artifacts_written=0, duplicates_skipped=0)
        _emit_run_record(run_record_writer, component=component, summary=summary,
                         agent_version=_FI_RW_AGENT_VERSION, stage=stage,
                         cloudwatch_client=cloudwatch_client,
                         agent_id=FI_RECALL_WARRANTY_AGENT_ID,
                         extra=_rw_record_fields({}, rows_selected=0,
                                                 model_stats=model_stats))
        raise

    for _ in range(stats["items_written"]):
        obs.record_artifact_written()
    for _ in range(stats["items_skipped_unchanged"]):
        obs.record_duplicate_skipped()

    if model_stats is not None:
        tokens = model_stats.input_tokens + model_stats.output_tokens
        obs.record_tokens(tokens, cost_usd=model_stats.cost_usd())

    record = obs.finish()
    summary = _summary(record)
    summary.update(stats)
    summary["rows_refused_at_select"] = select_refused
    emit_metrics(cloudwatch_client, stage, component, vins_selected=len(recall_rows),
                 artifacts_written=record.artifacts_written,
                 duplicates_skipped=record.duplicates_skipped)
    _emit_run_record(run_record_writer, component=component, summary=summary,
                     agent_version=_FI_RW_AGENT_VERSION, stage=stage,
                     cloudwatch_client=cloudwatch_client,
                     agent_id=FI_RECALL_WARRANTY_AGENT_ID,
                     extra=_rw_record_fields(stats, rows_selected=len(recall_rows),
                                             select_refused=select_refused,
                                             model_stats=model_stats))
    return summary


def _run_recall_from_env(stage: str, event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Production wiring for :func:`run_recall` from the stack's environment."""
    import time as _time

    import boto3
    from botocore.config import Config

    from agents.tier2.artifact_writer import ArtifactWriter

    # Guardrail check must happen before building any client (S4 / brief).
    guardrail_id = os.environ.get("FI_COST_GUARDRAIL_ID", "")
    guardrail_version = os.environ.get("FI_COST_GUARDRAIL_VERSION", "")
    if not guardrail_id or not guardrail_version:
        log.error("runner: event=fi_rw_guardrail_unset component=recall "
                  "(FI_COST_GUARDRAIL_ID / FI_COST_GUARDRAIL_VERSION not set)")
        raise RuntimeError("FI_COST_GUARDRAIL_ID and FI_COST_GUARDRAIL_VERSION must be set")

    # Cap parsing — raises on malformed value.
    recall_cap_raw = os.environ.get("FI_RECALL_MAX_MODEL_CALLS")
    model_call_cap = (
        _parse_rw_cap(recall_cap_raw, source="FI_RECALL_MAX_MODEL_CALLS")
        if recall_cap_raw is not None
        else _FI_RECALL_DEFAULT_CAP
    )

    model_id = os.environ.get("FI_RW_MODEL_ID", _FI_RW_DEFAULT_MODEL_ID)

    region = os.environ.get("DYNAMODB_REGION", "us-west-2")
    adp_region = os.environ.get("ADP_REGION", "us-east-1")
    workgroup = os.environ.get("ATHENA_WORKGROUP", f"cvx-{stage}-analytics")
    results_bucket = os.environ.get("ATHENA_RESULTS_BUCKET", "")
    if not results_bucket:
        raise RuntimeError("ATHENA_RESULTS_BUCKET must be set")
    results_s3_path = f"s3://{results_bucket}/tier2/fi-recall-warranty/"
    cms_vehicles_table = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{stage}-storage-vehicles")
    fleet_locations_table = os.environ.get(
        "CMS_FLEET_LOCATIONS_TABLE", f"cms-{stage}-storage-fleet-locations")
    groundings_table = os.environ.get(
        "CMS_VEHICLE_GROUNDINGS_TABLE", f"cms-{stage}-storage-vehicle-groundings")
    actions_table = os.environ.get("AVX_ACTIONS_TABLE", f"vsa-{stage}-avx-actions")

    ddb_client = boto3.client("dynamodb", region_name=region)
    avx_writer = ArtifactWriter(
        table_name=os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{stage}-avx-findings"),
        dynamodb_client=ddb_client,
    )
    athena_client = boto3.client("athena", region_name=adp_region)
    bedrock_client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            read_timeout=_FI_RW_BEDROCK_READ_TIMEOUT_S,
            connect_timeout=_FI_RW_BEDROCK_CONNECT_TIMEOUT_S,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    # Landing fix L2 (rebase onto main's assigned-driver owner_id, issue 2026-10-07): the
    # run's own directory, as every other handler builds it.
    drivers = _assigned_driver_directory(stage, ddb_client=ddb_client)

    def owner_resolver(vin: str) -> StandingFacts:
        return resolve_standing_facts(vin, ddb_client=ddb_client, vehicles_table=cms_vehicles_table,
                                      drivers=drivers)

    budget_deadline = _time.monotonic() + _DEFAULT_MAX_WALL_SECONDS
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        def remaining_seconds() -> float:
            return min(context.get_remaining_time_in_millis() / 1000.0,
                       budget_deadline - _time.monotonic())
    else:
        def remaining_seconds() -> float:
            return budget_deadline - _time.monotonic()

    return run_recall(
        stage=stage,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
        ddb_client=ddb_client,
        vehicles_table=cms_vehicles_table,
        fleet_locations_table=fleet_locations_table,
        groundings_table=groundings_table,
        actions_table=actions_table,
        avx_writer=avx_writer,
        owner_resolver=owner_resolver,
        cloudwatch_client=boto3.client("cloudwatch", region_name=region),
        bedrock_client=bedrock_client,
        remaining_seconds=remaining_seconds,
        model_call_cap=model_call_cap,
        model_id=model_id,
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
    )


# ---------------------------------------------------------------------------
# warranty component (spec 2026-10-05-fi-recall-warranty-stage-c D8 / T3.5B)
# ---------------------------------------------------------------------------


def run_warranty(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    avx_writer: Any,
    owner_resolver: Optional[Callable[[str], Any]] = None,
    cloudwatch_client: Optional[Any] = None,
    run_id: Optional[str] = None,
    run_record_writer: Optional[Callable] = None,
    now: Optional[datetime.datetime] = None,
    bedrock_client: Optional[Any] = None,
    remaining_seconds: Optional[Callable[[], float]] = None,
    model_call_cap: Optional[int] = None,
    model_id: str = _FI_RW_DEFAULT_MODEL_ID,
    guardrail_id: str = "",
    guardrail_version: str = "",
) -> dict[str, Any]:
    """One ``warranty`` run: ``agents.fi_recall_warranty.warranty.run_warranty_loop`` plus
    a run record keyed ``agent-fi-recall-warranty#warranty`` on every exit."""
    from agents.fi_recall_warranty import selection as rw_selection
    from agents.fi_recall_warranty import warranty as rw_warranty
    from agents.fi_recall_warranty.writer import AGENT_VERSION as _FI_RW_AGENT_VERSION
    from agents.tier2.writer import _query_live_row

    component = "warranty"
    if run_id is None:
        run_id = _utc_now()
    obs = RunObserver(run_id=run_id, stage=stage, component=component)
    computed_at = _utc_now()

    def lookup_live(finding_key: str) -> Optional[dict[str, Any]]:
        return _query_live_row(
            finding_key=finding_key,
            table_name=avx_writer.table_name,
            dynamodb_client=avx_writer.dynamodb_client,
            now_str=_utc_now(),
        )

    def write(finding: dict[str, Any], standing: Any, refresh_on_noop: bool = False,
              update_on_same_hash: bool = False) -> dict[str, Any]:
        return avx_writer.write_finding(finding, standing=standing,
                                        refresh_on_noop=refresh_on_noop,
                                        update_on_same_hash=update_on_same_hash)

    # Model path: plan_fn closure (S4).
    model_stats_w: Optional[Any] = None
    plan_fn_warranty: Optional[Callable] = None
    if bedrock_client is not None:
        from agents.fi_recall_warranty import llm as rw_llm

        model_stats_w = rw_llm.ModelStats()
        _ms_w = model_stats_w

        def plan_fn_warranty(*, row, evidence):
            evidence_kinds = [e["kind"] for e in evidence] if evidence else []
            item_context = {
                e["kind"]: e["value"]
                for e in (evidence or [])
                if "diagnosis_source" not in e.get("kind", "")
            }
            item_context["evidence_kinds"] = list(evidence_kinds)

            def rules_fn(exc):
                from agents.fi_recall_warranty.warranty import _rules_narrative
                return {"narrative": _rules_narrative(row), "proceed": "file",
                        "detail": "",
                        "confidence": "low", "diagnosis_source": "rules"}

            return rw_llm.draft_claim(
                bedrock_client=bedrock_client,
                item_context=item_context,
                evidence_kinds=evidence_kinds,
                rules_fn=rules_fn,
                stats=_ms_w,
                model_id=model_id,
                guardrail_id=guardrail_id,
                guardrail_version=guardrail_version,
            )

    try:
        candidate_rows = rw_selection.select_warranty_candidates(
            stage, athena_client=athena_client, workgroup=workgroup,
            results_s3_path=results_s3_path)
        warranty_terms = rw_selection.select_warranty_terms(
            stage, athena_client=athena_client, workgroup=workgroup,
            results_s3_path=results_s3_path)
        stats = rw_warranty.run_warranty_loop(
            candidate_rows=candidate_rows,
            computed_at=computed_at,
            standing_fn=owner_resolver,
            lookup_live=lookup_live,
            write_finding=write,
            plan_fn=plan_fn_warranty,
            warranty_terms=warranty_terms,
            model_call_cap=model_call_cap,
            remaining_seconds=remaining_seconds,
        )
    except Exception as exc:
        if model_stats_w is not None:
            obs.record_tokens(model_stats_w.input_tokens + model_stats_w.output_tokens,
                              cost_usd=model_stats_w.cost_usd())
        record = obs.finish(abort_reason=type(exc).__name__)
        summary = _summary(record)
        emit_metrics(cloudwatch_client, stage, component, vins_selected=0,
                     artifacts_written=0, duplicates_skipped=0)
        _emit_run_record(run_record_writer, component=component, summary=summary,
                         agent_version=_FI_RW_AGENT_VERSION, stage=stage,
                         cloudwatch_client=cloudwatch_client,
                         agent_id=FI_RECALL_WARRANTY_AGENT_ID,
                         extra=_rw_record_fields({}, rows_selected=0,
                                                 model_stats=model_stats_w))
        raise

    for _ in range(stats["items_written"]):
        obs.record_artifact_written()
    for _ in range(stats["items_skipped_unchanged"]):
        obs.record_duplicate_skipped()

    if model_stats_w is not None:
        tokens = model_stats_w.input_tokens + model_stats_w.output_tokens
        obs.record_tokens(tokens, cost_usd=model_stats_w.cost_usd())

    record = obs.finish()
    summary = _summary(record)
    summary.update(stats)
    emit_metrics(cloudwatch_client, stage, component, vins_selected=len(candidate_rows),
                 artifacts_written=record.artifacts_written,
                 duplicates_skipped=record.duplicates_skipped)
    _emit_run_record(run_record_writer, component=component, summary=summary,
                     agent_version=_FI_RW_AGENT_VERSION, stage=stage,
                     cloudwatch_client=cloudwatch_client,
                     agent_id=FI_RECALL_WARRANTY_AGENT_ID,
                     extra=_rw_record_fields(stats, rows_selected=len(candidate_rows),
                                             model_stats=model_stats_w))
    return summary


def _run_warranty_from_env(stage: str, event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Production wiring for :func:`run_warranty` from the stack's environment."""
    import time as _time

    import boto3
    from botocore.config import Config

    from agents.tier2.artifact_writer import ArtifactWriter

    # Guardrail check before building any client (S4 / brief).
    guardrail_id = os.environ.get("FI_COST_GUARDRAIL_ID", "")
    guardrail_version = os.environ.get("FI_COST_GUARDRAIL_VERSION", "")
    if not guardrail_id or not guardrail_version:
        log.error("runner: event=fi_rw_guardrail_unset component=warranty "
                  "(FI_COST_GUARDRAIL_ID / FI_COST_GUARDRAIL_VERSION not set)")
        raise RuntimeError("FI_COST_GUARDRAIL_ID and FI_COST_GUARDRAIL_VERSION must be set")

    warranty_cap_raw = os.environ.get("FI_WARRANTY_MAX_MODEL_CALLS")
    model_call_cap = (
        _parse_rw_cap(warranty_cap_raw, source="FI_WARRANTY_MAX_MODEL_CALLS")
        if warranty_cap_raw is not None
        else _FI_WARRANTY_DEFAULT_CAP
    )

    model_id = os.environ.get("FI_RW_MODEL_ID", _FI_RW_DEFAULT_MODEL_ID)

    region = os.environ.get("DYNAMODB_REGION", "us-west-2")
    adp_region = os.environ.get("ADP_REGION", "us-east-1")
    workgroup = os.environ.get("ATHENA_WORKGROUP", f"cvx-{stage}-analytics")
    results_bucket = os.environ.get("ATHENA_RESULTS_BUCKET", "")
    if not results_bucket:
        raise RuntimeError("ATHENA_RESULTS_BUCKET must be set")
    results_s3_path = f"s3://{results_bucket}/tier2/fi-recall-warranty/"
    cms_vehicles_table = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{stage}-storage-vehicles")

    ddb_client = boto3.client("dynamodb", region_name=region)
    avx_writer = ArtifactWriter(
        table_name=os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{stage}-avx-findings"),
        dynamodb_client=ddb_client,
    )
    athena_client = boto3.client("athena", region_name=adp_region)
    bedrock_client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            read_timeout=_FI_RW_BEDROCK_READ_TIMEOUT_S,
            connect_timeout=_FI_RW_BEDROCK_CONNECT_TIMEOUT_S,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    # Landing fix L2: the run's own assigned-driver directory (see _run_recall_from_env).
    drivers = _assigned_driver_directory(stage, ddb_client=ddb_client)

    def owner_resolver(vin: str) -> StandingFacts:
        return resolve_standing_facts(vin, ddb_client=ddb_client, vehicles_table=cms_vehicles_table,
                                      drivers=drivers)

    budget_deadline = _time.monotonic() + _DEFAULT_MAX_WALL_SECONDS
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        def remaining_seconds() -> float:
            return min(context.get_remaining_time_in_millis() / 1000.0,
                       budget_deadline - _time.monotonic())
    else:
        def remaining_seconds() -> float:
            return budget_deadline - _time.monotonic()

    return run_warranty(
        stage=stage,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
        avx_writer=avx_writer,
        owner_resolver=owner_resolver,
        cloudwatch_client=boto3.client("cloudwatch", region_name=region),
        bedrock_client=bedrock_client,
        remaining_seconds=remaining_seconds,
        model_call_cap=model_call_cap,
        model_id=model_id,
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
    )


# ---------------------------------------------------------------------------
# service component: FI fleet-health agent (spec D8, task 3.8)
# ---------------------------------------------------------------------------


def _parse_fault_grounding_enabled(raw: str) -> bool:
    """Parse ``FAULT_GROUNDING_ENABLED`` strictly.

    Accepts only the literal strings ``"true"`` and ``"false"`` (case-insensitive).
    Any other value raises ``RuntimeError`` rather than defaulting silently —
    an operator who sets ``FAULT_GROUNDING_ENABLED=yes`` or leaves it blank
    must hear about it rather than discovering the flag was ignored.

    Mutation guard: changing the default to ``True`` when the value is absent
    or unrecognised → test_fault_grounding_flag_parse_strict fails.

    Raises
    ------
    RuntimeError
        If the value is not ``"true"`` or ``"false"``.
    """
    normalised = raw.strip().lower()
    if normalised == "true":
        return True
    if normalised == "false":
        return False
    raise RuntimeError(
        f"FAULT_GROUNDING_ENABLED must be 'true' or 'false', got {raw!r}. "
        "Set it to exactly 'true' to enable fault-based vehicle grounding, "
        "or 'false' (the default) to leave it off."
    )


def _parse_model_enabled(raw: Optional[str], name: str) -> bool:
    """Parse a model-enabled env var strictly.

    Absent → ``False``.  ``"true"`` / ``"false"`` (case-insensitive) → bool.
    Anything else → ``RuntimeError`` naming the variable.

    Mutation guard: a non-raising fallback here would silently enable or
    disable the model path; ``test_parse_model_enabled_strict`` catches it.
    """
    if raw is None:
        return False
    normalised = raw.strip().lower()
    if normalised == "true":
        return True
    if normalised == "false":
        return False
    raise RuntimeError(
        f"{name} must be 'true' or 'false' (or absent for false), got {raw!r}."
    )


def _service_record_fields(stats: dict[str, Any]) -> dict[str, Any]:
    """Fleet-health service counters carried into its run record (write_run_record names them)."""
    fields: dict[str, Any] = {"vins_selected": stats.get("vins_selected", 0)}
    # W4 (design answer 9): forward all counters to the run record so the
    # deploy stage's live check can read them.  Mutation guard: removing a
    # forward → test_run_service_run_record_carries_service_counters fails.
    for name in (
        "grounding_read_errors",
        "book_items_without_rooftops",
        "p0_directive_fallbacks",
        "vins_refused",
        "exhausted_items_written",
        "agent2_read_errors",
        "registry_read_errors",  # W7: vehicle-id and fleet-location read errors
        "vehicles_over_cap",      # W4: vehicles skipped by _MAX_VINS_SERVICE cap
        # G3F3.1 (a2 W2): managed-first selection fallback count (populated by run()).
        "managed_select_errors",
    ):
        if name in stats:
            fields[name] = int(stats[name])
    # empty_snapshot_keys is a list[str], not an int — forward as-is.
    if "empty_snapshot_keys" in stats:
        fields["empty_snapshot_keys"] = stats["empty_snapshot_keys"]
    return fields


def run_service(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    ddb_client: Any,
    findings_table: str,
    actions_table: str,
    groundings_table: str,
    vehicles_table: str,
    fleet_locations_table: str,
    avx_writer: Any,
    owner_resolver: Optional[Callable[[str], Any]] = None,
    cloudwatch_client: Optional[Any] = None,
    run_id: Optional[str] = None,
    run_record_writer: Optional[Callable] = None,
    now: Optional[datetime.datetime] = None,
    fault_grounding_enabled: bool = False,
    bedrock_client: Optional[Any] = None,
    model_config: Optional[Any] = None,
    remaining_seconds: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """One ``service`` run: select managed VINs, run ``run_health_loop``, emit metrics.

    Run record keyed ``agent-fi-fleet-health#service`` on every exit.
    AVX table only (no legacy health-findings artifact).

    Mutation guards
    ---------------
    - Drop ``drivers=`` from ``resolve_standing_facts`` → owner_id missing in the
      written Finding; test fails.
    - Drop ``emit_metrics`` on the abort (exception) exit → abort-path metric test fails.
    - Default ``fault_grounding_enabled`` to ``True`` → test_fault_grounding_flag_parse_strict
      fails (caught in ``_run_service_from_env``, not here).
    - Drop vehicle_id / position lookup → grounded_fn never called; rooftop order wrong.
    """
    from agents.fi_fleet_health import loop as fh_loop
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_recall_warranty.selection import check_grounded as _check_grounded
    from agents.tier2.writer import _query_live_row

    component = "service"
    if run_id is None:
        run_id = _utc_now()
    obs = RunObserver(run_id=run_id, stage=stage, component=component)
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    computed_at = _utc_now()

    # Immutable closures so the loop receives named-parameter callables.

    def grounded_fn(vehicle_id: str) -> tuple[bool, int]:
        return _check_grounded(
            vehicle_id, ddb_client=ddb_client, groundings_table=groundings_table
        )

    def history_fn(vin: str) -> tuple[dict, int]:
        return fh_loop.read_attempt_history_health(
            vin, ddb_client=ddb_client,
            findings_table=findings_table,
            actions_table=actions_table,
        )

    def standing_fn(vin: str) -> Any:
        if owner_resolver is not None:
            return owner_resolver(vin)
        return None

    def lookup_live(finding_key: str) -> Optional[dict[str, Any]]:
        return _query_live_row(
            finding_key=finding_key,
            table_name=avx_writer.table_name,
            dynamodb_client=avx_writer.dynamodb_client,
            now_str=_utc_now(),
        )

    def write_finding_fn(finding: dict[str, Any], standing: Any,
                         refresh_on_noop: bool = False,
                         update_on_same_hash: bool = False) -> dict[str, Any]:
        return avx_writer.write_finding(finding, standing=standing,
                                        refresh_on_noop=refresh_on_noop,
                                        update_on_same_hash=update_on_same_hash)

    def rv_fn(vin: str) -> Optional[dict[str, Any]]:
        return fh_sel.recall_visit_fn(
            vin, ddb_client=ddb_client,
            findings_table=findings_table,
            now=now_dt,
        )

    # Model path setup: build budget before the try so both exits can read it.
    model_budget: Optional[Any] = None
    if bedrock_client is not None:
        if model_config is None:
            raise ValueError("run_service: a Bedrock client needs a model_config (guardrail, caps)")
        from agents.fi_fleet_health.llm import ModelBudget as _ModelBudget
        model_budget = _ModelBudget(
            max_calls=model_config.max_model_calls,
            max_tokens=model_config.max_tokens,
        )
    _model_deferred: int = 0  # updated after loop; read by both exits

    try:
        _snap_keys = list(_SNAPSHOT_TABLES)
        _snap_kw = dict(
            athena_client=athena_client, workgroup=workgroup,
            results_s3_path=results_s3_path,
        )
        _dates_before = _read_snapshot_dates(stage, _snap_keys, **_snap_kw)
        _snapshot_date = _require_fresh(_dates_before, today=now_dt.date())
        _empty = _empty_keys(_dates_before)  # ADP keys whose marker says row_count == 0
        fault_rows = (
            [] if "vehicle_fault_status_daily" in _empty
            else fh_sel.select_fault_rows(
                stage, athena_client=athena_client, workgroup=workgroup,
                results_s3_path=results_s3_path,
            )
        )
        pm_rows = (
            [] if "pm_vehicle_status_daily" in _empty
            else fh_sel.select_pm_rows(
                stage, athena_client=athena_client, workgroup=workgroup,
                results_s3_path=results_s3_path,
            )
        )
        managed_vins = fh_sel.select_managed_vins(
            stage, athena_client=athena_client, workgroup=workgroup,
            results_s3_path=results_s3_path,
        )
        usage_rows = (
            [] if "vehicle_utilisation_daily" in _empty
            else fh_sel.select_daily_usage(
                stage, athena_client=athena_client, workgroup=workgroup,
                results_s3_path=results_s3_path,
            )
        )
        _dates_after = _read_snapshot_dates(stage, _snap_keys, **_snap_kw)
        if _dates_after != _dates_before:
            raise StaleSnapshotError("changed during run", dates=_dates_after)
        fleet_ids = list({r.get("fleet_id") for r in managed_vins if r.get("fleet_id")})
        agent2_rows, agent2_read_errors = fh_sel.select_agent2_findings(
            fleet_ids, ddb_client=ddb_client, findings_table=findings_table,
            now=now_dt,
        )
        vehicle_rows, counters = fh_sel.build_vehicle_rows(
            fault_rows=fault_rows, pm_rows=pm_rows, managed_vins=managed_vins,
            usage_rows=usage_rows, agent2_rows=agent2_rows,
        )

        # G3F3.1 (a2 W1): sort by the full classified severity (fault + Agent 2 + PM),
        # not fault bands only. Ties rotate by a date-salted VIN hash so the same
        # vehicles aren't cut every night.
        # Design answer 11: "uses the classified severity, and ties rotate by a
        # date-salted VIN hash so the same vehicles aren't cut every night."
        from agents.fi_fleet_health.bands import _most_severe as _fh_most_severe, _SEVERITY_ORDER
        import hashlib as _hl

        def _severity_key(row: dict) -> tuple[int, int]:
            faults = row.get("open_faults") or []
            a2 = row.get("agent2_findings") or []
            pm = row.get("pm_tasks") or []
            severity = _fh_most_severe(faults, a2, pm)
            order = _SEVERITY_ORDER.get(severity, len(_SEVERITY_ORDER)) if severity else len(_SEVERITY_ORDER)
            vin = row.get("vin", "")
            # Date-salted hash for tie-breaking: different rotation each night.
            salt = now_dt.date().isoformat()
            tiebreak = int(_hl.md5(f"{salt}:{vin}".encode()).hexdigest(), 16)
            return (order, tiebreak)  # lower order = higher severity, sort ascending

        _vehicles_over_cap: int = 0
        if len(vehicle_rows) > _MAX_VINS_SERVICE:
            # reverse=False: _severity_key returns (-order, tiebreak); lower tuple wins.
            vehicle_rows = sorted(vehicle_rows, key=_severity_key)
            _vehicles_over_cap = len(vehicle_rows) - _MAX_VINS_SERVICE
            vehicle_rows = vehicle_rows[:_MAX_VINS_SERVICE]

        # C5: populate vehicle_id and fleet-home position for each row.
        # grounded_fn uses vehicle_id; rank_rooftops uses position as origin.
        # Errors per VIN are counted in stats["registry_read_errors"] and
        # don't abort the run (fail-open on registry read errors).
        from agents.fi_recall_warranty.selection import (
            lookup_vehicle_cms_ids as _lookup_ids,
            lookup_fleet_locations as _lookup_locs,
            RegistryReadError as _RegistryReadError,
        )
        _loc_cache: dict[str, dict] = {}
        _registry_read_errors: int = 0  # W7: counts vehicle + fleet-location errors
        for row in vehicle_rows:
            vin = row.get("vin", "")
            # Only resolve if the assembler left these fields unset.
            if row.get("vehicle_id") is None:
                try:
                    ids = _lookup_ids(vin, ddb_client=ddb_client, vehicles_table=vehicles_table)
                except _RegistryReadError:
                    ids = None
                    _registry_read_errors += 1  # W7: count vehicle-id lookup failures
                if ids is not None:
                    vid, fleet_id_from_ids, location_id = ids
                    row["vehicle_id"] = vid
                    if location_id and fleet_id_from_ids and row.get("position") is None:
                        if fleet_id_from_ids not in _loc_cache:
                            try:
                                _loc_cache[fleet_id_from_ids] = _lookup_locs(
                                    fleet_id_from_ids, ddb_client=ddb_client,
                                    fleet_locations_table=fleet_locations_table,
                                )
                            except _RegistryReadError:
                                _registry_read_errors += 1  # W7: count loc-lookup failures
                                _loc_cache[fleet_id_from_ids] = {}
                        coords = _loc_cache[fleet_id_from_ids].get(location_id)
                        if coords:
                            row["position"] = {
                                "latitude": coords[0],
                                "longitude": coords[1],
                                "source": "fleet_home",
                            }

        # DMS stock rows for rooftop ranking.
        from agents.fi_recall_warranty.selection import select_parts_stock as _sps
        stock_rows = _sps(
            stage, athena_client=athena_client, workgroup=workgroup,
            results_s3_path=results_s3_path,
        )

        obs.record_rows_read(len(vehicle_rows))

        stats = fh_loop.run_health_loop(
            vehicle_rows=vehicle_rows,
            stock_rows=stock_rows,
            computed_at=computed_at,
            grounded_fn=grounded_fn,
            history_fn=history_fn,
            standing_fn=standing_fn,
            lookup_live=lookup_live,
            write_finding=write_finding_fn,
            recall_visit_fn=rv_fn,
            fault_grounding_enabled=fault_grounding_enabled,
            now=now_dt,
            bedrock_client=bedrock_client,
            model_config=model_config,
            model_budget=model_budget,
            remaining_seconds=remaining_seconds,
        )
        if model_budget is not None:
            _model_deferred = stats.get("model_deferred", 0)
        # W4/W10: fold agent2_read_errors into the loop stats so they reach the run record.
        stats["agent2_read_errors"] = agent2_read_errors
        # W7: fold registry read errors (vehicle-id + fleet-location lookups) into stats.
        stats["registry_read_errors"] = _registry_read_errors
        # W4: fold vehicles_over_cap into stats so the run record carries it.
        stats["vehicles_over_cap"] = _vehicles_over_cap
        # G3F6F1.5: snapshot date verified by freshness check above.
        stats["snapshot_date"] = _snapshot_date.isoformat()
        # AVX-M2: ADP keys whose marker said row_count==0 (rows not read from Athena).
        stats["empty_snapshot_keys"] = sorted(_empty)

    except Exception as exc:
        record = obs.finish(abort_reason=type(exc).__name__)
        summary = _summary(record)
        # Mutation guard: emit_metrics MUST be called on the abort exit too.
        emit_metrics(cloudwatch_client, stage, component, vins_selected=0,
                     artifacts_written=0, duplicates_skipped=0)
        _abort_extra: Optional[dict[str, Any]] = None
        if model_budget is not None:
            _abort_extra = {
                "model_calls": model_budget.calls,
                "tokens_used": model_budget.tokens_used,
                "cost_usd": model_budget.cost_usd(),
                "rules_fallbacks": model_budget.rules_fallbacks,
                "guardrail_interventions": model_budget.guardrail_interventions,
                "guardrail_errors": model_budget.guardrail_errors,
                # deferred omitted: the loop never returned, so the count is unknown
                # (review g43 cycle 1 S4).
            }
        _emit_run_record(
            run_record_writer, component=component, summary=summary,
            agent_version=AVX_AGENT_VERSION, stage=stage,
            cloudwatch_client=cloudwatch_client,
            agent_id=FI_FLEET_HEALTH_AGENT_ID,
            extra=_abort_extra,
        )
        raise

    for _ in range(stats["items_written"]):
        obs.record_artifact_written()
    for _ in range(stats["items_skipped_unchanged"]):
        obs.record_duplicate_skipped()

    record = obs.finish()
    summary = _summary(record)
    summary.update(stats)
    emit_metrics(
        cloudwatch_client, stage, component,
        vins_selected=stats.get("vins_selected", 0),
        artifacts_written=record.artifacts_written,
        duplicates_skipped=record.duplicates_skipped,
    )
    _success_extra = _service_record_fields(stats)
    if model_budget is not None:
        _success_extra.update({
            "model_calls": model_budget.calls,
            "tokens_used": model_budget.tokens_used,
            "cost_usd": model_budget.cost_usd(),
            "rules_fallbacks": model_budget.rules_fallbacks,
            "guardrail_interventions": model_budget.guardrail_interventions,
            "guardrail_errors": model_budget.guardrail_errors,
            "deferred": _model_deferred,
        })
    _emit_run_record(
        run_record_writer, component=component, summary=summary,
        agent_version=AVX_AGENT_VERSION, stage=stage,
        cloudwatch_client=cloudwatch_client,
        agent_id=FI_FLEET_HEALTH_AGENT_ID,
        extra=_success_extra,
    )
    return summary


def _run_service_from_env(stage: str) -> dict[str, Any]:
    """Production wiring for :func:`run_service` from the stack's environment."""
    import time as _time

    import boto3
    from botocore.config import Config

    from agents.fi_fleet_health.llm import ModelConfig as _FhModelConfig, parse_model_call_cap as _fh_parse_cap
    from agents.tier2.artifact_writer import ArtifactWriter

    region = os.environ.get("DYNAMODB_REGION", "us-west-2")
    adp_region = os.environ.get("ADP_REGION", "us-east-1")
    workgroup = os.environ.get("ATHENA_WORKGROUP", f"cvx-{stage}-analytics")
    results_bucket = os.environ.get("ATHENA_RESULTS_BUCKET", "")
    if not results_bucket:
        raise RuntimeError("ATHENA_RESULTS_BUCKET must be set")
    results_s3_path = f"s3://{results_bucket}/tier2/fi-fleet-health/"
    cms_vehicles_table = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{stage}-storage-vehicles")
    fleet_locations_table = os.environ.get(
        "CMS_FLEET_LOCATIONS_TABLE", f"cms-{stage}-storage-fleet-locations"
    )
    findings_table = os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{stage}-avx-findings")
    actions_table = os.environ.get("AVX_ACTIONS_TABLE", f"vsa-{stage}-avx-actions")
    groundings_table = os.environ.get(
        "CMS_VEHICLE_GROUNDINGS_TABLE", f"cms-{stage}-storage-vehicle-groundings"
    )

    # Strict boolean parse: only "true" / "false" accepted (default "false").
    # Mutation guard: default to True on unrecognised value → _parse_fault_grounding_enabled raises.
    _fge_raw = os.environ.get("FAULT_GROUNDING_ENABLED", "false")
    fault_grounding_enabled = _parse_fault_grounding_enabled(_fge_raw)

    # Model switch: parsed strictly; absent = off.
    model_enabled = _parse_model_enabled(
        os.environ.get("FI_SERVICE_MODEL_ENABLED"), "FI_SERVICE_MODEL_ENABLED"
    )

    bedrock_client = None
    model_config = None
    remaining_seconds = None

    if model_enabled:
        # Guardrail required before any client is built (decision 9).
        guardrail_id = os.environ.get("FI_COST_GUARDRAIL_ID", "")
        guardrail_version = os.environ.get("FI_COST_GUARDRAIL_VERSION", "")
        if not guardrail_id or not guardrail_version:
            log.error("runner: event=fi_fh_guardrail_unset component=service "
                      "(FI_COST_GUARDRAIL_ID / FI_COST_GUARDRAIL_VERSION not set)")
            raise RuntimeError(
                "FI_COST_GUARDRAIL_ID and FI_COST_GUARDRAIL_VERSION must be set"
            )
        cap = _fh_parse_cap(os.environ.get("FI_HEALTH_MAX_MODEL_CALLS"))
        model_config = _FhModelConfig(
            guardrail_id=guardrail_id,
            guardrail_version=guardrail_version,
            max_model_calls=cap,
            max_tokens=_component_max_tokens("service"),
        )
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                read_timeout=_FI_RW_BEDROCK_READ_TIMEOUT_S,
                connect_timeout=_FI_RW_BEDROCK_CONNECT_TIMEOUT_S,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )
        # The scheduled path gets no Lambda context here, so the run's wall budget is the
        # clock (TIER2_MAX_WALL_SECONDS, 600 in the stack, under the 15-minute timeout).
        budget_deadline = _time.monotonic() + _DEFAULT_MAX_WALL_SECONDS

        def remaining_seconds() -> float:
            return budget_deadline - _time.monotonic()

    ddb_client = boto3.client("dynamodb", region_name=region)
    avx_writer = ArtifactWriter(
        table_name=findings_table,
        dynamodb_client=ddb_client,
    )
    athena_client = boto3.client("athena", region_name=adp_region)
    # Mutation guard: passing drivers= is mandatory for resolve_standing_facts to
    # provide an owner_id.  Omitting it disables the assigned-driver projection.
    drivers = _assigned_driver_directory(stage, ddb_client=ddb_client)

    def owner_resolver(vin: str) -> StandingFacts:
        return resolve_standing_facts(
            vin, ddb_client=ddb_client, vehicles_table=cms_vehicles_table,
            drivers=drivers,
        )

    return run_service(
        stage=stage,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
        ddb_client=ddb_client,
        findings_table=findings_table,
        actions_table=actions_table,
        groundings_table=groundings_table,
        vehicles_table=cms_vehicles_table,
        fleet_locations_table=fleet_locations_table,
        avx_writer=avx_writer,
        owner_resolver=owner_resolver,
        cloudwatch_client=boto3.client("cloudwatch", region_name=region),
        fault_grounding_enabled=fault_grounding_enabled,
        bedrock_client=bedrock_client,
        model_config=model_config,
        remaining_seconds=remaining_seconds,
    )


# ---------------------------------------------------------------------------
# rebalance component: FI rebalancing agent (spec G4-D10, task 4.4)
# ---------------------------------------------------------------------------


def run_rebalance(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    ddb_client: Any,
    findings_table: str,
    vehicles_table: str,
    groundings_table: str,
    fleet_locations_table: str,
    avx_writer: Any,
    cloudwatch_client: Optional[Any] = None,
    run_id: Optional[str] = None,
    run_record_writer: Optional[Callable] = None,
    now: Optional[datetime.datetime] = None,
    bedrock_client: Any = None,
    model_id: str = "",
    guardrail_id: str = "",
    guardrail_version: str = "",
    max_model_calls_per_run: Optional[int] = None,
    min_net_benefit_usd: Optional[float] = None,
    ssm_client: Any = None,
) -> dict[str, Any]:
    """One ``rebalance`` run: calls ``agents.fi_rebalancing.agent.run_rebalance``.

    With *ssm_client* (production), the run first loads its U9 policy from SSM
    (``agents.fi_rebalancing.policy.load_from_ssm``) and installs it for the run; an absent,
    unreadable or malformed parameter aborts the run before any read or write (run record
    ``aborted``). Without it (tests), the agent uses whatever policy is already installed.

    Run record keyed ``agent-fi-rebalancing#rebalance`` on every exit.
    Fleet-scoped: does NOT call ``_assigned_driver_directory`` (spec G4-D9;
    ``standing=None`` for every write; ``test_owner_driver_directory`` pins the
    callers of ``_assigned_driver_directory``).

    Mutation guards
    ---------------
    - Drop ``emit_metrics`` on the abort exit → abort-path metric test fails.
    - Drop run record on abort exit → staleness alarm fires on an aborted run.
    - Pass ``refresh_on_noop=True`` to ``write_fn`` → items that should be refreshed
      only on content change are written unconditionally.
    - Pass ``refresh_on_noop=False`` to ``refresh_fn`` → identical-hash items are not
      kept alive (expiry not extended); test_refresh_fn_passes_refresh_on_noop fails.
    """
    import contextlib

    from agents.fi_rebalancing import agent as rebal_agent
    from agents.fi_rebalancing import policy as rebal_policy

    component = "rebalance"
    if run_id is None:
        run_id = _utc_now()
    obs = RunObserver(run_id=run_id, stage=stage, component=component)
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    computed_at = _utc_now()

    def write_fn(finding: dict[str, Any], standing: Any = None,
                 refresh_on_noop: bool = False) -> dict[str, Any]:
        return avx_writer.write_finding(finding, standing=None,
                                        refresh_on_noop=False)

    def refresh_fn(finding: dict[str, Any], standing: Any = None,
                   refresh_on_noop: bool = False) -> dict[str, Any]:
        return avx_writer.write_finding(finding, standing=None,
                                        refresh_on_noop=True)

    try:
        rebal_config: dict[str, Any] = {}
        if max_model_calls_per_run is not None:
            rebal_config["max_model_calls_per_run"] = int(max_model_calls_per_run)
        if min_net_benefit_usd is not None:
            rebal_config["min_net_benefit_usd"] = float(min_net_benefit_usd)
        run_policy = (
            rebal_policy.active(rebal_policy.load_from_ssm(stage, ssm_client))
            if ssm_client is not None else contextlib.nullcontext()
        )
        with run_policy:
            stats = rebal_agent.run_rebalance(
                stage=stage,
                athena_client=athena_client,
                workgroup=workgroup,
                results_s3_path=results_s3_path,
                ddb_client=ddb_client,
                findings_table=findings_table,
                vehicles_table=vehicles_table,
                groundings_table=groundings_table,
                fleet_locations_table=fleet_locations_table,
                write_fn=write_fn,
                refresh_fn=refresh_fn,
                now=now_dt,
                bedrock_client=bedrock_client,
                model_id=model_id,
                guardrail_id=guardrail_id,
                guardrail_version=guardrail_version,
                config=(rebal_config or None),
            )
    except Exception as exc:
        record = obs.finish(abort_reason=type(exc).__name__)
        summary = _summary(record)
        emit_metrics(cloudwatch_client, stage, component,
                     vins_selected=0, artifacts_written=0, duplicates_skipped=0)
        # An aborted run's model spend is not known here (the agent's stats are lost with
        # the exception), so its record carries no token or cost field rather than a 0.
        _emit_run_record(
            run_record_writer, component=component, summary=summary,
            agent_version=AVX_AGENT_VERSION, stage=stage,
            cloudwatch_client=cloudwatch_client,
            agent_id=FI_REBALANCING_AGENT_ID,
            extra={k: v for k, v in _rebalance_record_fields({}).items()
                   if k not in ("tokens_used", "cost_usd")},
        )
        raise

    artifacts = stats.get("move_items_written", 0) + stats.get("gap_items_written", 0)
    for _ in range(artifacts):
        obs.record_artifact_written()
    for _ in range(stats.get("items_unchanged", 0)):
        obs.record_duplicate_skipped()
    # The model's spend (every Converse attempt, retries too): the payload's tokens_used and
    # estimated_cost_usd come from the observer, the run record's from the extra fields.
    obs.record_tokens(int(stats.get("tokens_used", 0)),
                      cost_usd=float(stats.get("cost_usd", 0.0)))

    record = obs.finish()
    summary = _summary(record)
    summary.update(stats)
    emit_metrics(
        cloudwatch_client, stage, component,
        vins_selected=0,
        artifacts_written=record.artifacts_written,
        duplicates_skipped=record.duplicates_skipped,
    )
    _emit_run_record(
        run_record_writer, component=component, summary=summary,
        agent_version=AVX_AGENT_VERSION, stage=stage,
        cloudwatch_client=cloudwatch_client,
        agent_id=FI_REBALANCING_AGENT_ID,
        extra=_rebalance_record_fields(stats),
    )
    return summary


def _rebalance_record_fields(stats: dict[str, Any]) -> dict[str, Any]:
    """Counters forwarded from run_rebalance stats into its run record (write_run_record names them).

    W11: vins_selected = vehicles_checked (the number of vehicles the agent
    considered for eligibility; no selection cursor exists for the rebalance
    component).  open_items_read_errors, locations_incomplete,
    gap_items_without_vehicle and forecast_days_missing forwarded under their
    own names (W-n5 / W-b2, G4-2 F8), and candidates_forecast_days_missing
    (W-c3-1: what build_candidates dropped for a missing forecast day).  registry_read_errors is NOT written
    for the rebalance component — that field is the CMS reads counter for the
    service component.
    """
    return {
        "model_calls": int(stats.get("model_calls", 0)),
        # Prompt 40: the run record carried no token or cost field for this component.
        "tokens_used": int(stats.get("tokens_used", 0)),
        "cost_usd": float(stats.get("cost_usd", 0.0)),
        "grounding_read_errors": int(stats.get("grounding_read_errors", 0)),
        "rows_refused": int(stats.get("items_refused", 0)),
        "vins_selected": int(stats.get("vehicles_checked", 0)),
        "open_items_read_errors": int(stats.get("open_items_read_errors", 0)),
        "locations_incomplete": int(stats.get("locations_incomplete", 0)),
        "gap_items_without_vehicle": int(stats.get("gap_items_without_vehicle", 0)),
        "forecast_days_missing": int(stats.get("forecast_days_missing", 0)),
        "candidates_forecast_days_missing": int(
            stats.get("candidates_forecast_days_missing", 0)),
    }


def _run_rebalance_from_env(stage: str) -> dict[str, Any]:
    """Production wiring for :func:`run_rebalance` from the stack's environment."""
    import boto3

    from agents.tier2.artifact_writer import ArtifactWriter

    region = os.environ.get("DYNAMODB_REGION", "us-west-2")
    adp_region = os.environ.get("ADP_REGION", "us-east-1")
    workgroup = os.environ.get("ATHENA_WORKGROUP", f"cvx-{stage}-analytics")
    results_bucket = os.environ.get("ATHENA_RESULTS_BUCKET", "")
    if not results_bucket:
        raise RuntimeError("ATHENA_RESULTS_BUCKET must be set")
    results_s3_path = f"s3://{results_bucket}/tier2/fi-rebalancing/"
    findings_table = os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{stage}-avx-findings")
    cms_vehicles_table = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{stage}-storage-vehicles")
    fleet_locations_table = os.environ.get(
        "CMS_FLEET_LOCATIONS_TABLE", f"cms-{stage}-storage-fleet-locations"
    )
    groundings_table = os.environ.get(
        "CMS_VEHICLE_GROUNDINGS_TABLE", f"cms-{stage}-storage-vehicle-groundings"
    )

    ddb_client = boto3.client("dynamodb", region_name=region)
    avx_writer = ArtifactWriter(
        table_name=findings_table,
        dynamodb_client=ddb_client,
    )
    athena_client = boto3.client("athena", region_name=adp_region)

    # task 5.2: optional model wiring.  Reads the four env vars the stack sets
    # (FI_REBAL_MODEL_ID, FI_REBAL_MAX_MODEL_CALLS plus the guardrail pair
    # already set for Stage B / fi_cost).  If any of the three required pieces
    # is unset the runner skips the model path and uses rules only (same shape
    # as the ``bedrock_client=None`` test path in ``agents/fi_rebalancing/
    # agent.py``).  No RuntimeError here — the agent is defined to work
    # without a model, so a missing env var is a configuration choice (ships
    # disabled → ships without model, both are fine).
    #
    # The guardrail reuses Stage B's ``FI_COST_GUARDRAIL_ID`` /
    # ``FI_COST_GUARDRAIL_VERSION`` per spec D8 step 7's parenthetical ("Stage
    # B's guardrail").  Separate env var names would require a separate
    # Bedrock Guardrail resource; the current fi_cost guardrail blocks the
    # same PII and off-topic patterns the rebalance detail text has.
    model_id = os.environ.get("FI_REBAL_MODEL_ID", "")
    guardrail_id = os.environ.get("FI_COST_GUARDRAIL_ID", "")
    guardrail_version = os.environ.get("FI_COST_GUARDRAIL_VERSION", "")
    try:
        _raw_max_calls: int = int(
            os.environ.get("FI_REBAL_MAX_MODEL_CALLS", "12")
        )
        # Clamp to [0, _REBAL_MAX_MODEL_CALLS_CEILING]: negative → 0, >ceiling → ceiling
        # (T1.3 item 9).
        max_model_calls_per_run: Optional[int] = max(0, min(_raw_max_calls,
                                                            _REBAL_MAX_MODEL_CALLS_CEILING))
    except ValueError:
        max_model_calls_per_run = 12
    bedrock_client: Any = None
    if model_id and guardrail_id and guardrail_version:
        bedrock_client = boto3.client("bedrock-runtime", region_name=region)

    return run_rebalance(
        stage=stage,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
        ddb_client=ddb_client,
        findings_table=findings_table,
        vehicles_table=cms_vehicles_table,
        groundings_table=groundings_table,
        fleet_locations_table=fleet_locations_table,
        avx_writer=avx_writer,
        cloudwatch_client=boto3.client("cloudwatch", region_name=region),
        bedrock_client=bedrock_client,
        model_id=model_id,
        guardrail_id=guardrail_id,
        guardrail_version=guardrail_version,
        max_model_calls_per_run=max_model_calls_per_run,
        # The U9 policy (agents/fi_rebalancing/policy.py), seeded from
        # deployment/config/avx-policy-defaults.json; read inside the run so a
        # missing parameter still writes the aborted run record.
        ssm_client=boto3.client("ssm", region_name=region),
    )


# ---------------------------------------------------------------------------
# investigation component: FI quality agent via SQS (spec D10, task 3.8)
# ---------------------------------------------------------------------------


def _run_investigation_sqs(
    records: list[dict[str, Any]],
    context: Any,
) -> dict[str, Any]:
    """Handle a batch of SQS investigation records.

    Each record's body is the bare investigation id string (spec D10: "the
    investigation id only").  The id is validated against the UUID shape that
    ``POST /investigations`` creates — the shared regex is imported from
    ``agents.fi_quality.write.INVESTIGATION_ID_RE`` so the shape is defined
    in exactly one place.  Any body that does not match (wrong format, JSON
    object, empty string) is treated as a failed item — not a crash — so the
    message reaches the DLQ after one redrive.

    **ATHENA_RESULTS_BUCKET guard**: if the env var is unset every record is
    reported as a failed item immediately, without calling ``run_investigation``
    or touching AWS.  The run record and heartbeat are still emitted so the
    staleness alarm does not fire on a misconfigured Lambda.  This mirrors
    the guard in ``_run_recall_from_env``.

    Returns ``{"batchItemFailures": [...]}`` (may be empty on a fully successful batch).

    One run record keyed ``agent-fi-quality#investigation`` is written per Lambda
    invocation (per the heartbeat requirement); the heartbeat fires on every exit.

    Mutation guards
    ---------------
    - Report every SQS message as failed (return all messageIds) → test counting
      zero failures gets one failure instead → fails.
    - Never emit the run record / heartbeat → run-staleness alarm fires.
    - Parse body as JSON instead of bare string → contract test that feeds the
      route's actual MessageBody (a bare UUID) fails at the body-validation step
      because ``json.loads("<uuid>")`` raises ``JSONDecodeError``.
    - Accept any non-empty string instead of UUID-shaped only → test that feeds
      a body with correct UUID shape but wrong pattern fails to detect the mismatch.
    """
    import boto3

    from agents.fi_quality.job import run_investigation
    from agents.fi_quality.write import INVESTIGATION_ID_RE
    from agents.tier2.artifact_writer import ArtifactWriter
    from agents.tier2.run_records import get_runs_table_name, write_run_record

    stage = os.environ.get("VSA_STAGE", "staging")
    component = "investigation"
    run_id = _utc_now()
    obs = RunObserver(run_id=run_id, stage=stage, component=component)

    region = os.environ.get("DYNAMODB_REGION", "us-west-2")
    adp_region = os.environ.get("ADP_REGION", "us-east-1")
    workgroup = os.environ.get("ATHENA_WORKGROUP", f"cvx-{stage}-analytics")
    results_bucket = os.environ.get("ATHENA_RESULTS_BUCKET", "")

    # ── ATHENA_RESULTS_BUCKET guard ───────────────────────────────────────────
    # Mirrors _run_recall_from_env: refuse to run without the results bucket.
    # For SQS the refusal is graceful — every record is a failed item — so the
    # Lambda returns success and SQS can redrive once the env var is set.
    # The run record and heartbeat are still emitted so the staleness alarm does
    # not fire on a misconfigured Lambda.
    #
    # Mutation guard: remove this block → missing-bucket test passes despite no
    # bucket being set; run_investigation is called with an empty results_s3_path
    # and the Athena call silently stores results in an unrelated location.
    if not results_bucket:
        log.error(
            "runner: ATHENA_RESULTS_BUCKET env var is not set — "
            "all %d SQS investigation record(s) failed",
            len(records),
        )
        failed_all = [
            {"itemIdentifier": r["messageId"]}
            for r in records
            if r.get("messageId")
        ]
        cloudwatch_client = boto3.client("cloudwatch", region_name=region)
        record_obs = obs.finish(abort_reason="ATHENA_RESULTS_BUCKET not set")
        summary = _summary(record_obs)
        summary["vins_selected"] = 0
        emit_metrics(
            cloudwatch_client, stage, component,
            vins_selected=0,
            artifacts_written=0,
            duplicates_skipped=0,
        )
        _emit_run_record(
            None, component=component, summary=summary,
            agent_version=AVX_AGENT_VERSION, stage=stage,
            cloudwatch_client=cloudwatch_client,
            agent_id=FI_QUALITY_AGENT_ID,
        )
        return {"batchItemFailures": failed_all}

    results_s3_path = f"s3://{results_bucket}/tier2/fi-quality/"
    findings_table = os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{stage}-avx-findings")
    investigations_table = os.environ.get(
        "AVX_INVESTIGATIONS_TABLE", f"vsa-{stage}-avx-investigations"
    )

    ddb_client = boto3.client("dynamodb", region_name=region)
    avx_writer = ArtifactWriter(table_name=findings_table, dynamodb_client=ddb_client)
    athena_client = boto3.client("athena", region_name=adp_region)
    cloudwatch_client = boto3.client("cloudwatch", region_name=region)

    def _write_finding(finding: dict[str, Any]) -> Optional[dict[str, Any]]:
        result = avx_writer.write_finding(finding, standing=None)
        obs.record_artifact_written()
        # W5 fix: return the writer's result so run_investigation records the
        # authoritative finding_id (not the locally-generated UUID).  On a
        # _set_completed failure + redrive the writer deduplicates by finding_key
        # and returns the first row's finding_id; the job must use that id.
        return result

    failed: list[dict[str, str]] = []
    processed = 0

    # S2: parse SQS_MAX_RECEIVE_COUNT once before the loop so a bad value
    # fails the whole batch with a run record (same guard as ATHENA_RESULTS_BUCKET),
    # rather than failing per-record with no heartbeat.
    _max_receive_count_raw = os.environ.get("SQS_MAX_RECEIVE_COUNT", "2")
    try:
        max_receive_count = int(_max_receive_count_raw)
    except (ValueError, TypeError):
        log.warning(
            "runner: SQS_MAX_RECEIVE_COUNT=%r is not an integer — "
            "treating all records as failed",
            _max_receive_count_raw,
        )
        failed_all = [{"itemIdentifier": r.get("messageId", "")}
                      for r in records if r.get("messageId")]
        cloudwatch_client = boto3.client("cloudwatch", region_name=region)
        record_obs = obs.finish(abort_reason="SQS_MAX_RECEIVE_COUNT_INVALID")
        summary = _summary(record_obs)
        summary["vins_selected"] = 0
        emit_metrics(
            cloudwatch_client, stage, component,
            vins_selected=0,
            artifacts_written=0,
            duplicates_skipped=0,
        )
        _emit_run_record(
            None, component=component, summary=summary,
            agent_version=AVX_AGENT_VERSION, stage=stage,
            cloudwatch_client=cloudwatch_client,
            agent_id=FI_QUALITY_AGENT_ID,
        )
        return {"batchItemFailures": failed_all}

    # ── Model path wiring (task 4.3, stage A2) ───────────────────────────────
    # FI_INVESTIGATION_MODEL_ENABLED strict parse: absent → False, bad value →
    # fail every record and write the run record (same shape as the guards above).
    try:
        model_enabled = _parse_model_enabled(
            os.environ.get("FI_INVESTIGATION_MODEL_ENABLED"),
            "FI_INVESTIGATION_MODEL_ENABLED",
        )
    except RuntimeError as exc:
        log.error(
            "runner: event=fi_fh_model_enabled_invalid component=investigation error=%s", exc
        )
        failed_all = [{"itemIdentifier": r.get("messageId", "")}
                      for r in records if r.get("messageId")]
        record_obs = obs.finish(abort_reason="FI_INVESTIGATION_MODEL_ENABLED_INVALID")
        summary = _summary(record_obs)
        summary["vins_selected"] = 0
        emit_metrics(
            cloudwatch_client, stage, component,
            vins_selected=0, artifacts_written=0, duplicates_skipped=0,
        )
        _emit_run_record(
            None, component=component, summary=summary,
            agent_version=AVX_AGENT_VERSION, stage=stage,
            cloudwatch_client=cloudwatch_client,
            agent_id=FI_QUALITY_AGENT_ID,
        )
        return {"batchItemFailures": failed_all}

    bedrock_client = None
    model_config = None
    if model_enabled:
        from agents.fi_quality.llm import investigation_config as _inv_config
        from botocore.config import Config
        guardrail_id = os.environ.get("FI_COST_GUARDRAIL_ID", "").strip()
        guardrail_version = os.environ.get("FI_COST_GUARDRAIL_VERSION", "").strip()
        _guardrail_ok = bool(guardrail_id and guardrail_version)
        if _guardrail_ok:
            try:
                model_config = _inv_config(
                    guardrail_id=guardrail_id,
                    guardrail_version=guardrail_version,
                    max_tokens=_component_max_tokens("investigation"),
                )
            except ValueError:
                # Review g43 cycle 1 S2: a config ModelConfig refuses gets decision 9's
                # failure shape (every record failed, run record written), not a raise.
                _guardrail_ok = False
        if not _guardrail_ok:
            log.error(
                "runner: event=fi_fh_guardrail_unset component=investigation "
                "(FI_COST_GUARDRAIL_ID / FI_COST_GUARDRAIL_VERSION not set or invalid)"
            )
            failed_all = [{"itemIdentifier": r.get("messageId", "")}
                          for r in records if r.get("messageId")]
            record_obs = obs.finish(abort_reason="FI_COST_GUARDRAIL_UNSET")
            summary = _summary(record_obs)
            summary["vins_selected"] = 0
            emit_metrics(
                cloudwatch_client, stage, component,
                vins_selected=0, artifacts_written=0, duplicates_skipped=0,
            )
            _emit_run_record(
                None, component=component, summary=summary,
                agent_version=AVX_AGENT_VERSION, stage=stage,
                cloudwatch_client=cloudwatch_client,
                agent_id=FI_QUALITY_AGENT_ID,
            )
            return {"batchItemFailures": failed_all}
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                read_timeout=_FI_RW_BEDROCK_READ_TIMEOUT_S,
                connect_timeout=_FI_RW_BEDROCK_CONNECT_TIMEOUT_S,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )

    # Remaining-time callable: reads the Lambda context on every call so the
    # model path gets a live countdown, not a snapshot.  None when the context
    # does not expose get_remaining_time_in_millis (e.g. scheduled invocations
    # in unit tests that pass object() as context).
    if hasattr(context, "get_remaining_time_in_millis"):
        def _ctx_remaining_seconds() -> float:
            return context.get_remaining_time_in_millis() / 1000.0
    else:
        _ctx_remaining_seconds = None

    # Accumulates usage across all records for the run record (model path only).
    total_model_usage: dict[str, Any] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    for record in records:
        message_id = record.get("messageId", "")
        try:
            body_raw = record.get("body", "")
            # Body is the bare investigation id (spec D10: "the investigation id only").
            # Validate against the shared UUID shape from agents.fi_quality.write so the
            # shape is defined in one place.  Anything that does not match — JSON objects,
            # empty strings, stray whitespace only, wrong format — is a failed item.
            investigation_id = body_raw.strip() if isinstance(body_raw, str) else ""
            if not investigation_id or not INVESTIGATION_ID_RE.match(investigation_id):
                raise ValueError(
                    f"SQS body is not a valid investigation id: {body_raw!r}"
                )
        except Exception as exc:  # noqa: BLE001 — malformed body is a failed item
            log.warning(
                "runner: event=sqs_malformed_body messageId=%s error=%s",
                message_id, type(exc).__name__,
            )
            if message_id:
                failed.append({"itemIdentifier": message_id})
            continue

        # Read ApproximateReceiveCount from SQS record attributes (W2 fix).
        approx_receive_count = int(
            (record.get("attributes") or {}).get("ApproximateReceiveCount", "1")
        )
        # max_receive_count is parsed once before the loop (S2).

        # Deadline check (decision 10): do not start a new record when the Lambda
        # is near its time limit.  Both the Athena bundle read and the model calls
        # need headroom; 300 s covers two 150 s MODEL_CALL_RESERVE_S slots.
        # Mutation guard: lower margin to 0 → test two-sided boundary fails.
        if (_ctx_remaining_seconds is not None
                and _ctx_remaining_seconds() < _INVESTIGATION_START_MARGIN_S):
            log.warning(
                "runner: event=investigation_deadline_exceeded messageId=%s "
                "remaining_s=%.1f margin_s=%.1f — skipping remaining records",
                message_id, _ctx_remaining_seconds(), _INVESTIGATION_START_MARGIN_S,
            )
            if message_id:
                failed.append({"itemIdentifier": message_id})
            continue

        record_usage: dict[str, Any] = {}
        try:
            run_investigation(
                investigation_id,
                athena_client=athena_client,
                workgroup=workgroup,
                results_s3_path=results_s3_path,
                stage=stage,
                ddb_client=ddb_client,
                investigations_table=investigations_table,
                write_finding=_write_finding,
                approximate_receive_count=approx_receive_count,
                max_receive_count=max_receive_count,
                bedrock_client=bedrock_client,
                model_config=model_config,
                remaining_seconds=_ctx_remaining_seconds if model_enabled else None,
                model_usage_out=record_usage if model_enabled else None,
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001 — one failure must not block others
            log.warning(
                "runner: event=investigation_failed messageId=%s investigation_id=%s error=%s",
                message_id, investigation_id, type(exc).__name__,
            )
            if message_id:
                failed.append({"itemIdentifier": message_id})
        finally:
            # Review g43 cycle 1 W1: the job fills record_usage right after its model
            # calls, before the Finding write and completion, either of which can raise.
            # The spend happened, so it is counted whether the record then succeeds or not.
            if model_enabled:
                total_model_usage["calls"] += record_usage.get("calls", 0)
                total_model_usage["input_tokens"] += record_usage.get("input_tokens", 0)
                total_model_usage["output_tokens"] += record_usage.get("output_tokens", 0)

    # Mutation guard: every message reported as failed → test counting zero failures gets
    # at least one failure instead.
    record_obs = obs.finish(abort_reason=f"{len(failed)} of {len(records)} failed"
                             if failed else None)
    summary = _summary(record_obs)
    summary["vins_selected"] = processed
    emit_metrics(
        cloudwatch_client, stage, component,
        vins_selected=processed,
        artifacts_written=record_obs.artifacts_written,
        duplicates_skipped=record_obs.duplicates_skipped,
    )
    # Run record + heartbeat on every exit (mutation guard: drop the call → staleness alarm fires).
    model_extra: Optional[dict[str, Any]] = None
    if model_enabled:
        from agents.fi_fleet_health.llm import cost_usd as _cost_usd
        in_tok = total_model_usage["input_tokens"]
        out_tok = total_model_usage["output_tokens"]
        model_extra = {
            "model_calls": total_model_usage["calls"],
            "tokens_used": in_tok + out_tok,
            "cost_usd": _cost_usd(in_tok, out_tok),
        }
    _emit_run_record(
        None, component=component, summary=summary,
        agent_version=AVX_AGENT_VERSION, stage=stage,
        cloudwatch_client=cloudwatch_client,
        agent_id=FI_QUALITY_AGENT_ID,
        extra=model_extra,
    )

    return {"batchItemFailures": failed}


def _component_to_finding_kind(component: str) -> str:
    """Map a runner component name to an AVX finding_kind string.

    Spec: health-component findings map to ``health.<component>``.
    """
    return f"health.{component}"


def _severity_hint_to_severity(severity_hint: str) -> str:
    """Map an agent severity_hint to an AVX severity enum value.

    Agent 2 emits severity_hint values (``"routine"``, ``"attention"``,
    ``"p0"``); the AVX Finding schema uses a matching ``severity`` enum.
    Values not in the enum map to ``"routine"`` as a safe default.
    """
    _valid = {"p0", "attention", "soon", "routine", "informational"}
    return severity_hint if severity_hint in _valid else "routine"


class OwnerProjectionIntegrityError(RuntimeError):
    """Raised when the owner resolved for a VIN is unstable within one build.

    F11.2 Accept 4.  ``_build_artifact`` resolves the owner, then re-resolves it
    once immediately before handing the value to the writer.  A disagreement
    between the two reads means the value is not a reliable authorization fact,
    and a *wrong* projected owner is strictly worse than an absent one: it grants
    approve rights to the wrong caller.  So this raises rather than degrading to
    ``None``, and the caller's per-VIN error handling drops that one Finding.
    """


def _build_artifact(
    vin: str,
    component: str,
    rows: list[dict],
    agent_fields: dict[str, Any],
    *,
    tokens_used: int = 0,
    cost_usd: float = 0.0,
    owner_resolver: Optional[Callable[[str], Any]] = None,
    inputs_hash: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
) -> tuple[dict[str, Any], Any]:
    """Assemble a D2-conforming AVX Finding artifact from agent output.

    Produces a Finding dict that satisfies the AVX Finding schema, including
    all D2-required fields (``finding_key``, ``finding_id``, ``scope``,
    ``finding_kind``, ``audit_trail``).  Legacy fields (``pk``, ``sk``,
    ``finding``, ``severity_hint``) are also included for backward
    compatibility with tests that check the health-findings table's DDB key
    structure.

    Parameters
    ----------
    vin:
        Vehicle identifier (used for ``scope.vin`` and legacy ``pk``).
    component:
        Health component (``"battery"`` / ``"tires"`` / ``"brakes"``).
        Maps to ``finding_kind`` via ``_component_to_finding_kind()``.
    rows:
        The normalised ADP input rows for this VIN.  Used to compute
        ``inputs_hash``.
    agent_fields:
        Fields returned by the agent function: must include ``finding``,
        ``severity_hint``, ``confidence``, and ``evidence``.
    owner_resolver:
        F11.2.  Optional ``(vin) -> owner_id | None`` callable, normally
        :func:`agents.tier2.owner_resolver.resolve_owner_id` partially applied
        with a client and table name.  When omitted, no owner is resolved and the
        second tuple element is ``None`` — which is why every existing caller
        that does not pass it keeps its current behaviour exactly.

    Returns
    -------
    tuple[dict, str | None]
        ``(artifact, owner_id)``.

        The artifact is the D2-conforming Finding dict.  ``owner_id`` is returned
        **alongside** it rather than on it, deliberately: the artifact is
        schema-validated against ``finding.schema.json``
        (``additionalProperties: false``) and is hashed into ``inputs_hash``, so
        an extra key would either fail validation or change the hash.  Returning
        a tuple makes the DDB-item-vs-artifact boundary structural — the value is
        not in the dict, so no caller can accidentally persist it into the
        artifact — which is a stronger guarantee than a convention could give.

        Both the deterministic fallback (``run_health_agent``) and the
        Strands-backed production path (``make_strands_agent_fn``) produce
        ``agent_fields``; this function assembles the Finding; ``runner.run()``
        routes the write through ``writer.write_finding()`` (D2 algorithm),
        forwarding ``owner_id`` for projection onto the persisted item only.

    Raises
    ------
    OwnerProjectionIntegrityError
        If the owner resolved at the start of this call differs from the owner
        re-resolved immediately before returning.  See F11.2 Accept 4.
    """
    from agents.tier2.writer import compute_finding_key  # lazy import

    import uuid

    # ── F11.2: resolve the owner of record for this VIN ──────────────────────
    # First of two reads.  The resolver is fail-closed by contract: it returns
    # None for every unresolvable case and never raises, so no guard is needed
    # here and a registry problem cannot abort the run.
    resolved_standing: Any = None
    if owner_resolver is not None:
        resolved_standing = owner_resolver(vin)

    inputs_hash = inputs_hash if inputs_hash is not None else _compute_inputs_hash(rows)
    computed_at = _utc_now()
    ttl = _compute_ttl(ttl_seconds)
    expires_at = _ttl_to_iso(ttl)

    finding_kind = _component_to_finding_kind(component)
    severity = _severity_hint_to_severity(agent_fields.get("severity_hint", "routine"))

    # D2: stable identity key.  Identifies the open condition on this VIN/component.
    # Does NOT change as inputs drift.
    finding_key = compute_finding_key(
        agent_id="agent-2-vehicle-health",
        scope_field="vin",
        scope_value=vin,
        finding_kind=finding_kind,
    )

    # New finding_id minted here.  write_finding() will PRESERVE it if a live row
    # with the same finding_key already exists (in-place update path).  This id
    # is used only on the PutItem path (new row).
    finding_id = f"FIND-{uuid.uuid4()}"

    # Legacy pk/sk retained for backward compat with health-findings table key structure
    # and existing test assertions.
    pk = f"VEH#{vin}"
    sk = f"FINDING#{component}#{inputs_hash}"

    # The "finding" text from agent_fields becomes the AVX headline + detail.
    finding_text = agent_fields.get("finding", "")

    # The headline rule lives in agents.tier2.card_text. When the agent supplies a
    # headline (the coverage-verdict guard sets one it has checked), it is written
    # as-is so the guard's check covers this exact string; otherwise it is derived.
    from agents.tier2.card_text import derive_headline as _derive_headline
    _supplied = agent_fields.get("headline")
    if isinstance(_supplied, str) and _supplied.strip() and len(_supplied) <= 120:
        _headline = _supplied
    elif finding_text:
        _headline = _derive_headline(finding_text)
    else:
        _headline = f"{component.capitalize()} assessment."

    artifact = {
        # --- D2 AVX Finding schema fields ---
        "finding_id": finding_id,
        "finding_key": finding_key,
        "agent_id": "agent-2-vehicle-health",
        "agent_version": AGENT_VERSION,
        "scope": {"vin": vin},
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": expires_at,
        "confidence": agent_fields.get("confidence", "low"),
        "finding_kind": finding_kind,
        "headline": _headline,
        "detail": finding_text,
        "evidence": agent_fields.get("evidence", []),
        "inputs_hash": inputs_hash,
        # Real measured usage, passed in by run(). These were literal 0 / 0.0
        # until 2026-09-18, which meant the run SUMMARY could report 1650 tokens
        # while the persisted artifact claimed the run was free. The Tier 2
        # artifact contract exists so a reader can audit a finding; a cost field
        # that always says zero is not a neutral default, it is a false claim
        # about an artifact that cost money to produce.
        "tokens_used": tokens_used,
        "cost_usd": cost_usd,
        "audit_trail": [],
        # --- Legacy fields for backward compatibility ---
        "pk": pk,
        "sk": sk,
        "finding": finding_text,
        "severity_hint": agent_fields.get("severity_hint", "routine"),
        "ttl": ttl,
    }

    # ── F11.2 Accept 4: write-time owner-binding integrity ───────────────────
    # Second of two reads, immediately before the value leaves this function for
    # the writer.  Equality is asserted rather than assumed: the projected
    # owner_id becomes an authorization fact the approve route trusts, and a
    # value that was true at the start of this call but is not true now is not
    # safe to persist.  The check is INSIDE _build_artifact by design — putting
    # it downstream would let a future caller construct an artifact and write it
    # without ever running it.
    if owner_resolver is not None:
        reverify = owner_resolver(vin)
        if reverify != resolved_standing:
            raise OwnerProjectionIntegrityError(
                "owner resolution is unstable for this VIN within a single "
                f"artifact build (vin_hash={_mask_vin(vin)}, "
                f"component={component}); refusing to project a value that "
                "may not be current"
            )

    return artifact, resolved_standing


def _build_partial_artifact(
    component: str,
    partial_finding: str,
    _last_partial: Optional[str],
) -> dict[str, Any]:
    """Build a minimal well-formed partial artifact on budget breach.

    Uses a synthetic ``inputs_hash`` derived from the partial finding text
    so the artifact is structurally valid and idempotent.  The VIN is
    redacted in the partial artifact because on breach we may not have
    cleanly associated it.
    """
    synthetic_input = f"partial:{component}:{partial_finding}".encode()
    inputs_hash = hashlib.sha256(synthetic_input).hexdigest()
    computed_at = _utc_now()
    ttl = _compute_ttl()

    return {
        "pk": "VEH#REDACTED",
        "sk": f"FINDING#{component}#{inputs_hash}",
        "computed_at": computed_at,
        "confidence": "low",
        "evidence": [],
        "agent_version": AGENT_VERSION,
        "inputs_hash": inputs_hash,
        "finding": partial_finding,
        "severity_hint": "routine",
        "ttl": ttl,
    }


# ---------------------------------------------------------------------------
# Hash / TTL helpers
# ---------------------------------------------------------------------------


def _compute_inputs_hash(rows: list[dict]) -> str:
    """Compute a stable SHA-256 over normalised ADP rows.

    Normalisation (per spec § D4):
    1. Sort rows by ``(vin, computed_at)`` or all available keys if those are absent.
    2. Serialise each row's numeric fields with a fixed precision.
    3. Use JSON with sorted keys and no extra whitespace.
    4. Hash the UTF-8 bytes of the concatenated JSON strings.
    """
    import json

    def _normalise_value(v: Any) -> Any:
        # Athena's GetQueryResults returns every cell as `str | None`, so this
        # float branch does not fire for the three ADP sources. It exists for
        # non-Athena callers (and for tests, which pass real floats) so a future
        # source cannot destabilise inputs_hash — and therefore M6 — through
        # float repr differences. Documented rather than removed so it is not
        # mistaken for proof that precision instability has been handled on the
        # Athena path, where it cannot arise. Review Cycle 12 Suggestion 2.
        if isinstance(v, float):
            return f"{v:.6f}"
        return v

    def _normalise_row(row: dict) -> dict:
        return {k: _normalise_value(v) for k, v in row.items()}

    # Sort rows for stability; use all key-value pairs as the sort key.
    sorted_rows = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))
    serialised = json.dumps(
        [_normalise_row(r) for r in sorted_rows],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialised).hexdigest()


def _compute_ttl(seconds: Optional[int] = None) -> int:
    """Return an epoch-seconds TTL value *seconds* (default ``_TTL_SECONDS``) from now."""
    import calendar

    return calendar.timegm(
        (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=_TTL_SECONDS if seconds is None else seconds)
        ).timetuple()
    )


def _ttl_to_iso(ttl: int) -> str:
    """Convert an epoch-seconds TTL to an ISO-8601 UTC 'Z' string.

    Used to populate ``expires_at`` on D2-conforming Finding dicts from the
    same TTL value that drives DynamoDB TTL reaping.  Keeps the two values
    in sync without computing the datetime twice.
    """
    return (
        datetime.datetime.fromtimestamp(ttl, tz=datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string ending in 'Z'."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Production dependency wiring (lazy-imported to keep unit tests clean)
# ---------------------------------------------------------------------------


def _production_dependencies(
    stage: str, component: str, *, max_vins: Optional[int] = None, max_rows: int = _DEFAULT_MAX_ROWS
) -> tuple[FindingSource, ArtifactWriterProtocol, ArtifactWriterProtocol]:
    """Lazily import and instantiate the production source and writers.

    Import happens at invocation time, not at module load, so unit tests that
    inject doubles never trigger AWS SDK setup or environment-variable
    requirements.

    All configuration is consumed from the CDK-provided environment variables
    listed in spec § "The environment contract between the stack and the runner".
    The stack is the source of truth — it resolves account, region, and stage at
    synth time so the runner **derives nothing** and **reconstructs nothing**.

    Environment variables consumed (all set by the CDK stack):

    +------------------------+----------------------------------------------+
    | Variable               | Used for                                     |
    +========================+==============================================+
    | HEALTH_FINDINGS_TABLE  | DynamoDB table name for the legacy writer    |
    | AVX_FINDINGS_TABLE     | DynamoDB table name for the AVX writer       |
    | DYNAMODB_REGION        | boto3 DynamoDB client region                 |
    | ADP_REGION             | boto3 Athena client region (always us-east-1)|
    | ATHENA_WORKGROUP       | StartQueryExecution workgroup name           |
    | ATHENA_RESULTS_BUCKET  | Athena OutputLocation (fully resolved)       |
    +------------------------+----------------------------------------------+

    The account-id env var is intentionally absent from this list — Lambda's runtime
    does not set it, and the stack never needs to add it because the results bucket
    is already fully resolved (account and region substituted at synth time).
    Client-side reconstruction of a bucket name from a missing env var is the
    defect class eliminated by this contract (Critical #2, review Cycle 6).

    Returns
    -------
    tuple[FindingSource, ArtifactWriterProtocol, ArtifactWriterProtocol]
        ``(source, legacy_writer, avx_writer)`` — the source for fetching ADP
        rows, the legacy health-findings writer, and the AVX findings writer.

    Parameters
    ----------
    stage:
        Deployment stage, e.g. ``"staging"`` or ``"prod"``.
    component:
        Health component — must be a member of ``VALID_COMPONENTS``.
    max_vins:
        VIN cap forwarded to the source constructor.  Bounds how many
        distinct VINs the source samples when no explicit VIN list is passed.
    max_rows:
        Athena result-row ceiling forwarded to the source.  Raising
        ``AthenaResultTooLargeError`` rather than silently truncating (T6a.1).
    """
    import boto3

    from agents.tier2.artifact_writer import ArtifactWriter
    from agents.tier2.registry import AGENT_MODULES

    # Resolve per-component batch limit if not explicitly provided.
    # No global fallback — each component must have its own value (spec § D4).
    if max_vins is None:
        max_vins = _selection_limit_for_component(component)

    # ── DynamoDB writer ──────────────────────────────────────────────────────
    # Table names are resolved at synth time and delivered via env vars.
    # The env var names are declared in the registry entries:
    #   artifact_table_env_var     → "HEALTH_FINDINGS_TABLE"  (gains its first
    #   avx_findings_table_env_var → "AVX_FINDINGS_TABLE"      consumer here).
    # We read them as literal strings so the bidirectional env-contract test
    # (which parses runner.py's AST for os.environ.get(literal, ...) calls)
    # can verify they agree with the CDK stack's environment block.
    #
    # Registry-vs-literal drift is enforced by a TEST, not at runtime:
    # `test_registry_env_var_names_match_runner_literals`. This is deliberate and
    # replaces two runtime checks that Fix Group 17 review Cycle 2 showed could not
    # work (W3). The invariant is a DEVELOPER one — a registry field disagreeing with
    # a source literal in this same file cannot arise in a correctly-built
    # deployment, only from an edit — so a runtime check has no good option:
    #   - `raise` here aborts `lambda_handler` before `run()` is reached, so an
    #     AVX-only drift kills the legacy nightly health run. That is exactly what
    #     T16.2's Constraints forbid, and moving the check below the legacy writer's
    #     CONSTRUCTION did not fix it: constructing a writer is not writing with it.
    #   - degrading to `avx_writer=None` instead would silence the AVX write, which
    #     is the invisible-failure mode this group exists to prevent.
    # A test fails in CI before the code ships, which is the only placement with a
    # good outcome. `assert` was also the wrong mechanism regardless: `python -O`
    # strips it, so the guard was enforced by a statement the interpreter may delete.
    _module = AGENT_MODULES["agent-2-vehicle-health"]
    table_name = os.environ.get("HEALTH_FINDINGS_TABLE", f"vsa-{stage}-health-findings")
    avx_table_name = os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{stage}-avx-findings")
    dynamodb_region = os.environ.get("DYNAMODB_REGION", "us-west-2")

    ddb_client = boto3.client("dynamodb", region_name=dynamodb_region)
    writer = ArtifactWriter(table_name=table_name, dynamodb_client=ddb_client)

    avx_writer = ArtifactWriter(table_name=avx_table_name, dynamodb_client=ddb_client)

    # ── Athena client ────────────────────────────────────────────────────────
    # ADP lake and Athena workgroup are always in us-east-1 regardless of which
    # region the Lambda runs in (per decisions.md T1.2 and the CDK stack comment).
    # Consume the CDK-provided ADP_REGION rather than hardcoding the string here.
    adp_region = os.environ.get("ADP_REGION", "us-east-1")
    athena_client = boto3.client("athena", region_name=adp_region)

    # ── Athena workgroup + results bucket ────────────────────────────────────
    # Both are set by the stack to fully resolved values.  The results bucket
    # includes account id and region, substituted at synth time.  Reading them
    # here avoids any client-side reconstruction.
    workgroup = os.environ.get("ATHENA_WORKGROUP", f"cvx-{stage}-analytics")
    results_bucket = os.environ.get("ATHENA_RESULTS_BUCKET", "")
    if not results_bucket:
        # Fallback is intentionally fragile: a blank bucket fails at Athena
        # StartQueryExecution, which surfaces the misconfiguration immediately
        # rather than silently producing an unreachable path.
        log.error(
            "runner: ATHENA_RESULTS_BUCKET env var is not set — "
            "Athena queries will fail.  Ensure the CDK stack has deployed this variable."
        )
    results_s3_path = f"s3://{results_bucket}/tier2/" if results_bucket else ""

    # ── Source is component-specific ────────────────────────────────────────
    # Component set is driven from contract.VALID_COMPONENTS — adding a component
    # to the contract without wiring it here becomes a test failure, not a silent gap.
    if component == "battery":
        from agents.health.adp_battery_source import AdpBatterySource  # type: ignore[import]

        source: FindingSource = AdpBatterySource(
            stage,
            athena_client=athena_client,
            workgroup=workgroup,
            results_s3_path=results_s3_path,
            max_vins=max_vins,
            max_rows=max_rows,
        )
    elif component == "tires":
        from agents.health.adp_tire_source import AdpTireSource  # type: ignore[import]

        source = AdpTireSource(
            stage,
            athena_client=athena_client,
            workgroup=workgroup,
            results_s3_path=results_s3_path,
            max_vins=max_vins,
            max_rows=max_rows,
        )
    elif component == "brakes":
        from agents.health.adp_brake_source import AdpBrakeSource  # type: ignore[import]

        source = AdpBrakeSource(
            stage,
            athena_client=athena_client,
            workgroup=workgroup,
            results_s3_path=results_s3_path,
            max_vins=max_vins,
            max_rows=max_rows,
        )
    else:
        raise ValueError(
            f"Unknown component {component!r}; "
            f"must be one of {sorted(VALID_COMPONENTS)}"
        )

    return source, writer, avx_writer


# ---------------------------------------------------------------------------
# Cursor store helpers
# ---------------------------------------------------------------------------


class _NullCursorStore:
    """A no-op cursor store that always returns None (starts from pool beginning).

    Used when ``cursor_store`` is not injected and ``vins`` is None — safe but
    non-advancing: every run starts fresh.  Intended for tests that don't
    exercise selection; production always injects a real cursor store.
    """

    def get_cursor(self, component: str) -> Optional[dict[str, Any]]:
        return None

    def put_cursor(self, component: str, item: dict[str, Any]) -> None:
        pass  # writes are silently discarded


class _DynamoBackedCursorStore:
    """Production cursor store backed by the health-findings DynamoDB table.

    Reads cursor items via a direct ``GetItem`` call; writes via
    ``ArtifactWriter.put_raw`` (unconditional overwrite — the cursor is mutable
    state and must not pass ``validate_artifact``).

    Cursor items live at ``pk="CURSOR#{component}", sk="STATE"`` (spec § D3).
    They carry NO ``ttl`` attribute — DynamoDB TTL reaps only items that carry
    the attribute, and losing the cursor silently resets all pagination progress.
    """

    def __init__(
        self,
        writer: ArtifactWriterProtocol,
        *,
        table_name: str,
        ddb_client: Any,
    ) -> None:
        self._writer = writer
        self._table_name = table_name
        self._ddb_client = ddb_client

    def get_cursor(self, component: str) -> Optional[dict[str, Any]]:
        """Return the cursor item for *component*, or None if absent."""
        pk = f"CURSOR#{component}"
        try:
            response = self._ddb_client.get_item(
                TableName=self._table_name,
                Key={
                    "pk": {"S": pk},
                    "sk": {"S": "STATE"},
                },
            )
            item = response.get("Item")
            if item is None:
                return None
            # Deserialise from DynamoDB wire format ({"S": "..."}, {"N": "..."})
            # to plain Python types.
            return _deserialise_ddb_item(item)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "runner: cursor store get_cursor failed for component=%r: %s",
                component,
                exc,
            )
            return None

    def put_cursor(self, component: str, item: dict[str, Any]) -> None:
        """Persist *item* as the cursor for *component* (unconditional overwrite)."""
        self._writer.put_raw(item)


def _deserialise_ddb_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a DynamoDB wire-format item to plain Python types.

    Only handles the types used in cursor items:
      - ``{"S": "..."}``  → str
      - ``{"N": "..."}``  → int (all numeric cursor fields are integers)
      - ``{"NULL": True}`` → None
    """
    result: dict[str, Any] = {}
    for key, typed_val in item.items():
        if "S" in typed_val:
            result[key] = typed_val["S"]
        elif "N" in typed_val:
            # pool_exhausted_count is always an integer; parse as int.
            val = typed_val["N"]
            result[key] = int(val) if "." not in val else float(val)
        elif "NULL" in typed_val:
            result[key] = None
        else:
            # Unexpected type — store as-is; _read_cursor_safe handles malformed items.
            result[key] = typed_val
    return result


def _make_production_cursor_store(
    writer: ArtifactWriterProtocol,
) -> "_DynamoBackedCursorStore":
    """Construct the production cursor store from the same env vars the writer uses.

    Called from ``lambda_handler`` after the writer is already constructed.
    Re-reads the same table name and region env vars so the cursor store stays
    in the same table as the artifacts — no new configuration required.
    """
    import boto3

    table_name = os.environ.get("HEALTH_FINDINGS_TABLE", "vsa-unknown-health-findings")
    dynamodb_region = os.environ.get("DYNAMODB_REGION", "us-west-2")
    ddb_client = boto3.client("dynamodb", region_name=dynamodb_region)

    return _DynamoBackedCursorStore(
        writer=writer,
        table_name=table_name,
        ddb_client=ddb_client,
    )


def _default_agent_fn(cap: Optional[Any] = None) -> Callable[[str, list[dict]], dict]:
    """Return the production agent function for the default (Agent 2) module.

    Routes through the pluggable registry so new Tier 2 agent modules can be
    registered in ``agents.tier2.registry`` without touching this function.
    Lazy import keeps the Lambda cold-start shallow when unit tests inject a
    double via the ``agent_fn`` parameter of ``run()``.

    Wire-up (spec Group 2 — "Wire _default_agent_fn to make_strands_agent_fn"):
    The registry entry for Agent 2 exposes both ``run_agent`` (deterministic
    fallback) and ``make_strands_agent_fn`` (Strands-backed production path).
    This function calls ``make_strands_agent_fn()`` to obtain the LLM-backed
    callable.  That factory already carries a ``try/except ImportError`` fallback
    that returns ``run_health_agent`` when ``strands`` is not installed — so the
    deterministic path is preserved.  After the CDK bundling fix (commit 708eca4),
    ImportError should NEVER fire in production: if it does, a WARNING-level
    CloudWatch alarm (``vsa-{stage}-tier2-strands-import-error-{region}``) fires.

    C5 wall-clock circuit breaker (2026-10-04 review cycle 1): the per-batch
    LLM-call cap is read here so the Direction B env-contract test sees the
    stack-set variable consumed by runner.py, and threaded to the factory.

    W3 cycle-2 (2026-10-04): when ``cap`` is a :class:`BudgetCap`, a
    ``wall_deadline_fn`` is threaded into ``make_strands_agent_fn`` so the
    breaker trips before a slow run can exhaust the runner's wall budget.
    Pre-W3 the breaker was call-count-only — unsafe at the observed 4-6.6
    s/call on 2026-10-04.  When ``cap`` is ``None`` (older callers that
    constructed their own agent_fn), behaviour is unchanged.

    W3 cycle-3 (2026-10-04 Critical): the per-call safety margin alone was
    not enough.  When the breaker trips with ~1,900 brake VINs still to
    process, each one still runs a legacy put, an owner lookup and an AVX
    write (~0.05-0.1 s per VIN on staging), which at 6.6 s per model call
    could leave ~100 s of writes to do inside a 10 s margin — the
    shared-fake-clock simulation at 6.6 s/call still aborted on wall time.
    The breaker now also reserves time for the writes it will leave
    behind: on each call it reserves
    ``remaining_vins × TIER2_WALL_PER_VIN_WRITE_SECONDS``.  Per spec § D6
    this still trips the breaker, not the budget cap; the batch completes
    with ``abort_reason=None`` and every VIN gets an artifact (model-
    written if the call fit, deterministic if it did not).
    """
    from agents.tier2.registry import AGENT_MODULES  # local import — stays lazy

    # Read the per-batch LLM-call ceiling from the Lambda env.  Default 100
    # was sized for ~3 s/call; wall time, not call count, binds a slow batch.
    _max_llm_calls = int(os.environ.get("TIER2_MAX_LLM_CALLS_PER_BATCH", "100"))

    # W3 cycle-2: per-call wall-time safety margin.  Reserved for the model
    # call itself.  Read HERE in runner.py (not only in make_strands_agent_fn)
    # so Direction B of the env contract test — the stack sets no variable
    # the runner cannot show it reads — stays honest.
    _wall_safety = float(
        os.environ.get("TIER2_WALL_SAFETY_PER_CALL_SECONDS", "10.0")
    )

    # W3 cycle-3: per-VIN write cost the breaker reserves for the rest of
    # the batch when it trips.  Default 0.1 s above the measured 0.05-0.096 s
    # per VIN on staging.  Same Direction-B rationale as above.
    _wall_per_vin_write = float(
        os.environ.get("TIER2_WALL_PER_VIN_WRITE_SECONDS", "0.1")
    )

    # W3 cycle-2: thread the wall-time deadline through to the agent factory.
    # The factory's _llm_caller uses it to short-circuit before any call that
    # would plausibly exceed the remaining wall budget.
    wall_deadline_fn: Optional[Callable[[], float]] = None
    if cap is not None:
        # Capture ``cap`` by reference — ``elapsed_seconds`` is read on each call.
        wall_deadline_fn = lambda: cap.max_wall_seconds - cap.elapsed_seconds  # noqa: E731

    module = AGENT_MODULES["agent-2-vehicle-health"]
    return module.make_strands_agent_fn(
        max_llm_calls_per_batch=_max_llm_calls,
        wall_deadline_fn=wall_deadline_fn,
        wall_safety_per_call_seconds=_wall_safety,
        wall_per_vin_write_seconds=_wall_per_vin_write,
    )


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def _summary(record: Any) -> dict[str, Any]:
    """Convert a RunRecord to a Lambda-return-value dict."""
    return record.to_dict()


def _breaker_summary(agent_fn: Any) -> dict[str, Any]:
    """Read the LLM circuit-breaker counters off ``agent_fn`` (if present).

    Review cycle 3 Warning (2026-10-04): the counters that
    ``make_strands_agent_fn`` tracks (``llm_calls``, ``llm_skipped_by_breaker``,
    ``llm_breaker_trip_reason``) are exposed as attributes on the returned
    callable, but ``run()`` and ``_summary()`` were never reading them, so
    neither the ``tier2_run`` JSON record nor the ``vsa-{stage}-avx-agent-runs``
    row ever carried a breaker signal.  Without those fields the deploy stage
    cannot tell from a run record whether the breaker tripped, or on what.

    Reads via ``getattr`` with safe defaults so a test double or the
    deterministic-only ``run_health_agent`` fallback (returned by
    ``make_strands_agent_fn`` on ``ImportError``) still works.
    """
    try:
        llm_calls = int(getattr(agent_fn, "llm_calls", {}).get("n", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        llm_calls = 0
    try:
        skipped = int(getattr(agent_fn, "llm_skipped_by_breaker", {}).get("count", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        skipped = 0
    try:
        reason = str(getattr(agent_fn, "llm_breaker_trip_reason", {}).get("reason", "") or "")
    except (AttributeError, TypeError, ValueError):
        reason = ""
    # Classify the trip reason into a short label the deploy stage can switch
    # on without parsing a free-text sentence.  ``wall_time`` and ``call_count``
    # match the two branches of ``_llm_caller``; empty when the breaker never
    # fired.
    if not reason:
        tag = ""
    elif "wall budget low" in reason:
        tag = "wall_time"
    elif "has reached the per-batch limit" in reason:
        tag = "call_count"
    else:
        tag = "other"
    return {
        "llm_calls": llm_calls,
        "llm_skipped_by_breaker": skipped,
        "llm_breaker_trip_reason": tag,
    }


def _selection_record_extra(summary: dict[str, Any]) -> dict[str, Any]:
    """Selection fields the health run record carries: ``managed_select_errors`` always, and the
    U7 ``managed_coverage`` record when selection wrote one (absent keeps the record unchanged)."""
    extra: dict[str, Any] = {"managed_select_errors": summary.get("managed_select_errors", 0)}
    if summary.get("managed_coverage") is not None:
        extra["managed_coverage"] = summary["managed_coverage"]
    return extra


def _emit_run_record(
    run_record_writer: Optional[Callable],
    *,
    component: str,
    summary: dict[str, Any],
    agent_version: str,
    stage: Optional[str] = None,
    cloudwatch_client: Optional[Any] = None,
    agent_id: str = HEALTH_AGENT_ID,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Call ``run_record_writer`` (or the default ``write_run_record``) once.

    After a stored record, emits the ``RunRecordsWritten`` heartbeat
    (:func:`~agents.tier2.observability.emit_run_record_heartbeat`) that the
    run-staleness alarms watch. "Stored" is ``write_run_record`` returning
    ``True``, or an injected writer returning anything other than ``False``
    without raising. No heartbeat without ``stage`` and ``cloudwatch_client``.

    Swallows all exceptions so a failed write never fails the run (spec D10).
    When ``run_record_writer`` is ``None``, reads ``AVX_AGENT_RUNS_TABLE`` and
    creates a production DDB client to call :func:`~agents.tier2.run_records.write_run_record`.

    Parameters
    ----------
    run_record_writer:
        Injected callable, or ``None`` to use the production default.
        Signature: ``(*, agent_id, component, started_at, finished_at,
        status, artifacts_written, duplicates_skipped, agent_version, **kwargs)``
    component:
        Health component name (e.g. ``"battery"``).
    summary:
        The run summary dict returned by :func:`_summary`.
    agent_version:
        Plain semver string embedded in the record.
    stage, cloudwatch_client:
        Dimensions and client for the heartbeat; the caller's own ``stage`` and
        ``cloudwatch_client`` (the ones passed to ``emit_metrics``).
    agent_id:
        The record's agent (pk ``{agent_id}#{component}``). The health components use
        the default; the FI cost component passes ``FI_COST_AGENT_ID``.
    extra:
        Optional component-specific counters forwarded to the writer (the cost
        component's ``deferred``, ``model_calls`` and so on; ``write_run_record``
        names them). ``None`` keeps every existing record byte-identical.
    """
    kwargs = dict(
        agent_id=agent_id,
        component=component,
        started_at=summary.get("started_at", ""),
        finished_at=summary.get("finished_at", ""),
        status=summary.get("status", "completed"),
        artifacts_written=summary.get("artifacts_written", 0),
        duplicates_skipped=summary.get("duplicates_skipped", 0),
        agent_version=agent_version,
        # Cycle 3 Warning: carry the LLM circuit-breaker counters into the
        # DynamoDB run record so the deploy stage's live check can tell
        # whether the breaker tripped, and whether it tripped on time or on
        # call count.  Missing when the summary came from a path that does
        # not use ``make_strands_agent_fn`` (test doubles), hence the
        # ``.get(..., default)`` reads below.
        llm_calls=summary.get("llm_calls", 0),
        llm_skipped_by_breaker=summary.get("llm_skipped_by_breaker", 0),
        llm_breaker_trip_reason=summary.get("llm_breaker_trip_reason", ""),
    )
    if extra:
        kwargs.update(extra)
    stored = False
    try:
        if run_record_writer is not None:
            stored = run_record_writer(**kwargs) is not False
        else:
            from agents.tier2.run_records import (  # noqa: PLC0415
                get_runs_table_name,
                write_run_record,
            )
            import boto3  # noqa: F811,PLC0415
            try:
                table_name = get_runs_table_name()
            except RuntimeError:
                # Group 2 security review S3: a missing table name is a deploy defect,
                # not a transient write failure. Still no write and the run carries on
                # (D10), but logged at ERROR with its own event so it is not mistaken
                # for the write failure below; the run-staleness alarm then fires.
                log.error(
                    "runner: event=run_record_table_unset component=%s agent_id=%s "
                    "(AVX_AGENT_RUNS_TABLE is not set; no run record written)",
                    component,
                    agent_id,
                )
                return
            region = os.environ.get("DYNAMODB_REGION", "us-west-2")
            ddb = boto3.resource("dynamodb", region_name=region)
            stored = write_run_record(**kwargs, ddb_client=ddb, table_name=table_name) is True
    except Exception:  # noqa: BLE001
        log.warning(
            "runner: event=run_record_write_failed component=%s agent_id=%s",
            component,
            agent_id,
        )
        return
    if stored and stage and cloudwatch_client is not None:
        emit_run_record_heartbeat(cloudwatch_client, stage, component)
