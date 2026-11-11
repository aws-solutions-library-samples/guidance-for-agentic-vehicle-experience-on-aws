"""Tests for FG2d review findings — lambdas/api-avx-findings.

Covers:
  Critical 1 — stored Action validates action.schema.json (approve FI + decline).
  Warning 2  — target_system=cms refused for non-FI kinds (W2).
  Warning 3  — operator_required kinds need operator standing on any Finding (W3).
  Warning 4  — location rule: approve-as-recommended passes; extra body key → 400 (W4).
  Warning 5  — second override with different parameters → 409 (W5).
  Warning 7  — FM mutants: FM28c (kind/target mismatch), FM5 (override on raw params),
               FM2/FM3 (fleet_id stored), FM1 (fleet_id stripped from response),
               FM25b/FM26 (decline copies rec kind/target), FM14 (approve-then-decline
               → 409), FM10 (platform-admin on FI item → operator standing).

Each mutant section names the mutation, confirms it is caught, and reverts it.

Run:
    python3 -m pytest lambdas/api-avx-findings/tests/test_fg2d_review.py -v
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest
from botocore.exceptions import ClientError

# ── path setup ────────────────────────────────────────────────────────────────
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h  # noqa: E402
from agents.executors.base import ExecuteResult  # noqa: E402

try:
    import jsonschema  # noqa: F401
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False

# ── action schema (loaded once) ───────────────────────────────────────────────
_ACTION_SCHEMA_PATH = pathlib.Path(_REPO_ROOT) / "agents" / "tier2" / "schemas" / "action.schema.json"
_ACTION_SCHEMA = json.loads(_ACTION_SCHEMA_PATH.read_text())

# ── shared fixtures ───────────────────────────────────────────────────────────

def _claims(sub="op-sub-mrdn-01", groups=None, fleet_ids=None):
    c: dict = {"sub": sub}
    c["cognito:groups"] = (
        json.dumps(groups) if isinstance(groups, list) else (groups or "fleet-operator")
    )
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
        "body": json.dumps(body) if body is not None else None,
    }


def _decode(resp: dict) -> dict:
    return json.loads(resp["body"])


# A fleet-scoped FI queue item (has recommended_action).
_FI_FINDING = {
    "finding_id": "FIND-fa2d0001-0000-0000-0000-000000000000",
    "finding_key": "a" * 64,
    "agent_id": "agent-fi-cost",
    "agent_version": "1.0.0",
    "scope": {"fleet_id": "fleet-mrdn-01"},
    "fleet_id": "fleet-mrdn-01",
    "severity": "soon",
    "computed_at": "2026-10-02T02:00:00Z",
    "expires_at": "2099-10-09T02:00:00Z",
    "confidence": "high",
    "finding_kind": "fleet.cost.anomaly",
    "headline": "Cost anomaly detected",
    "detail": "...",
    "evidence": [
        {
            "kind": "cost_signal",
            "source": "adp-cost",
            "source_ref": "ref-001",
            "value": 4200,
            "computed_at": "2026-10-02T02:00:00Z",
            "provenance": "live",
        }
    ],
    "inputs_hash": "b" * 64,
    "tokens_used": 0,
    "cost_usd": 0.0,
    "audit_trail": [],
    "recommended_action": {
        "action_kind": "acknowledge_only",
        "target_system": "none",
        "parameters": {"priority": "normal"},
    },
}

# A plain Finding without recommended_action.
_PLAIN_FINDING = {k: v for k, v in _FI_FINDING.items() if k != "recommended_action"}
_PLAIN_FINDING = {**_PLAIN_FINDING, "finding_id": "FIND-fa2d0002-0000-0000-0000-000000000000",
                  "agent_id": "agent-2-vehicle-health", "finding_kind": "health.tires"}


# ── stateful DDB table fake ───────────────────────────────────────────────────

def _make_fake_actions_table():
    """Stateful DDB table fake; honours attribute_not_exists condition."""
    _CCF = type("ConditionalCheckFailedException", (ClientError,), {})

    class _FakeTable:
        def __init__(self):
            self._store: dict = {}
            self.meta = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
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


# ── hermetic fixture ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(h, "_dispatch_action",
                        lambda action, claims: ExecuteResult.success(target_ref=None),
                        raising=False)
    monkeypatch.setattr(h, "_record_dispatch_result",
                        lambda action, result: action,
                        raising=False)
    monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
    monkeypatch.setattr(h, "_load_finding_by_id",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("test must patch _load_finding_by_id")),
                        raising=False)
    from unittest.mock import MagicMock
    fake_resource = MagicMock()
    fake_resource.Table.return_value = _make_fake_actions_table()
    monkeypatch.setattr(h, "_get_ddb", lambda: fake_resource, raising=False)
    monkeypatch.setattr(h, "_ddb", fake_resource, raising=False)


# ── helpers ───────────────────────────────────────────────────────────────────

def _approve(finding, body_params=None, claims=None, extra_body=None):
    """Run the approve route; return (response, stored_items_dict)."""
    from unittest.mock import patch
    table = _make_fake_actions_table()
    body = {
        "target_system": finding.get("recommended_action", {}).get("target_system", "none"),
        "action_kind": finding.get("recommended_action", {}).get("action_kind", "acknowledge_only"),
        "parameters": body_params if body_params is not None else {},
    }
    if extra_body:
        body.update(extra_body)
    event = _event(
        "POST",
        "/findings/{finding_id}/approve",
        path_params={"finding_id": finding["finding_id"]},
        claims=claims or _claims(fleet_ids="fleet-mrdn-01"),
        body=body,
    )
    with patch.object(h, "_load_finding_by_id", return_value=finding), \
         patch.object(h, "_get_ddb") as mock_ddb:
        mock_ddb.return_value.Table.return_value = table
        resp = h.handler(event, None)
    return resp, dict(table._store)


def _decline(finding, reason=None, claims=None):
    """Run the decline route; return (response, stored_items_dict)."""
    from unittest.mock import patch
    table = _make_fake_actions_table()
    body = {"reason": reason} if reason is not None else None
    event = _event(
        "POST",
        "/findings/{finding_id}/decline",
        path_params={"finding_id": finding["finding_id"]},
        claims=claims or _claims(fleet_ids="fleet-mrdn-01"),
        body=body,
    )
    with patch.object(h, "_load_finding_by_id", return_value=finding), \
         patch.object(h, "_get_ddb") as mock_ddb:
        mock_ddb.return_value.Table.return_value = table
        resp = h.handler(event, None)
    return resp, dict(table._store)


def _validate_action(item: dict):
    """Validate item against action.schema.json, raising jsonschema.ValidationError on fail."""
    if not _HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")
    import jsonschema
    jsonschema.validate(item, _ACTION_SCHEMA,
                        format_checker=jsonschema.FormatChecker())


# =============================================================================
# Critical 1 — stored Action validates action.schema.json (approve FI + decline)
# =============================================================================

@pytest.mark.skipif(not _HAS_JSONSCHEMA, reason="jsonschema not installed")
class TestStoredActionSchemaValidation:
    """Every stored decision Action validates against action.schema.json.

    Catches FM20: removing 'evidence' from the decline action.
    Catches a parallel mutant on the approve action.

    Mutation verification:
      FM20 — change `finding.get("evidence", [])` to `[]` in the decline path,
             then delete `"evidence": []` from the action dict.  The schema
             validation below fails: 'evidence' is a required property.
      Approve FM20 analogue — remove `"evidence": finding.get("evidence", [])` from
             the approve path.  Same failure.
    """

    def test_approve_fi_stored_action_validates_schema(self):
        """The Action written by the FI approve route validates action.schema.json."""
        resp, stored = _approve(_FI_FINDING,
                                body_params={"priority": "normal"})
        assert resp["statusCode"] == 200, f"approve failed: {resp.get('body', '')[:200]}"
        action_id = _decode(resp).get("action_id")
        assert action_id, "response must carry action_id"
        item = stored.get(action_id)
        assert item is not None, f"action_id {action_id} not in stored items"
        # Strip fleet_id (storage-only; not in schema's additionalProperties).
        item_for_schema = {k: v for k, v in item.items() if k != "fleet_id"}
        _validate_action(item_for_schema)

    def test_decline_stored_action_validates_schema(self):
        """The Action written by the decline route validates action.schema.json.

        FM20: removing 'evidence' (or changing it to be absent) causes
        'evidence is a required property' from the schema validator.
        """
        resp, stored = _decline(_FI_FINDING, reason="handled offline")
        assert resp["statusCode"] == 200, f"decline failed: {resp.get('body', '')[:200]}"
        action_id = _decode(resp).get("action_id")
        assert action_id, "response must carry action_id"
        item = stored.get(action_id)
        assert item is not None, f"action_id {action_id} not in stored items"
        item_for_schema = {k: v for k, v in item.items() if k != "fleet_id"}
        _validate_action(item_for_schema)

    def test_decline_evidence_is_copied_from_finding(self):
        """The decline action's evidence equals the Finding's evidence (not hard-coded []).

        FM20 catches both the absence of evidence AND evidence that is always [],
        because a Finding with non-empty evidence would differ from [].
        """
        resp, stored = _decline(_FI_FINDING)
        assert resp["statusCode"] == 200
        action_id = _decode(resp).get("action_id")
        item = stored.get(action_id, {})
        assert item.get("evidence") == _FI_FINDING["evidence"], (
            f"decline evidence must be copied from the Finding; "
            f"got {item.get('evidence')!r}, expected {_FI_FINDING['evidence']!r}"
        )


# =============================================================================
# Warning 2 — target_system=cms refused for non-FI (executable) kinds
# =============================================================================

class TestTargetSystemCmsRefused:
    """W2 (FG2d review): `cms` is not in the executable target systems.

    Mutation verification:
      W2 mutant — remove `if row.executable` from _VALID_TARGET_SYSTEMS.
      Then `_approve(_PLAIN_FINDING, ...)` with target_system=cms returns 200.
      With the fix it returns 400 (unknown target_system).
    """

    def test_cms_target_on_non_fi_finding_is_400(self):
        """`acknowledge_only` with target_system=cms returns 400.

        Before this fix, `cms` was in _VALID_TARGET_SYSTEMS because non-executable
        FI kinds (ground_vehicle, move_vehicles, …) have target_system=cms.  That
        meant a pre-existing kind could be approved to CmsExecutor, which records
        it `declined` with "CMS cannot carry out this action yet" and lands the
        Action in the caller's by_created_by partition with a target iOS cannot decode.
        """
        resp, stored = _approve(
            _PLAIN_FINDING,
            body_params={},
            extra_body={"target_system": "cms", "action_kind": "acknowledge_only"},
        )
        assert resp["statusCode"] == 400, (
            f"target_system=cms on a non-FI kind must be 400 (unknown target_system); "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored == {}, "nothing may be written"

    def test_non_cms_executable_target_is_accepted(self):
        """Positive control: a valid executable target is still accepted."""
        resp, stored = _approve(_PLAIN_FINDING, body_params={})
        assert resp["statusCode"] == 200, f"none target must still work: {resp.get('body', '')[:200]}"
        assert stored, "Action must be written"


# =============================================================================
# Warning 3 — operator_required kinds need operator standing on any Finding
# =============================================================================

class TestOperatorRequiredKind:
    """W3 (FG2d review): `book_recall_service` is operator_required; a `user`-standing
    caller must be denied even on a Finding without recommended_action.

    Mutation verification:
      W3 mutant — remove the `if _POLICY_ACTION_KINDS_TABLE[action_kind].operator_required`
                  check from _approve_finding.
      Then a service-advisor (standing=user) with book_recall_service on a VIN-scoped
      Finding without recommended_action returns 200.  With the fix it returns 403.
    """

    # A VIN-scoped plain Finding (no recommended_action) with the fleet projected.
    _VIN_PLAIN = {
        "finding_id": "FIND-fa2d0003-0000-0000-0000-000000000000",
        "finding_key": "c" * 64,
        "agent_id": "agent-2-vehicle-health",
        "agent_version": "1.0.0",
        "scope": {"vin": "VIN-TEST-0001"},
        "fleet_id": "fleet-mrdn-01",
        "vehicle_id": "VEH-TEST-0001",
        "severity": "soon",
        "computed_at": "2026-10-02T02:00:00Z",
        "expires_at": "2099-10-09T02:00:00Z",
        "confidence": "high",
        "finding_kind": "health.tires",
        "headline": "Tire pressure low",
        "detail": "...",
        "evidence": [],
        "inputs_hash": "c" * 64,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        # no recommended_action — plain Finding
    }

    def test_service_advisor_denied_on_operator_required_kind(self):
        """service-advisor (standing=user) must not approve book_recall_service.

        `book_recall_service` is operator_required=True (policy table).  The check
        must fire regardless of whether the Finding carries recommended_action.
        """
        sa_claims = _claims(sub="sa-sub-01", groups="service-advisor")
        resp, stored = _approve(
            self._VIN_PLAIN,
            body_params={"dealer_id": "dealer-seattle"},
            extra_body={"target_system": "dms", "action_kind": "book_recall_service"},
            claims=sa_claims,
        )
        assert resp["statusCode"] == 403, (
            f"service-advisor must not approve an operator_required kind; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored == {}, "nothing may be written"

    def test_operator_can_approve_operator_required_kind(self):
        """CHANGED by Fix Group G3-1 F5 (G3-18, review cycle 1 routes C1): the plain-Finding
        booking this test approved is now refused, because a Stage C kind with a server-set
        list takes its parameters only from a matching recommended_action (no body-only
        path). The operator-can-approve control moved to
        test_approve_fix_f5.py::TestPlainFindingRefused::test_control_matching_recommendation_writes.
        What this test still pins: the operator passes the 403 standing check (400, not 403)."""
        op_claims = _claims(sub="op-sub-01", groups="fleet-operator",
                            fleet_ids="fleet-mrdn-01")
        finding = {**self._VIN_PLAIN, "scope": {"vin": "VIN-TEST-0001"}}
        resp, stored = _approve(
            finding,
            body_params={"dealer_id": "dealer-seattle", "vehicle_vin": "VIN-TEST-0001"},
            extra_body={"target_system": "dms", "action_kind": "book_recall_service"},
            claims=op_claims,
        )
        assert resp["statusCode"] == 400, (
            f"plain-Finding booking must be refused (G3-18); "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored == {}

    def test_non_operator_required_kind_allows_user_standing(self):
        """Positive control: acknowledge_only (operator_required=False) accepts user standing."""
        sa_claims = _claims(sub="sa-sub-01", groups="service-advisor")
        resp, _ = _approve(_PLAIN_FINDING, body_params={}, claims=sa_claims)
        assert resp["statusCode"] == 200, (
            f"service-advisor must approve acknowledge_only; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )

    def test_fleet_operator_in_dms_groups_with_vehicle_standing_denied_on_operator_required_kind(self):
        """Critical (FG3c review): fleet-operator with `user` standing via vehicle_id claim is
        denied on an operator_required kind, even though they are in _DMS_BOOKING_GROUPS.

        Scenario:
          - Caller is `fleet-operator` (admitted by _DMS_BOOKING_GROUPS for the dms check).
          - Caller's `custom:vehicleId` matches the Finding's projected vehicle_id
            → `_check_approve_scope` returns "user" (fleet_driver standing).
          - Caller's `custom:fleetIds` names a DIFFERENT fleet → no operator standing.
          - `book_recall_service` is operator_required=True.
          - Expected: 403, nothing written.

        Mutation verification:
          FW3a — delete the two-line `operator_required` check.
          With the check absent this returns 200 (stored dms Action with decided_by.kind=user).
          With the check present it returns 403.

        This is the caller Cycle 7b named as the load-bearing path.
        It is distinct from test_service_advisor_denied_on_operator_required_kind:
        service-advisor is NOT in _DMS_BOOKING_GROUPS, so the DMS check would deny it first —
        that test never reaches the operator_required gate.  This test reaches the gate.
        """
        import os
        from unittest.mock import patch
        # VIN-scoped plain Finding whose projected vehicle_id the caller holds.
        vin_plain_with_vehicle = {
            **self._VIN_PLAIN,
            "vehicle_id": "VEH-FG3C-0001",
        }
        # Caller: fleet-operator (in _DMS_BOOKING_GROUPS) whose custom:vehicleId
        # matches the Finding's vehicle_id → _check_approve_scope returns "user".
        # custom:fleetIds names a DIFFERENT fleet → no operator standing via fleet-manager.
        caller_claims = _claims(
            sub="fo-sub-fg3c-01",
            groups="fleet-operator",
            fleet_ids="fleet-OTHER-not-mrdn",
        )
        caller_claims["custom:vehicleId"] = "VEH-FG3C-0001"

        table = _make_fake_actions_table()
        event = _event(
            "POST",
            "/findings/{finding_id}/approve",
            path_params={"finding_id": vin_plain_with_vehicle["finding_id"]},
            claims=caller_claims,
            body={
                "target_system": "dms",
                "action_kind": "book_recall_service",
                "parameters": {"dealer_id": "dealer-fg3c"},
            },
        )
        with patch.object(h, "_load_finding_by_id", return_value=vin_plain_with_vehicle), \
             patch.object(h, "_get_ddb") as mock_ddb, \
             patch.dict(os.environ, {
                 "AVX_FLEET_IDS_CLAIM_TRUSTED": "true",
                 "AVX_VEHICLE_ID_CLAIM_TRUSTED": "true",
             }):
            mock_ddb.return_value.Table.return_value = table
            resp = h.handler(event, None)

        assert resp["statusCode"] == 403, (
            f"fleet-operator with user standing must not approve an operator_required kind; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}\n"
            f"stored={table._store}"
        )
        assert table._store == {}, (
            f"nothing may be written on operator_required denial; stored={table._store}"
        )


# =============================================================================
# Warning 4 — location rule (corrected direction)
# =============================================================================
# Additional tests beyond those already in test_decline_override.py.

class TestLocationRuleCorrectedDirection:
    """W4 (FG2d review): the location check runs parameters → Finding, not the reverse.

    The two new tests below complement test_approve_as_recommended_no_location_repeat_passes
    and test_body_location_key_not_in_finding_is_400 in test_decline_override.py.

    Mutation verification:
      W4 mutant — revert _fi_location_match to the old direction (Finding → parameters).
      test_approve_as_recommended_passes_move_finding → 400 instead of 200.
      test_approve_as_recommended_passes_single_loc  → 400 instead of 200.
      Both fail, catching the mutant.
    """

    def test_approve_as_recommended_passes_move_finding(self):
        """A move recommendation approved exactly as stated passes.

        The recommendation carries from/to location IDs; the body repeats them
        exactly.  The Finding's location matches → 200.
        """
        location = {"from_location_id": "loc-a", "to_location_id": "loc-b"}
        finding = {**_FI_FINDING, "location": location,
                   "recommended_action": {
                       "action_kind": "acknowledge_only",
                       "target_system": "none",
                       "parameters": dict(location),
                   }}
        resp, stored = _approve(finding, body_params=dict(location))
        assert resp["statusCode"] == 200, (
            f"move recommendation approved as stated must be 200; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        action_id = list(stored.keys())[0] if stored else None
        assert action_id and stored[action_id].get("decision") == "approved"

    def test_approve_with_wrong_from_location_is_400(self):
        """Body from_location_id differs from the Finding's → 400."""
        location = {"from_location_id": "loc-a", "to_location_id": "loc-b"}
        finding = {**_FI_FINDING, "location": location,
                   "recommended_action": {
                       "action_kind": "acknowledge_only",
                       "target_system": "none",
                       "parameters": dict(location),
                   }}
        body_params = {"from_location_id": "loc-WRONG", "to_location_id": "loc-b"}
        resp, stored = _approve(finding, body_params=body_params)
        assert resp["statusCode"] == 400, (
            f"from_location_id mismatch must be 400; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored == {}


# =============================================================================
# Warning 1 (FG3c review) — Finding with no location must reject body location keys
# =============================================================================

class TestLocationNoLocationField:
    """W1 (FG3c review cycle 8): a Finding with no `location` field must return 400
    for any body that carries a location key, and a null body value is also rejected.

    Contract § 4: "A body ID cannot equal a location that is not there."
    Mirrors the VIN check in _dms_parameters: _dms_parameters refuses a VIN key
    when the Finding has none; _fi_location_match must do the same for location keys.

    Previous behaviour (before FG3c fix):
      `_fi_location_match` returned True when `location` was not a dict, so
      a body `{location_id: loc-Z}` on a Finding with no `location` got 200.
      Also, `None == location.get(key)` was True when the Finding lacked the key
      and the body sent `key: null`, letting a null value through silently.

    Mutation verification:
      FW1-no-loc — revert to `if not isinstance(location, dict): return True`.
      test_no_location_finding_refuses_body_location_key → 200 instead of 400.
      The assertion fails, catching the mutant.

      FW1-null-val — revert to `parameters[key] != location.get(key)` (drop the
      `key not in location` check).
      test_null_location_value_is_400 → 200 instead of 400.
      The assertion fails, catching the mutant.
    """

    # FI Finding without a `location` field.
    _FI_NO_LOC = {k: v for k, v in _FI_FINDING.items() if k != "location"}

    def test_no_location_finding_refuses_body_location_key(self):
        """Finding with no `location` field + body with location_id → 400.

        Contract § 4: a body ID cannot equal a location that is not there.
        The previous code treated missing location as "no constraint" → 200.
        """
        assert "location" not in self._FI_NO_LOC, "fixture must not carry a location field"
        resp, stored = _approve(
            self._FI_NO_LOC,
            body_params={"location_id": "loc-Z"},
        )
        assert resp["statusCode"] == 400, (
            f"Finding with no location + body location_id must be 400; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored == {}, "nothing may be written on a location mismatch"

    def test_null_location_value_is_400(self):
        """Body `to_location_id: null` on a Finding with no `to_location_id` → 400.

        Previous behaviour: `None == location.get('to_location_id')` = `None == None`
        was True, so the check passed silently.  After the fix the key-presence test
        fires first: `'to_location_id' not in {'location_id': 'loc-a'}` → False.
        """
        finding = {**_FI_FINDING, "location": {"location_id": "loc-a"}}
        resp, stored = _approve(
            finding,
            body_params={"to_location_id": None},
        )
        assert resp["statusCode"] == 400, (
            f"null to_location_id where Finding has no to_location_id must be 400; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored == {}, "nothing may be written on a location mismatch"

    def test_present_location_key_with_matching_value_still_passes(self):
        """Positive control: body key matches Finding's location → 200 (no regression)."""
        location = {"location_id": "loc-a"}
        finding = {**_FI_FINDING, "location": location,
                   "recommended_action": {
                       "action_kind": "acknowledge_only",
                       "target_system": "none",
                       "parameters": {"location_id": "loc-a"},
                   }}
        resp, stored = _approve(finding, body_params={"location_id": "loc-a"})
        assert resp["statusCode"] == 200, (
            f"matching location key must still produce 200; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored, "Action must be written"


# =============================================================================
# Warning 5 — second override with different parameters → 409
# =============================================================================

class TestDuplicateOverride409:
    """W5 (FG2d review): a second `overridden` decision with different parameters
    must return 409, not 200.

    Contract § 4: "Repeating the same decision returns it (200).  Any other decision
    gets 409 already_decided."  'Same decision' is the word AND the parameters.

    Mutation verification:
      W5 mutant — in the idempotency block change the condition to compare ONLY
                  `existing.get("decision") == decision` (drop the parameter comparison).
      Then approve override A, then approve override B → 200 instead of 409.
      The assertion below fails, catching the mutant.
    """

    def test_second_override_with_different_params_is_409(self):
        """Operator A overrides with priority=urgent; operator B with priority=low → 409."""
        from unittest.mock import patch
        table = _make_fake_actions_table()
        base = {
            "httpMethod": "POST",
            "resource": "/findings/{finding_id}/approve",
            "pathParameters": {"finding_id": _FI_FINDING["finding_id"]},
            "queryStringParameters": {},
        }

        def _do_approve(params, sub):
            event = {
                **base,
                "requestContext": {"authorizer": {"claims": _claims(sub=sub,
                                                                     fleet_ids="fleet-mrdn-01")}},
                "body": json.dumps({
                    "target_system": "none",
                    "action_kind": "acknowledge_only",
                    "parameters": params,
                }),
            }
            with patch.object(h, "_load_finding_by_id", return_value=_FI_FINDING), \
                 patch.object(h, "_get_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value = table
                return h.handler(event, None)

        # First override: priority=urgent
        resp1 = _do_approve({"priority": "urgent"}, "op-sub-A")
        assert resp1["statusCode"] == 200, (
            f"first override must be 200: {resp1.get('body', '')[:200]}"
        )
        assert _decode(resp1).get("decision") == "overridden"

        # Second override with DIFFERENT parameters: priority=low
        resp2 = _do_approve({"priority": "low"}, "op-sub-B")
        assert resp2["statusCode"] == 409, (
            f"second override with different params must be 409 already_decided; "
            f"got {resp2['statusCode']}: {resp2.get('body', '')[:200]}"
        )
        assert _decode(resp2).get("error") == "already_decided", (
            f"error code must be 'already_decided', got {_decode(resp2)!r}"
        )

    def test_same_override_repeated_is_200(self):
        """Positive control: repeating the SAME override returns 200 (idempotent)."""
        from unittest.mock import patch
        table = _make_fake_actions_table()
        base = {
            "httpMethod": "POST",
            "resource": "/findings/{finding_id}/approve",
            "pathParameters": {"finding_id": _FI_FINDING["finding_id"]},
            "queryStringParameters": {},
        }

        def _do_approve(params, sub):
            event = {
                **base,
                "requestContext": {"authorizer": {"claims": _claims(sub=sub,
                                                                     fleet_ids="fleet-mrdn-01")}},
                "body": json.dumps({
                    "target_system": "none",
                    "action_kind": "acknowledge_only",
                    "parameters": params,
                }),
            }
            with patch.object(h, "_load_finding_by_id", return_value=_FI_FINDING), \
                 patch.object(h, "_get_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value = table
                return h.handler(event, None)

        resp1 = _do_approve({"priority": "urgent"}, "op-sub-A")
        assert resp1["statusCode"] == 200

        # Same params by a different caller
        resp2 = _do_approve({"priority": "urgent"}, "op-sub-B")
        assert resp2["statusCode"] == 200, (
            f"repeated identical override must be 200; "
            f"got {resp2['statusCode']}: {resp2.get('body', '')[:200]}"
        )

    def test_repeat_vin_scoped_decision_returns_200(self):
        """FW5b (FG3c review): an identical repeat on a VIN-scoped FI item returns 200.

        On a VIN-scoped Finding the server fills `vehicle_vin` into parameters AFTER
        the override-detection comparison (which uses the raw body).  The idempotency
        comparison must use the already-filled `parameters` variable, so that the
        stored row (with `vehicle_vin`) matches the second request's filled parameters
        and returns 200.

        FW5b mutant — change `not _approve_request_differs(existing, target_system,
        parameters)` to compare against `body.get("parameters", {})` (body as sent,
        without filling).  Then stored {vehicle_vin, ...} != body {} → 409.
        The assertion below fails, catching the mutant.
        """
        from unittest.mock import patch
        # VIN-scoped FI item; rec has no vehicle_vin, server fills it.
        vin_fi = {
            **_FI_FINDING,
            "finding_id": "FIND-fa2d-fw5b-0000-0000-000000000000",
            "scope": {"vin": "VIN-FW5B-0001"},
            "fleet_id": "fleet-mrdn-01",
            "recommended_action": {
                "action_kind": "acknowledge_only",
                "target_system": "none",
                "parameters": {},          # recommendation has no vehicle_vin
            },
        }
        table = _make_fake_actions_table()
        base = {
            "httpMethod": "POST",
            "resource": "/findings/{finding_id}/approve",
            "pathParameters": {"finding_id": vin_fi["finding_id"]},
            "queryStringParameters": {},
        }

        def _do_approve(sub):
            event = {
                **base,
                "requestContext": {"authorizer": {"claims": _claims(sub=sub,
                                                                     fleet_ids="fleet-mrdn-01")}},
                "body": json.dumps({
                    "target_system": "none",
                    "action_kind": "acknowledge_only",
                    "parameters": {},      # body as sent: no vehicle_vin
                }),
            }
            with patch.object(h, "_load_finding_by_id", return_value=vin_fi), \
                 patch.object(h, "_get_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value = table
                return h.handler(event, None)

        resp1 = _do_approve("op-sub-FW5b-A")
        assert resp1["statusCode"] == 200, (
            f"first VIN-scoped approve must be 200: {resp1.get('body', '')[:200]}"
        )
        # Stored row now has parameters = {"vehicle_vin": "VIN-FW5B-0001"}.
        action_id = _decode(resp1).get("action_id")
        assert action_id and action_id in table._store
        assert table._store[action_id]["parameters"].get("vehicle_vin") == "VIN-FW5B-0001", (
            "stored parameters must include server-filled vehicle_vin"
        )

        # Second request: identical body (no vehicle_vin), server fills it again.
        # The idempotency check compares BOTH sides as filled → they match → 200.
        resp2 = _do_approve("op-sub-FW5b-B")
        assert resp2["statusCode"] == 200, (
            f"identical repeat on VIN-scoped FI item must return 200 (idempotent); "
            f"got {resp2['statusCode']}: {resp2.get('body', '')[:200]}\n"
            f"(FW5b mutant: comparing body as sent instead of filled params turns this into 409)"
        )


# =============================================================================
# Warning 7 — FM mutants with no pre-existing test
# =============================================================================

class TestFM28c_KindTargetMustMatchRecommendation:
    """FM28c (FG2d review): the kind/target mismatch check has no test.

    Mutation: delete the `action_kind != rec.get("action_kind") or target_system !=
    rec.get("target_system")` check.  A mismatching body kind or target produces a
    valid 200 Action instead of 400.

    Mutation verification:
      FM28c — remove the entire kind+target equality check.  Both test cases below
              would then return 200.  The assertions fail, catching the mutant.
      FM29   — remove only the kind half.  test_mismatched_action_kind fails.
      FM28   — remove only the target half.  test_mismatched_target_system fails.
    """

    # A recommendation for acknowledge_only/none.
    _FI = _FI_FINDING

    def test_mismatched_action_kind_is_400(self):
        """Body action_kind differs from recommended_action.action_kind → 400."""
        resp, stored = _approve(
            self._FI,
            body_params={"priority": "normal"},
            extra_body={
                "action_kind": "file_warranty_pre",   # wrong kind
                "target_system": "none",
            },
        )
        assert resp["statusCode"] == 400, (
            f"mismatched action_kind must be 400; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored == {}, "nothing may be written on a kind mismatch"

    def test_mismatched_target_system_is_400(self):
        """Body target_system differs from recommended_action.target_system → 400.

        Only valid executable targets (not cms) can be used here; use chat as
        a non-none valid target that differs from none.
        """
        # Recommendation is none; body sends chat.
        resp, stored = _approve(
            self._FI,
            body_params={"priority": "normal"},
            extra_body={
                "action_kind": "acknowledge_only",
                "target_system": "chat",   # differs from rec's "none"
            },
        )
        assert resp["statusCode"] == 400, (
            f"mismatched target_system must be 400; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        assert stored == {}, "nothing may be written on a target mismatch"


class TestFM5_OverrideComputedOnRawBodyParams:
    """FM5 (FG2d review): the override comparison uses the body as sent (before
    AVX fills derived values such as vehicle_vin).

    A VIN-scoped FI item: the recommendation has no vehicle_vin, the body also
    has no vehicle_vin.  AVX fills vehicle_vin from the Finding's scope AFTER the
    comparison.  The decision must be 'approved', not 'overridden'.

    Mutation: compute the override on the FILLED parameters instead of the raw body.
      `stored["parameters"] == {vehicle_vin: VIN}` would differ from
      `recommended_action.parameters == {}` → decision='overridden' instead of
      'approved'.

    Mutation verification:
      FM5 — compare filled parameters against rec.  The assertion below fails because
            decision becomes 'overridden'.
    """

    _VIN_FI = {
        **_FI_FINDING,
        "finding_id": "FIND-fa2d0004-0000-0000-0000-000000000000",
        "scope": {"vin": "VIN-MRDN-0015"},
        "fleet_id": "fleet-mrdn-01",
        "recommended_action": {
            "action_kind": "acknowledge_only",
            "target_system": "none",
            # No vehicle_vin in recommendation.
            "parameters": {},
        },
    }

    def test_override_uses_raw_body_params_not_filled(self):
        """Body parameters match recommendation (both {}) → decision=approved, not overridden.

        AVX fills vehicle_vin after the comparison, so the comparison is on {}=={}."""
        resp, stored = _approve(self._VIN_FI, body_params={})
        assert resp["statusCode"] == 200, (
            f"VIN-scoped FI approve with matching params must be 200; "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        action_id = list(stored.keys())[0] if stored else None
        assert action_id is not None
        assert stored[action_id].get("decision") == "approved", (
            f"Body sent {{}}  == rec {{}} → decision must be 'approved'; "
            f"got {stored[action_id].get('decision')!r}. "
            f"(FM5 mutant: computing override on filled params makes {{}} != "
            f"{{vehicle_vin: …}} → 'overridden')"
        )

    def test_override_detected_when_body_differs_from_rec(self):
        """Positive control: body differs from rec → decision=overridden."""
        resp, stored = _approve(self._VIN_FI, body_params={"priority": "urgent"})
        assert resp["statusCode"] == 200
        action_id = list(stored.keys())[0] if stored else None
        assert action_id
        assert stored[action_id].get("decision") == "overridden"


class TestFM2FM3_FleetIdStoredOnActions:
    """FM2/FM3 (FG2d review): fleet_id must be stored on both approve and decline Actions.

    The by_fleet GSI and the activity feed (D11, contract § 9) depend on the
    top-level fleet_id attribute.  Removing it silently breaks both.

    Mutation verification:
      FM2 — remove fleet_id from decline's action dict.  Assertion fails.
      FM3 — remove fleet_id from approve's action dict.  Assertion fails.
    """

    def test_approve_fi_stores_fleet_id(self):
        """The approve Action carries the Finding's fleet_id (storage-only)."""
        resp, stored = _approve(_FI_FINDING, body_params={"priority": "normal"})
        assert resp["statusCode"] == 200
        action_id = list(stored.keys())[0] if stored else None
        assert action_id
        assert stored[action_id].get("fleet_id") == "fleet-mrdn-01", (
            f"approve Action must store fleet_id for by_fleet GSI; "
            f"got {stored[action_id].get('fleet_id')!r}"
        )

    def test_decline_stores_fleet_id(self):
        """The decline Action carries the Finding's fleet_id (storage-only)."""
        resp, stored = _decline(_FI_FINDING)
        assert resp["statusCode"] == 200
        action_id = list(stored.keys())[0] if stored else None
        assert action_id
        assert stored[action_id].get("fleet_id") == "fleet-mrdn-01", (
            f"decline Action must store fleet_id for by_fleet GSI; "
            f"got {stored[action_id].get('fleet_id')!r}"
        )


class TestFM1_FleetIdStrippedFromResponse:
    """FM1 (FG2d review): fleet_id must not appear in the response.

    The schema's root `additionalProperties: false` rejects it; CMS would also
    return it to the browser, which contract § 7 forbids.

    Mutation verification:
      FM1 — remove the _strip_action_for_response call from approve/decline.
            fleet_id appears in the response body.  Assertions below fail.
    """

    def test_approve_response_strips_fleet_id(self):
        """The approve response must not contain fleet_id."""
        resp, _ = _approve(_FI_FINDING, body_params={"priority": "normal"})
        assert resp["statusCode"] == 200
        body = _decode(resp)
        assert "fleet_id" not in body, (
            f"approve response must strip fleet_id; got fleet_id={body.get('fleet_id')!r}"
        )

    def test_decline_response_strips_fleet_id(self):
        """The decline response must not contain fleet_id."""
        resp, _ = _decline(_FI_FINDING)
        assert resp["statusCode"] == 200
        body = _decode(resp)
        assert "fleet_id" not in body, (
            f"decline response must strip fleet_id; got fleet_id={body.get('fleet_id')!r}"
        )


class TestFM25bFM26_DeclineCopiesRecKindAndTarget:
    """FM25b/FM26 (FG2d review): decline must copy action_kind and target_system from
    the recommendation, not use a hardcoded value.

    Every test fixture recommends acknowledge_only/none; a hardcoded value would
    pass those fixtures.  This test uses a Finding recommending book_recall_service
    (which is executable but needs DMS parameters).  We test only the field values
    stored on decline — decline requires no DMS call.

    Mutation verification:
      FM25b — hardcode action_kind="acknowledge_only" in decline action dict.
              Assertion on stored action_kind fails.
      FM26  — hardcode target_system="none".
              Assertion on stored target_system fails.
    """

    _RECALL_FI = {
        **_FI_FINDING,
        "finding_id": "FIND-fa2d0005-0000-0000-0000-000000000000",
        "recommended_action": {
            "action_kind": "book_recall_service",
            "target_system": "dms",
            "parameters": {"dealer_id": "dealer-seattle"},
        },
    }

    def test_decline_copies_action_kind_from_recommendation(self):
        """The decline Action's action_kind equals the recommendation's, not a constant."""
        resp, stored = _decline(self._RECALL_FI, reason="handled at depot")
        assert resp["statusCode"] == 200, (
            f"decline must succeed; got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        action_id = list(stored.keys())[0] if stored else None
        assert action_id
        assert stored[action_id].get("action_kind") == "book_recall_service", (
            f"decline action_kind must be copied from rec ('book_recall_service'); "
            f"got {stored[action_id].get('action_kind')!r} (FM25b: hardcoded constant)"
        )

    def test_decline_copies_target_system_from_recommendation(self):
        """The decline Action's target_system equals the recommendation's, not a constant."""
        resp, stored = _decline(self._RECALL_FI, reason="handled at depot")
        assert resp["statusCode"] == 200
        action_id = list(stored.keys())[0] if stored else None
        assert action_id
        assert stored[action_id].get("target_system") == "dms", (
            f"decline target_system must be copied from rec ('dms'); "
            f"got {stored[action_id].get('target_system')!r} (FM26: hardcoded constant)"
        )


class TestFM14_ApproveThenDeclineIs409:
    """FM14 (FG2d review): after approve, decline must return 409 (already_decided).

    Only decline-then-approve was tested before; approve-then-decline was not.

    Mutation verification:
      FM14 — let decline return 200 over any existing decision (drop the
             `if existing.get("decision") != "declined": return 409` arm).
             The assertion below fails because status 409 is not returned.
    """

    def test_approve_then_decline_is_409(self):
        """Approve the FI item first, then attempt decline → 409 already_decided."""
        from unittest.mock import patch
        table = _make_fake_actions_table()
        finding = _FI_FINDING

        def _handler(event):
            with patch.object(h, "_load_finding_by_id", return_value=finding), \
                 patch.object(h, "_get_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value = table
                return h.handler(event, None)

        # Step 1: approve
        approve_event = _event(
            "POST", "/findings/{finding_id}/approve",
            path_params={"finding_id": finding["finding_id"]},
            claims=_claims(fleet_ids="fleet-mrdn-01"),
            body={"target_system": "none", "action_kind": "acknowledge_only",
                  "parameters": {"priority": "normal"}},
        )
        resp1 = _handler(approve_event)
        assert resp1["statusCode"] == 200, f"approve must succeed: {resp1.get('body', '')[:200]}"
        assert _decode(resp1).get("decision") == "approved"

        # Step 2: decline after approve → 409
        decline_event = _event(
            "POST", "/findings/{finding_id}/decline",
            path_params={"finding_id": finding["finding_id"]},
            claims=_claims(fleet_ids="fleet-mrdn-01"),
            body={"reason": "changed my mind"},
        )
        resp2 = _handler(decline_event)
        assert resp2["statusCode"] == 409, (
            f"decline after approve must be 409 already_decided; "
            f"got {resp2['statusCode']}: {resp2.get('body', '')[:200]}"
        )
        assert _decode(resp2).get("error") == "already_decided", (
            f"error code must be 'already_decided', got {_decode(resp2)!r}"
        )


class TestFM10_PlatformAdminIsOperatorOnFIItem:
    """FM10 (FG2d review): platform-admin on an FI item must be classified operator.

    If _check_approve_scope returns 'user' for a platform-admin (e.g. because the
    operator branch is guarded by a group check that is wrong), the FI item denies
    the admin with 403.

    Mutation verification:
      FM10 — in _check_approve_scope, change the `if "platform-admin" in groups:
             return "operator"` line to `return "user"`.
             A platform-admin approve on an FI item then returns 403 (not 200).
             The assertion below fails, catching the mutant.
    """

    def test_platform_admin_can_approve_fi_item(self):
        """platform-admin is classified operator and can approve FI queue items."""
        admin_claims = _claims(sub="admin-sub-01", groups="platform-admin")
        resp, stored = _approve(_FI_FINDING, body_params={"priority": "normal"},
                                claims=admin_claims)
        assert resp["statusCode"] == 200, (
            f"platform-admin must approve FI item (operator standing); "
            f"got {resp['statusCode']}: {resp.get('body', '')[:200]}"
        )
        action_id = list(stored.keys())[0] if stored else None
        assert action_id
        assert stored[action_id].get("decided_by", {}).get("kind") == "operator", (
            f"platform-admin decided_by.kind must be 'operator'; "
            f"got {stored[action_id].get('decided_by', {}).get('kind')!r} "
            f"(FM10: platform-admin classified as 'user')"
        )

    def test_platform_admin_decided_by_kind_is_operator_in_response(self):
        """The response also carries decided_by.kind=operator for platform-admin."""
        admin_claims = _claims(sub="admin-sub-01", groups="platform-admin")
        resp, _ = _approve(_FI_FINDING, body_params={"priority": "normal"},
                           claims=admin_claims)
        assert resp["statusCode"] == 200
        body = _decode(resp)
        assert body.get("decided_by", {}).get("kind") == "operator", (
            f"platform-admin response decided_by.kind must be 'operator'; "
            f"got {body.get('decided_by', {}).get('kind')!r}"
        )



# ---------------------------------------------------------------------------
# Group 2 review, core W3: a v2.5 FI kind goes only to its policy target.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["none", "vehicle-command", "retail", "chat"])
def test_book_recall_service_must_target_dms(target):
    """book_recall_service with any other target was recorded `completed` with nothing
    sent to DMS. Mutation: drop the policy-target check -> 200 and a write."""
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    import handler as h  # noqa: PLC0415

    finding = {"finding_id": "FIND-target-0001", "scope": {"vin": "VIN-T-1"},
               "fleet_id": "fleet-mrdn-01", "evidence": []}
    table = MagicMock()
    claims = {"sub": "op-1", "cognito:groups": '["platform-admin"]'}
    with patch.object(h, "_load_finding_by_id", return_value=finding), \
         patch.object(h, "_get_ddb") as ddb, \
         patch.object(h, "_dispatch_action") as dispatch:
        ddb.return_value.Table.return_value = table
        resp = h.handler({
            "httpMethod": "POST", "resource": "/findings/{finding_id}/approve",
            "pathParameters": {"finding_id": "FIND-target-0001"}, "queryStringParameters": {},
            "requestContext": {"authorizer": {"claims": claims}},
            "body": json.dumps({"action_kind": "book_recall_service", "target_system": target,
                                "parameters": {}})}, None)
    assert resp["statusCode"] == 400
    table.put_item.assert_not_called()
    dispatch.assert_not_called()
