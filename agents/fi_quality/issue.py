"""quality.issue Finding writer for the FI quality agent — Stage D task 3.3.

Contract § 3: a ``quality.issue`` Finding is written after the
``record_engineering_decision`` executor returns a 200 response with a non-null
``draft_id``.  It signals that a campaign draft exists and a second product-engineer
approval is needed to issue it.

Key rules (§ 3):
  - One per thread record: ``subject_key = "<thread_id>/issue"``
  - Population-scoped: ``scope.population_id = "meridian-motors"``
  - ``decided_by.kind`` carried from the Action — always ``"engineer"``
  - Server-set ``thread_id`` and ``draft_id`` (from the writer's 200 response)
  - Severity: ``"soon"`` for software_update / hardware_recall; ``"routine"`` otherwise
  - Expiry: 30 days
  - No model text in any field (neither reasoning nor tradeoffs)

Hook wiring (task 3.3 / registry.py):
  ``make_issue_hook()`` returns a callable matching the signature expected by
  ``AdpExecutor.on_decision_recorded`` — ``(action, response) -> None``.
  It is called only when ``body.get("draft_id") is not None`` (spec § 2.8 / adp.py).
  A missing ``AVX_FINDINGS_TABLE`` env var or any write error is logged and
  never raised (task constraint).

Module-level ``_make_ddb_client`` is the boto3 factory; the conftest AWS-guard
and tests use ``patch.object(issue_module, "_make_ddb_client")`` to replace it.

Public API
----------
ISSUE_FINDING_KIND   : str
ISSUE_EXPIRY_DAYS    : int

build_issue_finding_from_hook(action, response, *, computed_at_fn=None) -> dict
    Build a quality.issue Finding from the hook's (action, response) pair.

make_issue_hook(*, ddb_client=None) -> Callable
    Factory; returns the callable injected into AdpExecutor as
    ``on_decision_recorded``.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Mapping, Optional

from agents.tier2.writer import compute_finding_key

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Both issue Findings use the same agent_id as the investigation.
AGENT_ID: str = "agent-fi-quality"
AGENT_VERSION: str = "0.1.0"
ISSUE_FINDING_KIND: str = "quality.issue"
POPULATION_ID: str = "meridian-motors"

#: TTL in days (contract § 3.8).
ISSUE_EXPIRY_DAYS: int = 30

#: Remedy types that produce a draft and a 'soon' severity.
_DRAFT_REMEDY_TYPES: frozenset[str] = frozenset({
    "software_update",
    "hardware_recall",
})


# ---------------------------------------------------------------------------
# boto3 factory (patched in tests by the conftest AWS-guard)
# ---------------------------------------------------------------------------

def _make_ddb_client() -> Any:
    """Construct a boto3 DynamoDB client for us-west-2 (AVX findings region).

    Module-level so ``patch.object(issue_module, '_make_ddb_client')`` works
    in tests that need to inspect writes without real AWS.

    c W1 fix (design answer 8 of cycle 2): short timeouts and no retries so
    two hook attempts cannot outlast the approve route's Lambda budget (30 s).
    connect=2 s / read=3 s / total_max_attempts=1.

    Mutation guard (test_issue_hook_client_has_short_timeouts):
        A client with 60 s timeouts survives every hook call; the test asserts
        the config values match the short-timeout spec.
    """
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415
    return boto3.client(
        "dynamodb",
        region_name="us-west-2",
        config=Config(
            connect_timeout=2,
            read_timeout=3,
            retries={"total_max_attempts": 1},
        ),
    )


# ---------------------------------------------------------------------------
# ISO helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 Z string."""
    import datetime
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _iso_add_days(iso: str, days: int) -> str:
    """Add *days* to an ISO-8601 UTC string."""
    import datetime
    dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt + datetime.timedelta(days=days)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Evidence extractors (read from the investigation Finding, never model text)
# ---------------------------------------------------------------------------

def _extract_evidence_value(finding: Mapping[str, Any], kind: str) -> Optional[dict]:
    """Return the value dict of the first evidence entry with the given kind."""
    for entry in finding.get("evidence") or []:
        if isinstance(entry, Mapping) and entry.get("kind") == kind:
            val = entry.get("value")
            if isinstance(val, dict):
                return val
    return None


# ---------------------------------------------------------------------------
# Issue Finding builder
# ---------------------------------------------------------------------------

