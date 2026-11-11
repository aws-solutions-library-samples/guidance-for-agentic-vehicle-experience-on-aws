"""Red tests: Stage D executor behaviour — task 1.4 items 11 and 12.

Contract § 5 (G5), § 4 (server-set parameters), and executor dispatch for Stage D kinds.

  11. G5: each engineering kind refused for operator, user, rule and a missing decided_by,
      with the fixed sentence "Blocked: engineering decisions need a product engineer.";
      G1–G4 refuse engineer (engineer cannot pass operator-required guardrails).

  12. Executors:
      12a. book_fleet_service body carries `lines` built from the server-set `needs`;
           DmsExecutor sends lines, not raw needs.
      12b. AdpExecutor 200 → executed; 422 → refused (target_refused);
           400 → failed (executor_terminal_error); 5xx after retries → failed
           (executor_exhausted_retries).
      12c. ground_vehicle fault form: forwards `fault_codes` (never `recall_id`),
           and exactly one of the two is accepted (having both → 400).

Every new-behaviour test is xfail(strict=True, reason="FI stage D task 1.4"). Imports
are inside test bodies so collection never errors against non-existent modules.

Governes `agents/executors/tests/` (the package conftest.py guards all AWS clients).
CSV custom:fleetIds in all claims fixtures.

Run:
    .venv/bin/python -m pytest agents/executors/tests/test_stage_d_kinds.py -q
"""
from __future__ import annotations

import json
from typing import Any

import pytest
import responses as rsps  # type: ignore[import]
from unittest.mock import MagicMock, patch

from agents.executors.base import ActionStatus, FailureKind

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"
_ACTION_UUID = "aabbccdd-eeff-4000-8000-001122334455"

_OPERATOR_CLAIMS: dict = {
    "sub": "op-sub-d-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": "fleet-meridian-01,fleet-meridian-02",
    "token": "Bearer.fake.stage-d.jwt",
}

_ENGINEER_CLAIMS: dict = {
    "sub": "eng-sub-d-0001",
    "cognito:groups": ["product-engineer"],
    "custom:fleetIds": "fleet-meridian-01",
}

_DMS_BASE = "http://dms-staging-d.test"
_DMS_PREFIX = f"{_DMS_BASE}/api/dms/fleet"
_ADP_WRITER_ARN = "arn:aws:lambda:us-east-1:123456789012:function:adp-staging-digital-thread-writer"

#: The G5 sentence from contract § 5 — pinned as a literal string.
_G5_SENTENCE = "Blocked: engineering decisions need a product engineer."

#: G1–G4 sentences from failure_reason.py — one per guardrail, pinned as
#: literals so a mutation that changes the builder text is caught here too.
_G1_G4_SENTENCES: dict[str, str] = {
    "G1": "Blocked: exempting a safety recall needs a fleet operator.",
    "G2": "Blocked: grounding a vehicle needs a fleet operator.",
    "G3": "Blocked: sending a warranty claim needs a fleet operator.",
    "G4": "Blocked: completing a recall needs a fleet operator.",
}

#: Engineering kinds that G5 applies to.
_G5_KINDS = [
    "record_engineering_decision",
    "issue_recall",
    "issue_software_campaign",
]

#: Operator-required kinds (G1–G3/G4) that an engineer cannot pass.
_OPERATOR_KINDS = [
    ("exempt_recall", "cms", "G1"),
    ("ground_vehicle", "cms", "G2"),
    ("submit_warranty_claim", "dms", "G3"),
    ("complete_recall", "cms", "G4"),   # Fix C3: G4 was missing from the parametrize list
]


def _make_dms_executor():
    from agents.executors.dms import DmsExecutor
    from agents.executors.base import RetryPolicy

    return DmsExecutor(
        enqueue_ios_owner=MagicMock(),
        events_client=MagicMock(),
        cloudwatch_client=MagicMock(),
        ssm_client=MagicMock(),
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
    )


def _make_cms_executor():
    from agents.executors.cms import CmsExecutor
    from agents.executors.base import RetryPolicy

    return CmsExecutor(
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
    )


def _make_adp_executor():
    from agents.executors.adp import AdpExecutor
    from agents.executors.base import RetryPolicy

    return AdpExecutor(
        thread_writer_arn=_ADP_WRITER_ARN,
        lambda_client=MagicMock(),
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
    )


# ============================================================================
# Item 11 — Guardrail G5: engineering kinds refused for non-engineers
# ============================================================================

