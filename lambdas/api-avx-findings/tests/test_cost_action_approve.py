"""Tests for the cost-action approve-route fixes — FG3 review Cycle 1.

Critical C1 — `cms` may only be targeted by action kinds whose policy row
              names `cms`. Before the fix, Stage B making apply_cost_action
              executable with target=cms put "cms" into _VALID_TARGET_SYSTEMS
              for ALL executable kinds (regression of FG2d W2).

Warning W2  — `apply_cost_action` is only valid when the Finding carries a
              matching recommended_action. Without the guard an operator could
              approve it on a health/recall Finding with a body-supplied VIN,
              recording a cost decision outside "one decision per Finding"
              against a non-cost finding_id.

Mutation verification (inline, per RULES.md § 6):
  C1 mutant: remove `if target_system == "cms" and _row.target_system != "cms":` from
             handler.py. Then TestCmsTargetPerKind::test_cms_target_on_acknowledge_only_is_400
             returns 200 (broken). With the fix it returns 400.
  W2 mutant: remove the `if action_kind == "apply_cost_action" and rec is None...` check.
             Then TestApplyCostActionRequiresRecommendation::test_apply_cost_action_on_plain_finding_is_400
             returns 200. With the fix it returns 400.

Pattern: each test uses `_approve()` which patches `h._load_finding_by_id` and
`h._get_ddb` and stubs `h._dispatch_action` / `h._record_dispatch_result` via the
autouse fixture, matching the test_fg2d_review.py pattern in this directory.

Run:
    python3 -m pytest lambdas/api-avx-findings/tests/test_cost_action_approve.py -v
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest
from botocore.exceptions import ClientError
from unittest.mock import patch

# ── path setup ────────────────────────────────────────────────────────────────
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AVX_STAGE", "test")
os.environ.setdefault("AVX_FINDINGS_TABLE", "vsa-test-avx-findings")
os.environ.setdefault("AVX_ACTIONS_TABLE", "vsa-test-avx-actions")
os.environ.setdefault("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")

import handler as h  # noqa: E402
from agents.executors.base import ExecuteResult  # noqa: E402

# ── stateful DDB table fake ───────────────────────────────────────────────────

def _make_fake_actions_table():
    """Stateful DDB table fake; honours attribute_not_exists condition."""
    _CCF = type("ConditionalCheckFailedException", (ClientError,), {})

    class _FakeTable:
        def __init__(self):
            self._store: dict = {}
            from unittest.mock import MagicMock  # noqa: PLC0415
            self.meta = MagicMock()
            self.meta.client.exceptions.ConditionalCheckFailedException = _CCF

        def put_item(self, **kwargs):
            item = kwargs.get("Item", {})
            action_id = item.get("action_id")
            condition = kwargs.get("ConditionExpression", "")
            if (
                isinstance(condition, str)
                and "attribute_not_exists" in condition
                and action_id in self._store
            ):
                raise _CCF(
                    {"Error": {"Code": "ConditionalCheckFailedException",
                               "Message": "The conditional request failed"}},
                    "PutItem",
                )
            if action_id is not None:
                self._store[action_id] = item
            return {}

        def get_item(self, **kwargs):
            key = kwargs.get("Key", {})
            action_id = key.get("action_id")
            item = self._store.get(action_id)
            return {"Item": item} if item else {}

        def update_item(self, **_kw):
            return {"Attributes": {}}

    return _FakeTable()


# ── autouse fixture ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Stub dispatch and DDB to prevent real AWS calls."""
    monkeypatch.setattr(h, "_dispatch_action",
                        lambda action, claims: ExecuteResult.success(target_ref=None),
                        raising=False)
    monkeypatch.setattr(h, "_record_dispatch_result",
                        lambda action, result: action,
                        raising=False)
    monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
    monkeypatch.setattr(h, "_load_finding_by_id",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("test must override _load_finding_by_id")),
                        raising=False)
    from unittest.mock import MagicMock  # noqa: PLC0415
    fake_resource = MagicMock()
    fake_resource.Table.return_value = _make_fake_actions_table()
    monkeypatch.setattr(h, "_get_ddb", lambda: fake_resource, raising=False)
    monkeypatch.setattr(h, "_ddb", fake_resource, raising=False)


# ── shared helpers ────────────────────────────────────────────────────────────

