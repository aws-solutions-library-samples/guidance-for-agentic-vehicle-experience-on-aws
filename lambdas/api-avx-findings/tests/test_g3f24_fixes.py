"""Route-level tests for G3F2.4 fixes: C2 (missing _is_pop_finding route tests) and C1 confirmation.

C2: ``_is_pop_finding = set(_pop_scope) == {"population_id"}`` — the exact single-key
check prevents a Finding with scope ``{fleet_id, population_id}`` or ``{vin, population_id}``
from being treated as a population-scoped Finding.  The code is correct at 0782f25, but
neither the approve nor the decline path has a test that catches the old formula
``bool(population_id) and not vin``.

Mutation for C2:
  Revert ``set(_pop_scope) == {"population_id"}`` to ``bool(population_id) and not vin``
  on the approve route (:1130) and the decline route (:1467).
  Under the mutant a finding with scope ``{fleet_id: "x", population_id: "y"}`` has
  ``population_id`` truthy and no ``vin`` key, so ``_is_pop_finding`` becomes True and
  the engineer is passed through to a 200 response.  The tests here assert 403 (or 400)
  for those scopes, so they FAIL under the mutant.

C1 (route → DMS body): a route-level test confirms that the approve path sends
``line_no`` as an int to DMS (not the old string).

Run:
    .venv/bin/python -m pytest lambdas/api-avx-findings/tests/test_g3f24_fixes.py -q
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import responses as rsps  # type: ignore[import]

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
# Constants
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000016"
_FLEET_ID = "fleet-meridian-02"
_POPULATION_ID = "meridian-motors"
_INVESTIGATION_ID = "inv-g3f24-0001"
_PATTERN_ID = "pat-g3f24-P0AA6"
_OPTION_ID = "opt-g3f24-001"
_THREAD_ID = "thr-g3f24-0001"
_DRAFT_ID = "dft-g3f24-0001"
_DEALER_ID = "dealer-g3f24-seattle"
_DMS_BASE = "http://dms-g3f24.test"
_DMS_FLEET_RO_URL = f"{_DMS_BASE}/api/dms/fleet/repair-orders"

_ENGINEER_CLAIMS: dict = {
    "sub": "eng-sub-g3f24-0001",
    "cognito:groups": ["product-engineer"],
}
# Operator claims include a token (needed by DmsExecutor for the JWT guard)
# and fleet membership.
_OPERATOR_CLAIMS: dict = {
    "sub": "op-sub-g3f24-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": _FLEET_ID,
    "token": "Bearer.fake.g3f24.jwt",
}

# ---------------------------------------------------------------------------
# Finding builders
# ---------------------------------------------------------------------------

def _pop_investigation_finding() -> dict:
    """Population-scoped quality.investigation Finding (scope has population_id ONLY)."""
    return {
        "finding_id": "FND-g3f24-pop-inv-0001",
        "finding_kind": "quality.investigation",
        "agent_id": "agent-fi-quality",
        "scope": {"population_id": _POPULATION_ID},
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


def _mixed_fleet_pop_finding() -> dict:
    """Scope has BOTH fleet_id and population_id — not a pure population Finding."""
    return {
        "finding_id": "FND-g3f24-mixed-fleet-pop-0001",
        "finding_kind": "quality.investigation",
        "agent_id": "agent-fi-quality",
        "scope": {"fleet_id": _FLEET_ID, "population_id": _POPULATION_ID},
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


def _mixed_vin_pop_finding() -> dict:
    """Scope has BOTH vin and population_id — not a pure population Finding."""
    return {
        "finding_id": "FND-g3f24-mixed-vin-pop-0001",
        "finding_kind": "quality.investigation",
        "agent_id": "agent-fi-quality",
        "scope": {"vin": _VIN, "population_id": _POPULATION_ID},
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


def _fleet_health_finding_with_needs(needs: list | None = None) -> dict:
    """VIN-scoped fleet.health.service Finding (for the DMS body line_no test)."""
    if needs is None:
        needs = [{"labor_op": "LAB-BRK-42", "need_id": "need-g3f24-001"}]
    return {
        "finding_id": "FND-g3f24-health-0001",
        "finding_kind": "fleet.health.service",
        "agent_id": "agent-fi-fleet-health",
        "severity": "p1",
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "fleet_id": _FLEET_ID,
        "evidence": [],
        "expires_at": "2099-12-31T00:00:00Z",
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": _VIN,
                "needs": needs,
                "dealer_id": _DEALER_ID,
                "urgency": "this_week",
            },
        },
    }

# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def _fake_table() -> Any:
    """Minimal fake DDB Table: records put_item; raises CCF on duplicate."""
    from botocore.exceptions import ClientError

    _CCF = type("ConditionalCheckFailedException", (ClientError,), {})

    class _FT:
        def __init__(self):
            self._store: dict = {}
            self.meta = MagicMock()
            self.meta.client.exceptions.ConditionalCheckFailedException = _CCF

        def put_item(self, **kw):
            item = kw.get("Item", {})
            aid = item.get("action_id")
            cond = kw.get("ConditionExpression", "")
            if "attribute_not_exists" in str(cond) and aid in self._store:
                raise _CCF(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
                    "PutItem",
                )
            if aid is not None:
                self._store[aid] = item
            return {}

        def get_item(self, **kw):
            aid = kw.get("Key", {}).get("action_id")
            item = self._store.get(aid)
            return {"Item": item} if item else {}

    return _FT()


def _approve_event(finding_id: str, body: dict, claims: dict,
                   token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return {
        "httpMethod": "POST",
        "resource": "/findings/{finding_id}/approve",
        "pathParameters": {"finding_id": finding_id},
        "queryStringParameters": {},
        "headers": headers,
        "requestContext": {"authorizer": {"claims": claims}},
        "body": json.dumps(body),
    }


def _decline_event(finding_id: str, body: dict | None, claims: dict) -> dict:
    return {
        "httpMethod": "POST",
        "resource": "/findings/{finding_id}/decline",
        "pathParameters": {"finding_id": finding_id},
        "queryStringParameters": {},
        "headers": {},
        "requestContext": {"authorizer": {"claims": claims}},
        "body": json.dumps(body) if body is not None else None,
    }


def _decode(resp: dict) -> dict:
    return json.loads(resp["body"])


# ---------------------------------------------------------------------------
# C2 — _is_pop_finding route check: approve path
# ---------------------------------------------------------------------------

class TestIsPopFindingRouteApprove:
    """C2 (G3F2.4): mixed-scope Findings must not be treated as population-scoped.

    ``_is_pop_finding = set(_pop_scope) == {"population_id"}`` rejects any scope that
    carries additional keys (fleet_id, vin, …).  Without this exact check, a Finding
    whose scope is ``{fleet_id: "x", population_id: "y"}`` would be treated as a
    population Finding and an engineer could approve it, bypassing the fleet-operator
    gate.

    Mutation for C2 approve (:1130):
        Revert to ``bool(population_id) and not vin``.
        A scope ``{fleet_id, population_id}`` has ``population_id`` truthy and no vin key,
        so ``_is_pop_finding`` becomes True; the engineer passes the group check and gets 200.
        The tests here assert 403 or 400, so they FAIL.
    """

    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch):
        """Block DDB/dispatch unless the test patches them."""
        monkeypatch.setattr(h, "_dispatch_action",
                            lambda action, claims: MagicMock(status="executed", target_ref=None),
                            raising=False)
        monkeypatch.setattr(h, "_record_dispatch_result",
                            lambda action, result: action,
                            raising=False)
        fake_res = MagicMock()
        fake_res.Table.return_value = _fake_table()
        monkeypatch.setattr(h, "_get_ddb", lambda: fake_res, raising=False)
        monkeypatch.setattr(h, "_ddb", fake_res, raising=False)

    def _eng_approve(self, finding: dict, action_kind: str = "record_engineering_decision",
                     target: str = "adp") -> dict:
        """Approve event sent by an engineer."""
        body = {
            "action_kind": action_kind,
            "target_system": target,
            "parameters": {
                "investigation_id": _INVESTIGATION_ID,
                "pattern_id": _PATTERN_ID,
                "option_id": _OPTION_ID,
                "notes": "",
            },
        }
        event = _approve_event(finding["finding_id"], body, _ENGINEER_CLAIMS)
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            return h.handler(event, None)

    def test_fleet_pop_scope_engineer_approve_is_403_or_400(self):
        """Engineer approves on scope {fleet_id, population_id} → 403 or 400, nothing written.

        Current code: set(scope) != {"population_id"} → _is_pop_finding=False →
        _check_approve_scope(engineer) → None → 403.

        Mutation (C2 approve): revert _is_pop_finding to bool(population_id) and not vin.
        {fleet_id, population_id} has population_id truthy and no vin → _is_pop_finding=True →
        engineer passes group check → eventually 200 → FAILS.
        """
        finding = _mixed_fleet_pop_finding()
        resp = self._eng_approve(finding)
        assert resp["statusCode"] in (403, 400), (
            f"Engineer on scope {{fleet_id, population_id}} must get 403 or 400; "
            f"got {resp['statusCode']}: {_decode(resp)}. "
            "Mutation (C2): revert to bool(population_id) and not vin → 200 → FAILS."
        )

    def test_vin_pop_scope_engineer_approve_is_403_or_400(self):
        """Engineer approves on scope {vin, population_id} → 403 or 400.

        Mutation (C2 approve): revert _is_pop_finding formula.
        {vin, population_id} has no standalone vin when old formula uses _pop_scope.get("vin").
        Actually both keys are present; old formula: bool(population_id) and not vin → False
        since vin IS present.  So this scope already gets 403 under the old formula too.
        This test is a consistency guard ensuring the exact-key check also refuses it.
        """
        finding = _mixed_vin_pop_finding()
        resp = self._eng_approve(finding)
        assert resp["statusCode"] in (403, 400), (
            f"Engineer on scope {{vin, population_id}} must get 403 or 400; "
            f"got {resp['statusCode']}: {_decode(resp)}. "
        )

    def test_pop_only_scope_engineer_approve_200_control(self):
        """Engineer approves on scope {population_id} (pure pop) → 200. Control.

        This confirms the correct code does NOT over-reject valid population Findings.
        """
        finding = _pop_investigation_finding()
        resp = self._eng_approve(finding)
        assert resp["statusCode"] == 200, (
            f"Engineer must approve a pure population-scoped Finding; "
            f"got {resp['statusCode']}: {_decode(resp)}."
        )

    def test_fleet_pop_scope_nothing_written(self):
        """Engineer on scope {fleet_id, population_id} → no Action written to DDB.

        The 403 fires before any DDB put_item.

        Mutation (C2 approve): revert formula → engineer passes → Action written → FAILS.
        """
        finding = _mixed_fleet_pop_finding()
        fake_res = MagicMock()
        ft = _fake_table()
        fake_res.Table.return_value = ft
        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": _INVESTIGATION_ID,
                    "pattern_id": _PATTERN_ID,
                    "option_id": _OPTION_ID,
                    "notes": "",
                },
            },
            claims=_ENGINEER_CLAIMS,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res), \
             patch.object(h, "_ddb", fake_res):
            resp = h.handler(event, None)
        assert resp["statusCode"] in (403, 400), (
            f"Expected 403 or 400; got {resp['statusCode']}."
        )
        assert ft._store == {}, (
            f"No Action must be written on 403; got {ft._store}. "
            "Mutation: approve proceeds → put_item fired → store non-empty → FAILS."
        )


# ---------------------------------------------------------------------------
# C2 — _is_pop_finding route check: decline path
# ---------------------------------------------------------------------------

class TestIsPopFindingRouteDecline:
    """C2 (G3F2.4): same mixed-scope test on the decline route.

    Mutation for C2 decline (:1467):
        Revert to ``bool(population_id) and not vin``.
        {fleet_id, population_id}: population_id truthy, no vin → _is_pop_finding=True →
        engineer passes group check → 200.  Tests here assert 403, so they FAIL.
    """

    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch):
        """Block DDB unless the test patches it."""
        fake_res = MagicMock()
        fake_res.Table.return_value = _fake_table()
        monkeypatch.setattr(h, "_get_ddb", lambda: fake_res, raising=False)
        monkeypatch.setattr(h, "_ddb", fake_res, raising=False)

    def _eng_decline(self, finding: dict) -> dict:
        """Decline event sent by an engineer."""
        # The decline route only accepts {"reason": "..."} — nothing else.
        event = _decline_event(finding["finding_id"], {"reason": "test"}, _ENGINEER_CLAIMS)
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            return h.handler(event, None)

    def test_fleet_pop_scope_engineer_decline_is_403(self):
        """Engineer declines on scope {fleet_id, population_id} → 403.

        Current code: _is_pop_finding=False → _check_approve_scope(engineer) → None → 403.

        Mutation (C2 decline): revert formula → _is_pop_finding=True for {fleet_id, population_id}
        (population_id truthy, no vin) → engineer passes group check → eventually 200 → FAILS.
        """
        finding = _mixed_fleet_pop_finding()
        resp = self._eng_decline(finding)
        assert resp["statusCode"] == 403, (
            f"Engineer on scope {{fleet_id, population_id}} must get 403 on decline; "
            f"got {resp['statusCode']}: {_decode(resp)}. "
            "Mutation (C2 decline): revert formula → 200 → FAILS."
        )

    def test_vin_pop_scope_engineer_decline_is_403(self):
        """Engineer declines on scope {vin, population_id} → 403."""
        finding = _mixed_vin_pop_finding()
        resp = self._eng_decline(finding)
        assert resp["statusCode"] == 403, (
            f"Engineer on scope {{vin, population_id}} must get 403 on decline; "
            f"got {resp['statusCode']}: {_decode(resp)}."
        )

    def test_pop_only_scope_engineer_decline_200_control(self):
        """Engineer declines on scope {population_id} (pure pop) → 200. Control.

        Confirms the correct code does NOT over-reject valid population Findings.
        """
        finding = _pop_investigation_finding()
        fake_res = MagicMock()
        ft = _fake_table()
        fake_res.Table.return_value = ft
        event = _decline_event(finding["finding_id"], {"reason": "test"}, _ENGINEER_CLAIMS)
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res), \
             patch.object(h, "_ddb", fake_res):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            f"Engineer must decline a pure population-scoped Finding; "
            f"got {resp['statusCode']}: {_decode(resp)}."
        )

    def test_fleet_pop_scope_nothing_recorded(self):
        """Engineer on scope {fleet_id, population_id} → no decline Action written to DDB.

        Mutation (C2 decline): revert formula → engineer passes → Action written → FAILS.
        """
        finding = _mixed_fleet_pop_finding()
        fake_res = MagicMock()
        ft = _fake_table()
        fake_res.Table.return_value = ft
        event = _decline_event(finding["finding_id"], {"reason": "test"}, _ENGINEER_CLAIMS)
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res), \
             patch.object(h, "_ddb", fake_res):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            f"Expected 403; got {resp['statusCode']}."
        )
        assert ft._store == {}, (
            f"No Action must be written on 403; got {ft._store}. "
            "Mutation (C2): formula reverted → decline proceeds → written → FAILS."
        )


# ---------------------------------------------------------------------------
# C1 (route level) — approve route → DMS body: line_no is int
# ---------------------------------------------------------------------------

class TestApproveRouteDmsBodyLineNoInt:
    """C1 (G3F2.4): confirm that the approve route sends line_no as an int through to DMS.

    The executor-level test (TestBookFleetServiceLineNo in test_stage_d_kinds.py) already
    asserts type(line_no) is int.  This test exercises the full path from lambda_handler
    through _dispatch_action → DmsExecutor._dispatch_fleet_service → DMS HTTP call so that
    a regression at any layer (route, executor, or body builder) is caught here.

    Mutation: revert ``"line_no": idx + 1`` to ``"line_no": str(idx + 1)`` in
    agents/executors/dms.py:_fleet_service_body.
    Under the mutant sent_body["lines"][0]["line_no"] is the string "1" (not int 1).
    type(line_no) is int → False → FAILS.
    """

    @rsps.activate
    def test_book_fleet_service_approve_sends_int_line_no(self):
        """lambda_handler → DmsExecutor → DMS body has line_no as int.

        The finding has a single need (LAB-BRK-42).  The operator approves; we intercept
        the DMS POST and inspect the body.  The Authorization header supplies the JWT so
        DmsExecutor's JWT guard passes.
        load_retry_policy is patched to avoid SSM (base.py reads SSM for the retry config;
        patching it keeps the test hermetic without affecting behaviour since the default
        RetryPolicy is used anyway when SSM is unreachable).

        Mutation: send line_no as str → type(lines[0]["line_no"]) is int → False → FAILS.
        """
        from agents.executors.base import RetryPolicy

        rsps.add(
            rsps.POST,
            _DMS_FLEET_RO_URL,
            json={"ro_id": "RO-g3f24-line-no-001"},
            status=201,
        )

        finding = _fleet_health_finding_with_needs([
            {"labor_op": "LAB-BRK-42", "need_id": "need-g3f24-001"},
        ])
        body = {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {
                "dealer_id": _DEALER_ID,
                "urgency": "this_week",
            },
        }
        # Authorization header → _bearer_token(event) → dispatch_claims["token"]
        event = _approve_event(
            finding["finding_id"],
            body,
            _OPERATOR_CLAIMS,
            token="fake-jwt-for-dms-g3f24",
        )

        fake_res = MagicMock()
        ft = _fake_table()
        fake_res.Table.return_value = ft

        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res), \
             patch.object(h, "_ddb", fake_res), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: action), \
             patch("agents.executors.dms.load_retry_policy",
                   return_value=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0)), \
             patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            resp = h.handler(event, None)

        assert resp["statusCode"] in (200, 201, 202), (
            f"book_fleet_service must approve; got {resp['statusCode']}: {_decode(resp)}. "
        )
        assert len(rsps.calls) == 1, (
            f"One DMS POST must have been fired; got {len(rsps.calls)}."
        )
        sent = json.loads(rsps.calls[0].request.body)
        lines = sent.get("lines", [])
        assert lines, "DMS body must have at least one line."
        line_no = lines[0].get("line_no")
        assert type(line_no) is int, (  # noqa: E721 — exact type, not isinstance
            f"line_no must be int (DMS stores NUMBER); got {type(line_no).__name__!r} ({line_no!r}). "
            "Mutation: revert to str(idx+1) → type is str → FAILS."
        )
        assert line_no == 1, (
            f"First line must carry line_no=1; got {line_no!r}. "
            "Mutation: omit line_no → None → FAILS."
        )
