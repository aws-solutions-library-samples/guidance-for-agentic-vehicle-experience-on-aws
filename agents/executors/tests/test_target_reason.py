"""The target's own refusal sentence (contract § 10b; issue 2026-10-23-executor-drops-cms-refusal-reason).

When CMS refuses an Action, the operator reads CMS's sentence ("Vehicle <vin> is grounded and
can't be moved."), not only AVX's § 10a one ("CMS refused the request (HTTP 409)."). These tests
pin:

* the word-for-word pass-through, from each CMS route and DMS's warranty 422, through
  ``registry.dispatch()`` into the audit entry, beside the unchanged ``failure_reason``;
* the fallback: no sentence (no body, not JSON, wrong field, empty) records none, and
  ``failure_reason`` is today's sentence;
* the 5xx, terminal-error and success cases: nothing is recorded, even when an executor
  carried text;
* the cleaning: plain text only, control/bidi/zero-width characters removed, markup or
  over-length text dropped whole, and every kept value passes the schema's backstop pattern;
* that the ``fleet_manager`` chat tool never passes it to a model.

Run: ``.venv/bin/python -m pytest agents/executors/tests/test_target_reason.py -q``
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
from unittest.mock import patch

import jsonschema
import pytest
import responses as rsps  # type: ignore[import]

from agents.executors import registry
from agents.executors.base import ActionStatus, ExecuteResult, FailureKind, RetryPolicy
from agents.executors.cms import CmsExecutor
from agents.executors.dms import DmsExecutor
from agents.executors.target_reason import (
    MAX_BODY_BYTES,
    MAX_LEN,
    clean_target_reason,
    kept_target_reason,
    read_reason_field,
)

_REPO = pathlib.Path(__file__).resolve().parents[3]
_SCHEMA = json.loads((_REPO / "agents/tier2/schemas/action.schema.json").read_text())
_AUDIT_ENTRY_VALIDATOR = jsonschema.Draft202012Validator(
    {**_SCHEMA["$defs"]["audit_entry"], "$defs": _SCHEMA["$defs"]}
)
_SCHEMA_PATTERN = re.compile(_SCHEMA["$defs"]["audit_entry"]["properties"]["target_reason"]["pattern"])

_CMS_BASE = "https://cms-staging.test"
_DMS_BASE = "https://dms-staging.test"
_VIN = "MRDVEH00000000021"
_GROUNDED = f"Vehicle {_VIN} is grounded and can't be moved."
#: DMS's warranty verdict is a reason code, not a sentence (DMS source/_lib/warranty_rules.py:
#: miles_term_expired, date_term_expired; a rule's denial_reason_code otherwise).
_VERDICT = "miles_term_expired"

_CLAIMS = {
    "token": "op-jwt-0001",
    "sub": "op-sub-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": "fleet-meridian-01",
}
_OPERATOR = {"kind": "operator", "id": "op-sub-0001"}

_MOVE = {
    "action_id": "AVX-ACT-e1000000-0000-4000-8000-000000000001",
    "action_kind": "move_vehicles",
    "target_system": "cms",
    "decided_by": _OPERATOR,
    "decision": "approved",
    "finding_id": "FIND-e1000000-0000-4000-8000-000000000001",
    "parameters": {
        "fleet_id": "fleet-meridian-01",
        "from_location_id": "loc-sea-downtown-02",
        "to_location_id": "loc-sea-airport-01",
        "vehicle_vins": [_VIN],
        "forecast_week": "2026-W42",
        "transfer_cost_usd": 84.06,
        "predicted_on_rent_hours_gain": 35.0,
        "predicted_turnaways_avoided": 2,
    },
}
_GROUND = {
    "action_id": "AVX-ACT-e2000000-0000-4000-8000-000000000002",
    "action_kind": "ground_vehicle",
    "target_system": "cms",
    "decided_by": _OPERATOR,
    "decision": "approved",
    "finding_id": "FIND-e2000000-0000-4000-8000-000000000002",
    "parameters": {"vehicle_vin": _VIN, "recall_id": "26V123000"},
}
_COST = {
    "action_id": "AVX-ACT-e3000000-0000-4000-8000-000000000003",
    "action_kind": "apply_cost_action",
    "target_system": "cms",
    "decided_by": _OPERATOR,
    "decision": "approved",
    "finding_id": "FIND-e3000000-0000-4000-8000-000000000003",
    "parameters": {"vehicle_vin": _VIN, "cost_action": "schedule_inspection",
                   "month": "2026-10", "cause": "repeat_repair"},
}
_WARRANTY = {
    "action_id": "AVX-ACT-e4000000-0000-4000-8000-000000000004",
    "action_kind": "submit_warranty_claim",
    "target_system": "dms",
    "decided_by": _OPERATOR,
    "decision": "approved",
    "finding_id": "FIND-e4000000-0000-4000-8000-000000000004",
    "parameters": {"vehicle_vin": _VIN, "ro_id": "RO-1", "line_no": "1",
                   "dealer_id": "DLR-1", "labor_op": "LOP-1"},
}
_URLS = {
    "move_vehicles": f"{_CMS_BASE}/api/v1/fleet-intelligence/moves",
    "ground_vehicle": f"{_CMS_BASE}/api/v1/fleet-intelligence/groundings",
    "apply_cost_action": f"{_CMS_BASE}/api/v1/fleet-intelligence/cost-actions",
}
_POLICY = RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)


@pytest.fixture(autouse=True)
def _endpoints():
    with patch.dict(os.environ, {"VSA_CMS_API_ENDPOINT": _CMS_BASE,
                                 "VSA_DMS_API_ENDPOINT": _DMS_BASE}, clear=False):
        yield


def _dispatch(action):
    """registry.dispatch() with test executors and no escalation side effects."""
    from unittest.mock import MagicMock

    executors = {
        "cms": CmsExecutor(sleep_fn=lambda _: None, retry_policy=_POLICY),
        "dms": DmsExecutor(enqueue_ios_owner=MagicMock(), events_client=MagicMock(),
                           cloudwatch_client=MagicMock(), ssm_client=MagicMock(),
                           sleep_fn=lambda _: None, retry_policy=_POLICY),
    }
    with patch.dict(registry.EXECUTORS, executors):
        return registry.dispatch(action, _CLAIMS, fire_escalation=False)


def _assert_entry_valid(result):
    entry = result.audit_entry(by="executor-test", at="2026-10-23T00:00:00Z")
    entry.pop("last_error_class", None)  # folded into note by both writers
    errors = list(_AUDIT_ENTRY_VALIDATOR.iter_errors(entry))
    assert not errors, [e.message for e in errors]
    return entry


# ---------------------------------------------------------------------------
# Word-for-word pass-through, end to end through registry.dispatch()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action,status,sentence", [
    (_MOVE, 409, _GROUNDED),
    (_MOVE, 409, "Vehicle MRDVEH00000000021 is no longer at loc-sea-downtown-02."),
    (_MOVE, 409, "loc-sea-airport-01 has room for 3 more vehicles."),
    (_MOVE, 409, "Groundings couldn't be read, so the move was refused."),
    (_MOVE, 400, "Vehicle MRDVEH00000000021 is not in this fleet."),
    (_GROUND, 400, "Invalid campaignId: '26V123000'"),
    (_COST, 409, "Cost action already recorded with a different body"),
])
@rsps.activate
def test_cms_sentence_is_recorded_word_for_word(action, status, sentence):
    rsps.add(rsps.POST, _URLS[action["action_kind"]], json={"error": sentence}, status=status)
    result = _dispatch(action)
    assert result.status == ActionStatus.DECLINED
    assert result.failure_kind == FailureKind.TARGET_REFUSED
    assert result.target_reason == sentence
    # failure_reason keeps the fixed § 10a sentence beside it.
    assert result.failure_reason == f"CMS refused the request (HTTP {status})."
    entry = _assert_entry_valid(result)
    assert entry["target_reason"] == sentence
    assert entry["error_status"] == status


def test_rule_approved_move_keeps_cms_sentence_from_the_signed_route(monkeypatch):
    """The SigV4 internal route shares _move_attempts; its refusal reads the same field."""
    from botocore.credentials import Credentials  # noqa: PLC0415

    rule_move = {**_MOVE, "decided_by": {"kind": "rule", "id": "fleet-meridian-01/RULE-1"},
                 "decision": "auto_approved"}
    monkeypatch.setenv("VSA_CMS_FI_INTERNAL_API_ENDPOINT",
                       "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod")

    class _FakeResp:
        status_code = 409
        content = json.dumps({"error": "The move's transfer cost is over the rule's limit."}).encode()

    executor = CmsExecutor(sleep_fn=lambda _: None, retry_policy=_POLICY,
                           credentials_fn=lambda: Credentials("AKIDEXAMPLE", "secret", "tok"))
    with patch("agents.executors.cms.requests.post", return_value=_FakeResp()) as post, \
         patch.dict(registry.EXECUTORS, {"cms": executor}):
        result = registry.dispatch(rule_move, {}, fire_escalation=False)
    assert post.call_args.args[0].endswith("/internal/moves")  # the signed route was used
    assert result.status == ActionStatus.DECLINED
    assert result.target_reason == "The move's transfer cost is over the rule's limit."


@rsps.activate
@pytest.mark.parametrize("code", [_VERDICT, "date_term_expired", "labor_op_mismatch",
                                  "mileage_exceeded", "vehicle_age_exceeded"])
def test_dms_warranty_422_verdict_reason_is_recorded_word_for_word(code):
    """DMS's 422 body as warranty_claims.py builds it: message and verdict_reason both
    carry verdict.reason, a reason code; AVX records the code unchanged."""
    with rsps.RequestsMock() as mock:
        mock.add(rsps.POST, f"{_DMS_BASE}/api/dms/fleet/warranty-claims",
                 json={"message": code, "verdict_reason": code, "matched_rule_id": "R-7"},
                 status=422)
        result = _dispatch(_WARRANTY)
    assert result.status == ActionStatus.DECLINED
    assert result.target_reason == code
    assert result.failure_reason == "DMS refused the request (HTTP 422)."
    _assert_entry_valid(result)


def _dms_executor():
    from unittest.mock import MagicMock

    return DmsExecutor(enqueue_ios_owner=MagicMock(), events_client=MagicMock(),
                       cloudwatch_client=MagicMock(), ssm_client=MagicMock(),
                       sleep_fn=lambda _: None, retry_policy=_POLICY)


@pytest.mark.parametrize("status,carried", [(422, _VERDICT), (409, None), (400, None)])
@rsps.activate
def test_dms_executor_reads_verdict_reason_only_on_a_422(status, carried):
    """dms.py's own 422-only read (a deliberate second gate), seen without dispatch()."""
    rsps.add(rsps.POST, f"{_DMS_BASE}/api/dms/fleet/warranty-claims",
             json={"verdict_reason": _VERDICT}, status=status)
    result = _dms_executor().dispatch(_WARRANTY, _CLAIMS)
    assert result.target_reason == carried


