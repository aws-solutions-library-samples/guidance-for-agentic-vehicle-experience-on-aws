"""
``process_vehicle`` — D6 steps 3–6 for one vehicle (fi_cost Tier 2 component).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6, § D11, § D9
Decisions: decisions.md security review cycle 1, item 1 (ApplyGuardrail on
           headline and detail; GUARDRAIL_INTERVENED handled like a failed
           validation: one retry, then rules fallback; blocked text never reaches
           write_finding).
           decisions.md 2026-10-04 item 2 (severity from ADP; no band table here).

``process_vehicle`` is called once per vehicle in the run loop (Group 4, agent.py).
It owns D6 steps 3–6:

  Step 3 — compute inputs_hash; skip if the live Finding carries the same hash.
  Step 4 — one Bedrock Converse call forcing the record_diagnosis tool.
  Step 5 — validate the tool input; run ApplyGuardrail on headline and detail;
            on any failure, exactly one retry; then the rules fallback.
  Step 6 — add inputs_hash to the Finding and call write_finding().

Returns run stats: {"duplicates_skipped": int, "written": int, "tokens_used": int}.

Environment variables (read at call time; set by CDK in production):
  FI_COST_GUARDRAIL_ID      — guardrail identifier (ID string or full ARN)
  FI_COST_GUARDRAIL_VERSION — guardrail version ("DRAFT" or "1", "2", …)

``run_cost_loop`` (task 4.1) is the component's run: D6 steps 1–6 for every selected
vehicle plus D10's sell timing.  The runner (``agents/tier2/runner.run_cost``) wires
the AWS clients, writes the run record and emits the metrics.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import uuid
from typing import Any, Callable

from agents.fi_cost.diagnose import (
    AGENT_VERSION,
    PLAYBOOK_TEXT as _PLAYBOOK_TEXT,
    RECORD_DIAGNOSIS_TOOL_SCHEMA,
    TOOL_CONFIG,
    run_cost_diagnosis,
)
from agents.fi_cost.evidence import compute_inputs_hash
from agents.fi_cost.fallback import rules_fallback
from agents.fi_cost.validate import VALID_ACTIONS, VALID_CAUSES, validate_tool_input

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model configuration (environment-overridable, consistent with the Tier 2
# health component and the supervisor).
# ---------------------------------------------------------------------------

#: Claude Sonnet 4.6 through the us-west-2 cross-region inference profile.
#: Spec D6: "one Bedrock Converse call per vehicle, Claude Sonnet 4.6 through
#: the us-west-2 inference profile".
_DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"

# Sonnet 4.6 list prices (D11 citation: spec D11 "model cost").
# Source: Anthropic / AWS pricing as of Stage B design date.
# Used to compute cost_usd = input × $3 + output × $15 per million tokens.
_SONNET_4_6_INPUT_PRICE_PER_MILLION: float = 3.0   # USD per 1M input tokens
_SONNET_4_6_OUTPUT_PRICE_PER_MILLION: float = 15.0  # USD per 1M output tokens


def _model_id() -> str:
    return os.environ.get("FI_COST_MODEL_ID", _DEFAULT_MODEL_ID)


def _guardrail_id() -> str:
    """FI_COST_GUARDRAIL_ID: guardrail identifier (ID or ARN); see hand-back."""
    return os.environ.get("FI_COST_GUARDRAIL_ID", "")


def _guardrail_version() -> str:
    """FI_COST_GUARDRAIL_VERSION: guardrail version string; see hand-back."""
    return os.environ.get("FI_COST_GUARDRAIL_VERSION", "DRAFT")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def process_vehicle(
    evidence: dict[str, Any],
    live_finding: dict[str, Any] | None,
    bedrock_client: Any,              # boto3 bedrock-runtime client or stub
    write_finding: Callable,          # write_finding(finding, **kw) from writer.py
) -> dict[str, Any]:
    """D6 steps 3–6 for one vehicle.

    Parameters
    ----------
    evidence:
        Flat evidence dict for this vehicle (from ``gather_evidence``; includes
        all evidence keys from interface.md).
    live_finding:
        The vehicle's current Finding from DynamoDB, or ``None`` on first run.
        Used for the idempotency check in step 3.
    bedrock_client:
        boto3 ``bedrock-runtime`` client or a test stub.  Must implement
        ``.converse(**kwargs)`` and ``.apply_guardrail(**kwargs)``.
    write_finding:
        Callable with signature ``write_finding(finding, **kw)`` — the
        ``write_finding`` helper from ``agents.tier2.writer``.

    Returns
    -------
    dict
        Run statistics: ``{"duplicates_skipped": int, "written": int,
        "tokens_used": int}``.
    """
    # ---- Step 3: inputs_hash and skip-unchanged check (D11) ----
    inputs_hash = compute_inputs_hash(evidence)
    if live_finding is not None and live_finding.get("inputs_hash") == inputs_hash:
        log.debug(
            "fi_cost.agent: skip vin=%s (unchanged; hash=%s)",
            evidence.get("vin"),
            inputs_hash[:8],
        )
        return {"duplicates_skipped": 1, "written": 0, "tokens_used": 0}

    # ---- Step 4+5: Converse + validate + guardrail with exactly one retry ----
    tool_input, input_tokens, output_tokens, diagnosis_source = _diagnose_with_retry(
        evidence, bedrock_client
    )

    # ---- Step 6: build Finding, add inputs_hash + token accounting, write ----
    try:
        finding = run_cost_diagnosis(evidence, tool_input, diagnosis_source=diagnosis_source)
    except Exception as exc:  # noqa: BLE001 — a model reply that can't be built still gets a Finding
        if diagnosis_source != "model":
            raise
        log.warning("fi_cost.agent: model reply could not be built (%s); using rules fallback", exc)
        finding = run_cost_diagnosis(evidence, rules_fallback(evidence), diagnosis_source="rules")
    finding["inputs_hash"] = inputs_hash
    tokens_used = input_tokens + output_tokens
    finding["tokens_used"] = tokens_used
    finding["cost_usd"] = round(
        input_tokens * _SONNET_4_6_INPUT_PRICE_PER_MILLION / 1_000_000
        + output_tokens * _SONNET_4_6_OUTPUT_PRICE_PER_MILLION / 1_000_000,
        6,
    )

    write_finding(finding)
    return {"duplicates_skipped": 0, "written": 1, "tokens_used": tokens_used}


# ---------------------------------------------------------------------------
# Internal: Converse call with retry and guardrail
# ---------------------------------------------------------------------------


def _diagnose_with_retry(
    evidence: dict[str, Any],
    bedrock_client: Any,
) -> tuple[dict[str, Any], int, int, str]:
    """Make up to two Converse calls; return (tool_input, input_tokens, output_tokens, source).

    ``source`` is ``"model"`` when a model reply passed validation and the guardrail, and
    ``"rules"`` when the rules fallback produced the reply (the code path, never the reply's
    own fields).

    Flow (decisions security review cycle 1, item 1; C4 fix):

    Any exception from ``converse`` or ``apply_guardrail`` counts as a failed
    attempt: one retry, then the rules fallback.  The run never aborts on a
    model error (C4).

    Attempt 1:
      - Call converse (exception → failed attempt, retry).
      - Extract tool_use input.
      - Validate.
      - If validation passes: run ApplyGuardrail on headline + detail.
        - Exception from apply_guardrail → treat like GUARDRAIL_INTERVENED → retry.
        - If GUARDRAIL_INTERVENED: treat like failed validation → retry.
        - Otherwise: return (input, tokens).
      - If validation fails: retry (exactly one retry).

    Attempt 2 (retry):
      - Same as attempt 1.
      - If still fails: fall back.

    Fallback: rules_fallback(evidence) is returned — the run always produces a
    Finding even when the model is unavailable.  Tokens from failed attempts
    are kept; the fallback itself contributes none.
    """
    total_input: int = 0
    total_output: int = 0
    error_context: str = ""

    for attempt in range(2):
        # C4: wrap converse in try/except — exception counts as a failed attempt.
        try:
            resp, n_input, n_output = _converse(evidence, bedrock_client, error_context)
        except Exception as exc:
            error_context = f"Bedrock converse error: {exc}"
            log.warning(
                "fi_cost.agent: attempt %d — converse exception: %s", attempt + 1, exc
            )
            continue

        total_input += n_input
        total_output += n_output

        try:
            tool_input = _extract_tool_input(resp)
        except Exception as exc:  # noqa: BLE001 — a malformed response is a failed attempt
            tool_input = None
            log.warning("fi_cost.agent: attempt %d — unreadable response: %s", attempt + 1, exc)
        if tool_input is None:
            # Model did not call the tool; treat as validation failure.
            error_context = "Model did not call record_diagnosis. Use the tool."
            log.warning(
                "fi_cost.agent: attempt %d — no toolUse in response", attempt + 1
            )
            continue

        try:
            is_valid, validation_error = validate_tool_input(tool_input, evidence)
        except Exception as exc:  # noqa: BLE001 — never abort on a model reply (D6 step 5)
            is_valid, validation_error = False, f"reply could not be validated: {exc}"
        if not is_valid:
            error_context = f"Validation error: {validation_error}"
            log.warning(
                "fi_cost.agent: attempt %d — validation failed: %s",
                attempt + 1,
                validation_error,
            )
            continue

        # Validation passed — apply the guardrail to narration fields.
        # C4: wrap apply_guardrail in try/except — exception → treat like INTERVENED.
        try:
            blocked, trips = _guardrail_check(
                bedrock_client,
                tool_input.get("headline", ""),
                tool_input.get("detail", ""),
                vin=str(evidence.get("vin") or ""),
                cause=tool_input.get("cause"),
                action=tool_input.get("action"),
            )
        except Exception as exc:
            error_context = (
                f"Bedrock guardrail error: {exc}. "
                "Rewrite the headline and detail without PII or off-topic content."
            )
            log.warning(
                "fi_cost.agent: attempt %d — guardrail exception: %s", attempt + 1, exc
            )
            continue

        if blocked:
            error_context = _guardrail_retry_hint(trips, tool_input.get("action"))
            log.warning(
                "fi_cost.agent: attempt %d — guardrail INTERVENED", attempt + 1
            )
            continue

        # Both validation and guardrail passed.
        return tool_input, total_input, total_output, "model"

    # Both attempts failed — rules fallback.  Tokens spent on the failed attempts are kept
    # (they were billed); the fallback itself uses none.
    log.info("fi_cost.agent: both attempts failed; using rules fallback")
    return rules_fallback(evidence), total_input, total_output, "rules"


def _converse(
    evidence: dict[str, Any],
    bedrock_client: Any,
    error_context: str,
) -> tuple[dict[str, Any], int, int]:
    """Make one Bedrock Converse call.  Returns (response, input_tokens, output_tokens)."""
    vin = evidence.get("vin", "unknown")
    system_text = (
        f"{_PLAYBOOK_TEXT}\n\n"
        "You are diagnosing a cost anomaly for one fleet vehicle. "
        "Call the record_diagnosis tool exactly once."
    )

    # Include any prior validation error so the model can self-correct.
    user_content = _format_evidence_message(evidence)
    if error_context:
        user_content = f"Previous attempt failed: {error_context}\n\n{user_content}"

    response = bedrock_client.converse(
        modelId=_model_id(),
        system=[{"text": system_text}],
        messages=[{"role": "user", "content": [{"text": user_content}]}],
        toolConfig=TOOL_CONFIG,
    )

    usage = response.get("usage", {})
    n_input = usage.get("inputTokens", 0)
    n_output = usage.get("outputTokens", 0)
    return response, n_input, n_output


def _extract_tool_input(response: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the record_diagnosis tool input from a Converse response, or None."""
    content = (
        response.get("output", {})
        .get("message", {})
        .get("content", [])
    )
    for item in content:
        tool_use = item.get("toolUse")
        if tool_use and tool_use.get("name") == "record_diagnosis":
            return tool_use.get("input", {})
    return None