class TestGuardrailG5:
    """G5: engineering action kinds refused unless decided_by.kind == 'engineer'."""

    @pytest.mark.parametrize("kind", _G5_KINDS)
    def test_g5_refuses_operator_decided_by(self, kind: str):
        """G5 fires when decided_by.kind == 'operator' on an engineering kind."""
        from agents.executors import registry

        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": kind,
            "target_system": "adp",
            "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
            "parameters": {
                "investigation_id": "inv-001",
                "pattern_id": "pat-001",
                "option_id": "opt-001",
            },
        }
        result = registry.dispatch(
            action, _OPERATOR_CLAIMS, fire_escalation=False
        )
        assert result.status == ActionStatus.DECLINED
        assert result.failure_kind == FailureKind.TARGET_REFUSED
        assert result.guardrail == "G5", (
            f"Expected guardrail G5 for kind={kind!r} with operator decided_by."
        )

    @pytest.mark.parametrize("kind", _G5_KINDS)
    def test_g5_refuses_rule_decided_by(self, kind: str):
        """G5 fires when decided_by.kind == 'rule' (auto-approval path)."""
        from agents.executors import registry

        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": kind,
            "target_system": "adp",
            "decided_by": {"kind": "rule", "id": "auto-approve"},
            "parameters": {},
        }
        result = registry.dispatch(action, {}, fire_escalation=False)
        assert result.status == ActionStatus.DECLINED
        assert result.guardrail == "G5"

    @pytest.mark.parametrize("kind", _G5_KINDS)
    def test_g5_refuses_missing_decided_by(self, kind: str):
        """G5 fires when decided_by is absent."""
        from agents.executors import registry

        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": kind,
            "target_system": "adp",
            "parameters": {},
        }
        result = registry.dispatch(action, {}, fire_escalation=False)
        assert result.status == ActionStatus.DECLINED
        assert result.guardrail == "G5"

    @pytest.mark.parametrize("kind", _G5_KINDS)
    def test_g5_failure_reason_is_fixed_sentence(self, kind: str):
        """G5 failure_reason is the fixed sentence from contract § 5 — byte for byte."""
        from agents.executors import registry

        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": kind,
            "target_system": "adp",
            "decided_by": {"kind": "operator", "id": "op-001"},
            "parameters": {},
        }
        result = registry.dispatch(action, _OPERATOR_CLAIMS, fire_escalation=False)
        audit = result.audit_entry(by="op-001", at="2026-10-05T10:00:00Z")
        assert audit.get("failure_reason") == _G5_SENTENCE, (
            f"Expected failure_reason={_G5_SENTENCE!r} for G5. "
            "Mutation: change or omit the sentence → this assertion fails."
        )

    def test_g5_drop_mutation_operator_passes_unblocked(self):
        """Mutation guard: without G5 an operator passes — must cause this test to fail.

        The test asserts 403/DECLINED; a deleted G5 returns success → test fails.
        """
        from agents.executors import registry

        kind = "record_engineering_decision"
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": kind,
            "target_system": "adp",
            "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
            "parameters": {
                "investigation_id": "inv-001",
                "pattern_id": "pat-001",
                "option_id": "opt-001",
            },
        }
        result = registry.dispatch(action, _OPERATOR_CLAIMS, fire_escalation=False)
        assert result.status == ActionStatus.DECLINED, (
            "Mutation: if G5 is removed, an operator would pass unblocked. "
            "This assertion catches that."
        )

    @pytest.mark.parametrize("kind", _G5_KINDS)
    def test_g5_refuses_user_decided_by(self, kind: str):
        """G5 fires when decided_by.kind == 'user' on an engineering kind (Fix W19).

        The previous G5 test suite had no 'user' case. A G5 that only checks
        'operator' would let a 'user' through.
        Mutation: G5 only rejects 'operator' decided_by → user passes, this fails.
        """
        from agents.executors import registry

        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": kind,
            "target_system": "adp",
            "decided_by": {"kind": "user", "id": "user-sub-0001"},
            "parameters": {},
        }
        result = registry.dispatch(action, {}, fire_escalation=False)
        assert result.status == ActionStatus.DECLINED, (
            f"G5 must fire for decided_by.kind='user' on {kind!r}. "
            "Mutation: G5 only rejects 'operator' → user passes unblocked, this fails."
        )
        assert result.guardrail == "G5", (
            f"Guardrail must be G5 for decided_by.kind='user' on {kind!r}. "
            "Mutation: allow 'user' → different guardrail or None, this fails."
        )
        audit = result.audit_entry(by="user-sub-0001", at="2026-10-05T10:00:00Z")
        assert audit.get("failure_reason") == _G5_SENTENCE, (
            "G5 failure_reason must be the fixed sentence for 'user' just as for 'operator'. "
            "Mutation: different sentence for user → mismatch, this fails."
        )

    def test_g5_positive_engineer_not_blocked_by_g5(self):
        """Positive control: decided_by.kind == 'engineer' must not be blocked by G5.

        Fix W19: without a positive control, a G5 that refuses everyone (including
        engineers) passes all the refusal tests. Stubs the adp executor so the
        positive control never invokes real AWS Lambda.

        Guard: requires 'adp' to already be registered in registry.EXECUTORS (Stage D).
        KeyError if adp is not yet registered → test fails → xfail(strict=True) catches it,
        keeping the red-test state correct while Stage D code is missing.
        Once 'adp' IS registered, patch.dict replaces the real executor with a stub
        (MagicMock lambda_client) for the duration of this test.

        Mutation: make G5 refuse engineers too → result.guardrail == 'G5' for an
        engineer, this assertion catches it.
        """
        from agents.executors import registry

        # Guard: KeyError if Stage D has not registered adp yet → xfailed.
        _ = registry.EXECUTORS["adp"]

        kind = "record_engineering_decision"
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": kind,
            "target_system": "adp",
            "decided_by": {"kind": "engineer", "id": _ENGINEER_CLAIMS["sub"]},
            "parameters": {
                "investigation_id": "inv-001",
                "pattern_id": "pat-001",
                "option_id": "opt-001",
            },
        }
        # Replace the registered AdpExecutor with a stub that has a MagicMock lambda
        # client so dispatch never touches real AWS.
        stub_executor = _make_adp_executor()
        with patch.dict(registry.EXECUTORS, {"adp": stub_executor}):
            result = registry.dispatch(action, _ENGINEER_CLAIMS, fire_escalation=False)
        assert result.guardrail != "G5", (
            f"An engineer must not be blocked by G5 on {kind!r}. "
            "Mutation: make G5 refuse engineers → guardrail='G5', this fails."
        )


    @pytest.mark.parametrize("kind,target,guardrail", _OPERATOR_KINDS)
    def test_g1_g4_refuse_engineer_decided_by(
        self, kind: str, target: str, guardrail: str
    ):
        """G1–G4 refuse decided_by.kind == 'engineer' (engineer cannot pass operator-required).

        Not xfail: G1–G4 already check decided_by.kind == 'operator'; an engineer is
        refused today (existing behaviour). This is a regression guard that must stay
        green when G5 is added.

        Fix C3: also asserts result.guardrail == guardrail so that a mutation that
        disables one guardrail (making the result DECLINED for a different reason)
        is caught. Mutation: disable G2 → exempt_recall still DECLINED via G1, but
        ground_vehicle is not DECLINED by G2 — guardrail assertion catches it.
        """
        from agents.executors import registry

        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": kind,
            "target_system": target,
            "decided_by": {"kind": "engineer", "id": _ENGINEER_CLAIMS["sub"]},
            "parameters": {"vehicle_vin": _VIN},
        }
        result = registry.dispatch(action, _ENGINEER_CLAIMS, fire_escalation=False)
        assert result.status == ActionStatus.DECLINED, (
            f"An engineer decided_by must be refused by {guardrail} on kind={kind!r}."
        )
        # Fix C3: assert the guardrail tag so a mutation disabling one guardrail is caught.
        assert result.guardrail == guardrail, (
            f"Expected guardrail={guardrail!r} for kind={kind!r}, got {result.guardrail!r}. "
            f"Mutation: disable {guardrail} → DECLINED by a different guardrail or None, fails."
        )
        # Fix C3: assert the fixed sentence so a mutation changing the text is caught.
        audit = result.audit_entry(by=_ENGINEER_CLAIMS["sub"], at="2026-10-05T10:00:00Z")
        expected_sentence = _G1_G4_SENTENCES[guardrail]
        assert audit.get("failure_reason") == expected_sentence, (
            f"Expected failure_reason={expected_sentence!r} for {guardrail} on kind={kind!r}. "
            f"Mutation: change {guardrail} sentence → mismatch, this fails."
        )