_VIN = "MRD0000000000C1AA"
_FLEET_ID = "fleet-mrdn-01"


def _operator_claims(fleet_id: str = _FLEET_ID) -> dict:
    return {
        "sub": "op-sub-c1-0001",
        "cognito:groups": "fleet-operator",
        "custom:fleetIds": fleet_id,
    }


def _approve(
    finding: dict,
    target_system: str,
    action_kind: str,
    parameters: dict | None = None,
    claims: dict | None = None,
):
    """Run the approve route against `finding`; return (response, stored_dict)."""
    table = _make_fake_actions_table()
    body: dict[str, Any] = {
        "target_system": target_system,
        "action_kind": action_kind,
        "parameters": parameters or {},
    }
    event: dict[str, Any] = {
        "httpMethod": "POST",
        "resource": "/findings/{finding_id}/approve",
        "pathParameters": {"finding_id": finding["finding_id"]},
        "queryStringParameters": {},
        "requestContext": {
            "authorizer": {"claims": claims or _operator_claims()}
        },
        "body": json.dumps(body),
        "headers": {},
    }
    with patch.object(h, "_load_finding_by_id", return_value=finding), \
         patch.object(h, "_get_ddb") as mock_ddb:
        mock_ddb.return_value.Table.return_value = table
        resp = h.handler(event, None)
    return resp, dict(table._store)


def _decode(resp: dict) -> dict:
    return json.loads(resp["body"])


# ── fixtures ──────────────────────────────────────────────────────────────────

#: A plain Finding with no recommended_action (health kind).
_PLAIN_FINDING: dict[str, Any] = {
    "finding_id": "FIND-c1test-0001-0000-0000-000000000000",
    "finding_key": "a" * 64,
    "agent_id": "agent-2-vehicle-health",
    "agent_version": "1.0.0",
    "scope": {"fleet_id": _FLEET_ID},
    "fleet_id": _FLEET_ID,
    "severity": "soon",
    "computed_at": "2026-10-02T02:00:00Z",
    "expires_at": "2099-10-09T02:00:00Z",
    "confidence": "high",
    "finding_kind": "health.tires",
    "headline": "Tire pressure low",
    "detail": "...",
    "evidence": [],
    "inputs_hash": "a" * 64,
    "tokens_used": 0,
    "cost_usd": 0.0,
    "audit_trail": [],
    # no recommended_action
}

#: A VIN-scoped cost Finding with an apply_cost_action recommendation.
_COST_FINDING: dict[str, Any] = {
    "finding_id": "FIND-c1test-0002-0000-0000-000000000000",
    "finding_key": "b" * 64,
    "agent_id": "agent-fi-cost",
    "agent_version": "1.0.0",
    "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
    "fleet_id": _FLEET_ID,
    "severity": "soon",
    "computed_at": "2026-10-02T02:00:00Z",
    "expires_at": "2099-10-09T02:00:00Z",
    "confidence": "high",
    "finding_kind": "fleet.cost.anomaly",
    "headline": "Cost anomaly",
    "detail": "...",
    "evidence": [],
    "inputs_hash": "b" * 64,
    "tokens_used": 0,
    "cost_usd": 0.0,
    "audit_trail": [],
    "recommended_action": {
        "action_kind": "apply_cost_action",
        "target_system": "cms",
        "parameters": {
            "cost_action": "reassign_or_remarket",
            "vehicle_vin": _VIN,
            "month": "2026-09",
            "cause": "low_utilisation",
        },
    },
}

#: A recall Finding with a non-cost recommendation (book_recall_service → dms).
_RECALL_FINDING: dict[str, Any] = {
    "finding_id": "FIND-c1test-0003-0000-0000-000000000000",
    "finding_key": "c" * 64,
    "agent_id": "agent-fi-recall-warranty",
    "agent_version": "1.0.0",
    "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
    "fleet_id": _FLEET_ID,
    "severity": "p0",
    "computed_at": "2026-10-02T02:00:00Z",
    "expires_at": "2099-10-09T02:00:00Z",
    "confidence": "high",
    "finding_kind": "fleet.recall.vehicle",
    "headline": "Recall campaign open",
    "detail": "...",
    "evidence": [],
    "inputs_hash": "c" * 64,
    "tokens_used": 0,
    "cost_usd": 0.0,
    "audit_trail": [],
    "recommended_action": {
        "action_kind": "book_recall_service",
        "target_system": "dms",
        "parameters": {
            "vehicle_vin": _VIN,
            "recall_id": "MRD-2024-001",
            "dealer_id": "dealer-mrdn-01",
        },
    },
}