@rsps.activate
def test_dms_reads_verdict_reason_not_message():
    rsps.add(rsps.POST, f"{_DMS_BASE}/api/dms/fleet/warranty-claims",
             json={"message": "some other text"}, status=422)
    assert _dispatch(_WARRANTY).target_reason is None


@rsps.activate
def test_dms_warranty_refusal_other_than_422_records_no_sentence():
    rsps.add(rsps.POST, f"{_DMS_BASE}/api/dms/fleet/warranty-claims",
             json={"verdict_reason": _VERDICT}, status=409)
    result = _dispatch(_WARRANTY)
    assert result.status == ActionStatus.DECLINED
    assert result.target_reason is None


# ---------------------------------------------------------------------------
# Fallback: no usable sentence → no field, today's failure_reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [
    {"body": ""},
    {"body": "Vehicle is grounded"},                      # not JSON
    {"json": {"message": _GROUNDED}},                    # API Gateway's field, not CMS's
    {"json": {"error": ""}},
    {"json": {"error": 409}},
    {"json": [_GROUNDED]},
    {"body": json.dumps({"error": "x" * (MAX_BODY_BYTES + 1)})},
])
@rsps.activate
def test_no_usable_sentence_falls_back_to_todays_reason(kwargs):
    rsps.add(rsps.POST, _URLS["move_vehicles"], status=409, **kwargs)
    result = _dispatch(_MOVE)
    assert result.status == ActionStatus.DECLINED
    assert result.target_reason is None
    assert result.failure_reason == "CMS refused the request (HTTP 409)."
    entry = _assert_entry_valid(result)
    assert "target_reason" not in entry


