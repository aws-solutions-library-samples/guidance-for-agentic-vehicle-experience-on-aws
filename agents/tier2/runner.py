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
from agents.tier2.contract import VALID_COMPONENTS
from agents.tier2.observability import RunObserver, emit_metrics
from agents.tier2.owner_resolver import StandingFacts
from agents.tier2.owner_resolver import mask_vin as _mask_vin
from agents.tier2.owner_resolver import resolve_standing_facts

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------

#: Default component for production runs.
_DEFAULT_COMPONENT = "battery"

#: Default budget limits.  Operator can override via Lambda env vars.
_DEFAULT_MAX_TOKENS = int(os.environ.get("TIER2_MAX_TOKENS", "100000"))
_DEFAULT_MAX_WALL_SECONDS = float(os.environ.get("TIER2_MAX_WALL_SECONDS", "540.0"))

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
#   battery→ 375  rows-bound: 375 × 100 = 37,500  (25.0% headroom under 50,000)
#   brakes → 2000 write-path-bound: ~2,140 rows (well under ceiling); bounded by
#                  sequential conditional PutItem + agent_fn calls within 300s
_MAX_VINS_TIRES   = int(os.environ.get("TIER2_MAX_VINS_TIRES",   "156"))
_MAX_VINS_BATTERY = int(os.environ.get("TIER2_MAX_VINS_BATTERY", "375"))
_MAX_VINS_BRAKES  = int(os.environ.get("TIER2_MAX_VINS_BRAKES",  "2000"))

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
    # Guard: if a new component is added to VALID_COMPONENTS without a corresponding
    # limit here, the run fails loudly rather than defaulting to any shared number.
    raise ValueError(
        f"No per-component VIN batch limit defined for component {component!r}. "
        f"Add a TIER2_MAX_VINS_{{COMPONENT}} env var and a branch in "
        f"_selection_limit_for_component().  Valid components: {sorted(VALID_COMPONENTS)}."
    )

#: Default TTL: 8 days from now (in seconds).  Superseded findings age out
#: rather than accumulating — spec § D4.
_TTL_SECONDS = 8 * 24 * 60 * 60

#: Agent version tag, embedded in every artifact.
AGENT_VERSION = "tier2-health-v0.1.0"

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
        agent_fn = _default_agent_fn()

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

        resolved_vins = _selection.resolve_batch(
            source=source,
            cursor_store=effective_cursor_store,
            component=component,
            limit=selection_limit,
        )
        vins_selected = len(resolved_vins)
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
        return summary

    # --- per-VIN judgment loop (still a batch: one invocation, N VINs) ------
    last_partial: Optional[str] = None

    try:
        for vin, vin_row_list in vin_rows.items():
            # Check budget *before* each unit of work (spec § D6: in the loop).
            cap.check()

            # Perform judgment — this is the Strands-agent call.
            artifact_fields = agent_fn(vin, vin_row_list)

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
        return summary

    record = obs.finish()
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
    stage = event.get("stage") or os.environ.get("VSA_STAGE", "staging")
    component = event.get("component", _DEFAULT_COMPONENT)
    vins: Optional[list[str]] = event.get("vins")

    log.info("runner: lambda_handler invoked stage=%s component=%s", stage, component)

    if component == "diagnostics":
        return _run_diagnostics_from_env(stage)

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

    # ── F11.2: VIN → owner-of-record resolver ────────────────────────────────
    # Bound to the CMS vehicle registry.  The table name is consumed fully
    # resolved from CMS_VEHICLES_TABLE (the stack is the source of truth and
    # this is the same contract variable lambdas/shared/cms_client_real.py
    # reads); nothing is derived from parts here.  The registry lives in the
    # Lambda's own region and account for every stage deployed to date; the
    # stack's IAM grant is what actually constrains which table is reachable.
    #
    # Degrading to owner_resolver=None when the variable is unset would silently
    # disable the projection and read as "no owners exist" — indistinguishable
    # from a registry with no sold_to rows.  So the default is explicit and the
    # resolver is always wired; unresolvable VINs are the resolver's own
    # fail-closed path, which is observable in logs.
    cms_vehicles_table = os.environ.get(
        "CMS_VEHICLES_TABLE", f"cms-{stage}-storage-vehicles"
    )
    cms_ddb_client = boto3.client("dynamodb", region_name=lambda_region)

    def owner_resolver(vin: str) -> StandingFacts:
        return resolve_standing_facts(
            vin,
            ddb_client=cms_ddb_client,
            vehicles_table=cms_vehicles_table,
        )

    return run(
        stage=stage,
        source=source,
        writer=writer,
        vins=vins,
        component=component,
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
) -> dict[str, Any]:
    """One ``diagnostics`` run: every vehicle with an active non-P0 CMS DTC.

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
        raise DiagnosticsRunError(f"{failures} of {len(groups)} diagnostics Finding write(s) failed")
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

    def owner_resolver(vin: str) -> StandingFacts:
        return resolve_standing_facts(vin, ddb_client=ddb_client, vehicles_table=cms_vehicles_table)

    return run_diagnostics(
        stage,
        source=source,
        avx_writer=avx_writer,
        owner_resolver=owner_resolver,
        cloudwatch_client=boto3.client("cloudwatch", region_name=region),
    )


# ---------------------------------------------------------------------------
# Artifact construction helpers
# ---------------------------------------------------------------------------


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


def _default_agent_fn() -> Callable[[str, list[dict]], dict]:
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
    """
    from agents.tier2.registry import AGENT_MODULES  # local import — stays lazy

    module = AGENT_MODULES["agent-2-vehicle-health"]
    return module.make_strands_agent_fn()


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def _summary(record: Any) -> dict[str, Any]:
    """Convert a RunRecord to a Lambda-return-value dict."""
    return record.to_dict()
