"""Red-test skeletons: decline route and override detection — spec D8.

Expected interface (Group 2 adds to lambdas/api-avx-findings/handler.py):

    POST /findings/{finding_id}/decline
        Request body (optional): {"reason": "<string>"}
        Authorization: same fleet check as approve (_check_approve_scope)
        Only for FI queue items (Findings with recommended_action); 400 otherwise.
        Requires decided_by.kind: operator (403 otherwise).
        Response 200 (first decline):
            {
              "action_id": "<deterministic, derived from finding_id alone>",
              "status": "declined",
              "decision": "declined",
              "decided_by": {"kind": "operator", "id": "<token.sub>"},
              "parameters": {"decline_reason": "<from body, or empty string>"},
              "audit_trail": [],   ← creation is not a transition
              ...
            }
        Idempotent: same finding already declined → 200 with the same action_id.
        409 if the finding was already approved (already_decided).

    POST /findings/{finding_id}/approve with parameters != recommended_action.parameters
        → "decision": "overridden"  (server computes, client does not declare)

    POST /findings/{finding_id}/approve with parameters == recommended_action.parameters
        → "decision": "approved"

    POST /findings/{finding_id}/approve on a Finding with NO recommended_action (a non-FI
    approve) → "decided_by" is written and "decision" is ABSENT (contract § 1.2).

    Body fields that look like approver/decided_by are IGNORED — decided_by.id
    always comes from the verified token's sub (D6).

Tests are RED until:
  - decline route is added to handler.py (currently returns 404/405)
  - "decision" is set on FI approve responses and absent on non-FI ones
  - override detection logic is implemented
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ── sys.path setup (same pattern as test_handler.py) ─────────────────────────
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h  # noqa: E402  (handler.py exists; fails only if route is absent)

from agents.executors.base import ExecuteResult  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures / helpers (mirror test_handler.py style)
# ---------------------------------------------------------------------------

# W8: custom:fleetIds is sent as a CSV string, never a JSON array.
def _claims(sub="op-sub-mrdn-01", groups=None, fleet_ids=None):
    c: dict = {"sub": sub}
    c["cognito:groups"] = json.dumps(groups) if groups else "fleet-operator"
    if fleet_ids is not None:
        c["custom:fleetIds"] = (
            fleet_ids if isinstance(fleet_ids, str) else ",".join(fleet_ids)
        )
    return c


def _event(method, resource, path_params=None, claims=None, body=None) -> dict:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params or {},
        "queryStringParameters": {},
        "requestContext": {"authorizer": {"claims": claims or _claims()}},
        "body": json.dumps(body) if body else None,
    }


def _decode_body(resp: dict) -> dict:
    return json.loads(resp["body"])


# A fleet-scoped Finding with a recommended_action for override detection.
_FLEET_FINDING = {
    "finding_id": "FIND-decline-00000000-0000-0000-0000-000000000001",
    "finding_key": "d" * 64,
    "agent_id": "agent-2-vehicle-health",
    "agent_version": "1.0.0",
    "scope": {"fleet_id": "fleet-mrdn-01"},
    "fleet_id": "fleet-mrdn-01",
    "severity": "soon",
    "computed_at": "2026-10-02T02:00:00Z",
    "expires_at": "2099-10-09T02:00:00Z",
    "confidence": "high",
    "finding_kind": "health.tires",
    "headline": "Tire pressure low",
    "detail": "Three tires below threshold",
    "evidence": [],
    "inputs_hash": "e" * 64,
    "tokens_used": 0,
    "cost_usd": 0.0,
    "audit_trail": [],
    # recommended_action with specific parameters for override detection:
    "recommended_action": {
        "action_kind": "acknowledge_only",
        "target_system": "none",
        "parameters": {"priority": "normal"},
    },
}


# ---------------------------------------------------------------------------
# VIN-scoped FI finding for fleet-driver standing tests
# ---------------------------------------------------------------------------

# A VIN-scoped FI queue item.  fleet-driver with a trusted custom:vehicleId
# (AVX_VEHICLE_ID_CLAIM_TRUSTED=true) and matching vehicle_id passes
# _check_approve_scope as 'user', but the FI routes require 'operator'.
_VIN_FINDING_FI = {
    "finding_id": "FIND-decline-00000000-0000-0000-0000-000000000002",
    "finding_key": "e" * 64,
    "agent_id": "agent-2-vehicle-health",
    "agent_version": "1.0.0",
    "scope": {"vin": "VIN-MRDN-0015"},          # VIN-scoped, not fleet-scoped
    "fleet_id": "fleet-mrdn-01",                # projected fleet (vehicle's fleet)
    "vehicle_id": "MRDN0000000000015",              # projected vehicle_id for fleet-driver standing
    "severity": "soon",
    "computed_at": "2026-10-02T02:00:00Z",
    "expires_at": "2099-10-09T02:00:00Z",
    "confidence": "high",
    "finding_kind": "health.tires",
    "headline": "Tire pressure low",
    "detail": "Three tires below threshold",
    "evidence": [],
    "inputs_hash": "e" * 64,
    "tokens_used": 0,
    "cost_usd": 0.0,
    "audit_trail": [],
    "recommended_action": {                     # FI queue item
        "action_kind": "acknowledge_only",
        "target_system": "none",
        "parameters": {"priority": "normal"},
    },
}


# ---------------------------------------------------------------------------
# Stateful DynamoDB table fake for idempotency tests (Critical 2 / Warning)
# ---------------------------------------------------------------------------

def _make_fake_actions_table():
    """Return a stateful DDB table fake keyed by action_id.

    put_item honours ``ConditionExpression="attribute_not_exists(action_id)"``:
    raises ConditionalCheckFailedException only when the action_id was already
    written.  get_item returns the stored item for the given key.

    meta.client.exceptions.ConditionalCheckFailedException is a real ClientError
    subclass so the handler's ``except table.meta.client.exceptions...`` clause
    resolves correctly (a bare MagicMock is not an exception class and raises
    TypeError during exception handling).
    """
    _CCF = type("ConditionalCheckFailedException", (ClientError,), {})

    class _FakeTable:
        def __init__(self):
            self._store: dict = {}
            # Must be a real exception class so ``except <attr>:`` resolves.
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

    return _FakeTable()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Stop dispatch and DDB calls so tests exercise only route logic."""
    def _fake_dispatch(action, claims):
        return ExecuteResult.success(target_ref=None)

    monkeypatch.setattr(h, "_dispatch_action", _fake_dispatch, raising=False)
    monkeypatch.setattr(h, "_record_dispatch_result",
                        lambda action, result: action, raising=False)

    # The deployed staging value (decisions.md, D6 evidence). Without it every
    # fleet-operator below would be refused standing by a correct implementation.
    monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")

    # Review Cycle 3: no test may reach real AWS. A test that needs a Finding or a
    # table patches these itself (an inner patch.object overrides the defaults).
    def _no_finding_loader(*_a, **_k):
        raise AssertionError("test did not patch _load_finding_by_id")

    fake_resource = MagicMock()
    fake_resource.Table.return_value = _make_fake_actions_table()
    monkeypatch.setattr(h, "_load_finding_by_id", _no_finding_loader, raising=False)
    monkeypatch.setattr(h, "_get_ddb", lambda: fake_resource, raising=False)
    monkeypatch.setattr(h, "_ddb", fake_resource, raising=False)


