"""Unit tests for evals/runner/fi_text_case.py (decisions.md call 60 / call 63).

Six original tests + four W-2 tests (call 63):
1. The real case file loads and schema-validates (including tool_evidence key).
2. A reply with both phrases and the tool passes.
3. Dropping "do not drive" fails, naming it.
4. Dropping "park outside" fails, naming it.
5. No tool call in tool_calls fails, naming the tool.
6. The reviewer's probe transcript (drops "park outside") fails.
7. tool_calls=None without the D18 marker in reply fails.
8. tool_calls=None with D18 marker in reply passes.
9. Unknown response key is rejected (ValueError).
10. Case with no phrase assertion is rejected (ValueError).
"""
from __future__ import annotations

import pytest

from evals.runner.fi_text_case import evaluate_response, load_case
from evals.runner.schema import EvalCase, Expected, Input

_CASE_PATH = "evals/cases/fi/fleet-recall-safety-text-001.yaml"
_GOOD_REPLY = (
    "Do not drive this vehicle until the recall repair has been completed. "
    "Park outside and away from structures. Remedy code LIVETEST-D18."
)
# CHANGED (architect, infra cycle 3 S2): tool_evidence is now checked even when a tool list
# is passed, so the passing reply carries the marker a verbatim reading of the planted
# disclosure contains.
_NO_MARKER_REPLY = (
    "Do not drive this vehicle until the recall repair has been completed. "
    "Park outside and away from structures."
)
_MARKER_REPLY = (
    "Do not drive this vehicle until the recall repair has been completed. "
    "Park outside and away from structures. "
    "LIVETEST-D18"
)
_TOOL = "fleet_recommendation"


def test_real_case_loads() -> None:
    case = load_case(_CASE_PATH)
    assert case.id == "fleet-recall-safety-text-001"
    assert case.persona == "fleet-manager"
    resp = case.expected.response or {}
    assert resp.get("must_contain_all") == ["do not drive", "park outside"]
    assert resp.get("tool_evidence") == ["LIVETEST-D18"]


def test_full_reply_passes() -> None:
    case = load_case(_CASE_PATH)
    passed, missing = evaluate_response(case, _GOOD_REPLY, [_TOOL])
    assert passed, missing


def test_drop_do_not_drive_fails() -> None:
    case = load_case(_CASE_PATH)
    reply = "Park outside and away from structures."
    passed, missing = evaluate_response(case, reply, [_TOOL])
    assert not passed
    assert any("do not drive" in m for m in missing), missing


def test_drop_park_outside_fails() -> None:
    case = load_case(_CASE_PATH)
    reply = "Do not drive this vehicle until the recall repair has been completed."
    passed, missing = evaluate_response(case, reply, [_TOOL])
    assert not passed
    assert any("park outside" in m for m in missing), missing


def test_no_tool_call_fails() -> None:
    case = load_case(_CASE_PATH)
    passed, missing = evaluate_response(case, _GOOD_REPLY, [])
    assert not passed
    assert any(_TOOL in m for m in missing), missing


def test_probe_transcript_fails() -> None:
    """Reviewer probe: drops 'park outside' — must not pass (decisions.md call 60)."""
    case = load_case(_CASE_PATH)
    probe_reply = "Do not drive this vehicle until the recall repair is done."
    passed, missing = evaluate_response(case, probe_reply, [_TOOL])
    assert not passed, "probe transcript must fail: 'park outside' is absent"
    assert any("park outside" in m for m in missing), missing


# ── W-2 tests (call 63) ────────────────────────────────────────────────────


def test_tool_calls_none_without_marker_fails() -> None:
    """tool_calls=None: reply lacks LIVETEST-D18 → evidence check fails."""
    case = load_case(_CASE_PATH)
    passed, missing = evaluate_response(case, _NO_MARKER_REPLY, tool_calls=None)
    assert not passed
    assert any("LIVETEST-D18" in m for m in missing), missing


def test_tool_calls_none_with_marker_passes() -> None:
    """tool_calls=None: reply contains LIVETEST-D18 + both phrases → passes."""
    case = load_case(_CASE_PATH)
    passed, missing = evaluate_response(case, _MARKER_REPLY, tool_calls=None)
    assert passed, missing


def test_unknown_response_key_rejected() -> None:
    """Unknown response key (e.g. misspelt must_contain_one_of) raises ValueError."""
    case = EvalCase(
        id="test-unknown-key",
        description="test",
        tier=3,
        persona="fleet-manager",
        input=Input(type="text", utterance="hello"),
        expected=Expected(
            tool_calls=[{"name": "fleet_recommendation"}],
            response={"must_contain_all": ["ok"], "must_contain_one_of": ["x"]},
        ),
    )
    with pytest.raises(ValueError, match="unknown response keys"):
        evaluate_response(case, "ok", ["fleet_recommendation"])


def test_no_phrase_assertion_rejected() -> None:
    """Case with no must_contain_all and no tool_evidence raises ValueError."""
    case = EvalCase(
        id="test-no-phrase",
        description="test",
        tier=3,
        persona="fleet-manager",
        input=Input(type="text", utterance="hello"),
        expected=Expected(
            tool_calls=[{"name": "fleet_recommendation"}],
            response={"must_not_contain": ["bad word"]},
        ),
    )
    with pytest.raises(ValueError, match="no phrase assertion"):
        evaluate_response(case, "good reply", ["fleet_recommendation"])


def test_marker_required_even_with_a_tool_list() -> None:
    """Infra cycle 3 S2: a reply without the marker fails even when tool calls are known."""
    case = load_case(_CASE_PATH)
    passed, missing = evaluate_response(case, _NO_MARKER_REPLY, [_TOOL])
    assert not passed
    assert any("LIVETEST-D18" in m for m in missing), missing