# ============================================================================
# Item 12a — book_fleet_service: lines from server-set needs
# ============================================================================

class TestBookFleetServiceBody:
    """DmsExecutor for book_fleet_service sends lines built from server-set needs."""

    @rsps.activate
    def test_booking_body_carries_lines_from_needs(self):
        """The POST body sent to DMS contains `lines` derived from server-set needs."""
        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/repair-orders",
            json={"ro_id": "ro-fh-9001"},
            status=201,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "dealer_id": "dealer-meridian-001",
                    "urgency": "this_month",
                    "needs": [
                        {
                            "need_id": "need-001",
                            "labor_op": "LO-EV-BRAKE-001",
                            "description": "Brake inspection",
                            "pay_type": "customer_pay",
                        }
                    ],
                },
            }
            executor.dispatch(action, _OPERATOR_CLAIMS)

        assert len(rsps.calls) == 1
        sent_body = json.loads(rsps.calls[0].request.body)
        assert "lines" in sent_body, (
            "The DMS request body must carry `lines` for book_fleet_service "
            "(spec D9: lines built from server-set needs)."
        )
        lines = sent_body["lines"]
        assert len(lines) == 1
        assert lines[0].get("labor_op") == "LO-EV-BRAKE-001"

    @rsps.activate
    def test_booking_body_pay_type_customer_pay(self):
        """Lines carry pay_type 'customer_pay' (spec D9)."""
        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/repair-orders",
            json={"ro_id": "ro-fh-9002"},
            status=201,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "dealer_id": "dealer-meridian-001",
                    "urgency": "this_month",
                    "needs": [
                        {
                            "need_id": "need-002",
                            "labor_op": "LO-EV-TIRE-001",
                            "description": "Tire rotation",
                            "pay_type": "customer_pay",
                        }
                    ],
                },
            }
            executor.dispatch(action, _OPERATOR_CLAIMS)

        sent_body = json.loads(rsps.calls[0].request.body)
        lines = sent_body.get("lines", [])
        assert all(l.get("pay_type") == "customer_pay" for l in lines), (
            "All lines must carry pay_type=customer_pay (spec D9)."
        )


# ============================================================================
# Item 12b — AdpExecutor response codes
# ============================================================================