def build_issue_finding_from_hook(
    action: Mapping[str, Any],
    response: dict[str, Any],
    *,
    computed_at_fn: Optional[Callable[[], str]] = None,
) -> Optional[dict[str, Any]]:
    """Build a quality.issue Finding from a hook (action, response) pair.

    Returns ``None`` when the response has no ``draft_id`` or ``draft_type``
    (no campaign was created; nothing to issue).

    Mutation guard (test_issue_hook_does_not_include_model_reasoning):
        Copying ``quality.root_cause.reasoning`` into any quality.draft or
        quality.thread evidence field causes the sentinel assertion to fail.

    Parameters
    ----------
    action:
        The approved Action dict.  Must carry ``investigation_finding`` (the
        quality.investigation Finding), ``decided_by``, ``parameters``.
    response:
        The 200 JSON body from the digital-thread writer Lambda.  Keys used:
        ``thread_id``, ``version``, ``draft_id``, ``draft_type``.
    computed_at_fn:
        Optional callable ``() -> str``; defaults to ``_utc_now``.

    Returns
    -------
    dict or None
        quality.issue Finding dict, or ``None`` if no draft exists.
    """
    thread_id = response.get("thread_id")
    draft_id = response.get("draft_id")
    draft_type = response.get("draft_type")

    if not thread_id or not draft_id or not draft_type:
        return None

    computed_at = (computed_at_fn or _utc_now)()

    # ── Extract context from the investigation Finding (no model text) ────────
    inv_finding: Mapping[str, Any] = action.get("investigation_finding") or {}
    pattern_val = _extract_evidence_value(inv_finding, "quality.pattern") or {}
    code_family = pattern_val.get("code_family", "")

    decided_by = action.get("decided_by") or {}
    decided_by_kind = decided_by.get("kind")
    # W10 fix: refuse a missing decided_by.kind rather than defaulting to "engineer".
    if not decided_by_kind:
        log.error(
            "build_issue_finding_from_hook: missing decided_by.kind for thread=%s; "
            "returning None",
            thread_id,
        )
        return None

    action_params = action.get("parameters") or {}
    # decision_option_id from the Action (could be overridden from recommendation)
    decision_option_id = (
        action_params.get("option_id")
        or (inv_finding.get("recommended_action") or {})
        .get("parameters", {})
        .get("option_id")
        or ""
    )

    thread_version = int(response.get("version") or 1)

    # decided_at: use action.decided_at if set, otherwise now
    decided_at = action.get("decided_at") or computed_at

    # ── Severity (§ 3.3) ──────────────────────────────────────────────────────
    if draft_type in _DRAFT_REMEDY_TYPES:
        severity = "soon"
    elif draft_type:
        # W10 fix: unknown draft_type → return None and log error rather than
        # silently creating an issue_software_campaign with "Draft campaign" headline.
        log.error(
            "build_issue_finding_from_hook: unknown draft_type=%r for thread=%s; "
            "returning None to skip quality.issue write",
            draft_type, thread_id,
        )
        return None
    else:
        severity = "routine"

    # ── Headline (§ 3.4) ──────────────────────────────────────────────────────
    if draft_type == "hardware_recall":
        headline = f"Draft recall for {code_family}: approve to issue"
    elif draft_type == "software_update":
        headline = f"Draft software campaign for {code_family}: approve to issue"
    else:
        headline = f"Draft campaign for {code_family}: approve to issue"

    # ── subject_key (§ 3.2) ───────────────────────────────────────────────────
    subject_key = f"{thread_id}/issue"

    # ── finding_key ───────────────────────────────────────────────────────────
    finding_key = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="population_id",
        scope_value=POPULATION_ID,
        finding_kind=ISSUE_FINDING_KIND,
        subject_key=subject_key,
    )

    # ── Evidence (§ 3.6) — no model text ─────────────────────────────────────
    affected_vins_val = _extract_evidence_value(inv_finding, "quality.affected_vins") or {}
    affected_count = affected_vins_val.get("count") or 0

    evidence: list[dict[str, Any]] = [
        {
            "kind": "quality.thread",
            "source": "adp",
            "source_ref": thread_id,
            "value": {
                "thread_id":          thread_id,
                "thread_version":     thread_version,
                "decision_option_id": decision_option_id,
                "decided_by.kind":    decided_by_kind,
                "decided_at":         decided_at,
            },
            "computed_at": computed_at,
            "provenance": "live",
        },
        {
            "kind": "quality.draft",
            "source": "adp",
            "source_ref": draft_id,
            "value": {
                "draft_type":     draft_type,
                "draft_id":       draft_id,
                "campaign_id":    None,    # null while draft; set to campaign_id when issued
                "affected_count": affected_count,
            },
            "computed_at": computed_at,
            "provenance": "live",
        },
    ]

    # ── recommended_action (§ 3.7) ────────────────────────────────────────────
    # W7 (G3F1.5): map draft_type exhaustively — no fall-through to issue_software_campaign.
    # The W10 fix above already returns None for unknown draft_type, so only
    # hardware_recall and software_update reach here; the explicit else is a
    # defence-in-depth against future code changes that move the W10 check.
    if draft_type == "hardware_recall":
        action_kind = "issue_recall"
    elif draft_type == "software_update":
        action_kind = "issue_software_campaign"
    else:
        # Should not be reached after the W10 early-return above, but guard anyway.
        log.error(
            "build_issue_finding_from_hook: unrecognized draft_type=%r reached "
            "recommended_action builder for thread=%s; returning None",
            draft_type, thread_id,
        )
        return None

    recommended_action = {
        "action_kind": action_kind,
        "target_system": "adp",
        "parameters": {
            "thread_id": thread_id,   # server-set
            "draft_id":  draft_id,    # server-set
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
        "expires_at": _iso_add_days(computed_at, ISSUE_EXPIRY_DAYS),
        "confidence": "high",    # deterministic — no model inference for the issue Finding
        "finding_kind": ISSUE_FINDING_KIND,
        "headline": headline[:120],
        "detail": "",
        "evidence": evidence,
        "inputs_hash": "a" * 64,  # refresh-on-every-new-version; inputs_hash not used for dedup
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        "subject_key": subject_key,
        "recommended_action": recommended_action,
    }
    # No fleet_id anywhere (population-scoped).
    return finding


def _emit_hook_failure_metric() -> None:
    """Emit ``QualityIssueHookFailure`` metric (fire-and-forget, never raises)."""
    try:
        import os as _os  # noqa: PLC0415
        import boto3 as _boto3
        from botocore.config import Config as _Cfg  # noqa: PLC0415
        _cw = _boto3.client(
            "cloudwatch",
            region_name=_os.environ.get("AWS_REGION") or "us-west-2",
            config=_Cfg(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}),
        )
        _cw.put_metric_data(
            Namespace="AVX/Investigations",
            MetricData=[{
                "MetricName": "QualityIssueHookFailure",
                "Value": 1.0,
                "Unit": "Count",
            }],
        )
    except Exception:  # noqa: BLE001
        pass  # metric emission must never raise