def _summarise_blocked_policies(resp: dict[str, Any]) -> list[dict[str, str]]:
    """Return a list of blocked-policy identifiers from an ApplyGuardrail response.

    Each entry is one blocked assessment with:
      - ``policy`` — ``topic``/``pii``/``regex``/``content``/``word``/``managed_word``
      - ``name``   — the configured name of the blocked item (topic name, PII type,
                      content filter type, word-list type). Never the detected text.

    PII `match` fields and `wordPolicy.customWords.match` fields are DELIBERATELY
    omitted — those fields can carry a slice of the blocked reply (e.g. ``Jane Doe``)
    and this summariser must never record reply text in logs. If the caller needs
    the detected string for debugging it must call ApplyGuardrail itself; the agent
    log is a public operational surface and must stay text-free.

    Decisions 2026-10-10 PO note on the FiCost shared guardrail: fi_recall_warranty
    agents share the same guardrail via FI_COST_GUARDRAIL_ID, and the 13:13Z stage
    C recall run tripped 36 of 95 replies without the agent code recording which
    policy fired. This helper is the mechanical fix.
    """
    trips: list[dict[str, str]] = []
    for a in resp.get("assessments", []) or []:
        for t in (a.get("topicPolicy") or {}).get("topics") or []:
            if t.get("action") == "BLOCKED":
                trips.append({"policy": "topic", "name": t.get("name", "")})
        for p in (a.get("sensitiveInformationPolicy") or {}).get("piiEntities") or []:
            if p.get("action") == "BLOCKED":
                trips.append({"policy": "pii", "name": p.get("type", "")})
        for r in (a.get("sensitiveInformationPolicy") or {}).get("regexes") or []:
            if r.get("action") == "BLOCKED":
                trips.append({"policy": "regex", "name": r.get("name", "")})
        for c in (a.get("contentPolicy") or {}).get("filters") or []:
            if c.get("action") == "BLOCKED":
                trips.append({"policy": "content", "name": c.get("type", "")})
        for w in (a.get("wordPolicy") or {}).get("customWords") or []:
            if w.get("action") == "BLOCKED":
                # `match` contains the matched word — omit to avoid leaking text.
                trips.append({"policy": "word", "name": "custom"})
        for w in (a.get("wordPolicy") or {}).get("managedWordLists") or []:
            if w.get("action") == "BLOCKED":
                trips.append({"policy": "managed_word", "name": w.get("type", "")})
    return trips