# ---------------------------------------------------------------------------
# POST /findings/{finding_id}/decline — basic behaviour
# ---------------------------------------------------------------------------

class TestDeclineRoute:
    """The decline route records decision=declined with decided_by from the token."""

    def _decline_event(self, finding_id, body=None, claims=None):
        return _event(
            method="POST",
            resource="/findings/{finding_id}/decline",
            path_params={"finding_id": finding_id},
            claims=claims or _claims(fleet_ids="fleet-mrdn-01"),
            body=body,
        )

    def test_decline_returns_200(self):
        """POST /findings/{finding_id}/decline returns 200 for an authorised caller."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._decline_event(_FLEET_FINDING["finding_id"]),
                None,
            )

        # RED: decline route does not exist yet; handler returns 404 or 405.
        assert resp["statusCode"] == 200, (
            f"Decline route not implemented; got {resp['statusCode']}: "
            f"{resp.get('body', '')[:200]}"
        )

    def test_decline_sets_status_declined(self):
        """The response body carries status=declined."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._decline_event(_FLEET_FINDING["finding_id"]),
                None,
            )

        # RED: route absent.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body.get("status") == "declined"

    def test_decline_sets_decision_declined(self):
        """The response body carries decision=declined."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._decline_event(_FLEET_FINDING["finding_id"]),
                None,
            )

        # RED: route absent; decision field not set.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body.get("decision") == "declined", (
            f"Expected decision='declined', got {body.get('decision')!r}"
        )

    def test_decline_decided_by_id_equals_token_sub(self):
        """decided_by.id in the response equals the token's sub (D6)."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}
        caller_sub = "op-sub-mrdn-decline-01"

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._decline_event(
                    _FLEET_FINDING["finding_id"],
                    claims=_claims(sub=caller_sub, fleet_ids="fleet-mrdn-01"),
                ),
                None,
            )

        # RED: route absent.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        decided_by = body.get("decided_by", {})
        assert decided_by.get("id") == caller_sub, (
            f"decided_by.id must be the token's sub {caller_sub!r}, "
            f"got {decided_by.get('id')!r}"
        )
        # Security review Cycle 1 W2: and on the stored row.
        stored = [c.kwargs["Item"] for c in mock_table.put_item.call_args_list
                  if "Item" in c.kwargs]
        assert stored and all(i.get("decided_by", {}).get("id") == caller_sub for i in stored)

    def test_decline_body_decided_by_is_ignored_in_storage(self):
        """Security review Cycle 1 W2: a decided_by in the decline body never reaches the
        stored row or the response."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}
        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._decline_event(
                    _FLEET_FINDING["finding_id"],
                    body={"reason": "x",
                          "decided_by": {"kind": "operator", "id": "attacker-sub"}},
                    claims=_claims(sub="real-op-sub", fleet_ids="fleet-mrdn-01"),
                ),
                None,
            )
        # additionalProperties: false on the decline body may make this a 400; either
        # way the attacker id is never stored or returned.
        stored = [c.kwargs["Item"] for c in mock_table.put_item.call_args_list
                  if "Item" in c.kwargs]
        assert "attacker-sub" not in json.dumps(stored, default=str)
        assert "attacker-sub" not in resp.get("body", "")
        assert resp["statusCode"] in (200, 400)
        if resp["statusCode"] == 200:
            assert stored and stored[-1]["decided_by"]["id"] == "real-op-sub"

    def test_decline_reason_over_500_characters_is_400(self):
        """Security review Cycle 1 S1: the reason is bounded (OpenAPI maxLength 500,
        contract § 5), and nothing is written."""
        mock_table = MagicMock()
        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._decline_event(_FLEET_FINDING["finding_id"], body={"reason": "x" * 501}),
                None,
            )
        assert resp["statusCode"] == 400
        mock_table.put_item.assert_not_called()

    def test_decline_scope_check_denies_wrong_fleet(self):
        """Fleet check: a fleet-B operator cannot decline a fleet-A Finding."""
        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING):
            resp2 = h.handler(
                self._decline_event(
                    _FLEET_FINDING["finding_id"],
                    claims=_claims(fleet_ids="fleet-mrdn-WRONG"),
                ),
                None,
            )
        # RED: route absent → 404/405, not 403.
        assert resp2["statusCode"] == 403, (
            "Fleet-B operator must not decline a Fleet-A Finding"
        )

    def test_decline_reason_from_body_is_recorded(self):
        """W10: the reason is in parameters.decline_reason; audit_trail is empty on creation."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._decline_event(
                    _FLEET_FINDING["finding_id"],
                    body={"reason": "budget constraints"},
                ),
                None,
            )

        # RED: route absent.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        # W10: reason is in parameters.decline_reason, not a top-level key (contract § 5)
        params = body.get("parameters", {})
        assert params.get("decline_reason") == "budget constraints", (
            f"Expected parameters.decline_reason='budget constraints', got params={params!r}"
        )
        # W10: audit_trail is empty [] on a freshly created declined action (§ 5:
        # "creation is not a transition")
        assert body.get("audit_trail") == [], (
            f"Expected empty audit_trail on decline creation, got {body.get('audit_trail')!r}"
        )

    # ── New tests for W10 ────────────────────────────────────────────────────

    def test_decline_refused_without_recommended_action(self):
        """W10: decline on a Finding without recommended_action → 400 (contract § 5).

        The decline route is only for FI queue items.
        """
        finding_no_rec = {k: v for k, v in _FLEET_FINDING.items()
                          if k != "recommended_action"}

        with patch.object(h, "_load_finding_by_id", return_value=finding_no_rec):
            resp = h.handler(
                self._decline_event(finding_no_rec["finding_id"]),
                None,
            )

        # RED: route absent → 404/405, not 400.
        assert resp["statusCode"] == 400, (
            "Decline on a Finding with no recommended_action must return 400 (§ 5)"
        )

    def test_decline_refused_for_fleet_driver_on_vin_fi_finding(self, monkeypatch):
        """W10: fleet-driver admitted as 'user' (not 'operator') gets 403 on FI decline.

        fleet-driver with a trusted custom:vehicleId on a VIN-scoped FI Finding passes
        _check_approve_scope as 'user', but the FI decline route requires
        decided_by.kind == 'operator' → 403.
        Catches mutation (h): recording 'operator' for a fleet-driver.
        """
        monkeypatch.setenv("AVX_VEHICLE_ID_CLAIM_TRUSTED", "true")
        driver_claims = _claims(sub="driver-sub-fleet-01", groups=["fleet-driver"])
        driver_claims["custom:vehicleId"] = "MRDN0000000000015"  # matches _VIN_FINDING_FI.vehicle_id

        with patch.object(h, "_load_finding_by_id", return_value=_VIN_FINDING_FI):
            resp = h.handler(
                self._decline_event(
                    _VIN_FINDING_FI["finding_id"],
                    claims=driver_claims,
                ),
                None,
            )

        # RED: route absent → 404/405, not 403.
        assert resp["statusCode"] == 403, (
            "fleet-driver is admitted as 'user' by _check_approve_scope but must not "
            "decline an FI queue item — FI requires decided_by.kind == 'operator' (§ 5)"
        )

    def test_decline_refused_for_service_advisor_on_fi_finding(self):
        """W10: service-advisor admitted as 'user' (not 'operator') gets 403 on FI decline.

        service-advisor is always admitted by _check_approve_scope, but the FI decline
        route requires decided_by.kind == 'operator' → 403.
        Catches mutation (h): recording 'operator' for a service advisor.
        """
        sa_claims = _claims(sub="sa-sub-mrdn-01", groups=["service-advisor"])

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING):
            resp = h.handler(
                self._decline_event(
                    _FLEET_FINDING["finding_id"],
                    claims=sa_claims,
                ),
                None,
            )

        # RED: route absent → 404/405, not 403.
        assert resp["statusCode"] == 403, (
            "service-advisor is admitted as 'user' but must not decline an FI queue "
            "item — FI requires decided_by.kind == 'operator' (§ 5)"
        )

    def test_approve_refused_for_service_advisor_on_fi_finding(self):
        """W10: service-advisor gets 403 on FI-item approve (FI requires operator).

        The operator constraint is on the Finding (FI queue item), not the route.
        Catches mutation (h): recording 'operator' for a service advisor on the
        approve path.
        """
        sa_claims = _claims(sub="sa-sub-mrdn-01", groups=["service-advisor"],
                            fleet_ids="fleet-mrdn-01")
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                _event(
                    "POST",
                    "/findings/{finding_id}/approve",
                    path_params={"finding_id": _FLEET_FINDING["finding_id"]},
                    claims=sa_claims,
                    body={
                        "target_system": "none",
                        "action_kind": "acknowledge_only",
                        "parameters": {"priority": "normal"},
                    },
                ),
                None,
            )

        # RED: FI operator check not yet implemented → returns 200; expect 403.
        assert resp["statusCode"] == 403, (
            "service-advisor must not approve an FI queue item — "
            "FI requires decided_by.kind == 'operator' (§ 4)"
        )

    def test_service_advisor_can_approve_non_fi_finding(self):
        """Positive control: service-advisor gets 200 on a non-FI Finding.

        decided_by.kind must be 'user' for a service-advisor, not 'operator'.
        The statusCode check passes today; the decided_by.kind assertion is RED
        until T2.2 sets decided_by on the approve response.
        """
        finding_no_rec = {k: v for k, v in _FLEET_FINDING.items()
                          if k != "recommended_action"}
        sa_claims = _claims(sub="sa-sub-mrdn-01", groups=["service-advisor"],
                            fleet_ids="fleet-mrdn-01")
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=finding_no_rec), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                _event(
                    "POST",
                    "/findings/{finding_id}/approve",
                    path_params={"finding_id": finding_no_rec["finding_id"]},
                    claims=sa_claims,
                    body={"target_system": "none", "action_kind": "acknowledge_only"},
                ),
                None,
            )

        # Scope check passes: service-advisor is always admitted.
        assert resp["statusCode"] == 200, (
            "service-advisor must be able to approve a non-FI Finding"
        )
        body = _decode_body(resp)
        # RED: decided_by not yet set on the approve response (T2.2).
        # decided_by.kind must be 'user' (not 'operator') for a service advisor.
        assert body.get("decided_by", {}).get("kind") == "user", (
            "service-advisor's decided_by.kind must be 'user', not 'operator'; "
            "mutation (h) — recording 'operator' — is caught here"
        )

    def test_multi_fleet_operator_can_decline_owned_fleet(self):
        """W8: an operator with two fleets in a CSV custom:fleetIds can decline an owned fleet's Finding."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._decline_event(
                    _FLEET_FINDING["finding_id"],
                    # W8: two fleets as a single CSV string
                    claims=_claims(fleet_ids="fleet-mrdn-01,fleet-mrdn-02"),
                ),
                None,
            )

        # RED: route absent; also tests CSV parsing (T2.4).
        assert resp["statusCode"] == 200, (
            "An operator with 'fleet-mrdn-01,fleet-mrdn-02' in CSV custom:fleetIds "
            "must be able to decline a fleet-mrdn-01 Finding"
        )


# ---------------------------------------------------------------------------
# Body-supplied approver is ignored (D6)
# ---------------------------------------------------------------------------

class TestBodyApproverIgnored:
    """Any approver/decided_by in the approve body must be silently ignored (D6).

    decided_by.id must always equal the verified token's sub, not a body field.
    """

    def _approve_event(self, finding_id, body, claims=None):
        return _event(
            method="POST",
            resource="/findings/{finding_id}/approve",
            path_params={"finding_id": finding_id},
            claims=claims or _claims(sub="real-op-sub", fleet_ids="fleet-mrdn-01"),
            body=body,
        )

    def test_body_decided_by_is_ignored(self):
        """A decided_by field in the approve body is ignored; token sub is used."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._approve_event(
                    _FLEET_FINDING["finding_id"],
                    body={
                        "target_system": "none",
                        "action_kind": "acknowledge_only",
                        "parameters": {"priority": "normal"},
                        # Attacker tries to override the approver identity:
                        "decided_by": {"kind": "operator", "id": "attacker-sub"},
                    },
                    claims=_claims(sub="real-op-sub", fleet_ids="fleet-mrdn-01"),
                ),
                None,
            )

        # RED: decided_by field not yet implemented on approve response.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        decided_by = body.get("decided_by", {})
        assert decided_by.get("id") == "real-op-sub", (
            f"decided_by.id must be the token sub 'real-op-sub', "
            f"got {decided_by.get('id')!r} — body-supplied approver was used"
        )
        assert decided_by.get("id") != "attacker-sub", (
            "Body-supplied decided_by must be ignored; attacker sub must not appear"
        )
        # Security review Cycle 1 W2: the STORED row is the record of who approved;
        # a response rebuilt from the token proves nothing about it.
        stored = [c.kwargs["Item"] for c in mock_table.put_item.call_args_list
                  if "Item" in c.kwargs]
        assert stored, "the approve must write the decision Action"
        assert all(item.get("decided_by", {}).get("id") == "real-op-sub" for item in stored), (
            f"stored decided_by must be the token sub: {[i.get('decided_by') for i in stored]}"
        )
        assert "attacker-sub" not in json.dumps(stored, default=str)