# ---------------------------------------------------------------------------
# Never on a 5xx, a terminal error, an uncertain 409, a success or a guardrail block
# ---------------------------------------------------------------------------


@rsps.activate
def test_5xx_body_is_never_recorded():
    for _ in range(3):
        rsps.add(rsps.POST, _URLS["ground_vehicle"],
                 json={"error": "Traceback: internal-host.example token=abc"}, status=503)
    result = _dispatch(_GROUND)
    assert result.status == ActionStatus.FAILED
    assert result.target_reason is None
    assert "target_reason" not in _assert_entry_valid(result)


@rsps.activate
def test_409_after_an_uncertain_attempt_records_no_sentence():
    rsps.add(rsps.POST, _URLS["move_vehicles"], json={"error": "boom"}, status=503)
    rsps.add(rsps.POST, _URLS["move_vehicles"], json={"error": _GROUNDED}, status=409)
    result = _dispatch(_MOVE)
    assert result.status == ActionStatus.FAILED
    assert result.target_reason is None


@rsps.activate
def test_client_error_body_is_never_recorded():
    rsps.add(rsps.POST, _URLS["move_vehicles"], json={"error": "Not authorized"}, status=403)
    result = _dispatch(_MOVE)
    assert result.status == ActionStatus.FAILED
    assert result.target_reason is None