#: The fleet-action wording the playbook's Narration section requires for
#: ``driver_coaching``; the retry after a ``personal-advice`` block on a
#: ``driver_coaching`` reply repeats it.
#: Prompt 157 (2026-10-28): "driver coaching" read as personal advice to a person.
_FLEET_ACTION_DRIVER_COACHING = "enrol this vehicle in the fleet's eco-driving programme"


def _log_enum(value: Any, allowed: frozenset[str]) -> str:
    """Return *value* only if it is one of the closed enum values; otherwise ``?``.

    The INTERVENED log line must never carry reply text. Validation runs before the
    guardrail, so cause and action are enum values here; this keeps the line text-free
    even if a future caller passes something else.
    """
    return value if isinstance(value, str) and value in allowed else "?"


def _guardrail_retry_hint(trips: list[dict[str, str]], action: Any = None) -> str:
    """Build the retry's error context from the blocked policies (names only).

    Always names every blocked policy (type and configured name, never the detected
    text). When ``personal-advice`` is among them, adds the fleet-action instruction,
    and for a ``driver_coaching`` reply the playbook's wording for it.
    """
    named = ", ".join(sorted({f"{t.get('policy')} {t.get('name')}" for t in trips})) or "unknown"
    hint = (
        f"Output was blocked by the content guardrail ({named}). "
        "Rewrite the headline and detail without PII or off-topic content."
    )
    if any(t.get("policy") == "topic" and t.get("name") == "personal-advice" for t in trips):
        hint += (
            " The personal-advice topic read the reply as personal advice to a person: "
            "state the recommended action as a fleet action on this vehicle, and do not "
            "address a driver or describe a driver's behaviour."
        )
        if action == "driver_coaching":
            hint += f" For driver_coaching write: {_FLEET_ACTION_DRIVER_COACHING}."
    return hint


