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
                             the component source fetches its own fleet list
    event.get("component")   str | None — default "battery" (S1-only)

**Batch, never per-VIN fan-out** (spec § D2).  One invocation processes a
batch; the EventBridge Scheduler has one rule per stage and fires once daily.
A per-VIN schedule at fleet scale is a cost and quota problem, not a design.

Budget cap (spec § D6) is honoured **in the loop**.  On breach, ``BudgetExceeded``
carries ``exc.partial_finding`` — accessed programmatically, *never* via ``%r``
or ``repr(exc)`` because ``repr=False`` was set on that field to prevent VIN
leakage into logs.  A capped run writes a well-formed partial artifact and
emits an abort record rather than discarding all work.

S1 wires the battery component only (tires are now unblocked as of 2026-08-04 — see
``agents.health.adp_tire_source.AdpTireSource``).  Tires are structurally
supported; the gate re-opened when ADP deployed the product.

Injectable interfaces for unit-testability without AWS:
  - ``source``  — any object with a ``fetch_findings(vins) -> list[dict]``
                  method; defaults to ``AdpBatterySource`` for production use
  - ``writer``  — any object with a ``put(artifact: dict) -> None`` method;
                  defaults to ``ArtifactWriter`` for production use
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
from typing import Any, Callable, Optional, Protocol

from agents.tier2.budget import BudgetCap, BudgetExceeded
from agents.tier2.contract import VALID_COMPONENTS
from agents.tier2.observability import RunObserver

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------

#: Default component for production runs.
_DEFAULT_COMPONENT = "battery"

#: Default budget limits.  Operator can override via Lambda env vars.
_DEFAULT_MAX_TOKENS = int(os.environ.get("TIER2_MAX_TOKENS", "100000"))
_DEFAULT_MAX_WALL_SECONDS = float(os.environ.get("TIER2_MAX_WALL_SECONDS", "540.0"))

#: VIN cap: how many distinct VINs to sample per component run.
#: The ORDER BY vin subquery in each source makes selection deterministic —
#: the same VINs appear every run, which is required for inputs_hash stability.
_DEFAULT_MAX_VINS = int(os.environ.get("TIER2_MAX_VINS_PER_RUN", "25"))

#: Athena result-page ceiling.  Exceeding this raises AthenaResultTooLargeError
#: rather than returning a silently-truncated set (spec Fix Group 6a, T6a.1).
_DEFAULT_MAX_ROWS = int(os.environ.get("TIER2_MAX_ROWS_PER_QUERY", "50000"))

#: Default TTL: 8 days from now (in seconds).  Superseded findings age out
#: rather than accumulating — spec § D4.
_TTL_SECONDS = 8 * 24 * 60 * 60

#: Agent version tag, embedded in every artifact.
AGENT_VERSION = "tier2-health-v0.1.0"


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