# ---------------------------------------------------------------------------
# Hook factory
# ---------------------------------------------------------------------------

def make_issue_hook(
    *,
    ddb_client: Optional[Any] = None,
) -> Callable[[Mapping[str, Any], dict[str, Any]], None]:
    """Return the ``on_decision_recorded`` hook to inject into AdpExecutor.

    The hook is called by AdpExecutor._call_hook when the thread writer
    returns a 200 response with a non-null ``draft_id``.  It builds a
    quality.issue Finding and writes it through ``write_finding``.

    A missing ``AVX_FINDINGS_TABLE`` env var or any write error is logged
    and never raised (task constraint).

    Parameters
    ----------
    ddb_client:
        Optional injected DynamoDB client.  When None, the hook lazily calls
        ``_make_ddb_client()`` on first invocation.

    Returns
    -------
    Callable[[Mapping, dict], None]
        The hook callable.
    """
    _cached_client: list[Any] = []  # mutable container for lazy init

    def _hook(action: Mapping[str, Any], response: dict[str, Any]) -> None:
        import os  # noqa: PLC0415
        table_name = os.environ.get("AVX_FINDINGS_TABLE", "").strip()
        if not table_name:
            log.warning(
                "on_decision_recorded: AVX_FINDINGS_TABLE not set; "
                "skipping quality.issue write for thread %s",
                response.get("thread_id"),
            )
            # c W1 fix: emit metric on every failure path so 4.3 can alarm.
            _emit_hook_failure_metric()
            return

        finding = build_issue_finding_from_hook(action, response)
        if finding is None:
            log.debug(
                "on_decision_recorded: no draft_id / unknown draft_type in response; "
                "skipping quality.issue write"
            )
            # c W1 fix: a None finding means a draft exists but we can't write the
            # Finding — this is a failure path (draft left without a Finding).
            _emit_hook_failure_metric()
            return

        if ddb_client is not None:
            client = ddb_client
        elif _cached_client:
            client = _cached_client[0]
        else:
            client = _make_ddb_client()
            _cached_client.append(client)

        from agents.tier2.writer import write_finding  # noqa: PLC0415

        # W7 fix (decision 5): retry once inside the remaining dispatch budget,
        # then emit a CloudWatch metric on final failure so 4.3 can alarm on it.
        # The writer is idempotent on finding_key, so a retry is safe.
        _max_retries = 2
        last_exc: Optional[Exception] = None
        for attempt in range(_max_retries):
            try:
                write_finding(
                    finding,
                    table_name=table_name,
                    dynamodb_client=client,
                )
                log.debug(
                    "on_decision_recorded: wrote quality.issue finding for "
                    "thread %s draft %s (attempt %d)",
                    response.get("thread_id"),
                    response.get("draft_id"),
                    attempt + 1,
                )
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning(
                    "on_decision_recorded: attempt %d/%d failed for thread %s: %s",
                    attempt + 1, _max_retries,
                    response.get("thread_id"),
                    type(exc).__name__,
                )

        # All retries exhausted — emit a CloudWatch metric so 4.3 can alarm.
        # W7 fix (W4 c, decisions.md G3 cycle 2 answer 8): action_id is a top-level
        # Action field, not inside parameters (see action.schema.json, handler.py:1319).
        action_id = action.get("action_id", "")
        log.error(
            "on_decision_recorded: all %d attempts failed for thread %s "
            "action_id=%s: %s. Manual recovery: re-invoke the writer with the "
            "same action and response to write the quality.issue Finding.",
            _max_retries,
            response.get("thread_id"),
            action_id,
            type(last_exc).__name__ if last_exc else "unknown",
        )
        _emit_hook_failure_metric()

    return _hook
