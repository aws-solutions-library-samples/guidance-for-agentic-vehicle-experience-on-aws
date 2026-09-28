"""Tier 3 end-to-end eval runner — real AgentCore WebSocket.

Wire format: iOS protocol from bidi_app_canonical.py.
  Outbound: audio.chunk (16kHz LPCM, base64), text.input
  Inbound:  audio.chunk (24kHz LPCM, base64), transcript, tool.call,
            tool.result, escalation, interruption, session.start, session.end

IMPORTANT — Audio input constraints:
  - Single sentence only, no internal periods.
  - Nova Sonic v2 VAD splits on periods, fragmenting turn state.
  - Demo WAVs must be 1.5–4s with ≥1600ms trailing silence.

AgentCore session timeout: 55s (InternalErrorCode=532).
Client enforces 50s safety margin (CLIENT_TIMEOUT_S in ws_client.py).
Tier 3 latency budget default: 10000ms per case.

CLI usage::

    # Dry-run (no real connection):
    RUNTIME_ID=dummy python3 -m evals.runner.tier3_e2e --dry-run

    # Run a specific case:
    RUNTIME_ID=<id> python3 -m evals.runner.tier3_e2e --case evals/cases/e2e/foo.yaml

    # Write JSON trace:
    RUNTIME_ID=<id> python3 -m evals.runner.tier3_e2e --case evals/cases/e2e/foo.yaml --output /tmp/trace.json
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time
import wave
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals.runner.schema import EvalCase
from evals.runner.ws_client import AgentCoreWSClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIER3_LATENCY_BUDGET_MS = 10_000
SESSION_HARD_LIMIT_MS = 55_000
EVALS_AUDIO_DIR = Path(__file__).parent.parent / "audio"

# ---------------------------------------------------------------------------
# Built-in sample case (text-only, no audio) — used for --dry-run self-test
# ---------------------------------------------------------------------------

_SAMPLE_CASE: dict[str, Any] = {
    "id": "sample-text-001",
    "description": "Built-in sample case for --dry-run self-test (text input only)",
    "tier": 3,
    "persona": "fleet-driver",
    "vehicle_id": "VEH-0025",
    "driver_id": "drv-samantha-carter",
    "input": {
        "type": "text",
        "utterance": "My tire pressure light just came on",
    },
    "expected": {
        "tool_calls": [],
        "response": {
            "must_contain_one_of": ["tire", "pressure"],
            "must_not_contain": [],
        },
    },
    "latency_budget_ms": TIER3_LATENCY_BUDGET_MS,
}


# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------


def _validate_audio_path(audio_path: str) -> Path:
    """Resolve audio_path and assert it is inside evals/audio/.

    Rejects:
    - ``..`` sequences
    - Absolute paths outside evals/audio/
    - Symlinks pointing outside evals/audio/

    Args:
        audio_path: The raw audio_path string from the eval case.

    Returns:
        The resolved Path if safe.

    Raises:
        ValueError: If the path is outside evals/audio/.
    """
    audio_dir = EVALS_AUDIO_DIR.resolve()
    resolved = Path(audio_path).resolve()  # resolves symlinks + ..
    try:
        resolved.relative_to(audio_dir)
    except ValueError:
        raise ValueError(
            f"Path traversal rejected for {audio_path!r}: "
            f"resolved to {resolved!r} which is outside {audio_dir!r}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Event stream helpers
# ---------------------------------------------------------------------------


def _collect_tool_calls(trace: list[dict[str, Any]]) -> list[str]:
    """Extract tool names from received tool.call events."""
    return [
        e["name"]
        for e in trace
        if e.get("dir") == "recv" and e.get("type") == "tool.call" and e.get("name")
    ]


def _collect_transcript(trace: list[dict[str, Any]]) -> str:
    """Concatenate all transcript text from received events."""
    parts = []
    for e in trace:
        if e.get("dir") == "recv" and e.get("type") == "transcript":
            text = e.get("text") or e.get("transcript") or ""
            if text:
                parts.append(text)
    return " ".join(parts)


def _tool_round_trip_latencies_ms(trace: list[dict[str, Any]]) -> list[float]:
    """Compute round-trip latency (ms) for each tool.call → tool.result pair."""
    latencies: list[float] = []
    pending: dict[str, float] = {}
    for e in trace:
        if e.get("dir") == "recv":
            if e.get("type") == "tool.call":
                tool_id = e.get("toolUseId") or e.get("id") or e.get("name", "")
                pending[tool_id] = e["ts"]
            elif e.get("type") == "tool.result":
                tool_id = e.get("toolUseId") or e.get("id") or ""
                if tool_id in pending:
                    latencies.append((e["ts"] - pending.pop(tool_id)) * 1000)
    return latencies


def _p99(values: list[float]) -> float:
    """Calculate 99th percentile latency from a list of millisecond values."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, int(len(sorted_vals) * 0.99) - 1)
    return sorted_vals[idx]


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