_PLAIN = "A plain sentence the cleaner keeps unchanged."


class _CarryingExecutor:
    """An executor that carries a keepable sentence on whatever result it is given, so
    only registry.dispatch()'s keep gate (target, kind, status) can drop it."""

    def __init__(self, result):
        self._result = result

    def dispatch(self, action, claims):
        return dataclasses.replace(self._result, target_reason=_PLAIN)


def _dispatch_carrying(target, result):
    action = {**_MOVE, "target_system": target, "action_kind": "acknowledge_only"}
    with patch.dict(registry.EXECUTORS, {target: _CarryingExecutor(result)}):
        return registry.dispatch(action, _CLAIMS, fire_escalation=False)


@pytest.mark.parametrize("target,result", [
    ("cms", ExecuteResult.refused(last_error_status=409, last_error_class="HTTPError")),
    ("cms", ExecuteResult.refused(last_error_status=400)),
    ("dms", ExecuteResult.refused(last_error_status=422)),
])
def test_dispatch_keeps_the_carried_sentence_on_an_allowed_refusal(target, result):
    """Control for the drop cases below: the same payload passes the gate when allowed."""
    assert _dispatch_carrying(target, result).target_reason == _PLAIN


@pytest.mark.parametrize("target,result", [
    ("cms", ExecuteResult.exhausted(retry_count=3, last_error_status=503,
                                    last_error_class="HTTPError")),
    ("cms", ExecuteResult.terminal_error(last_error_status=403, last_error_class="HTTPError")),
    ("cms", ExecuteResult.terminal_error(last_error_status=400, last_error_class="HTTPError")),
    ("cms", ExecuteResult.refused(last_error_status=None)),
    ("cms", ExecuteResult.success(target_ref="x", status=ActionStatus.EXECUTED)),
    ("dms", ExecuteResult.refused(last_error_status=409, last_error_class="HTTPError")),
    ("dms", ExecuteResult.refused(last_error_status=400)),
    ("dms", ExecuteResult.terminal_error(last_error_status=400, last_error_class="HTTPError")),
    ("retail", ExecuteResult.refused(last_error_status=409, last_error_class="HTTPError")),
    ("vehicle-command", ExecuteResult.refused(last_error_status=409)),
    ("adp", ExecuteResult.refused(last_error_status=422)),
])
def test_dispatch_drops_text_outside_an_allowed_refusal(target, result):
    assert _dispatch_carrying(target, result).target_reason is None


def test_dispatch_cleans_text_an_executor_carried_on_an_allowed_refusal():
    class _Raw:
        def dispatch(self, action, claims):
            return ExecuteResult.refused(last_error_status=409, last_error_class="HTTPError",
                                         target_reason="  Vehicle X\nis\tgrounded\u202e.\u200b ")
    with patch.dict(registry.EXECUTORS, {"cms": _Raw()}):
        out = registry.dispatch(_MOVE, _CLAIMS, fire_escalation=False)
    assert out.target_reason == "Vehicle X is grounded."