class TestAdpExecutorResponseCodes:
    """AdpExecutor: 200 → executed; 422 → refused; 400 → failed; 5xx → failed.

    The executor invokes a Lambda (the digital-thread writer) in us-east-1 cross-region.
    Responses are stubbed via the Lambda client mock.
    """

    #: Minimal investigation Finding — provides the structured evidence the
    #: executor reads to build the payload.  Kept here so each test does not
    #: duplicate it.  The response-code tests do not check payload contents;
    #: they only verify that the response StatusCode is mapped correctly.
    _FINDING: dict = {
        "finding_id": "FND-rc-code-001",
        "finding_kind": "quality.investigation",
        "agent_id": "agent-fi-quality",
        "scope": {"population_id": "meridian-motors"},
        "evidence": [
            {
                "kind": "quality.root_cause",
                "source": "agent-fi-quality",
                "source_ref": "inv-rc-001",
                "value": {
                    "candidate_id": "cand-rc-001",
                    "failure_mode_id": "fm-rc-001",
                    "diagnosis_source": "model",
                    "evidence_refs": [],
                    "agent_version": "agent-fi-quality@1.0.0",
                },
                "computed_at": "2026-10-05T00:00:00Z",
                "provenance": {
                    "at": "2026-10-05T00:00:00Z", "agent_version": "1.0.0",
                    "inputs_hash": "rc001", "from_expires_at": "2026-10-05T00:00:00Z",
                    "to_expires_at": "2026-10-15T00:00:00Z",
                },
            },
        ],
        "recommended_action": {
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "parameters": {
                "investigation_id": "inv-rc-001",
                "pattern_id": "pat-rc-P0AA6",
                "option_id": "opt-rc-001",
                "notes": "",
            },
        },
    }

    def test_200_maps_to_executed(self):
        """Lambda invoke 200 with FunctionError absent → executed."""
        from agents.executors.adp import AdpExecutor
        from agents.executors.base import RetryPolicy

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 200, "body": json.dumps({"thread_id": "thr-001"})}
            ).encode()),
        }
        executor = AdpExecutor(
            thread_writer_arn=_ADP_WRITER_ARN,
            lambda_client=mock_lambda,
            sleep_fn=lambda _: None,
            retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
        )
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "decided_by": {"kind": "engineer", "id": _ENGINEER_CLAIMS["sub"]},
            "parameters": {
                "investigation_id": "inv-001",
                "pattern_id": "pat-001",
                "option_id": "opt-001",
            },
            "investigation_finding": TestAdpExecutorResponseCodes._FINDING,
        }
        result = executor.dispatch(action, _ENGINEER_CLAIMS)
        assert result.status == ActionStatus.EXECUTED

    def test_422_maps_to_refused(self):
        """Lambda invoke returning a 422 application error → refused (target_refused)."""
        from agents.executors.adp import AdpExecutor
        from agents.executors.base import RetryPolicy

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 422, "body": json.dumps({"error": "pattern_not_found"})}
            ).encode()),
        }
        executor = AdpExecutor(
            thread_writer_arn=_ADP_WRITER_ARN,
            lambda_client=mock_lambda,
            sleep_fn=lambda _: None,
            retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
        )
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "decided_by": {"kind": "engineer", "id": "eng-sub-0001"},
            "parameters": {
                "investigation_id": "inv-unknown",
                "pattern_id": "pat-unknown",
                "option_id": "opt-001",
            },
            "investigation_finding": TestAdpExecutorResponseCodes._FINDING,
        }
        result = executor.dispatch(action, _ENGINEER_CLAIMS)
        assert result.status == ActionStatus.DECLINED
        assert result.failure_kind == FailureKind.TARGET_REFUSED

    def test_400_maps_to_failed(self):
        """Lambda invoke returning 400 → failed (executor_terminal_error)."""
        from agents.executors.adp import AdpExecutor
        from agents.executors.base import RetryPolicy

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 400, "body": json.dumps({"error": "malformed_request"})}
            ).encode()),
        }
        executor = AdpExecutor(
            thread_writer_arn=_ADP_WRITER_ARN,
            lambda_client=mock_lambda,
            sleep_fn=lambda _: None,
            retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
        )
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "decided_by": {"kind": "engineer", "id": "eng-sub-0001"},
            "parameters": {
                "investigation_id": "inv-001",
                "pattern_id": "pat-001",
                "option_id": "opt-MISSING",
            },
            "investigation_finding": TestAdpExecutorResponseCodes._FINDING,
        }
        result = executor.dispatch(action, _ENGINEER_CLAIMS)
        assert result.status == ActionStatus.FAILED
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR

    def test_5xx_exhausted_retries_maps_to_failed(self):
        """Lambda invoke 5xx repeated → failed (executor_exhausted_retries) after retries."""
        from agents.executors.adp import AdpExecutor
        from agents.executors.base import RetryPolicy

        mock_lambda = MagicMock()
        # Always returns 500.
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 500, "body": json.dumps({"error": "internal_error"})}
            ).encode()),
        }
        executor = AdpExecutor(
            thread_writer_arn=_ADP_WRITER_ARN,
            lambda_client=mock_lambda,
            sleep_fn=lambda _: None,
            retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
        )
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "decided_by": {"kind": "engineer", "id": "eng-sub-0001"},
            "parameters": {
                "investigation_id": "inv-001",
                "pattern_id": "pat-001",
                "option_id": "opt-001",
            },
            "investigation_finding": TestAdpExecutorResponseCodes._FINDING,
        }
        result = executor.dispatch(action, _ENGINEER_CLAIMS)
        assert result.status == ActionStatus.FAILED
        assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES


# ============================================================================
# Item 12b extension — AdpExecutor invoke payload (C8)
# ============================================================================

#: The complete field list that AdpExecutor must send to the thread writer for
#: record_engineering_decision, per decisions.md "Cycle 2: what the thread writer reads".
#: No model text from the Finding (reasoning, tradeoffs, detail) may appear here.
_ADP_PAYLOAD_FIELDS = frozenset({
    "action_id",
    "action_kind",
    "decided_by",
    "decision",
    "investigation_finding_id",
    "pattern_id",
    "candidate_id",
    "failure_mode_id",
    "option_id",
    "evidence_refs",
    "diagnosis_source",
    "agent_version",
    "notes",
})