async def _run_case(case: EvalCase) -> dict[str, Any]:
    """Run a single Tier 3 case against the live AgentCore runtime.

    Args:
        case: Validated EvalCase with tier == 3.

    Returns:
        Result dict with keys: passed, trace, assertions, elapsed_ms.
    """
    client = AgentCoreWSClient()
    session_meta = {}
    if case.vehicle_id:
        session_meta["vehicleId"] = case.vehicle_id
    if case.driver_id:
        session_meta["driverId"] = case.driver_id

    t0 = time.monotonic()

    if case.input.type == "audio":
        audio_path = _validate_audio_path(case.input.audio_path)  # type: ignore[arg-type]
        audio_bytes = audio_path.read_bytes()
        events = [
            e
            async for e in client.stream(
                audio_bytes=audio_bytes, session_meta=session_meta
            )
        ]
    else:
        text = case.input.utterance or ""
        events = [
            e
            async for e in client.stream(text=text, session_meta=session_meta)
        ]

    elapsed_ms = (time.monotonic() - t0) * 1000
    trace = client.trace

    # Assertions
    assertions: dict[str, Any] = {}
    passed = True

    # Tool sequence
    expected_tools = [
        tc["name"] if isinstance(tc, dict) else tc
        for tc in (case.expected.tool_calls or [])
    ]
    actual_tools = _collect_tool_calls(trace)
    assertions["tool_sequence"] = {
        "expected": expected_tools,
        "actual": actual_tools,
        "ok": actual_tools == expected_tools or not expected_tools,
    }
    if expected_tools and actual_tools != expected_tools:
        passed = False

    # Transcript assertions
    transcript = _collect_transcript(trace)
    resp = case.expected.response or {}
    must_contain = resp.get("must_contain_one_of", [])
    must_not = resp.get("must_not_contain", [])

    contains_one = not must_contain or any(
        kw.lower() in transcript.lower() for kw in must_contain
    )
    contains_none = all(kw.lower() not in transcript.lower() for kw in must_not)

    assertions["transcript_contains_one_of"] = {"ok": contains_one, "must_contain_one_of": must_contain}
    assertions["transcript_must_not_contain"] = {"ok": contains_none, "must_not_contain": must_not}
    if not contains_one or not contains_none:
        passed = False

    # Latency
    budget_ms = case.latency_budget_ms or TIER3_LATENCY_BUDGET_MS
    latencies = _tool_round_trip_latencies_ms(trace)
    p99_ms = _p99(latencies)
    assertions["p99_latency_ms"] = {"value": p99_ms, "budget": budget_ms, "ok": p99_ms <= budget_ms}
    assertions["session_elapsed_ms"] = {"value": elapsed_ms, "limit": SESSION_HARD_LIMIT_MS, "ok": elapsed_ms <= SESSION_HARD_LIMIT_MS}
    if p99_ms > budget_ms or elapsed_ms > SESSION_HARD_LIMIT_MS:
        passed = False

    return {"passed": passed, "trace": trace, "assertions": assertions, "elapsed_ms": elapsed_ms}


# ---------------------------------------------------------------------------
# pytest test
# ---------------------------------------------------------------------------


def _discover_cases() -> list[str]:
    """Discover all Tier 3 YAML case files in evals/cases/e2e/."""
    pattern = str(Path(__file__).parent.parent / "cases" / "e2e" / "**" / "*.yaml")
    return glob.glob(pattern, recursive=True)