def _apply_guardrail(
    bedrock_client: Any,
    headline: str,
    detail: str,
    *,
    vin: str = "",
    cause: Any = None,
    action: Any = None,
) -> bool:
    """Apply the Bedrock Guardrail to headline and detail; True when blocked.

    A thin wrapper over ``_guardrail_check``; in fi_cost only tests call it.
    ``_diagnose_with_retry`` calls ``_guardrail_check`` directly (it needs the blocked
    policies for the retry hint), so patching this function does not affect retries.
    """
    return _guardrail_check(
        bedrock_client, headline, detail, vin=vin, cause=cause, action=action
    )[0]


def _guardrail_check(
    bedrock_client: Any,
    headline: str,
    detail: str,
    *,
    vin: str = "",
    cause: Any = None,
    action: Any = None,
) -> tuple[bool, list[dict[str, str]]]:
    """Apply the Bedrock Guardrail to headline and detail.

    Decisions security review cycle 1, item 1:
    ApplyGuardrail runs on headline and detail of every model reply with
    source='OUTPUT'.  Blocked is True when action is not exactly "NONE" (fail-closed).
    Any action other than "NONE" (including ``None``, ``""``, unknown values,
    or a missing key) blocks the text — T1.3 item 8.
    The guardrail ID and version are read from environment variables:
      FI_COST_GUARDRAIL_ID      — the guardrail identifier
      FI_COST_GUARDRAIL_VERSION — the version string

    When the guardrail returns GUARDRAIL_INTERVENED, the summarised assessment
    is logged (policy type + name only, never the reply text) so a reader of the
    Lambda logs can tell WHICH policy fired. Decisions 2026-10-10 PO note. The line
    also carries ``vin_hash`` (sha256[:8], never the VIN) and the reply's ``cause`` and
    ``action`` enum values (``?`` for anything else), so an intervention can be matched
    to its vehicle and diagnosis (prompt 157, 2026-10-28).

    Returns ``(blocked, trips)`` where ``trips`` is ``_summarise_blocked_policies``'s
    list (empty unless the guardrail intervened).
    """
    guardrail_id = _guardrail_id()
    guardrail_version = _guardrail_version()

    resp = bedrock_client.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=guardrail_version,
        source="OUTPUT",
        content=[
            {"text": {"text": headline}},
            {"text": {"text": detail}},
        ],
    )
    action_taken = resp.get("action")
    trips: list[dict[str, str]] = []
    if action_taken == "GUARDRAIL_INTERVENED":
        from agents.tier2.owner_resolver import mask_vin  # same fragment as the run loop's lines

        trips = _summarise_blocked_policies(resp)
        vin_hash = mask_vin(vin) if vin else "-"
        # json.dumps with sort_keys gives a stable, grep-friendly key=value shape.
        log.warning(
            "fi_cost.agent: guardrail INTERVENED; vin_hash=%s cause=%s action=%s "
            "policies_blocked=%s",
            vin_hash,
            _log_enum(cause, VALID_CAUSES),
            _log_enum(action, VALID_ACTIONS),
            json.dumps(trips, sort_keys=True),
        )
    # Fail-closed: block unless action is exactly "NONE" (T1.3 item 8).
    return action_taken != "NONE", trips