class TestAdpExecutorPayload:
    """C8: AdpExecutor invoke payload equals the decided field list; no model text.

    decisions.md 'Cycle 2: what the thread writer reads': AdpExecutor sends
    everything the record needs, taken server-side from the Finding and Action.
    The writer never reads AVX, so model text reaches a draft only if
    AdpExecutor puts it in the payload. These tests assert it does not.
    """

    def test_invoke_payload_keys_equal_decided_field_list(self):
        """Lambda invoke payload keys equal exactly _ADP_PAYLOAD_FIELDS; no extras.

        Mutation: add reasoning to the payload → extra key 'reasoning' present →
        payload_keys != _ADP_PAYLOAD_FIELDS → this fails.
        """
        from agents.executors.adp import AdpExecutor
        from agents.executors.base import RetryPolicy

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 200, "body": json.dumps({"thread_id": "thr-payload-001"})}
            ).encode()),
        }
        executor = AdpExecutor(
            thread_writer_arn=_ADP_WRITER_ARN,
            lambda_client=mock_lambda,
            sleep_fn=lambda _: None,
            retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
        )

        #: Investigation Finding with model text in quality fields — sentinels must not
        #: reach the Lambda invoke payload.
        #: Real shape: evidence array with {kind: "quality.root_cause", value: {...}}
        #: per finding.schema.json. The nested `quality` dict was the old fixture shape.
        finding = {
            "finding_id": "FND-c8-payload-001",
            "finding_kind": "quality.investigation",
            "agent_id": "agent-fi-quality",
            "scope": {"population_id": "meridian-motors"},
            "evidence": [
                {
                    "kind": "quality.root_cause",
                    "source": "agent-fi-quality",
                    "source_ref": "inv-c8-001",
                    "value": {
                        "candidate_id": "cand-payload-001",
                        "failure_mode_id": "fm-payload-001",
                        "reasoning": "SENTINEL_REASONING_TEXT",
                        "confidence": "high",
                        "diagnosis_source": "model",
                        "evidence_refs": ["ev-001"],
                        "agent_version": "agent-fi-quality@1.0.0",
                    },
                    "computed_at": "2026-10-05T00:00:00Z",
                    "provenance": {
                        "at": "2026-10-05T00:00:00Z",
                        "agent_version": "1.0.0",
                        "inputs_hash": "abc123",
                        "from_expires_at": "2026-10-05T00:00:00Z",
                        "to_expires_at": "2026-10-15T00:00:00Z",
                    },
                },
                {
                    "kind": "quality.options",
                    "source": "agent-fi-quality",
                    "source_ref": "inv-c8-001",
                    "value": [
                        {
                            "option_id": "opt-payload-001",
                            "tradeoffs": "SENTINEL_TRADEOFFS_TEXT",
                            "detail": "SENTINEL_DETAIL_TEXT",
                        }
                    ],
                    "computed_at": "2026-10-05T00:00:00Z",
                    "provenance": {
                        "at": "2026-10-05T00:00:00Z",
                        "agent_version": "1.0.0",
                        "inputs_hash": "abc123",
                        "from_expires_at": "2026-10-05T00:00:00Z",
                        "to_expires_at": "2026-10-15T00:00:00Z",
                    },
                },
            ],
            "recommended_action": {
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": "inv-c8-001",
                    "pattern_id": "pat-payload-P0AA6",
                    "option_id": "opt-payload-001",
                    "notes": "",
                },
            },
        }
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "decided_by": {"kind": "engineer", "sub": _ENGINEER_CLAIMS["sub"]},
            "decision": "approved",
            "investigation_finding": finding,
        }
        executor.dispatch(action, _ENGINEER_CLAIMS)

        assert mock_lambda.invoke.called, (
            "AdpExecutor must invoke the Lambda for record_engineering_decision."
        )
        call_kwargs = mock_lambda.invoke.call_args
        raw_payload = (
            call_kwargs.kwargs.get("Payload")
            or (call_kwargs.args[0] if call_kwargs.args else b"")
        )
        if isinstance(raw_payload, (bytes, bytearray)):
            payload = json.loads(raw_payload)
        else:
            payload = json.loads(raw_payload)

        payload_keys = set(payload.keys())
        assert payload_keys == _ADP_PAYLOAD_FIELDS, (
            f"AdpExecutor invoke payload keys must equal exactly the decided field list. "
            f"Extra keys: {payload_keys - _ADP_PAYLOAD_FIELDS!r}. "
            f"Missing keys: {_ADP_PAYLOAD_FIELDS - payload_keys!r}. "
            "Mutation: add 'reasoning' to the payload → extra key detected → this fails."
        )

    def test_invoke_payload_contains_no_model_text_sentinels(self):
        """Lambda invoke payload must not contain sentinel model text from the Finding.

        Sentinel strings are planted in quality.root_cause.reasoning and
        quality.options[].tradeoffs. A correct AdpExecutor builds the payload
        server-side from the Finding's structured fields, not from the free-text
        model fields. A payload that contains either sentinel fails C8.

        Mutation: copy quality.root_cause.reasoning into the payload →
        'SENTINEL_REASONING_TEXT' in json.dumps(payload) → this fails.
        """
        from agents.executors.adp import AdpExecutor
        from agents.executors.base import RetryPolicy

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(read=lambda: json.dumps(
                {"statusCode": 200, "body": json.dumps({"thread_id": "thr-c8-001"})}
            ).encode()),
        }
        executor = AdpExecutor(
            thread_writer_arn=_ADP_WRITER_ARN,
            lambda_client=mock_lambda,
            sleep_fn=lambda _: None,
            retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
        )

        finding = {
            "finding_id": "FND-c8-002",
            "finding_kind": "quality.investigation",
            "agent_id": "agent-fi-quality",
            "scope": {"population_id": "meridian-motors"},
            "evidence": [
                {
                    "kind": "quality.root_cause",
                    "source": "agent-fi-quality",
                    "source_ref": "inv-c8-002",
                    "value": {
                        "candidate_id": "cand-002",
                        "failure_mode_id": "fm-002",
                        "reasoning": "SENTINEL_REASONING_TEXT",
                        "confidence": "high",
                        "diagnosis_source": "model",
                        "evidence_refs": [],
                        "agent_version": "agent-fi-quality@1.0.0",
                    },
                    "computed_at": "2026-10-05T00:00:00Z",
                    "provenance": {
                        "at": "2026-10-05T00:00:00Z",
                        "agent_version": "1.0.0",
                        "inputs_hash": "def456",
                        "from_expires_at": "2026-10-05T00:00:00Z",
                        "to_expires_at": "2026-10-15T00:00:00Z",
                    },
                },
                {
                    "kind": "quality.options",
                    "source": "agent-fi-quality",
                    "source_ref": "inv-c8-002",
                    "value": [
                        {
                            "option_id": "opt-002",
                            "tradeoffs": "SENTINEL_TRADEOFFS_TEXT",
                        }
                    ],
                    "computed_at": "2026-10-05T00:00:00Z",
                    "provenance": {
                        "at": "2026-10-05T00:00:00Z",
                        "agent_version": "1.0.0",
                        "inputs_hash": "def456",
                        "from_expires_at": "2026-10-05T00:00:00Z",
                        "to_expires_at": "2026-10-15T00:00:00Z",
                    },
                },
            ],
            "recommended_action": {
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": "inv-c8-002",
                    "pattern_id": "pat-002",
                    "option_id": "opt-002",
                    "notes": "",
                },
            },
        }
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "decided_by": {"kind": "engineer", "sub": _ENGINEER_CLAIMS["sub"]},
            "decision": "approved",
            "investigation_finding": finding,
        }
        executor.dispatch(action, _ENGINEER_CLAIMS)

        call_kwargs = mock_lambda.invoke.call_args
        raw_payload = (
            call_kwargs.kwargs.get("Payload")
            or (call_kwargs.args[0] if call_kwargs.args else b"")
        )
        payload_str = (
            raw_payload.decode() if isinstance(raw_payload, (bytes, bytearray))
            else raw_payload
        )
        assert "SENTINEL_REASONING_TEXT" not in payload_str, (
            "quality.root_cause.reasoning sentinel must not appear in the invoke payload. "
            "Mutation: copy reasoning into the payload → sentinel string appears → "
            "this fails."
        )
        assert "SENTINEL_TRADEOFFS_TEXT" not in payload_str, (
            "quality.options[].tradeoffs sentinel must not appear in the invoke payload. "
            "Mutation: copy tradeoffs into the payload → sentinel string appears → "
            "this fails."
        )