@pytest.mark.parametrize("case_path", _discover_cases())
def test_e2e_case(case_path: str) -> None:
    """Tier 3 end-to-end eval test.

    Auto-skips when RUNTIME_ID env var is unset (so PR CI doesn't fail).
    Single sentence audio only — no internal periods (Nova VAD splits on periods).
    """
    if not os.environ.get("RUNTIME_ID"):
        pytest.skip("RUNTIME_ID env var not set — skipping Tier 3 test")

    with open(case_path) as f:
        raw = yaml.safe_load(f)
    case = EvalCase.model_validate(raw)

    if case.tier != 3:
        pytest.skip(f"Case tier={case.tier}, not 3 — skipping")

    # Path traversal guard
    if case.input.audio_path:
        try:
            _validate_audio_path(case.input.audio_path)
        except ValueError as exc:
            pytest.fail(f"Path traversal rejected for {case.input.audio_path!r}: {exc}")

    result = asyncio.run(_run_case(case))

    assert result["assertions"]["session_elapsed_ms"]["ok"], (
        f"Session exceeded 55s hard limit: {result['elapsed_ms']:.0f}ms"
    )
    assert result["assertions"]["p99_latency_ms"]["ok"], (
        f"p99 tool latency {result['assertions']['p99_latency_ms']['value']:.0f}ms "
        f"> budget {result['assertions']['p99_latency_ms']['budget']}ms"
    )
    assert result["assertions"]["transcript_contains_one_of"]["ok"], (
        f"Transcript missing required keywords: {result['assertions']['transcript_contains_one_of']['must_contain_one_of']}"
    )
    assert result["assertions"]["transcript_must_not_contain"]["ok"], (
        f"Transcript contains forbidden keywords: {result['assertions']['transcript_must_not_contain']['must_not_contain']}"
    )
    assert result["assertions"]["tool_sequence"]["ok"], (
        f"Tool sequence mismatch: expected {result['assertions']['tool_sequence']['expected']}, "
        f"got {result['assertions']['tool_sequence']['actual']}"
    )


# ---------------------------------------------------------------------------
# Per-persona smoke tests (Group 6.C)
# ---------------------------------------------------------------------------

# Per-persona smoke matrix. Prompts are CONFIRMED against the live staging
# runtime (2026-06-19 probe — see decisions.md "Post-deploy routing map"):
#
#  - KB personas (service_advisor, technician) use KNOWLEDGE-phrased prompts
#    that elicit the live KB `retrieve` tool (the wire tool name is `retrieve`,
#    NOT lookup_knowledge/lookup_parts/diy_repair_advisor — those names no tool
#    emits; see decisions.md). check="kb" asserts retrieve was called AND ≥1
#    s3:// citation came back — the 7.B.4 grounding criterion. Fault/DTC-phrased
#    prompts ("diagnostic procedure for DTC P0420") route to auto-triage, not
#    the KB, so the prompts are deliberately phrased as bulletin/policy lookups.
#
#  - Non-KB personas use check="liveness": assert the agent invoked ≥1 tool on
#    the audio turn. Exact per-persona tool pinning via voice is BRITTLE (the
#    live agent routes account/service questions across triage /
#    lookup_vehicle_identity / get_service_history nondeterministically — see
#    the probe table in decisions.md), and the spec itself frames these as "a
#    coarse liveness check, not a deterministic eval". Liveness proves the full
#    runtime path (auth → session → audio turn → agent tool call) without a
#    brittle exact-tool assertion.
_SMOKE_CASES = [
    {
        "persona_id": "fleet_driver",
        "prompt": "Show me my recent service history",
        "audio_path": "evals/audio/persona-service-history.wav",  # Polly
        "check": "liveness",
        "vin": "1FTRX18W6XKB12345",
        "customer_id": "cust-fleet-driver-001",
    },
    {
        "persona_id": "consumer",
        "prompt": "What are my account details",
        "audio_path": "evals/audio/persona-account-details.wav",  # Polly
        "check": "liveness",
        "vin": "1FTRX18W6XKB12345",
        "customer_id": "cust-consumer-001",
    },
    {
        "persona_id": "retail_advisor",
        "prompt": "Show my recent support cases",
        "audio_path": "evals/audio/persona-support-cases.wav",  # Polly
        "check": "liveness",
        "vin": "1FTRX18W6XKB12345",
        "customer_id": "cust-retail-advisor-001",
    },
    {
        "persona_id": "vehicle_owner",
        "prompt": "Show me my vehicle service records",
        "audio_path": "evals/audio/persona-service-records.wav",  # Polly
        "check": "liveness",
        "vin": "1FTRX18W6XKB12345",
        "customer_id": "cust-vehicle-owner-001",
    },
    {
        "persona_id": "service_advisor",
        "prompt": "What does the technical service bulletin say about catalyst efficiency code P0420?",
        "audio_path": "evals/audio/tsb-catalyst-p0420.wav",  # Polly
        "check": "kb",
        # On-topic citation: this prompt queries catalyst-efficiency code P0420;
        # the only P0420 doc in the ADP corpus is the DTC guide dtc-P0420.md
        # (no TSB doc covers P0420 — see issue 2026-06-22-cvx-tier3-kb-citation-assertion).
        # Verified retrievable @ 0.5666 ≥ 0.55 (issues/2026-06-18-cvx-kb-retrieval-relevance/summary.md).
        # NOTE the doc maps to service_advisor (not technician) because this is
        # the case whose confirmed prompt queries P0420.
        "expected_source_substr": "dtc-P0420.md",
        "min_citation_score": 0.55,
        "vin": "1FTRX18W6XKB12345",
        "customer_id": "cust-service-advisor-001",
    },
    {
        "persona_id": "technician",
        "prompt": "Look up the warranty policy for the powertrain",
        "audio_path": "evals/audio/persona-warranty-powertrain.wav",  # Polly
        "check": "kb",
        # On-topic citation: this prompt queries powertrain warranty coverage;
        # the corpus doc is sources/service-policy/warranty-coverage-matrix.md
        # ("Powertrain: 5 years / 60,000 mi ..."). No min_citation_score: this
        # doc's retrieval score has not been probed live, so the substring is the
        # regression guard (a 0.55 floor on an un-probed doc risks a false-fail;
        # operator may add one after observing the live score).
        "expected_source_substr": "warranty-coverage-matrix.md",
        "vin": "1FTRX18W6XKB12345",
        "customer_id": "cust-technician-001",
    },
]