# ---------------------------------------------------------------------------
# Override detection (D8)
# ---------------------------------------------------------------------------

class TestOverrideDetection:
    """Approve with differing parameters → decision=overridden; same → decision=approved."""

    def _approve_event(self, finding_id, body, claims=None):
        return _event(
            method="POST",
            resource="/findings/{finding_id}/approve",
            path_params={"finding_id": finding_id},
            claims=claims or _claims(sub="op-sub-mrdn-01", fleet_ids="fleet-mrdn-01"),
            body=body,
        )

    def test_approve_with_different_params_records_overridden(self):
        """Parameters differ from recommended_action.parameters → decision=overridden."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        # _FLEET_FINDING.recommended_action.parameters == {"priority": "normal"}
        # We approve with different parameters:
        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._approve_event(
                    _FLEET_FINDING["finding_id"],
                    body={
                        "target_system": "none",
                        "action_kind": "acknowledge_only",
                        "parameters": {"priority": "urgent"},  # differs from "normal"
                    },
                ),
                None,
            )

        # RED: "decision" field not set on approve response yet.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body.get("decision") == "overridden", (
            f"Parameters differ from recommended; expected decision='overridden', "
            f"got {body.get('decision')!r}"
        )

    def test_approve_with_matching_params_records_approved(self):
        """Parameters match recommended_action.parameters → decision=approved."""
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        # Match the recommended_action.parameters exactly:
        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._approve_event(
                    _FLEET_FINDING["finding_id"],
                    body={
                        "target_system": "none",
                        "action_kind": "acknowledge_only",
                        "parameters": {"priority": "normal"},  # exact match
                    },
                ),
                None,
            )

        # RED: "decision" field not set.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body.get("decision") == "approved", (
            f"Parameters match recommended; expected decision='approved', "
            f"got {body.get('decision')!r}"
        )

    def test_approve_without_recommended_action_omits_decision(self):
        """A non-FI approve (no recommended_action) writes decided_by and NO decision
        (contract § 1.2, T2.2 Accept 7a). Writing decision here would hide an owner's
        approve from iOS and make it immutable (review Cycle 3 Critical)."""
        finding_no_rec = {**_FLEET_FINDING}
        del finding_no_rec["recommended_action"]

        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=finding_no_rec), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                self._approve_event(
                    finding_no_rec["finding_id"],
                    body={"target_system": "none", "action_kind": "acknowledge_only"},
                ),
                None,
            )

        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert "decision" not in body, (
            f"a non-FI approve must not carry decision, got {body.get('decision')!r}"
        )
        assert body.get("decided_by", {}).get("kind") in ("operator", "user")


# ---------------------------------------------------------------------------
# Decline idempotency — one decision per Finding (contract §§ 4–5)
# ---------------------------------------------------------------------------

class TestDeclineIdempotency:
    """One decision per Finding: a repeat returns it; a different decision gets 409."""

    def _decline_event(self, finding_id, body=None, claims=None):
        return _event(
            method="POST",
            resource="/findings/{finding_id}/decline",
            path_params={"finding_id": finding_id},
            claims=claims or _claims(fleet_ids="fleet-mrdn-01"),
            body=body,
        )

    def _approve_event(self, finding_id, body, claims=None):
        return _event(
            method="POST",
            resource="/findings/{finding_id}/approve",
            path_params={"finding_id": finding_id},
            claims=claims or _claims(sub="op-sub-mrdn-01", fleet_ids="fleet-mrdn-01"),
            body=body,
        )

    def test_repeated_decline_returns_same_action_id(self):
        """W10: a second decline (by a different operator) returns 200 with the same action_id.

        The action_id is derived from the finding_id alone (contract § 4): both
        operators derive the same id, the second put raises ConditionalCheckFailed,
        and the handler reads back the existing row.

        A per-caller or per-sub derivation produces a different id for the second
        operator → no CCF → a new Action row is written → the two action_ids differ
        → test fails → mutation (f) caught.
        """
        import re

        fake_table = _make_fake_actions_table()
        op_a = _claims(sub="op-sub-mrdn-01", fleet_ids="fleet-mrdn-01")
        op_b = _claims(sub="op-sub-mrdn-02", fleet_ids="fleet-mrdn-01")  # different sub, same fleet

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = fake_table
            # Operator A declines first: put succeeds, action stored by action_id.
            resp1 = h.handler(
                self._decline_event(_FLEET_FINDING["finding_id"], claims=op_a),
                None,
            )
            # Operator B declines again: same action_id derived from finding_id → CCF
            # → handler reads existing row → 200 with the same action_id.
            resp2 = h.handler(
                self._decline_event(_FLEET_FINDING["finding_id"], claims=op_b),
                None,
            )

        # RED: route absent → 404/405.
        assert resp1["statusCode"] == 200, (
            "First decline must return 200 (§ 5)"
        )
        assert resp2["statusCode"] == 200, (
            "Repeated decline (ConditionalCheckFailed) must return 200 with the "
            "existing action (contract § 5)"
        )
        body1 = _decode_body(resp1)
        body2 = _decode_body(resp2)
        assert body1.get("action_id") == body2.get("action_id"), (
            "Both declines must share the same action_id (derived from finding_id "
            "alone, contract § 4); a per-caller derivation would produce two "
            "different ids"
        )
        # Format: ^AVX-ACT-[0-9a-f-]{36}$
        assert re.match(r"^AVX-ACT-[0-9a-f-]{36}$",
                        body1.get("action_id", "")), (
            "action_id must match ^AVX-ACT-[0-9a-f-]{36}$"
        )

    def test_decline_then_approve_is_already_decided(self):
        """W10: approving after declining returns 409 already_decided (contract §§ 4, 5).

        Operator A declines; Operator B (different sub, same fleet) tries to approve.
        Both derive the same action_id from the finding_id alone (§ 4), so the
        approve's put raises ConditionalCheckFailed → handler reads the 'declined'
        row → 409 already_decided.

        A per-caller derivation produces action_id_B ≠ action_id_A → no CCF →
        approve returns 200 → test fails → mutation (f) caught.
        """
        fake_table = _make_fake_actions_table()
        op_a = _claims(sub="op-sub-mrdn-01", fleet_ids="fleet-mrdn-01")
        op_b = _claims(sub="op-sub-mrdn-02", fleet_ids="fleet-mrdn-01")  # different sub, same fleet

        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = fake_table
            # Operator A declines first: put succeeds, 'declined' action stored.
            h.handler(
                self._decline_event(_FLEET_FINDING["finding_id"], claims=op_a),
                None,
            )
            # Operator B tries to approve: same action_id from finding_id → CCF
            # → handler reads existing 'declined' action → 409 already_decided.
            resp = h.handler(
                self._approve_event(
                    _FLEET_FINDING["finding_id"],
                    body={
                        "target_system": "none",
                        "action_kind": "acknowledge_only",
                        "parameters": {"priority": "normal"},
                    },
                    claims=op_b,
                ),
                None,
            )

        # RED: route absent; idempotency not implemented.
        assert resp["statusCode"] == 409, (
            "Approving a Finding that was already declined by a different operator "
            "must return 409 already_decided (contract § 4)"
        )
        body = _decode_body(resp)
        body_lower = json.dumps(body).lower()
        assert "already_decided" in body_lower, (
            f"Response must contain 'already_decided', got {body}"
        )


# ---------------------------------------------------------------------------
# Security review Cycle 1 Critical 1: the trust gate on the fleet claim.
#
# `_hermetic` sets AVX_FLEET_IDS_CLAIM_TRUSTED=true (the deployed staging value), so
# without these tests a route that read `custom:fleetIds` ungated, skipping
# `_check_approve_scope`, passed every test here (review Cycle 4 S3 reproduced it).
# With the flag unset or false the claim gives no fleet standing, so a fleet-operator
# naming the Finding's fleet must be refused and nothing written.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trust", [None, "false", "1", "yes"],
                         ids=["unset", "false", "one", "yes"])
class TestTrustGate:
    def _call(self, monkeypatch, trust, route, body):
        if trust is None:
            monkeypatch.delenv("AVX_FLEET_IDS_CLAIM_TRUSTED", raising=False)
        else:
            monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", trust)
        store = _make_fake_actions_table()
        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = store
            resp = h.handler(
                _event(method="POST", resource=f"/findings/{{finding_id}}/{route}",
                       path_params={"finding_id": _FLEET_FINDING["finding_id"]},
                       claims=_claims(fleet_ids="fleet-mrdn-01"), body=body),
                None,
            )
        return resp, store

    def test_decline_refused_when_the_fleet_claim_is_untrusted(self, monkeypatch, trust):
        resp, store = self._call(monkeypatch, trust, "decline", {"reason": "x"})
        assert resp["statusCode"] == 403
        assert store._store == {}, "nothing may be written"

    # The approve half passes today (approve already routes through
    # `_check_approve_scope`), so it is a green guard in test_handler.py
    # (`TestFIApproveTrustGate`), not a red test in this strict-xfail module.


# ---------------------------------------------------------------------------
# Security review Cycle 1 W1: every 403 is decided before the decision state is read.
# Otherwise a caller without standing learns from a 409 `already_decided` (or a 200
# repeat) that an FI item was decided, and its decision id.
# ---------------------------------------------------------------------------

class TestScopeBeforeDecisionState:
    def _decided_store(self):
        """A store holding the Finding's decline, written by its fleet's operator."""
        store = _make_fake_actions_table()
        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = store
            resp = h.handler(
                _event(method="POST", resource="/findings/{finding_id}/decline",
                       path_params={"finding_id": _FLEET_FINDING["finding_id"]},
                       claims=_claims(sub="op-a", fleet_ids="fleet-mrdn-01"),
                       body={"reason": "handled"}),
                None,
            )
        assert resp["statusCode"] == 200
        return store

    def _call(self, store, route, claims):
        rec = _FLEET_FINDING["recommended_action"]
        body = ({"reason": "again"} if route == "decline" else
                {"target_system": rec["target_system"], "action_kind": rec["action_kind"],
                 "parameters": rec["parameters"]})
        with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = store
            return h.handler(
                _event(method="POST", resource=f"/findings/{{finding_id}}/{route}",
                       path_params={"finding_id": _FLEET_FINDING["finding_id"]},
                       claims=claims, body=body),
                None,
            )

    @pytest.mark.parametrize("route", ["approve", "decline"])
    @pytest.mark.parametrize("who", ["other-fleet-operator", "service-advisor"])
    def test_a_caller_without_standing_gets_403_on_a_decided_item(self, route, who):
        store = self._decided_store()
        claims = (_claims(sub="op-b", fleet_ids="fleet-mrdn-WRONG")
                  if who == "other-fleet-operator"
                  else _claims(sub="sa-1", groups=["service-advisor"]))
        resp = self._call(store, route, claims)
        assert resp["statusCode"] == 403, resp
        body = resp.get("body") or ""
        assert "already_decided" not in body
        assert _PINNED_DECISION_ID not in body, "the decision id must not leak"

    @pytest.mark.parametrize("route", ["approve", "decline"])
    def test_the_403_does_not_depend_on_whether_the_item_was_decided(self, route):
        """Same caller, decided and undecided item: same status and body."""
        claims = _claims(sub="op-b", fleet_ids="fleet-mrdn-WRONG")
        decided = self._call(self._decided_store(), route, claims)
        undecided = self._call(_make_fake_actions_table(), route, claims)
        assert decided["statusCode"] == undecided["statusCode"] == 403
        assert decided.get("body") == undecided.get("body")


# ---------------------------------------------------------------------------
# Group 2 Accept 7b and 9: the tests review Cycle 2 found missing for mutations (d),
# (e), and the VIN rule G4 depends on.
# ---------------------------------------------------------------------------

class TestApproveFIRules:
    def _approve(self, finding, body, claims=None):
        store = _make_fake_actions_table()
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = store
            resp = h.handler(
                _event(method="POST", resource="/findings/{finding_id}/approve",
                       path_params={"finding_id": finding["finding_id"]},
                       claims=claims or _claims(fleet_ids="fleet-mrdn-01"), body=body),
                None,
            )
        return resp, store._store

    @pytest.mark.parametrize("kind,target", [
        ("move_vehicles", "cms"), ("exempt_recall", "cms"),
        ("complete_recall", "cms"),
    ])
    def test_non_executable_kind_is_400_with_nothing_written(self, kind, target):
        """Mutation (d): letting a non-executable kind through writes an Action."""
        finding = {**_FLEET_FINDING, "recommended_action": {
            "action_kind": kind, "target_system": target, "parameters": {}}}
        resp, stored = self._approve(finding, {"action_kind": kind, "target_system": target,
                                               "parameters": {}})
        assert resp["statusCode"] == 400
        assert stored == {}, "nothing may be written for a non-executable kind"

    @pytest.mark.parametrize("location,params", [
        ({"location_id": "loc-a"}, {"location_id": "loc-b"}),
        ({"from_location_id": "loc-a", "to_location_id": "loc-b"},
         {"from_location_id": "loc-a", "to_location_id": "loc-c"}),
    ], ids=["other-location", "other-destination"])
    def test_location_mismatch_is_400(self, location, params):
        """Mutation (e): skipping the location match records the wrong move."""
        finding = {**_FLEET_FINDING, "location": location, "recommended_action": {
            "action_kind": "acknowledge_only", "target_system": "none", "parameters": params}}
        resp, stored = self._approve(finding, {"action_kind": "acknowledge_only",
                                               "target_system": "none", "parameters": params})
        assert resp["statusCode"] == 400
        assert stored == {}

    def test_approve_as_recommended_no_location_repeat_passes(self):
        """W4 (FG2d review): a recommendation whose parameters do not repeat
        location_id can be approved exactly as stated (contract § 4).

        The old check ran Finding → parameters: it required every location key
        on the Finding to appear in the body, so approving this recommendation
        exactly returned 400 and adding location_id made the decision 'overridden'.
        The corrected check runs parameters → Finding: if the body carries no
        location key there is no conflict.
        """
        location = {"location_id": "loc-a"}
        # Recommendation parameters have no location key (schema requires repetition
        # only for moves).
        finding = {**_FLEET_FINDING, "location": location, "recommended_action": {
            "action_kind": "acknowledge_only", "target_system": "none", "parameters": {}}}
        resp, stored = self._approve(finding, {"action_kind": "acknowledge_only",
                                               "target_system": "none", "parameters": {}})
        # Body carries no location key → no conflict → 200 approved.
        assert resp["statusCode"] == 200, (
            f"Approving exactly as recommended (no location key in body) must be 200; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        decision_id = list(stored.keys())[0] if stored else None
        assert decision_id and stored[decision_id].get("decision") == "approved", (
            f"Body parameters match recommendation → decision must be 'approved', "
            f"got {stored.get(decision_id, {}).get('decision')!r}"
        )

    def test_body_location_key_not_in_finding_is_400(self):
        """W4 (FG2d review): a body parameter with a location key the Finding
        does not have is rejected (contract § 4).

        Finding has {location_id: A}; body adds to_location_id: Z.
        Z does not exist in the Finding's location → 400.
        """
        location = {"location_id": "loc-a"}
        finding = {**_FLEET_FINDING, "location": location, "recommended_action": {
            "action_kind": "acknowledge_only", "target_system": "none",
            "parameters": {"location_id": "loc-a"}}}
        body_params = {"location_id": "loc-a", "to_location_id": "loc-z"}
        resp, stored = self._approve(finding, {"action_kind": "acknowledge_only",
                                               "target_system": "none",
                                               "parameters": body_params})
        assert resp["statusCode"] == 400, (
            f"Body carries to_location_id not on the Finding → must be 400; "
            f"got {resp['statusCode']}"
        )
        assert stored == {}, "nothing may be written on a location mismatch"

    def test_matching_location_is_accepted(self):
        location = {"from_location_id": "loc-a", "to_location_id": "loc-b"}
        finding = {**_FLEET_FINDING, "location": location, "recommended_action": {
            "action_kind": "acknowledge_only", "target_system": "none", "parameters": location}}
        resp, _ = self._approve(finding, {"action_kind": "acknowledge_only",
                                          "target_system": "none", "parameters": dict(location)})
        assert resp["statusCode"] == 200

    def test_vin_scoped_fi_item_gets_the_findings_vin(self):
        """Accept 9: G4 compares the service record against a server-set VIN."""
        finding = {**_FLEET_FINDING, "scope": {"vin": "VIN-MRDN-0015"}}
        resp, stored = self._approve(finding, {"action_kind": "acknowledge_only",
                                               "target_system": "none",
                                               "parameters": {"priority": "normal"}})
        assert resp["statusCode"] == 200
        assert stored[_PINNED_DECISION_ID]["parameters"]["vehicle_vin"] == "VIN-MRDN-0015"

    def test_vin_scoped_fi_item_rejects_another_vin(self):
        finding = {**_FLEET_FINDING, "scope": {"vin": "VIN-MRDN-0015"}}
        resp, stored = self._approve(finding, {"action_kind": "acknowledge_only",
                                               "target_system": "none",
                                               "parameters": {"priority": "normal",
                                                              "vehicle_vin": "VIN-OTHER"}})
        assert resp["statusCode"] == 400
        assert stored == {}


# ---------------------------------------------------------------------------
# Review Cycle 3: the decision id is pinned by literal, not just by format, so
# a constant id or a different derivation cannot pass. Contract § 4 formula:
#   "AVX-ACT-" + uuid-shaped(sha256("<finding_id>|fi-decision").hexdigest()[:32])
# ---------------------------------------------------------------------------

_PINNED_DECISION_ID = "AVX-ACT-2e9e1581-571a-8940-5628-be69b348a2bf"


def _stored_actions() -> dict:
    """The rows the `_hermetic` fake actions table holds, keyed by action_id."""
    return h._get_ddb().Table(h._ACTIONS_TABLE)._store


def test_decline_action_id_is_the_pinned_per_finding_decision_id():
    with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING):
        resp = h.handler(
            _event(
                method="POST",
                resource="/findings/{finding_id}/decline",
                path_params={"finding_id": _FLEET_FINDING["finding_id"]},
                claims=_claims(fleet_ids="fleet-mrdn-01"),
                body={},
            ),
            None,
        )
    assert resp["statusCode"] == 200
    assert _decode_body(resp)["action_id"] == _PINNED_DECISION_ID
    # Security review Cycle 1 W2: the id actually written, not only the one returned.
    assert set(_stored_actions()) == {_PINNED_DECISION_ID}


def test_approve_action_id_is_the_same_pinned_decision_id():
    rec = _FLEET_FINDING["recommended_action"]
    with patch.object(h, "_load_finding_by_id", return_value=_FLEET_FINDING):
        resp = h.handler(
            _event(
                method="POST",
                resource="/findings/{finding_id}/approve",
                path_params={"finding_id": _FLEET_FINDING["finding_id"]},
                claims=_claims(fleet_ids="fleet-mrdn-01"),
                body={"target_system": rec["target_system"],
                      "action_kind": rec["action_kind"],
                      "parameters": rec["parameters"]},
            ),
            None,
        )
    assert resp["statusCode"] == 200
    assert _decode_body(resp)["action_id"] == _PINNED_DECISION_ID
    # Security review Cycle 1 W2: the id actually written, not only the one returned.
    assert set(_stored_actions()) == {_PINNED_DECISION_ID}