# ============================================================================
# Item 12c — ground_vehicle fault form: fault_codes only, never recall_id
# ============================================================================

class TestGroundVehicleFaultForm:
    """CmsExecutor fault form: fault_codes forwarded, never recall_id, not both."""

    _FAULT_FINDING_ID = "FND-g3f24-fault-0001"

    @rsps.activate
    def test_fault_form_forwards_fault_codes_not_recall_id(self):
        """ground_vehicle fault form sends the exact keys {vin, faultCodes, findingId, actionId}.

        W2 (G3F2.4): the Action now carries finding_id so findingId and actionId are
        in the body.  The assertion is the exact key set so two surviving mutants are caught:
        1. faultCodes: [] (empty list) → has_faults=False → body omits faultCodes → FAILS.
        2. campaignId added to fault form → extra key → body != expected dict → FAILS.

        Mutation 1: send faultCodes: [] → has_faults=False → faultCodes absent → FAILS.
        Mutation 2: add campaignId to fault form → extra key → FAILS.
        """
        rsps.add(
            rsps.POST,
            "https://cms-staging-d.test/api/v1/fleet-intelligence/groundings",
            json={"grounding_id": "grd-fault-001"},
            status=200,
        )
        _action_id = f"AVX-ACT-{_ACTION_UUID}"
        with patch.dict("os.environ",
                        {"VSA_CMS_API_ENDPOINT": "https://cms-staging-d.test"}):
            executor = _make_cms_executor()
            action = {
                "action_id": _action_id,
                "finding_id": self._FAULT_FINDING_ID,   # W2 fix: needed for findingId
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
                "parameters": {
                    "vehicle_vin": _VIN,
                    "fault_codes": ["P0AA6"],   # fault form — non-empty
                },
            }
            executor.dispatch(action, _OPERATOR_CLAIMS)

        assert len(rsps.calls) == 1
        sent_body = json.loads(rsps.calls[0].request.body)
        expected = {
            "vin": _VIN,
            "faultCodes": ["P0AA6"],
            "findingId": self._FAULT_FINDING_ID,
            "actionId": _action_id,
        }
        assert sent_body == expected, (
            f"Fault form must send exactly {{vin, faultCodes, findingId, actionId}}; "
            f"got {list(sent_body.keys())!r}. "
            "Mutation 1: faultCodes:[] → has_faults=False → faultCodes absent → FAILS. "
            "Mutation 2: campaignId added → extra key → FAILS."
        )
        assert isinstance(sent_body["faultCodes"], list) and sent_body["faultCodes"], (
            "faultCodes must be a non-empty list. "
            "Mutation: faultCodes:[] → falsy → body omits faultCodes → key absent → FAILS."
        )
        assert "campaignId" not in sent_body, (
            "Fault form must never carry campaignId (that is the recall form's field). "
            "Mutation: add campaignId to fault form → this assertion FAILS."
        )

    def test_both_fault_codes_and_recall_id_rejected(self):
        """ground_vehicle with both fault_codes and recall_id → 400 / refused.

        Contract § 4: exactly one of recall_id (Stage C) or fault_codes (Stage D).
        Having both is a schema error.
        """
        from agents.executors.cms import CmsExecutor
        from agents.executors.base import RetryPolicy

        executor = CmsExecutor(
            sleep_fn=lambda _: None,
            retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
        )
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
            "parameters": {
                "vehicle_vin": _VIN,
                "recall_id": "MRD-2024-001",   # Stage C form
                "fault_codes": ["P0AA6"],       # Stage D form — both present → error
            },
        }
        result = executor.dispatch(action, _OPERATOR_CLAIMS)
        assert result.status in (ActionStatus.DECLINED, ActionStatus.FAILED), (
            "Providing both recall_id and fault_codes must be rejected "
            "(contract § 4: exactly one of the two)."
        )

    @rsps.activate
    def test_recall_form_still_carries_recall_id(self):
        """Stage C recall form still carries recall_id and never fault_codes."""
        rsps.add(
            rsps.POST,
            "https://cms-staging-d.test/api/v1/fleet-intelligence/groundings",
            json={"grounding_id": "grd-recall-001"},
            status=200,
        )
        with patch.dict("os.environ",
                        {"VSA_CMS_API_ENDPOINT": "https://cms-staging-d.test"}):
            executor = _make_cms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
                "parameters": {
                    "vehicle_vin": _VIN,
                    "recall_id": "MRD-2024-001",  # Stage C form
                },
            }
            executor.dispatch(action, _OPERATOR_CLAIMS)

        sent_body = json.loads(rsps.calls[0].request.body)
        assert "campaignId" in sent_body, (
            "Stage C recall form must carry campaignId in the CMS request body "
            "(the CMS groundings API field for the recall campaign)."
        )
        assert "fault_codes" not in sent_body, (
            "Stage C recall form must not carry fault_codes."
        )