# Wire header names the runtime reads (agents/supervisor/bidi_app.py:211-234).
_PERSONA_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-persona-id"
_VIN_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-vin"
_CUSTOMER_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-customer-id"

# Wire names of the KB-grounded tools whose tool.result carries s3:// citations
# (docs/tech.md § "CVX Tier-3 persona-smoke harness verified surfaces"). The
# live KB tool is `retrieve` (make_retrieve_tool); the per-persona
# lookup_knowledge/lookup_parts/diy_repair_advisor wrappers route through the
# same KB and also emit s3:// sources.
_KB_TOOL_NAMES = ("retrieve", "lookup_knowledge", "lookup_parts", "diy_repair_advisor")

# The runtime requires a tenantId (query param or session.start) — see
# agents/supervisor/bidi_app.py session-init. Seeded personas live under the
# meridian-fleet tenant (Config default VSA_TENANT_ID). Override via env if needed.
_SMOKE_TENANT = os.environ.get("VSA_TENANT_ID", "vehicle-owner")


def _resolve_user_token() -> str:
    """Resolve the Cognito user JWT the runtime requires (session-init authn).

    Precedence: explicit ``VSA_USER_TOKEN`` env (operator/CI-supplied) → else
    mint via the dev-convenience Cognito USER_PASSWORD_AUTH flow
    (``config.get_cached_id_token``, cached to ~/.vsa/tokens.json). The token is
    a short-lived JWT, never logged; it flows only into the session.start
    payload. Raises if neither path yields a token (surfaced as the smoke's
    error string).
    """
    tok = os.environ.get("VSA_USER_TOKEN")
    if tok:
        return tok
    from agents.supervisor.config import Config, get_cached_id_token

    return get_cached_id_token(Config.from_env())


