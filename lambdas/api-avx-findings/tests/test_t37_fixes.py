"""Route-level test for task 3.7 defect 1.

Defect 1 — handler never sets investigation_finding on the action before dispatch.
  ``_approve_finding`` builds an action dict and calls ``_dispatch_action``.
  Before this fix, the action passed to the executor never carried
  ``investigation_finding``, so every real ``record_engineering_decision``
  approval ended with ``MissingFinding`` from ``AdpExecutor``.

Fix:
  For ``record_engineering_decision``, a dispatch-only copy of the action carries
  ``investigation_finding`` (the loaded Finding).  The persisted Action row
  written by ``put_item`` intentionally does NOT carry it — it is not an
  Action schema field and would grow the row unboundedly.

Route-level test (spec: ".kiro/specs/2026-10-05-fi-fleet-health-stage-d/"):
  Approve a population Finding built with real schema evidence:
  - assert _dispatch_action was called with an action containing
    ``investigation_finding`` whose ``finding_id`` matches the loaded Finding.
  - assert the action passed to ``put_item`` does NOT contain ``investigation_finding``.

Mutation targets documented inline; each was verified in a git-archive copy.

Run:
    .venv/bin/python -m pytest lambdas/api-avx-findings/tests/test_t37_fixes.py -q
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.dirname(_LAMBDAS_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AVX_STAGE", "test")
os.environ.setdefault("AVX_FINDINGS_TABLE", "vsa-test-avx-findings")
os.environ.setdefault("AVX_ACTIONS_TABLE", "vsa-test-avx-actions")
os.environ.setdefault("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")

import handler as h  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FINDING_ID = "FND-t37-pop-001"
_PATTERN_ID = "pat-t37-P0AA6"
_INVESTIGATION_ID = "inv-t37-aabbccdd"
_OPTION_ID = "opt-t37-001"

_ENGINEER_CLAIMS: dict = {
    "sub": "eng-sub-t37-0001",
    "cognito:groups": ["product-engineer"],
    "custom:fleetIds": "fleet-meridian-01",
}


def _population_finding_with_evidence() -> dict:
    """A population-scoped quality.investigation Finding with real schema evidence.

    Built to match the finding.schema.json shape:
    - top-level ``agent_version``
    - ``evidence[*].kind == "quality.root_cause"`` with nested ``value``
    - ``recommended_action`` for ``record_engineering_decision``
    """
    return {
        "finding_id": _FINDING_ID,
        "finding_kind": "quality.investigation",
        "agent_id": "agent-fi-quality",
        "agent_version": "agent-fi-quality@2.0.0",
        "scope": {"population_id": "meridian-motors"},
        "evidence": [
            {
                "kind": "quality.root_cause",
                "source": "agent-fi-quality",
                "source_ref": _INVESTIGATION_ID,
                "value": {
                    "candidate_id": "cand-t37-001",
                    "failure_mode_id": "fm-t37-001",
                    "diagnosis_source": "model",
                    "evidence_refs": ["ev-t37-001"],
                    "agent_version": "agent-fi-quality@1.0.0",
                },
                "computed_at": "2026-10-05T00:00:00Z",
                "provenance": {
                    "at": "2026-10-05T00:00:00Z",
                    "agent_version": "1.0.0",
                    "inputs_hash": "t37xyzabc",
                    "from_expires_at": "2026-10-05T00:00:00Z",
                    "to_expires_at": "2026-10-15T00:00:00Z",
                },
            },
        ],
        "recommended_action": {
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "parameters": {
                "investigation_id": _INVESTIGATION_ID,
                "pattern_id": _PATTERN_ID,
                "option_id": _OPTION_ID,
                "notes": "",
            },
        },
    }


def _approve_event(finding: dict) -> dict:
    return {
        "httpMethod": "POST",
        "resource": "/findings/{finding_id}/approve",
        "path": f"/findings/{finding['finding_id']}/approve",
        "pathParameters": {"findingId": finding["finding_id"]},
        "queryStringParameters": {},
        "body": json.dumps({
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "parameters": {
                "investigation_id": _INVESTIGATION_ID,
                "pattern_id": _PATTERN_ID,
                "option_id": _OPTION_ID,
            },
        }),
        "requestContext": {
            "authorizer": {"claims": _ENGINEER_CLAIMS},
        },
        "headers": {},
    }


# ---------------------------------------------------------------------------
# Defect 1 — investigation_finding injected into dispatch_action, not stored
# ---------------------------------------------------------------------------

class TestFindingPassedToDispatch:
    """_approve_finding must pass investigation_finding to the executor, not to put_item."""

    def test_dispatch_action_carries_investigation_finding(self):
        """_dispatch_action is called with an action that has investigation_finding set.

        The Finding loaded by the route must be forwarded to the executor so
        AdpExecutor can read the quality.root_cause evidence and build the
        Lambda payload.  Before this fix, _dispatch_action received an action
        without the field, causing MissingFinding.

        Mutation: remove the ``investigation_finding`` injection before calling
        ``_dispatch_action`` → dispatch_action has no 'investigation_finding'
        key → assertion fails.
        """
        finding = _population_finding_with_evidence()
        event = _approve_event(finding)

        dispatch_action_received: list[dict] = []

        # Capture the action _dispatch_action is called with.
        def capture_dispatch(action, claims):
            dispatch_action_received.append(dict(action))
            # Return a success-shaped ExecuteResult mock.
            from agents.executors.base import ActionStatus
            r = MagicMock()
            r.status = ActionStatus.EXECUTED
            r.target_ref = "thr-t37-001"
            r.failure_kind = None
            r.failure_reason = None
            r.guardrail = None
            return r

        mock_ddb = MagicMock()
        mock_table = MagicMock()
        mock_ddb.Table.return_value = mock_table
        mock_table.put_item.return_value = {}
        mock_table.update_item.return_value = {
            "Attributes": {
                "action_id": "AVX-ACT-dummy",
                "status": "executed",
                "finding_id": _FINDING_ID,
                "target_system": "adp",
                "action_kind": "record_engineering_decision",
                "parameters": {},
                "decided_by": {"kind": "engineer", "id": "eng-sub-t37-0001"},
                "created_by": "owner-eng-sub-t37-0001",
                "audit_trail": [],
            }
        }
        mock_table.meta.client.exceptions.ConditionalCheckFailedException = Exception

        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=mock_ddb), \
             patch.object(h, "_dispatch_action", side_effect=capture_dispatch), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: mock_table.update_item.return_value["Attributes"]):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"Expected 200 from a valid engineer approve; got {resp['statusCode']}. "
            f"Body: {resp.get('body')}"
        )
        assert dispatch_action_received, "_dispatch_action must have been called once."
        dispatched = dispatch_action_received[0]
        assert "investigation_finding" in dispatched, (
            "'investigation_finding' must be present in the action passed to "
            "_dispatch_action. "
            "Mutation: remove the injection → key absent → FAILS."
        )
        assert dispatched["investigation_finding"]["finding_id"] == _FINDING_ID, (
            f"investigation_finding.finding_id must equal {_FINDING_ID!r}; "
            f"got {dispatched['investigation_finding'].get('finding_id')!r}. "
            "Mutation: inject a wrong finding → mismatch → FAILS."
        )

    def test_put_item_action_has_no_investigation_finding(self):
        """The action stored in DynamoDB (put_item) must not contain investigation_finding.

        The finding is large (evidence arrays, provenance, etc.); storing it in
        every Action row would grow rows unboundedly.  It must flow to the
        executor only, never to the persisted row.

        Mutation: add ``investigation_finding`` to the action dict before
        ``put_item`` (e.g. omit the separate dispatch_action copy) → key present
        in captured put_item arg → assertion fails.
        """
        finding = _population_finding_with_evidence()
        event = _approve_event(finding)

        put_item_calls: list[dict] = []

        def capture_put_item(**kwargs):
            put_item_calls.append(kwargs.get("Item", {}))
            return {}

        mock_ddb = MagicMock()
        mock_table = MagicMock()
        mock_ddb.Table.return_value = mock_table
        mock_table.put_item.side_effect = capture_put_item
        mock_table.update_item.return_value = {
            "Attributes": {
                "action_id": "AVX-ACT-dummy",
                "status": "executed",
                "finding_id": _FINDING_ID,
                "target_system": "adp",
                "action_kind": "record_engineering_decision",
                "parameters": {},
                "decided_by": {"kind": "engineer", "id": "eng-sub-t37-0001"},
                "created_by": "owner-eng-sub-t37-0001",
                "audit_trail": [],
            }
        }
        mock_table.meta.client.exceptions.ConditionalCheckFailedException = Exception

        fake_result = MagicMock()
        from agents.executors.base import ActionStatus
        fake_result.status = ActionStatus.EXECUTED
        fake_result.target_ref = "thr-t37-002"
        fake_result.failure_kind = None
        fake_result.failure_reason = None
        fake_result.guardrail = None

        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=mock_ddb), \
             patch.object(h, "_dispatch_action", return_value=fake_result), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: mock_table.update_item.return_value["Attributes"]):
            resp = h.handler(event, None)

        assert put_item_calls, "DynamoDB put_item must have been called."
        stored_item = put_item_calls[0]
        assert "investigation_finding" not in stored_item, (
            "The persisted Action row (put_item) must NOT contain 'investigation_finding'. "
            "Mutation: inject the finding before put_item → key present → FAILS."
        )