# ============================================================================
# W2 (G3F1.5) — book_fleet_service: line_no and malformed-needs refuse
# ============================================================================

class TestBookFleetServiceLineNo:
    """W2 (G3F1.5): DMS fleet-service lines carry line_no as a string, numbered 1..n.

    DMS lines key on (ro_id, line_no) for warranty matching. Without line_no,
    warranty lookups would always miss.

    Mutation: omit line_no from lines → assertion fails.
    """

    @rsps.activate
    def test_two_needs_produce_two_numbered_lines(self):
        """Two needs in the recommendation → two lines with line_no 1 and 2 (int).

        W3 c fix: DMS stores NUMBER for line_no; was wrongly sent as string "1","2".
        Mutation: omit line_no → lines[0].get('line_no') is None → FAILS.
        Mutation: send string → type(line_no) is str, not int → FAILS.
        """
        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/repair-orders",
            json={"ro_id": "ro-fh-lineno-001"},
            status=201,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "dealer_id": "dealer-meridian-001",
                    "urgency": "this_week",
                    "needs": [
                        {"need_id": "need-001", "labor_op": "LAB-BRK-01",
                         "description": "Brake inspection"},
                        {"need_id": "need-002", "labor_op": "LAB-SUS-03",
                         "description": "Suspension check"},
                    ],
                },
            }
            executor.dispatch(action, _OPERATOR_CLAIMS)

        assert len(rsps.calls) == 1
        sent_body = json.loads(rsps.calls[0].request.body)
        lines = sent_body.get("lines", [])
        assert len(lines) == 2, f"Two needs must produce two lines; got {len(lines)}"
        assert lines[0].get("line_no") == 1, (
            f"First line must carry line_no=1 (int); got {lines[0].get('line_no')!r}. "
            "Mutation: omit line_no → None → FAILS."
        )
        assert lines[1].get("line_no") == 2, (
            f"Second line must carry line_no=2 (int); got {lines[1].get('line_no')!r}. "
            "Mutation: omit line_no → None → FAILS."
        )
        assert lines[0].get("labor_op") == "LAB-BRK-01"
        assert lines[1].get("labor_op") == "LAB-SUS-03"
        # W3 c fix: line_no is an int (DMS NUMBER key), not a string.
        assert type(lines[0]["line_no"]) is int, (
            f"line_no must be int (DMS stores NUMBER); got {type(lines[0]['line_no']).__name__}"
        )
        assert type(lines[1]["line_no"]) is int, (
            f"line_no must be int (DMS stores NUMBER); got {type(lines[1]['line_no']).__name__}"
        )

    @rsps.activate
    def test_single_need_produces_line_no_1(self):
        """Single need → one line with line_no=1 (int, W3 c fix)."""
        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/repair-orders",
            json={"ro_id": "ro-fh-lineno-002"},
            status=201,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "dealer_id": "dealer-meridian-001",
                    "urgency": "this_week",
                    "needs": [{"need_id": "need-001", "labor_op": "LAB-BRK-01"}],
                },
            }
            executor.dispatch(action, _OPERATOR_CLAIMS)

        sent_body = json.loads(rsps.calls[0].request.body)
        lines = sent_body.get("lines", [])
        assert len(lines) == 1
        # W3 c fix: int, not str.
        assert lines[0].get("line_no") == 1
        assert type(lines[0]["line_no"]) is int