# ---------------------------------------------------------------------------
# Internal: evidence formatting for the model prompt
# ---------------------------------------------------------------------------


def _format_evidence_message(evidence: dict[str, Any]) -> str:
    """Format the evidence dict as a structured text for the model's user message."""
    # Pretty-print as JSON for the model; exclude internal underscore keys.
    clean = {k: v for k, v in evidence.items() if not k.startswith("_")}
    return (
        f"Vehicle VIN: {evidence.get('vin', 'unknown')}\n"
        f"Fleet: {evidence.get('fleet_id', 'unknown')}\n"
        f"Data month: {evidence.get('year_month', 'unknown')}\n"
        f"Anomaly flag: {evidence.get('cost.flag', 'total')}\n"
        f"Severity: {evidence.get('severity', 'routine')}\n\n"
        f"Evidence:\n{json.dumps(clean, indent=2, default=str)}"
    )



# ---------------------------------------------------------------------------
# Run loop (task 4.1): D6 steps 1–6 for every selected vehicle, and D10
# ---------------------------------------------------------------------------

#: ``max_diagnoses_per_run`` when no policy value is set (U5; spec D11).  The policy value
#: lives in ``deployment/config/avx-policy-defaults.json``, seeded into SSM.
DEFAULT_MAX_DIAGNOSES_PER_RUN: int = 50

#: Largest cap any configured or one-run value may set.  Bounds a run's model bill at
#: about 2 x 500 Converse calls even when an operator mistypes an override.
MAX_DIAGNOSES_CEILING: int = 500

#: Seconds kept at the end of a run for the run record and metrics.
FINAL_RESERVE_S: float = 30.0

#: Seconds one diagnosis may take in the worst case: two attempts, each a Converse call
#: and an ApplyGuardrail call at the runner's Bedrock client timeouts (30 s read + 5 s
#: connect, no botocore retries), so 4 x 35 = 140 s, plus the standing re-read and the
#: write. A vehicle is only started when this much time is left; the rest are deferred.
DIAGNOSIS_RESERVE_S: float = 150.0

#: Consecutive vehicles whose write failed before the run stops diagnosing (the rest are
#: deferred). A write path that always fails (a missing grant, a schema change) would
#: otherwise pay for a full cap of diagnoses that are never stored, on every invocation.
MAX_CONSECUTIVE_WRITE_FAILURES: int = 3

#: Seconds one sell-timing write may take: a live-row query, two standing reads and
#: the write.
SELL_TIMING_RESERVE_S: float = 5.0

#: Seconds between Athena polls (``adp_source.run_query``'s default).
ATHENA_POLL_INTERVAL_S: float = 1.0


class CostRunDeadlineExceeded(RuntimeError):
    """The run's wall time ran out before its Athena reads finished (spec D13).

    The runner records the run as aborted; the next daily run starts again.
    """


class _DeadlineAthena:
    """Checks the run's clock before every Athena call and delegates to the client.

    ``run_query`` bounds a query by a poll count, which leaves out each poll's round trip
    (us-west-2 to us-east-1) and the result paging after the query succeeds. Checking the
    clock on every call makes the deadline the wall clock: past it the run raises
    :class:`CostRunDeadlineExceeded` with ``FINAL_RESERVE_S`` still left for the run record
    (spec D13: a query past its deadline aborts the run).
    """

    def __init__(self, client: Any, remaining_seconds: Callable[[], float]) -> None:
        self._client = client
        self._remaining = remaining_seconds

    def _check(self) -> None:
        if self._remaining() < FINAL_RESERVE_S:
            raise CostRunDeadlineExceeded("no wall time left for the Athena read")

    def start_query_execution(self, **kwargs: Any) -> Any:
        self._check()
        return self._client.start_query_execution(**kwargs)

    def get_query_execution(self, **kwargs: Any) -> Any:
        self._check()
        return self._client.get_query_execution(**kwargs)

    def get_query_results(self, **kwargs: Any) -> Any:
        self._check()
        return self._client.get_query_results(**kwargs)