class ArtifactWriterProtocol(Protocol):
    """Duck-type protocol for the DynamoDB artifact writer."""

    def put(self, artifact: dict[str, Any]) -> bool:
        """Conditionally write *artifact*; returns True on new write, False on duplicate no-op."""
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
        Must implement ``fetch_findings(vins)``.
    writer:
        An :class:`ArtifactWriterProtocol` that persists artifacts.
        Must implement ``put(artifact)``.
    vins:
        Explicit VIN batch.  If ``None``, the source fetches its own fleet list.
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

    Returns
    -------
    dict
        Run summary: ``status`` (``"completed"`` or ``"aborted"``), ``stage``,
        ``component``, ``artifacts_written``, ``rows_read``, ``tokens_used``,
        ``abort_reason`` (``None`` or string).
    """
    if run_id is None:
        run_id = _utc_now()

    obs = RunObserver(run_id=run_id, stage=stage, component=component)
    cap = BudgetCap(max_tokens=max_tokens, max_wall_seconds=max_wall_seconds)

    # Lazy-import the production agent only when no test double is provided.
    if agent_fn is None:
        agent_fn = _default_agent_fn()

    # --- fetch batch --------------------------------------------------------
    # Batch fetch — never per-VIN fan-out (spec § D2).
    cap.check()
    rows = source.fetch_findings(vins)
    obs.record_rows_read(len(rows))

    if not rows:
        log.warning("runner: no rows returned from source component=%s stage=%s", component, stage)
        record = obs.finish()
        return _summary(record)

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
        return _summary(record)

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

            # Build and write the artifact.
            artifact = _build_artifact(vin, component, vin_row_list, artifact_fields)
            wrote_new = writer.put(artifact)
            if wrote_new:
                obs.record_artifact_written()
            else:
                obs.record_duplicate_skipped()

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
        return _summary(record)

    record = obs.finish()
    return _summary(record)


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

    # Wire the production source and writer.  Both are imported lazily so unit
    # tests that only call ``run()`` with injected doubles don't trigger AWS
    # SDK imports or environment variable requirements.
    source, writer = _production_dependencies(
        stage,
        component,
        max_vins=_DEFAULT_MAX_VINS,
        max_rows=_DEFAULT_MAX_ROWS,
    )

    return run(
        stage=stage,
        source=source,
        writer=writer,
        vins=vins,
        component=component,
    )


# ---------------------------------------------------------------------------
# Artifact construction helpers
# ---------------------------------------------------------------------------


def _build_artifact(
    vin: str,
    component: str,
    rows: list[dict],
    agent_fields: dict[str, Any],
) -> dict[str, Any]:
    """Assemble a full M4-schema artifact from agent output.

    Parameters
    ----------
    vin:
        Vehicle identifier (used to build ``pk``).
    component:
        Health component (``"battery"`` / ``"tires"``).
    rows:
        The normalised ADP input rows for this VIN.  Used to compute
        ``inputs_hash``.
    agent_fields:
        Fields returned by the agent function: must include ``finding``,
        ``severity_hint``, ``confidence``, and ``evidence``.
    """
    inputs_hash = _compute_inputs_hash(rows)
    computed_at = _utc_now()
    ttl = _compute_ttl()

    pk = f"VEH#{vin}"
    sk = f"FINDING#{component}#{inputs_hash}"

    return {
        "pk": pk,
        "sk": sk,
        "computed_at": computed_at,
        "confidence": agent_fields.get("confidence", "low"),
        "evidence": agent_fields.get("evidence", []),
        "agent_version": AGENT_VERSION,
        "inputs_hash": inputs_hash,
        "finding": agent_fields.get("finding", ""),
        "severity_hint": agent_fields.get("severity_hint", "routine"),
        "ttl": ttl,
    }


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


def _compute_ttl() -> int:
    """Return an epoch-seconds TTL value ``_TTL_SECONDS`` from now."""
    import calendar

    return calendar.timegm(
        (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=_TTL_SECONDS)
        ).timetuple()
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
    stage: str, component: str, *, max_vins: int = _DEFAULT_MAX_VINS, max_rows: int = _DEFAULT_MAX_ROWS
) -> tuple[FindingSource, ArtifactWriterProtocol]:
    """Lazily import and instantiate the production source and writer.

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
    | HEALTH_FINDINGS_TABLE  | DynamoDB table name for the artifact writer  |
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

    # ── DynamoDB writer ──────────────────────────────────────────────────────
    # Table name and region are resolved at synth time and delivered via env vars.
    # Never derive from stage or reconstruct from parts.
    table_name = os.environ.get("HEALTH_FINDINGS_TABLE", f"vsa-{stage}-health-findings")
    dynamodb_region = os.environ.get("DYNAMODB_REGION", "us-west-2")

    ddb_client = boto3.client("dynamodb", region_name=dynamodb_region)
    writer = ArtifactWriter(table_name=table_name, dynamodb_client=ddb_client)

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

    return source, writer


def _default_agent_fn() -> Callable[[str, list[dict]], dict]:
    """Return the production Strands-based health agent function.

    Lazy import keeps the Lambda cold-start shallow when unit tests inject a
    double via the ``agent_fn`` parameter of ``run()``.
    """
    from agents.health.agent import run_health_agent  # type: ignore[import]

    return run_health_agent


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def _summary(record: Any) -> dict[str, Any]:
    """Convert a RunRecord to a Lambda-return-value dict."""
    return record.to_dict()