class TestBookFleetServiceMalformedNeeds:
    """W2 (G3F1.5): malformed needs → executor terminal_error, nothing sent.

    A need that isn't a Mapping or has no labor_op was silently dropped;
    now it causes the whole booking to be refused.

    Mutation guard: revert to `continue` on malformed needs → DMS is reached
    and records a call → len(rsps.calls) == 0 assertion FAILS.
    """

    def _dispatch_with_needs(self, needs: list) -> Any:
        """Dispatch book_fleet_service with the given needs; return the result.

        VSA_DMS_API_ENDPOINT is always set to _DMS_BASE so the executor can
        reach the endpoint if the body is valid (used by mutant-catch tests).
        """
        from agents.executors.dms import DmsExecutor
        from agents.executors.base import RetryPolicy

        executor = DmsExecutor(
            sleep_fn=lambda _: None,
            retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
        )
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": _VIN,
                "dealer_id": "dealer-meridian-001",
                "urgency": "this_week",
                "needs": needs,
            },
        }
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            return executor.dispatch(action, _OPERATOR_CLAIMS)

    @rsps.activate
    def test_non_dict_need_is_terminal_error_no_http_call(self):
        """A non-dict need → terminal_error, MissingDispatchParameter, and NO HTTP call to DMS.

        The executor detects the malformed body before posting (body is None →
        MissingDispatchParameter) so no requests are made.

        W1 (G3F2.4): endpoint is now configured; @rsps.activate + 201 stub mean the
        mutant (continue on non-dict need) reaches the HTTP layer and rsps records a
        call.  The test asserts len(rsps.calls) == 0 and last_error_class == 'MissingDispatchParameter',
        both of which distinguish the correct code from the mutant.

        Mutation: continue on non-dict need → executor posts with the remaining
        valid needs → 201 → IN_PROGRESS → FAILS on FAILED assertion.
        Mutation: continue on non-dict need → len(rsps.calls) == 1 → FAILS.
        Mutation: continue on non-dict need → last_error_class != MissingDispatchParameter → FAILS.
        """
        from agents.executors.base import ActionStatus

        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/repair-orders",
            json={"ro_id": "RO-malformed-001"},
            status=201,
        )
        result = self._dispatch_with_needs([
            {"labor_op": "LAB-BRK-01"},
            "malformed-string-need",   # not a dict
        ])
        # Result must be terminal_error (MissingDispatchParameter, not MissingJwt)
        assert result.status == ActionStatus.FAILED, (
            f"Non-dict need must produce terminal_error; got {result.status!r}. "
            "Mutation: continue → executor reaches HTTP layer → IN_PROGRESS → FAILS."
        )
        assert len(rsps.calls) == 0, (
            f"NO HTTP call must be made for a malformed-need body; "
            f"got {len(rsps.calls)} call(s). "
            "Mutation: continue → body built from valid need → POST fired → FAILS."
        )
        assert result.last_error_class == "MissingDispatchParameter", (
            f"last_error_class must be 'MissingDispatchParameter' (body is None before JWT "
            f"round-trip); got {result.last_error_class!r}. "
            "Mutation: continue → success path → last_error_class=None → FAILS."
        )

    @rsps.activate
    def test_missing_labor_op_is_terminal_error_no_http_call(self):
        """A need without labor_op → terminal_error, MissingDispatchParameter, NO HTTP call.

        W1 (G3F2.4): same fix as test_non_dict_need_is_terminal_error_no_http_call —
        endpoint configured, @rsps.activate + 201 stub so the mutant would be caught
        by the len(rsps.calls) and last_error_class assertions.

        Mutation: continue on missing labor_op → executor posts with remaining
        valid needs → 201 → IN_PROGRESS → FAILS on FAILED assertion.
        Mutation: continue on missing labor_op → len(rsps.calls) == 1 → FAILS.
        """
        from agents.executors.base import ActionStatus

        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/repair-orders",
            json={"ro_id": "RO-malformed-002"},
            status=201,
        )
        result = self._dispatch_with_needs([
            {"labor_op": "LAB-BRK-01"},
            {"need_id": "need-002"},   # missing labor_op
        ])
        assert result.status == ActionStatus.FAILED, (
            f"Missing labor_op must produce terminal_error; got {result.status!r}. "
            "Mutation: continue → executor reaches HTTP layer → FAILS."
        )
        assert len(rsps.calls) == 0, (
            f"NO HTTP call must be made; got {len(rsps.calls)} call(s). "
            "Mutation: continue on missing labor_op → POST fired → FAILS."
        )
        assert result.last_error_class == "MissingDispatchParameter", (
            f"last_error_class must be 'MissingDispatchParameter'; "
            f"got {result.last_error_class!r}. "
            "Mutation: continue → success path → last_error_class=None → FAILS."
        )

    @rsps.activate
    def test_blank_labor_op_is_terminal_error(self):
        """A need with blank labor_op → terminal_error, MissingDispatchParameter, NO HTTP call.

        G3F3.3 (c W2): cycle 3 found this test vacuous because it had no @rsps.activate
        and no 201 stub.  Without them, removing the `not labor_op.strip()` guard lets the
        executor reach the real `dms-staging-d.test` host, which fails for network reasons —
        the test stays green even though the property is broken.  With the stub in place, a
        `continue` on blank labor_op reaches the HTTP layer and rsps records the call.

        Mutation guard: remove `not labor_op.strip()` check in dms.py →
        executor builds a body and fires the POST → len(rsps.calls) == 1 → FAILS.
        Mutation guard: same removal → last_error_class != 'MissingDispatchParameter' → FAILS.
        """
        from agents.executors.base import ActionStatus

        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/repair-orders",
            json={"ro_id": "RO-blank-labor-001"},
            status=201,
        )
        result = self._dispatch_with_needs([
            {"labor_op": "   "},   # blank (only whitespace)
        ])
        assert result.status == ActionStatus.FAILED, (
            f"Blank labor_op must produce terminal_error; got {result.status!r}. "
            "Mutation: remove `not labor_op.strip()` → executor reaches HTTP layer → "
            "IN_PROGRESS → FAILS."
        )
        assert len(rsps.calls) == 0, (
            f"NO HTTP call must be made for a blank labor_op; "
            f"got {len(rsps.calls)} call(s). "
            "Mutation: remove blank check → body built → POST fired → FAILS."
        )
        assert result.last_error_class == "MissingDispatchParameter", (
            f"last_error_class must be 'MissingDispatchParameter'; "
            f"got {result.last_error_class!r}. "
            "Mutation: remove blank check → success path → last_error_class=None → FAILS."
        )