def test_guardrail_block_records_no_sentence():
    unguarded = {**_GROUND, "decided_by": {"kind": "user", "id": "u-1"}}
    out = registry.dispatch(unguarded, _CLAIMS, fire_escalation=False)
    assert out.guardrail == "G2"
    assert out.target_reason is None


# ---------------------------------------------------------------------------
# kept_target_reason: target, kind and status rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target,kind,status,kept", [
    ("cms", "target_refused", 409, True),
    ("cms", "target_refused", 400, True),
    ("cms", "target_refused", 422, True),
    ("cms", "target_refused", 404, False),   # our plumbing, not a refusal
    ("cms", "target_refused", 403, False),
    ("cms", "target_refused", 429, False),   # retryable
    ("cms", "target_refused", 503, False),
    ("cms", "target_refused", None, False),
    ("cms", "target_refused", True, False),  # bool is not a status
    ("cms", "target_refused", 409.0, False),
    ("cms", "executor_exhausted_retries", 409, False),
    ("cms", "executor_terminal_error", 409, False),
    ("cms", None, 409, False),
    ("dms", "target_refused", 422, True),
    ("dms", "target_refused", 409, False),
    ("dms", "target_refused", 400, False),
    ("retail", "target_refused", 409, False),
    ("adp", "target_refused", 422, False),
    (None, "target_refused", 409, False),
])
def test_kept_only_on_an_allowed_refusal(target, kind, status, kept):
    out = kept_target_reason(target_system=target, failure_kind=kind,
                             error_status=status, raw="A sentence.")
    assert (out == "A sentence.") is kept
    if not kept:
        assert out is None


# ---------------------------------------------------------------------------
# clean_target_reason: plain text, never cut
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentence", [
    _GROUNDED,
    _VERDICT,
    "This move wasn't approved by an operator.",
    "Invalid cost_action: 'xyz'",
    "Café d'été — 3 vehicles",
    "x" * MAX_LEN,
])
def test_an_ordinary_sentence_comes_back_unchanged(sentence):
    assert clean_target_reason(sentence) == sentence


@pytest.mark.parametrize("raw,expected", [
    ("Vehicle X\nis\tgrounded.", "Vehicle X is grounded."),
    ("  padded  ", "padded"),
    ("a\u00a0b", "a b"),                     # no-break space
    ("a\u2028b", "a b"),                     # line separator
    ("a\x00b\x07c\x7fd\x9be", "abcde"),       # C0, DEL, C1 controls
    ("\x1b[31mred", "[31mred"),               # the ESC is what makes it a terminal code
    ("abc\u202edef", "abcdef"),               # right-to-left override
    ("a\u200bb\u200dc\u2066d\ufeff", "abcd"),  # zero-width, isolate, BOM
    ("a\ue000b", "ab"),                       # private use
])
def test_controls_and_invisible_characters_are_removed(raw, expected):
    assert clean_target_reason(raw) == expected


@pytest.mark.parametrize("raw", [
    None, 42, b"bytes", {"error": "x"}, ["x"],
    " " * MAX_BODY_BYTES + "x",                # longer than any body the executor parses
    "", "   ", "\x00\u200b",
    "<script>alert(1)</script>", "a > b", "a<b",
    "x" * (MAX_LEN + 1),
    "a" * MAX_LEN + "\u200b" + "b",           # 201 visible characters once cleaned
])
def test_unusable_text_is_dropped_whole(raw):
    assert clean_target_reason(raw) is None


def test_whitespace_padding_does_not_count_against_the_length():
    assert clean_target_reason(" " * 50 + "x" * MAX_LEN + "\n" * 50) == "x" * MAX_LEN


def test_every_kept_value_matches_the_schema_backstop():
    """Across the BMP and some astral planes, whatever the cleaner keeps the schema admits."""
    checked = 0
    astral = [0x1F600, 0x1F6DE, 0xE0001, 0xE0041, 0xE007F, 0xF0000, 0x3FFFF, 0x10FFFF]
    for cp in list(range(0, 0x3100)) + list(range(0xD7F0, 0x10000)) + astral:
        out = clean_target_reason(f"a{chr(cp)}b")
        if out is not None:
            assert _SCHEMA_PATTERN.match(out), (hex(cp), out)
            assert len(out) <= MAX_LEN
            checked += 1
    assert checked > 10000  # anti-vacuity


# ---------------------------------------------------------------------------
# read_reason_field
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, content):
        self.content = content


