"""
Named shape transform: Agent 2 health artifact → AVX Finding.

Spec source:
    .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md § Fix Group 16
    → T16.1

Purpose
-------
A single public function ``health_artifact_to_finding()`` that converts the
dict produced by ``runner._build_artifact()`` into a **schema-valid** AVX
Finding conforming to ``agents/tier2/schemas/finding.schema.json``.

The runner's ``_build_artifact()`` already populates all D2 Finding fields
(``finding_id``, ``scope``, ``finding_kind``, ``severity``, etc.) but its
evidence entries carry only the agent-internal shape::

    {"source": ..., "value": ..., "provenance": ...}

The AVX Finding schema requires each evidence entry to also carry:
``kind``, ``source_ref``, and ``computed_at`` (see
``$defs.evidence_entry`` in the schema).  This transform enriches those
entries.

The transform also:
- Re-derives ``finding_key`` via the shared ``writer.compute_finding_key()``
  rather than forwarding the value already on the artifact.  This guarantees
  that the key is ALWAYS computed by the single canonical derivation — any
  divergence between the runner's pre-computed key and this module's result
  would be a D2 dedup bug, and the test suite catches it.
- Sets ``computed_at`` from the injected ``utc_now_fn`` (real time by
  default) so the Tier 2 artifact contract's staleness field is genuine.
- Strips legacy storage fields (``pk``, ``sk``, ``finding``,
  ``severity_hint``, ``ttl``) that are present on the runner's artifact for
  backward compatibility but are NOT part of the AVX Finding schema
  (``additionalProperties: false`` would reject them).
- Validates the output against the frozen schema before returning it.

Design invariants (T16.1 Verify mutations)
-----------------------------------------
(a) **Drop evidence[]** → ``test_evidence_required_by_schema`` fails.
    The Tier 2 artifact contract (agentic-tiers.md § "The Tier 2 artifact
    contract") requires evidence entries that support the finding.  A Finding
    asserting a conclusion it cannot support is the defect the contract names.
    The schema's ``required: ["evidence"]`` line enforces this; the test
    asserts schema validity AND that evidence is non-empty when the source
    artifact has evidence rows.

(b) **Hardcode ``computed_at``** → ``test_computed_at_is_fresh`` fails.
    Staleness must be real, per the artifact contract.  The test checks
    freshness RELATIVE to the call time (within a small window), NOT against
    a constant — which would admit any in-year hardcode, replicating the
    ``test_snapshot_date_must_not_be_hardcoded`` defect recorded in
    decisions.md.

(c) **Derive ``finding_key`` locally** → ``test_finding_key_matches_writer``
    fails.  A divergent key silently breaks D2 dedup and in-place update.
    The test asserts byte-equality with the value from
    ``writer.compute_finding_key()`` called on the same inputs.

No network calls, no boto3 — pure transformation.  Validated against the
frozen schema using the same ``Draft202012Validator`` the fixture tests use.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

from agents.tier2.runner import AVX_AGENT_VERSION as _AVX_AGENT_VERSION

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema loading (lazy-cached; the schema is frozen and never changes at
# runtime within a process lifetime)
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "finding.schema.json"
_SCHEMA: dict | None = None


def _get_schema() -> dict:
    global _SCHEMA  # noqa: PLW0603
    if _SCHEMA is None:
        _SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _SCHEMA


def _validate_finding(finding: dict) -> None:
    """Validate *finding* against the frozen AVX Finding schema.

    Uses ``Draft202012Validator`` — the same validator the fixture tests in
    ``tests/schemas/test_finding_action_schemas.py`` use.  Raises
    ``jsonschema.ValidationError`` on any violation.
    """
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(_get_schema())
    errors = list(validator.iter_errors(finding))
    if errors:
        messages = [e.message for e in errors]
        raise ValueError(
            f"AVX Finding failed schema validation ({len(errors)} error(s)): "
            + "; ".join(messages)
        )


# ---------------------------------------------------------------------------
# Evidence entry enrichment
# ---------------------------------------------------------------------------

#: Schema-required fields on each evidence entry.
_EVIDENCE_REQUIRED = frozenset({"kind", "source", "source_ref", "value", "computed_at", "provenance"})


def _enrich_evidence_entry(entry: dict[str, Any], *, run_ts: str) -> dict[str, Any]:
    """Ensure a single evidence entry carries all schema-required fields.

    Agent 2's deterministic builder produces entries with::

        {"source": "adp.tires.replacement_flagged", "value": ..., "provenance": "live"}

    The AVX Finding schema also requires ``kind``, ``source_ref``, and
    ``computed_at``.  This function backfills them from the existing fields
    rather than inventing values:

    - ``kind``:         The ``source`` string is already a namespaced
                        category (e.g. ``"adp.tires.replacement_flagged"``);
                        it is the best available ``kind`` value without
                        changing the agent contract.
    - ``source_ref``:   Set to ``"agent-2-health-run"`` — a stable,
                        non-unique sentinel that identifies the Tier 2 health
                        agent as the origin.  Agent 2 does not carry Athena
                        query IDs through to evidence entries; that would
                        require threading the source_ref through
                        ``adp_*_source.fetch_findings()``.  The field is
                        required by the schema but its value is opaque; this
                        honest sentinel is preferable to a fabricated query ID.
    - ``computed_at``:  Uses the run timestamp (``run_ts``) so the evidence
                        entry's time aligns with the finding's ``computed_at``.

    If an entry already carries a field (e.g. a future agent version enriches
    at source), the existing value is preserved.

    Parameters
    ----------
    entry:
        Single evidence entry dict, possibly partial.
    run_ts:
        ISO-8601 UTC timestamp of the transform run.

    Returns
    -------
    dict
        Schema-conforming evidence entry.
    """
    enriched = dict(entry)

    if "kind" not in enriched:
        # Use source as kind — it is already a namespaced category string.
        enriched["kind"] = enriched.get("source", "")

    if "source_ref" not in enriched:
        # Stable non-unique sentinel: the agent that produced this evidence.
        enriched["source_ref"] = "agent-2-health-run"

    if "computed_at" not in enriched:
        # Align evidence timestamp with the transform run.
        enriched["computed_at"] = run_ts

    return enriched


# ---------------------------------------------------------------------------
# TTL helper
# ---------------------------------------------------------------------------
# ``_FINDING_TTL_DAYS`` was deleted (Fix Group 17, T17.3).  Its value (7)
# diverged from the runner's canonical ``_TTL_SECONDS`` (8 days), causing AVX
# Findings to expire a day earlier than their legacy twins.  Expiry is now
# derived from the runner's ``_TTL_SECONDS`` so the two can never diverge.


def _expires_at_from_computed_at(computed_at: str, ttl_seconds: int | None = None) -> str:
    """Return ``computed_at + runner._TTL_SECONDS`` as an ISO-8601 UTC "Z" string.

    *ttl_seconds* overrides the window for a component with its own TTL (``diagnostics``,
    30 minutes; spec 2026-09-25-avx-own-vehicle-findings T4.2).

    Derives expiry from the runner's canonical TTL constant so the AVX
    Finding and the legacy artifact share the same expiry window.  The
    prior ``_FINDING_TTL_DAYS = 7`` invented a separate policy (7 days)
    that differed from the runner's 8-day TTL, causing D2 open-condition
    windows to close a day earlier on AVX Findings than on their legacy
    twins.

    Mutation guard: change ``_TTL_SECONDS`` in ``runner.py`` → the
    test asserting the expiry window tracks that constant fails.  Hardcode
    the expiry here → that test also fails.
    """
    from agents.tier2.runner import _TTL_SECONDS

    ts = datetime.datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
    expiry = ts + datetime.timedelta(seconds=_TTL_SECONDS if ttl_seconds is None else ttl_seconds)
    return expiry.isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Fields NOT in the AVX Finding schema (legacy storage fields on artifacts)
# ---------------------------------------------------------------------------

#: Legacy fields present on runner artifacts for backward compat with the
#: health-findings table.  The AVX Finding schema has
#: ``additionalProperties: false`` and rejects these.
_LEGACY_FIELDS = frozenset({"pk", "sk", "finding", "severity_hint", "ttl"})


# ---------------------------------------------------------------------------
# Public transform
# ---------------------------------------------------------------------------

def health_artifact_to_finding(
    artifact: dict[str, Any],
    *,
    utc_now_fn: Callable[[], str] | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Transform a health artifact produced by ``runner._build_artifact()``
    into a schema-valid AVX Finding.

    Parameters
    ----------
    artifact:
        Dict as returned by ``runner._build_artifact()``.  Must carry at
        minimum: ``agent_id``, ``scope``, ``finding_kind``, ``inputs_hash``,
        ``agent_version``, ``confidence``, ``severity``, ``headline``,
        ``detail``, ``evidence``, ``tokens_used``, ``cost_usd``,
        ``audit_trail``.

    utc_now_fn:
        Optional ``() -> str`` callable returning an ISO-8601 UTC "Z"
        string.  Injected by tests for deterministic timestamps.  Defaults
        to real wall-clock time.

    Returns
    -------
    dict
        Schema-valid AVX Finding.  Validated against
        ``agents/tier2/schemas/finding.schema.json`` before return.

    Raises
    ------
    ValueError
        If the output fails schema validation.
    KeyError
        If a required field is absent from *artifact*.
    """
    from agents.tier2.writer import compute_finding_key, _extract_scope

    # ── 1. Real-time timestamp ───────────────────────────────────────────────
    if utc_now_fn is None:
        def utc_now_fn() -> str:
            return (
                datetime.datetime.now(datetime.timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )

    run_ts: str = utc_now_fn()

    # ── 2. Extract scope (exactly one populated field required by schema) ────
    scope_field, scope_value = _extract_scope(artifact)

    # ── 3. Re-derive finding_key via the canonical writer function ───────────
    #
    # Mutation guard (c): if a caller or future code path re-derives the key
    # locally (even correctly), the test ``test_finding_key_matches_writer``
    # will compare byte-for-byte and fail if there is any deviation.  Calling
    # ``compute_finding_key()`` here is the ONLY acceptable derivation.
    finding_key = compute_finding_key(
        agent_id=artifact["agent_id"],
        scope_field=scope_field,
        scope_value=scope_value,
        finding_kind=artifact["finding_kind"],
    )

    # ── 4. Preserve or mint finding_id ──────────────────────────────────────
    finding_id: str = artifact.get("finding_id") or f"FIND-{uuid.uuid4()}"

    # ── 5. expires_at ────────────────────────────────────────────────────────
    #
    # If the artifact carries an expires_at from the runner (which computed it
    # at the time _build_artifact() ran), we recalculate from the new run_ts
    # so the expiry window is aligned with this transform's computed_at.
    expires_at: str = _expires_at_from_computed_at(run_ts, ttl_seconds)

    # ── 6. Enrich evidence entries ───────────────────────────────────────────
    #
    # Mutation guard (a): if evidence[] is dropped here the schema validator
    # fires on the missing required field — and the test asserts schema validity.
    raw_evidence: list[dict[str, Any]] = artifact.get("evidence", [])
    enriched_evidence: list[dict[str, Any]] = [
        _enrich_evidence_entry(entry, run_ts=run_ts) for entry in raw_evidence
    ]

    # ── 7. Assemble the Finding ──────────────────────────────────────────────
    finding: dict[str, Any] = {
        "finding_id": finding_id,
        "finding_key": finding_key,
        "agent_id": artifact["agent_id"],
        # Use the derived plain semver (e.g. "0.1.0") rather than the raw
        # AGENT_VERSION tag (e.g. "tier2-health-v0.1.0").  The AVX Finding
        # schema requires ``^[0-9]+\.[0-9]+\.[0-9]+$``; the tag fails it.
        # AVX_AGENT_VERSION is derived at import time and raises if unparseable.
        "agent_version": _AVX_AGENT_VERSION,
        "scope": artifact["scope"],
        "severity": artifact["severity"],
        "computed_at": run_ts,       # real time — mutation guard (b)
        "expires_at": expires_at,
        "confidence": artifact["confidence"],
        "finding_kind": artifact["finding_kind"],
        "headline": (artifact.get("headline") or "")[:120],
        "detail": artifact.get("detail", ""),
        "evidence": enriched_evidence,
        "inputs_hash": artifact["inputs_hash"],
        "tokens_used": artifact.get("tokens_used", 0),
        "cost_usd": float(artifact.get("cost_usd", 0.0)),
        "audit_trail": list(artifact.get("audit_trail", [])),
    }

    # ── 8. Validate against the frozen schema ────────────────────────────────
    _validate_finding(finding)

    return finding