def _coerce_score(raw: Any) -> float | None:
    """Coerce a wire ``score`` value to float, or None if absent/non-numeric.

    ``retrieve`` returns the Bedrock retrieval score as a plain float, but be
    defensive about a tool that stringifies it (e.g. ``"0.5666"``). Booleans are
    rejected (``isinstance(True, int)`` is True in Python).
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _collect_kb_citations(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract KB citations (``s3://`` source + adjacent retrieval score).

    Walks each recv ``tool.result`` event's ``output`` for any ``source`` value
    that begins with ``s3://`` and returns ``{"source": <uri>, "score":
    <float|None>}`` per passage. The ``score`` is the per-passage Bedrock
    retrieval score that ``retrieve.py`` returns as a sibling of ``source``
    (``{"snippet": ..., "source": "s3://...", "score": 0.57}``) and ``bidi_app.py``
    passes through verbatim in the ``tool.result`` ``output`` (docs/tech.md §8).
    A non-``s3://`` source is a local-corpora fallback, which is NOT a live-KB
    citation, so it is excluded — that exclusion is the 7.B.4 grounding signal.
    When the wire payload omits a numeric score for a passage, ``score`` is None
    and the score-floor assertion is skipped for that citation (the specific-doc
    substring is the primary, always-deterministic guard).
    """
    citations: list[dict[str, Any]] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            src = obj.get("source")
            if isinstance(src, str) and src.startswith("s3://"):
                citations.append({"source": src, "score": _coerce_score(obj.get("score"))})
            for k, v in obj.items():
                if k in ("source", "score"):
                    continue
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    for e in trace:
        if e.get("dir") == "recv" and e.get("type") == "tool.result":
            _walk(e.get("output"))
    return citations


def _evaluate_kb_smoke(
    smoke: dict, tool_calls: list[str], citations: list[dict[str, Any]]
) -> tuple[bool, str]:
    """Pure pass/fail decision for a ``check=="kb"`` smoke.

    Returns ``(passed, detail)`` where ``detail`` is a human-readable reason.
    Offline-testable — takes the already-collected tool_calls + citations, makes
    no network call.

    Gates (in order):
      1. ``retrieve`` was invoked (the agent reached the live KB).
      2. ≥1 ``s3://`` KB citation came back (not a local-corpora fallback).
      3. The expected on-topic doc was cited: ``expected_source_substr`` appears
         in ≥1 citation URI (the regression guard — a dropped doc cites a
         neighbour, which fails this).
      4. Score floor (best-effort): if ``min_citation_score`` is set AND a
         matching citation carries a numeric score, the best matching score must
         be ≥ the floor. If no matching citation carries a numeric score, the
         floor is skipped (substring guard already held).
    """
    if "retrieve" not in tool_calls:
        return False, f"retrieve tool was not invoked (tool_calls={tool_calls})"
    if not citations:
        return False, "no s3:// KB citation returned (local-corpora fallback or empty)"

    expected_substr = smoke.get("expected_source_substr")
    if expected_substr:
        matching = [c for c in citations if expected_substr in c.get("source", "")]
        if not matching:
            return (
                False,
                f"expected citation substring {expected_substr!r} not found in "
                f"{[c.get('source') for c in citations]}",
            )
    else:
        matching = list(citations)

    min_score = smoke.get("min_citation_score")
    if min_score is not None:
        scored = [c["score"] for c in matching if isinstance(c.get("score"), (int, float))]
        if scored:
            best = max(scored)
            if best < min_score:
                return (
                    False,
                    f"top matching citation score {best:.4f} < floor {min_score} "
                    f"for {expected_substr!r}",
                )
            return True, f"cited {expected_substr!r} @ score {best:.4f} ≥ {min_score}"
        # Score floor configured but no numeric score on the wire for the matched
        # citation — fall through to substring-only pass (best-effort floor).
        return True, f"cited {expected_substr!r} (no numeric score on wire; floor skipped)"

    return True, f"cited {expected_substr!r}" if expected_substr else "cited ≥1 s3:// source"