@pytest.mark.parametrize("content,expected", [
    (b'{"error": "No."}', "No."),
    (b'{"other": "No."}', None),
    (b'{"error": null}', None),
    (b'{"error": ["No."]}', None),
    (b'["No."]', None),
    (b"not json", None),
    (b"\xff\xfe", None),
    (b"", None),
    ("not bytes", None),
    (None, None),
])
def test_read_reason_field(content, expected):
    assert read_reason_field(_Resp(content), "error") == expected


def test_read_reason_field_size_limit_is_inclusive():
    body = json.dumps({"error": "z"}).encode()
    body = body + b" " * (MAX_BODY_BYTES - len(body))
    assert len(body) == MAX_BODY_BYTES
    assert read_reason_field(_Resp(body), "error") == "z"
    assert read_reason_field(_Resp(body + b" "), "error") is None


def test_read_reason_field_never_raises():
    class _Exploding:
        @property
        def content(self):
            raise RuntimeError("stream already consumed")
    assert read_reason_field(_Exploding(), "error") is None


# ---------------------------------------------------------------------------
# Schema: the field is valid only beside an allowed refusal
# ---------------------------------------------------------------------------

_REFUSED_ENTRY = {
    "at": "2026-10-23T00:00:00Z", "by": "executor-cms",
    "from_status": "pending_execution", "to_status": "declined",
    "failure_kind": "target_refused", "retry_count": 0, "error_status": 409,
    "failure_reason": "CMS refused the request (HTTP 409).", "target_reason": _GROUNDED,
}


@pytest.mark.parametrize("change", [
    {"to_status": "failed", "failure_kind": "executor_terminal_error"},
    {"error_status": 503},
    {"error_status": None},
    {"guardrail": "G2"},
    {"target_reason": "a\nb"},
    {"target_reason": "<b>x</b>"},
    {"target_reason": "abc\u202edef"},
    {"target_reason": "x" * (MAX_LEN + 1)},
    {"target_reason": ""},
])
def test_schema_refuses_target_reason_outside_a_refusal(change):
    entry = {k: v for k, v in {**_REFUSED_ENTRY, **change}.items() if v is not None}
    assert list(_AUDIT_ENTRY_VALIDATOR.iter_errors(entry))


def test_schema_accepts_the_refusal_entry():
    assert not list(_AUDIT_ENTRY_VALIDATOR.iter_errors(_REFUSED_ENTRY))


def test_openapi_pattern_equals_the_schema_pattern():
    import yaml  # noqa: PLC0415

    doc = yaml.safe_load((_REPO / "docs/api/avx-findings-actions.openapi.yaml").read_text())
    found = []

    def walk(node):
        if isinstance(node, dict):
            tr = node.get("target_reason")
            if isinstance(tr, dict) and "pattern" in tr:
                found.append(tr)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    assert len(found) == 1
    assert found[0]["pattern"] == _SCHEMA_PATTERN.pattern
    assert found[0]["maxLength"] == MAX_LEN


def test_schema_max_length_equals_the_cleaner():
    assert _SCHEMA["$defs"]["audit_entry"]["properties"]["target_reason"]["maxLength"] == MAX_LEN


# ---------------------------------------------------------------------------
# The chat tool never passes target text to a model
# ---------------------------------------------------------------------------


def test_fleet_manager_chat_tool_drops_target_reason():
    """The chat tool imports strands and pydantic (root requirements.txt), which the public
    lint.yml's requirements-dev.txt doesn't install, so it skips there and runs in the repo's
    dev venv. Mutation: add "target_reason" to fleet_recommendation._AUDIT_ENTRY_ALLOWLIST →
    this test fails."""
    pytest.importorskip("strands")
    pytest.importorskip("pydantic")
    from agents.supervisor.tools import fleet_recommendation as fr  # noqa: PLC0415

    action = {
        "action_id": "AVX-ACT-1", "finding_id": "FIND-1", "action_kind": "move_vehicles",
        "target_system": "cms", "status": "declined",
        "audit_trail": [dict(_REFUSED_ENTRY)],
    }
    filtered = fr._filter_action(action)
    assert filtered["audit_trail"], "anti-vacuity: the entry itself must survive"
    assert "failure_reason" in filtered["audit_trail"][0]
    assert "target_reason" not in filtered["audit_trail"][0]