class _CountingBedrock:
    """Counts Converse calls and their tokens, and delegates every call to the client.

    The count is taken before the call, so an attempt that raises still counts: it may
    have been billed, and the run record must not report fewer model calls than were made.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.guardrail_interventions = 0

    def converse(self, **kwargs: Any) -> Any:
        self.calls += 1
        response = self._client.converse(**kwargs)
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict):
            for attr, key in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens")):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    setattr(self, attr, getattr(self, attr) + value)
        return response

    def apply_guardrail(self, **kwargs: Any) -> Any:
        response = self._client.apply_guardrail(**kwargs)
        if isinstance(response, dict) and response.get("action") == "GUARDRAIL_INTERVENED":
            self.guardrail_interventions += 1
        return response


def _utc_iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _persisted_standing(live: dict[str, Any]) -> dict[str, Any]:
    from agents.tier2.writer import _STANDING_ATTRS

    return {attr: live[attr] for attr in _STANDING_ATTRS if live.get(attr) is not None}


def _desired_standing(vin: str, standing: Any) -> dict[str, str]:
    from agents.tier2.writer import _standing_projection

    return _standing_projection({"scope": {"vin": vin}}, standing)


def _is_unchanged(live: dict[str, Any] | None, inputs_hash: str, vin: str, standing: Any) -> bool:
    """True when the live Finding has this ``inputs_hash`` AND the same standing projection.

    A vehicle that moved fleet keeps its cost inputs, so the hash alone would leave the
    old fleet's projection on the item (and the old fleet's operators able to approve it)
    for up to 35 days.  The writer converges standing only when it is called, so a
    standing change is treated as a change (Fix Group 25 C1, the same drift).
    """
    if live is None or live.get("inputs_hash") != inputs_hash:
        return False
    return _desired_standing(vin, standing) == _persisted_standing(live)


def _new_stats() -> dict[str, Any]:
    return {
        "data_month": None,
        "anomalies_selected": 0,
        "invalid_vins": 0,
        "evidence_missing": 0,
        "duplicates_skipped": 0,
        "diagnosed": 0,
        "deferred": 0,
        "deferred_cap": 0,
        "deferred_wall_time": 0,
        "deferred_write_failures": 0,
        "anomalies_written": 0,
        "anomalies_noop": 0,
        "rules_fallbacks": 0,
        "guardrail_interventions": 0,
        "sell_timing_candidates": 0,
        "sell_timing_rejected_vins": 0,
        "sell_timing_written": 0,
        "sell_timing_skipped": 0,
        "sell_timing_deferred": 0,
        "owner_integrity_degraded": 0,
        "write_failures": 0,
        "model_calls": 0,
        "tokens_used": 0,
        "cost_usd": 0.0,
    }


def run_cost_loop(
    *,
    stage: str,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    bedrock_client: Any,
    write_finding: Callable[[dict[str, Any], Any], dict[str, Any]],
    lookup_live: Callable[[str], dict[str, Any] | None],
    resolve_standing: Callable[[str], Any] | None,
    max_diagnoses_per_run: int,
    remaining_seconds: Callable[[], float],
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """One ``cost`` run: D6 steps 1–6 for every selected vehicle, and D10's sell timing.

    Order:

    1. The latest complete data month (newest ``fleet_cost_events`` partition up to the
       month before ``now``).  None → nothing published yet; the run says so.
    2. Select (``select_anomalies``, not capped) and gather evidence for every selected
       VIN in one query; read the sell-timing inputs.
    3. Sell timing (D10): deterministic, no model call; written or skipped by hash.
    4. For each selected vehicle in excess order: skip if unchanged (same
       ``inputs_hash`` and standing as its live Finding: no call, no write); otherwise
       diagnose while fewer than ``max_diagnoses_per_run`` diagnoses have run, the
       wall time allows one (``DIAGNOSIS_RESERVE_S``) and fewer than
       ``MAX_CONSECUTIVE_WRITE_FAILURES`` writes in a row have failed; otherwise count it
       ``deferred`` for the next daily run.

    ``max_diagnoses_per_run`` counts vehicles diagnosed after the skip, not selected
    rows (decisions 2026-10-05 item 6).  A diagnosis is at most two Converse calls (one
    retry, D11); ``model_calls`` in the result is the count actually made.

    Every Athena call checks the clock (``_DeadlineAthena``).  Athena errors and an
    exhausted wall time before the reads finish raise, carrying the stats so far as
    ``exc.cost_stats`` (the runner records the run as aborted).  A failed write for one
    vehicle is counted in ``write_failures`` and the run continues; a model failure
    never fails the run (D6 step 5).

    Parameters
    ----------
    write_finding:
        ``(finding, standing) -> {"action": "put" | "update" | "noop", ...}``, the AVX
        writer (``ArtifactWriter.write_finding``).
    lookup_live:
        ``finding_key -> live row | None`` (the writer's ``by_finding_key`` query).
    resolve_standing:
        ``vin -> StandingFacts`` (the CMS registry), or ``None`` for no projection.
    remaining_seconds:
        Wall time left in this invocation (the Lambda context's remaining time).
    """
    from agents.fi_cost.evidence import _VIN_RE, gather_evidence
    from agents.fi_cost.finding_keys import FINDING_KIND_ANOMALY, cost_finding_key
    from agents.fi_cost.selection import find_latest_complete_month, select_anomalies
    from agents.fi_cost.sell_timing import (
        _EXPIRES_DAYS,
        build_sell_timing_finding,
        fetch_sell_timing_inputs,
        plan_sell_timing,
    )
    from agents.tier2.finding_transform import _validate_finding
    from agents.tier2.owner_resolver import mask_vin

    if (
        not isinstance(max_diagnoses_per_run, int)
        or isinstance(max_diagnoses_per_run, bool)
        or not 0 <= max_diagnoses_per_run <= MAX_DIAGNOSES_CEILING
    ):
        raise ValueError(
            f"max_diagnoses_per_run must be an integer from 0 to {MAX_DIAGNOSES_CEILING}"
        )
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    now_str = _utc_iso(now)
    expires_str = _utc_iso(now + datetime.timedelta(days=_EXPIRES_DAYS))

    stats = _new_stats()
    model = _CountingBedrock(bedrock_client)

    def _polls() -> int:
        budget = remaining_seconds() - FINAL_RESERVE_S
        if budget < ATHENA_POLL_INTERVAL_S:
            raise CostRunDeadlineExceeded("no wall time left for the next Athena read")
        return int(budget // ATHENA_POLL_INTERVAL_S)

    def _resolve(vin: str) -> Any:
        return resolve_standing(vin) if resolve_standing is not None else None

    def _reverified(vin: str, standing: Any) -> Any:
        # The projection is an authorization fact.  Re-read it immediately before the
        # write and project nothing if it moved (runner._build_artifact, F11.2 Accept 4).
        if resolve_standing is None:
            return None
        again = resolve_standing(vin)
        if again != standing:
            stats["owner_integrity_degraded"] += 1
            log.warning(
                "fi_cost.agent: event=owner_integrity_degraded vin_hash=%s", mask_vin(vin)
            )
            return None
        return standing

    def _finish() -> dict[str, Any]:
        stats["model_calls"] = model.calls
        stats["guardrail_interventions"] = model.guardrail_interventions
        stats["tokens_used"] = model.input_tokens + model.output_tokens
        stats["cost_usd"] = round(
            model.input_tokens * _SONNET_4_6_INPUT_PRICE_PER_MILLION / 1_000_000
            + model.output_tokens * _SONNET_4_6_OUTPUT_PRICE_PER_MILLION / 1_000_000,
            6,
        )
        return stats

    # The Athena client checks the run's clock on every call (W2, Group 4 review cycle 1).
    athena_client = _DeadlineAthena(athena_client, remaining_seconds)
    try:
        # ---- 1. Data month -------------------------------------------------------
        data_month = find_latest_complete_month(
            athena_client, workgroup, results_s3_path, now=now, stage=stage, max_polls=_polls()
        )
        stats["data_month"] = data_month
        if data_month is None:
            log.warning("fi_cost.agent: event=no_cost_data stage=%s (no fleet_cost_events month)", stage)
            return _finish()

        # ---- 2. Select, gather, sell-timing inputs (D6 steps 1-2, D10) ----------
        selected = select_anomalies(
            athena_client=athena_client,
            workgroup=workgroup,
            results_s3_path=results_s3_path,
            max_diagnoses_per_run=max_diagnoses_per_run,
            latest_complete_month=data_month,
            stage=stage,
            max_polls=_polls(),
        )
        vins: list[str] = []
        seen: set[str] = set()
        for row in selected:
            vin = row.get("vin")
            if not isinstance(vin, str) or not _VIN_RE.match(vin):
                # One malformed ADP row must not abort every run (gather_evidence refuses
                # to template it); it is counted and left out.
                stats["invalid_vins"] += 1
                continue
            if vin not in seen:
                seen.add(vin)
                vins.append(vin)
        stats["anomalies_selected"] = len(vins)

        evidence_by_vin: dict[str, dict[str, Any]] = {}
        ambiguous: set[str] = set()
        if vins:
            for ev in gather_evidence(
                vins, data_month, athena_client, workgroup, results_s3_path,
                stage=stage, max_polls=_polls(),
            ):
                vin = ev.get("vin")
                if vin in evidence_by_vin:
                    ambiguous.add(vin)  # two bundles for one vehicle: diagnose neither
                if ev.get("year_month") == data_month:
                    evidence_by_vin[vin] = ev
        for vin in ambiguous:
            evidence_by_vin.pop(vin, None)

        sell_rows = fetch_sell_timing_inputs(
            athena_client, workgroup, results_s3_path,
            year_month=data_month, stage=stage, max_polls=_polls(),
        )
        candidates, rejected = plan_sell_timing(sell_rows, year_month=data_month)
        stats["sell_timing_candidates"] = len(candidates)
        stats["sell_timing_rejected_vins"] = rejected

        # ---- 3. Sell timing (D10): deterministic, no model call ------------------
        for index, candidate in enumerate(candidates):
            if remaining_seconds() < FINAL_RESERVE_S + SELL_TIMING_RESERVE_S:
                stats["sell_timing_deferred"] += len(candidates) - index
                break
            vin = candidate["vin"]
            try:
                finding = build_sell_timing_finding(**candidate)
                live = lookup_live(finding["finding_key"])
                standing = _resolve(vin)
                if _is_unchanged(live, finding["inputs_hash"], vin, standing):
                    stats["sell_timing_skipped"] += 1
                    continue
                _validate_finding(finding)
                result = write_finding(finding, _reverified(vin, standing))
            except Exception as exc:  # noqa: BLE001 — one vehicle must not stop the others
                stats["write_failures"] += 1
                log.warning(
                    "fi_cost.agent: event=sell_timing_write_failed vin_hash=%s error=%s",
                    mask_vin(vin), type(exc).__name__,
                )
                continue
            if result.get("action") in ("put", "update"):
                stats["sell_timing_written"] += 1
            else:
                stats["sell_timing_skipped"] += 1

        # ---- 4. Anomalies: skip unchanged, then diagnose up to the cap -----------
        consecutive_write_failures = 0
        for vin in vins:
            evidence = evidence_by_vin.get(vin)
            if evidence is None:
                stats["evidence_missing"] += 1
                continue
            finding_key = cost_finding_key(vin, FINDING_KIND_ANOMALY, data_month)
            try:
                live = lookup_live(finding_key)
                standing = _resolve(vin)
            except Exception as exc:  # noqa: BLE001
                stats["write_failures"] += 1
                log.warning(
                    "fi_cost.agent: event=live_lookup_failed vin_hash=%s error=%s",
                    mask_vin(vin), type(exc).__name__,
                )
                continue

            # D6 step 3 / D11: skip before counting against the cap, so later daily runs
            # reach the vehicles an earlier run deferred.
            if _is_unchanged(live, compute_inputs_hash(evidence), vin, standing):
                stats["duplicates_skipped"] += 1
                continue
            if stats["diagnosed"] >= max_diagnoses_per_run:
                stats["deferred"] += 1
                stats["deferred_cap"] += 1
                continue
            if remaining_seconds() < FINAL_RESERVE_S + DIAGNOSIS_RESERVE_S:
                stats["deferred"] += 1
                stats["deferred_wall_time"] += 1
                continue
            if consecutive_write_failures >= MAX_CONSECUTIVE_WRITE_FAILURES:
                stats["deferred"] += 1
                stats["deferred_write_failures"] += 1
                continue

            stats["diagnosed"] += 1
            outcome: dict[str, Any] = {}

            def _write(finding: dict[str, Any], _vin: str = vin, _key: str = finding_key,
                       _standing: Any = standing, _outcome: dict[str, Any] = outcome,
                       **_kw: Any) -> None:
                finding["finding_id"] = f"FIND-{uuid.uuid4()}"
                finding["finding_key"] = cost_finding_key(
                    finding["scope"]["vin"], finding["finding_kind"], finding["subject_key"]
                )
                if finding["finding_key"] != _key:
                    raise ValueError("cost Finding key does not match the vehicle and data month")
                finding["computed_at"] = now_str
                finding["expires_at"] = expires_str
                finding.setdefault("audit_trail", [])
                _validate_finding(finding)
                _outcome["source"] = next(
                    (e.get("value") for e in finding.get("evidence", [])
                     if e.get("kind") == "cost.diagnosis_source"),
                    None,
                )
                _outcome["result"] = write_finding(finding, _reverified(_vin, _standing))

            try:
                # A live Finding whose standing moved is passed as None, so process_vehicle
                # does not skip it on the hash; the writer then converges the projection.
                standing_matches = (
                    live is not None
                    and _desired_standing(vin, standing) == _persisted_standing(live)
                )
                process_vehicle(
                    evidence=evidence,
                    live_finding=live if standing_matches else None,
                    bedrock_client=model,
                    write_finding=_write,
                )
            except Exception as exc:  # noqa: BLE001 — one vehicle must not stop the others
                stats["write_failures"] += 1
                consecutive_write_failures += 1
                log.warning(
                    "fi_cost.agent: event=cost_write_failed vin_hash=%s error=%s",
                    mask_vin(vin), type(exc).__name__,
                )
                continue
            consecutive_write_failures = 0
            if outcome.get("source") == "rules":
                stats["rules_fallbacks"] += 1
            action = (outcome.get("result") or {}).get("action")
            if action in ("put", "update"):
                stats["anomalies_written"] += 1
            else:
                stats["anomalies_noop"] += 1

    except Exception as exc:
        # The runner's aborted run record carries what was done before the abort: the
        # data month, and the model calls already billed (Group 4 review cycle 1).
        exc.cost_stats = _finish()  # type: ignore[attr-defined]
        raise
    return _finish()