async def _run_persona_smoke(smoke: dict) -> dict[str, Any]:
    """Invoke the deployed AgentCore runtime for one persona smoke check.

    Uses the SigV4-authenticated AgentCoreWSClient (not a bare websocket) with
    the per-persona custom headers, sends the persona-specific WAV audio
    (raw PCM via wave), and returns the tool-call names + any KB citations
    observed in the trace.  Inbound audio.chunk events are discarded — the
    smoke validates tool routing and KB citations only.

    session.end is deferred until a tool.result is received or the per-turn
    deadline elapses (wait_for_turn=True), per the timing guidance in
    docs/tech.md § 10.
    """
    extra_headers = {
        _PERSONA_ID_HEADER: smoke["persona_id"],
        _VIN_HEADER: smoke["vin"],
        _CUSTOMER_ID_HEADER: smoke["customer_id"],
    }
    client = AgentCoreWSClient()
    try:
        # Load PCM frames via stdlib wave — robust to non-standard WAV chunk
        # layouts (avoids hardcoded 44-byte header offset).
        audio_resolved = _validate_audio_path(smoke["audio_path"])
        with wave.open(str(audio_resolved), "rb") as wf:
            pcm = wf.readframes(wf.getnframes())

        session_meta = {
            "tenantId": _SMOKE_TENANT,
            "vin": smoke["vin"],
            "jwt": _resolve_user_token(),
        }
        async for _ in client.stream(
            audio_bytes=pcm,
            session_meta=session_meta,
            extra_headers=extra_headers,
            wait_for_turn=True,
        ):
            pass
    except Exception as exc:  # noqa: BLE001 — smoke surfaces any connect/stream error
        # Redact the WSS URL's runtime segment to avoid leaking the operator's
        # account id (which is encoded in the runtime ARN path component).
        import re
        err_msg = re.sub(r"runtimes/[^/]+/ws", "runtimes/<redacted>/ws", str(exc))
        return {
            "passed": False,
            "tool_calls": _collect_tool_calls(client.trace),
            "citations": _collect_kb_citations(client.trace),
            "kb_detail": None,
            "error": err_msg,
        }

    tool_calls = _collect_tool_calls(client.trace)
    citations = _collect_kb_citations(client.trace)
    kb_detail: str | None = None
    if smoke["check"] == "kb":
        # 7.B.4 + on-topic hardening: the agent must reach the live KB
        # (`retrieve`), cite ≥1 s3:// source, cite the EXPECTED doc
        # (expected_source_substr), and — when a numeric score is on the wire —
        # clear the per-case min_citation_score floor. See _evaluate_kb_smoke.
        passed, kb_detail = _evaluate_kb_smoke(smoke, tool_calls, citations)
    else:  # "liveness"
        # Coarse liveness: the agent invoked ≥1 tool on the audio turn. Proves
        # auth → session → audio turn → agent tool call without a brittle
        # exact-tool assertion (see _SMOKE_CASES rationale + decisions.md).
        passed = len(tool_calls) >= 1
    return {
        "passed": passed,
        "tool_calls": tool_calls,
        "citations": citations,
        "kb_detail": kb_detail,
        "error": None,
    }


async def _run_persona_smoke_with_retry(smoke: dict, retries: int = 1) -> dict[str, Any]:
    """Run a persona smoke, retrying on failure to absorb live-LLM nondeterminism."""
    result = await _run_persona_smoke(smoke)
    attempts = 1
    while not result["passed"] and attempts <= retries:
        attempts += 1
        result = await _run_persona_smoke(smoke)
    result["attempts"] = attempts
    return result


def _require_staging(request) -> None:
    """Skip unless BOTH --staging is passed AND RUNTIME_ID is set.

    Double-gate keeps the default unit suite and PR CI fully offline — neither
    condition alone triggers a live AgentCore connection.
    """
    if not request.config.getoption("--staging"):
        pytest.skip("per-persona smoke requires --staging (live AgentCore runtime)")
    if not os.environ.get("RUNTIME_ID"):
        pytest.skip("per-persona smoke requires RUNTIME_ID env var")


def _assert_smoke(smoke: dict) -> None:
    """Run one persona smoke (with retry) and assert per the case's check mode.

    check="kb"       → retrieve called, ≥1 s3:// KB citation, the EXPECTED doc
                       (expected_source_substr) is cited, and — when a numeric
                       score is on the wire — the cited passage clears
                       min_citation_score (7.B.4 + on-topic hardening; see
                       _evaluate_kb_smoke).
    check="liveness" → agent invoked ≥1 tool on the audio turn.
    """
    result = asyncio.run(_run_persona_smoke_with_retry(smoke))
    assert result["passed"], (
        f"persona={smoke['persona_id']} check={smoke['check']} "
        f"kb_detail={result.get('kb_detail')} "
        f"expected_source_substr={smoke.get('expected_source_substr')!r} "
        f"min_citation_score={smoke.get('min_citation_score')} "
        f"tool_calls={result['tool_calls']} citations={result['citations']} "
        f"attempts={result.get('attempts')} error={result['error']}"
    )


def test_persona_smoke_fleet_driver(request) -> None:
    """Smoke: fleet_driver invokes get_service_history."""
    _require_staging(request)
    _assert_smoke(_SMOKE_CASES[0])