# ── Critical C1: cms only for kinds whose row targets cms ─────────────────────


class TestCmsTargetPerKind:
    """C1: 'cms' as target_system is only valid when the kind's policy row says cms.

    Before the fix, Stage B making apply_cost_action executable with target=cms
    put 'cms' into _VALID_TARGET_SYSTEMS for all executable kinds, reopening FG2d W2
    (test_fg2d_review.py::TestTargetSystemCmsRefused::test_cms_target_on_non_fi_finding_is_400).

    Mutation verification for this module:
      Remove `if target_system == "cms" and _row.target_system != "cms":` from handler.py.
      Then test_cms_target_on_acknowledge_only_is_400 returns 200. With the fix: 400.
    """

    def test_cms_target_on_acknowledge_only_is_400(self) -> None:
        """`acknowledge_only` with target_system=cms → 400 (unknown target_system).

        acknowledge_only.target_system='none'. Before the fix, 'cms' was in
        _VALID_TARGET_SYSTEMS and acknowledge_only slipped through to CmsExecutor,
        recording a declined Action in the operator's by_created_by partition.
        """
        resp, stored = _approve(
            _PLAIN_FINDING,
            target_system="cms",
            action_kind="acknowledge_only",
        )
        assert resp["statusCode"] == 400, (
            f"acknowledge_only+cms must be 400 (unknown target_system); "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert not stored, "nothing must be written to the Actions table"

    def test_apply_cost_action_with_cms_is_accepted(self) -> None:
        """Positive control: apply_cost_action+cms on a cost Finding is accepted (200/202).

        apply_cost_action.target_system='cms'. The per-kind check must let it through.
        """
        resp, stored = _approve(
            _COST_FINDING,
            target_system="cms",
            action_kind="apply_cost_action",
            parameters={
                "cost_action": "reassign_or_remarket",
                "vehicle_vin": _VIN,
                "month": "2026-09",
                "cause": "low_utilisation",
            },
        )
        assert resp["statusCode"] in (200, 202), (
            f"apply_cost_action+cms on a cost Finding must not be rejected at the "
            f"per-kind target check; got {resp['statusCode']}: {resp.get('body', '')[:300]}"
        )
        assert stored, "an Action must be written"

    def test_acknowledge_only_with_none_target_still_accepted(self) -> None:
        """Positive control: acknowledge_only + target_system=none (its declared target) → 200."""
        resp, stored = _approve(
            _PLAIN_FINDING,
            target_system="none",
            action_kind="acknowledge_only",
        )
        assert resp["statusCode"] == 200, (
            f"acknowledge_only+none must still be accepted after the C1 fix; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored, "an Action must be written"


# ── Warning W2: apply_cost_action only on a cost recommendation ───────────────


class TestApplyCostActionRequiresRecommendation:
    """W2: apply_cost_action is refused (400) unless the Finding has a matching recommendation.

    Before the fix, an operator could approve apply_cost_action on a health or recall
    Finding with a body-supplied VIN. This bypassed "one decision per Finding" and
    recorded a cost decision against a non-cost finding_id with any VIN the operator chose.

    Mutation verification:
      Remove `if action_kind == "apply_cost_action" and (rec is None or ...)` from handler.py.
      Then test_apply_cost_action_on_plain_finding_is_400 returns 200. With the fix: 400.
    """

    def test_apply_cost_action_on_plain_finding_is_400(self) -> None:
        """apply_cost_action on a Finding with no recommended_action → 400."""
        resp, stored = _approve(
            _PLAIN_FINDING,
            target_system="cms",
            action_kind="apply_cost_action",
            parameters={
                "cost_action": "reassign_or_remarket",
                "month": "2026-09",
                "cause": "low_utilisation",
            },
        )
        assert resp["statusCode"] == 400, (
            f"apply_cost_action on a plain Finding (no recommended_action) must be 400; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert not stored, "nothing must be written"

    def test_apply_cost_action_on_recall_finding_is_400(self) -> None:
        """apply_cost_action on a recall Finding (book_recall_service recommendation) → 400.

        The Finding has a recommended_action, but its action_kind is book_recall_service,
        not apply_cost_action. The fix refuses this as well.
        """
        resp, stored = _approve(
            _RECALL_FINDING,
            target_system="cms",
            action_kind="apply_cost_action",
            parameters={
                "cost_action": "reassign_or_remarket",
                "month": "2026-09",
                "cause": "low_utilisation",
            },
        )
        assert resp["statusCode"] == 400, (
            f"apply_cost_action on a recall Finding must be 400; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert not stored, "nothing must be written"

    def test_vehicle_vin_server_set_from_scope_not_body(self) -> None:
        """A body VIN that doesn't match scope.vin → 400 (existing FI-item VIN check).

        Positive: the server-set VIN path is active for apply_cost_action on a cost
        Finding (the FI-item path fires). A body VIN that contradicts scope.vin is 400.
        """
        resp, _ = _approve(
            _COST_FINDING,
            target_system="cms",
            action_kind="apply_cost_action",
            parameters={
                "cost_action": "reassign_or_remarket",
                "vehicle_vin": "MRD9999999999999Z",  # wrong VIN
                "month": "2026-09",
                "cause": "low_utilisation",
            },
        )
        # The existing FI-item VIN check fires: body VIN != scope VIN → 400
        assert resp["statusCode"] == 400, (
            f"A body VIN that does not match scope.vin must be 400 for "
            f"apply_cost_action; got {resp['statusCode']}: {resp.get('body', '')[:300]}"
        )



class TestCostActionParametersCheckedBeforeWrite:
    """Group 3 review cycle 2 (executors W1): the approve route checks a cost action's
    parameters before writing anything, and `month` is server-set from the recommendation.

    Mutants: deleting the `cost_action_parameter_error` check lets the missing-cause override
    write a decision (stored non-empty); deleting the month check lets 2025-01 be stored.
    """

    def test_override_without_cause_is_400_and_writes_nothing(self) -> None:
        resp, stored = _approve(
            _COST_FINDING, target_system="cms", action_kind="apply_cost_action",
            parameters={"cost_action": "driver_coaching"},
        )
        assert resp["statusCode"] == 400, resp.get("body", "")[:300]
        assert "cause" in resp["body"]
        assert stored == {}, "nothing may be written before the parameters are valid"

    def test_unknown_cost_action_is_400_and_writes_nothing(self) -> None:
        resp, stored = _approve(
            _COST_FINDING, target_system="cms", action_kind="apply_cost_action",
            parameters={"cost_action": "sell_it", "cause": "low_utilisation"},
        )
        assert resp["statusCode"] == 400
        assert stored == {}

    def test_body_month_must_match_the_recommendation(self) -> None:
        resp, stored = _approve(
            _COST_FINDING, target_system="cms", action_kind="apply_cost_action",
            parameters={"cost_action": "reassign_or_remarket", "cause": "low_utilisation",
                        "month": "2025-01"},
        )
        assert resp["statusCode"] == 400
        assert "month" in resp["body"]
        assert stored == {}

    def test_month_is_filled_from_the_recommendation(self) -> None:
        resp, stored = _approve(
            _COST_FINDING, target_system="cms", action_kind="apply_cost_action",
            parameters={"cost_action": "driver_coaching", "cause": "efficiency"},
        )
        assert resp["statusCode"] in (200, 201), resp.get("body", "")[:300]
        [item] = stored.values()
        assert item["parameters"]["month"] == "2026-09"
        assert item["parameters"]["vehicle_vin"] == _VIN
        assert item["decision"] == "overridden"



    def test_cost_finding_without_a_vin_writes_nothing(self) -> None:
        """Review cycle 3 suggestion (probe P6): no scope.vin and no body VIN → refused before
        any write, never a recorded decision that then fails for good."""
        finding = {**_COST_FINDING, "scope": {"fleet_id": _FLEET_ID}}
        resp, stored = _approve(
            finding, target_system="cms", action_kind="apply_cost_action",
            parameters={"cost_action": "reassign_or_remarket", "cause": "low_utilisation"},
        )
        assert resp["statusCode"] in (400, 403), resp.get("body", "")[:300]
        assert stored == {}