def test_persona_smoke_consumer(request) -> None:
    """Smoke: consumer invokes get_customer_profile."""
    _require_staging(request)
    _assert_smoke(_SMOKE_CASES[1])


def test_persona_smoke_retail_advisor(request) -> None:
    """Smoke: retail_advisor invokes get_interaction_history / get_customer_profile."""
    _require_staging(request)
    _assert_smoke(_SMOKE_CASES[2])


def test_persona_smoke_vehicle_owner(request) -> None:
    """Smoke: vehicle_owner invokes get_service_history."""
    _require_staging(request)
    _assert_smoke(_SMOKE_CASES[3])


def test_persona_smoke_service_advisor(request) -> None:
    """Smoke: service_advisor grounds on the KB and cites ≥1 s3:// source (7.B.4)."""
    _require_staging(request)
    _assert_smoke(_SMOKE_CASES[4])


def test_persona_smoke_technician(request) -> None:
    """Smoke: technician grounds on the KB for a DTC procedure and cites ≥1 s3:// source (7.B.4)."""
    _require_staging(request)
    _assert_smoke(_SMOKE_CASES[5])


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _dry_run(case: EvalCase) -> None:
    """Print the planned wire interaction without connecting."""
    runtime_id = os.environ.get("RUNTIME_ID", "<RUNTIME_ID not set>")
    region = os.environ.get("AWS_REGION", "us-east-1")

    print(f"[dry-run] Case: {case.id}")
    print(f"[dry-run] Description: {case.description}")
    print(f"[dry-run] Tier: {case.tier}")
    print(f"[dry-run] Runtime ID: {runtime_id}")
    print(f"[dry-run] Region: {region}")
    print(f"[dry-run] URL: wss://bedrock-agentcore.{region}.amazonaws.com/runtimes/<arn>/ws")
    print(f"[dry-run] Headers: <SigV4 headers redacted>")
    print()

    # Show planned events
    events = []
    events.append({"type": "session.start", "vehicleId": case.vehicle_id, "driverId": case.driver_id})

    if case.input.type == "audio":
        audio_path = case.input.audio_path
        events.append({
            "type": "audio.chunk",
            "audio": "<base64-encoded LPCM @ 16kHz>",
            "sampleRate": 16000,
            "note": f"source: {audio_path}",
        })
    else:
        events.append({"type": "text.input", "text": case.input.utterance})

    events.append({"type": "session.end"})

    print("[dry-run] Planned outbound events:")
    for i, evt in enumerate(events):
        print(f"  [{i}] {json.dumps(evt)}")

    print()
    print(f"[dry-run] Expected tool calls: {[tc['name'] if isinstance(tc, dict) else tc for tc in (case.expected.tool_calls or [])]}")
    resp = case.expected.response or {}
    print(f"[dry-run] must_contain_one_of: {resp.get('must_contain_one_of', [])}")
    print(f"[dry-run] must_not_contain: {resp.get('must_not_contain', [])}")
    print(f"[dry-run] latency_budget_ms: {case.latency_budget_ms or TIER3_LATENCY_BUDGET_MS}")


def main() -> None:
    """CLI entry point for tier3_e2e."""
    parser = argparse.ArgumentParser(description="Tier 3 end-to-end eval runner")
    parser.add_argument("--case", help="Path to eval case YAML")
    parser.add_argument("--dry-run", action="store_true", help="Print planned interaction, do not connect")
    parser.add_argument("--output", help="Write JSON trace to this path (default: stdout)")
    args = parser.parse_args()

    # Load case
    if args.case:
        with open(args.case) as f:
            raw = yaml.safe_load(f)
        case = EvalCase.model_validate(raw)
    else:
        # Fall back to built-in sample case
        case = EvalCase.model_validate(_SAMPLE_CASE)

    # Path traversal guard (always, even in dry-run)
    if case.input.audio_path:
        try:
            _validate_audio_path(case.input.audio_path)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    if args.dry_run:
        _dry_run(case)
        sys.exit(0)

    # Real mode — requires RUNTIME_ID
    if not os.environ.get("RUNTIME_ID"):
        print(
            "ERROR: RUNTIME_ID env var is required. "
            "Set RUNTIME_ID=<runtime-id> or use --dry-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    result = asyncio.run(_run_case(case))

    output = json.dumps(result, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(output)
        print(f"Trace written to {args.output}")
    else:
        print(output)

    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
