"""Red tests — population approvals and Stage D broker/iOS invariants (task 1.4 items 10 and 13).

Contract §§ 2.7, 5, 7, 12; spec D11, D14.

  10. Population approvals:
      10a. Only product-engineer decides; AVX records decided_by.kind: engineer.
      10b. A fleet-operator gets 403 on approve for a population-scoped Finding.
      10c. option_id or notes changed from the recommendation → decision marked overridden.
      10d. investigation_id or pattern_id body value different from Finding → 400.

  13. Stage D findings do not carry fleet_id when population-scoped;
      every Stage D agent_id starts with "agent-fi-" (the broker skip and iOS exclusion
      rely on the agent_id prefix — Stage C D13, contract § 12).

Every test is xfail(strict=True, reason="FI stage D task 1.4"). All not-yet-built module
imports are inside test bodies so collection never errors. Path setup follows
test_server_set_params.py's precedent.

Run:
    .venv/bin/python -m pytest lambdas/api-avx-findings/tests/test_population_approve.py -q
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

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
# Shared helpers
# ---------------------------------------------------------------------------

def _make_fake_table():
    """Minimal fake DDB Table that records put_item calls and raises CCF on duplicate."""
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
                    {"Error": {"Code": "ConditionalCheckFailedException",
                               "Message": ""}},
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

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"
_FLEET_ID = "fleet-meridian-01"
_PATTERN_ID = "pat-001-P0AA6-plant-CGA"
_INVESTIGATION_ID = "inv-aaaa-bbbb-cccc-0001"
_FINDING_ID = "FND-pop-inv-0001"
_OPTION_ID = "opt-001"

#: Claims with CSV custom:fleetIds per task constraint.
_ENGINEER_CLAIMS: dict = {
    "sub": "eng-sub-pa-0001",
    "cognito:groups": ["product-engineer"],
    "custom:fleetIds": f"{_FLEET_ID},fleet-meridian-02",
}

_OPERATOR_CLAIMS: dict = {
    "sub": "op-sub-pa-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": f"{_FLEET_ID}",
}

#: A population-scoped investigation Finding with the recommended action.
def _investigation_finding(
    option_id: str = _OPTION_ID,
    investigation_id: str = _INVESTIGATION_ID,
    pattern_id: str = _PATTERN_ID,
) -> dict:
    return {
        "finding_id": _FINDING_ID,
        "finding_kind": "quality.investigation",
        "agent_id": "agent-fi-quality",
        "scope": {"population_id": "meridian-motors"},
        "recommended_action": {
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "parameters": {
                "investigation_id": investigation_id,
                "pattern_id": pattern_id,
                "option_id": option_id,
                "notes": "",
            },
        },
    }


def _approve_event(
    finding: dict | None = None,
    body: dict | None = None,
    claims: dict | None = None,
) -> dict:
    """Build a Lambda event for POST /findings/{id}/approve."""
    finding = finding or _investigation_finding()
    approve_body = {
        "action_kind": "record_engineering_decision",
        "target_system": "adp",
        "parameters": {
            "investigation_id": _INVESTIGATION_ID,
            "pattern_id": _PATTERN_ID,
            "option_id": _OPTION_ID,
            "notes": "",
        },
    }
    if body:
        approve_body.update(body)
    return {
        "httpMethod": "POST",
        "resource": "/findings/{finding_id}/approve",
        "path": f"/findings/{finding['finding_id']}/approve",
        "pathParameters": {"findingId": finding["finding_id"]},
        "queryStringParameters": {},
        "body": json.dumps(approve_body),
        "requestContext": {
            "authorizer": {"claims": claims or _ENGINEER_CLAIMS},
        },
        # W17 fix: resource key added so the handler routes on event["resource"].
        # Tests that need the finding available use patch.object(h, "_load_finding_by_id").
    }


def _approve_event_with_finding(
    finding: dict | None = None,
    body: dict | None = None,
    claims: dict | None = None,
) -> tuple[dict, dict]:
    """Return (event, finding) ready for use with patch('handler._load_finding_by_id').

    Fix W17: the approve tests must stub _load_finding_by_id rather than passing
    _test_finding_stub in the event, which no real handler would read.
    """
    finding = finding or _investigation_finding()
    event = _approve_event(finding=finding, body=body, claims=claims)
    return event, finding


# ============================================================================
# Item 10a — Only product-engineer can approve; decided_by.kind: engineer
# ============================================================================

class TestPopulationApproveAccess:
    """Only product-engineer decides on population-scoped Findings."""

    def test_engineer_can_approve_population_finding(self):
        """product-engineer successfully approves a quality.investigation Finding.

        Fix W17: uses patch.object(h, "_load_finding_by_id") instead of a string patch
        (the root conftest evicts sys.modules["handler"] before each test, so
        patch("handler._load_finding_by_id") patches a fresh module object, not h).
        Also stubs _get_ddb and _dispatch_action so no real AWS call is attempted.
        """
        finding = _investigation_finding()
        event, _ = _approve_event_with_finding(finding=finding, claims=_ENGINEER_CLAIMS)
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=MagicMock()), \
             patch.object(h, "_dispatch_action", return_value={"status": "executed"}), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: action):
            resp = h.handler(event, None)
        assert resp["statusCode"] in (200, 201, 202), (
            f"product-engineer must be able to approve a population Finding; "
            f"got {resp['statusCode']}."
        )

    def test_decided_by_kind_is_engineer_on_approve(self):
        """After approval by a product-engineer, AVX records decided_by.kind: engineer.

        Route-level test (C3 fix, G3F1.5): asserted on the approve response and on
        the DDB Item, not on the deleted _decide_population_finding helper.

        Mutation: change the approve route to record decided_by.kind = 'operator' for
        population Findings → response.decided_by.kind != 'engineer' → FAILS.
        """
        finding = _investigation_finding()
        event, _ = _approve_event_with_finding(finding=finding, claims=_ENGINEER_CLAIMS)

        fake_res = MagicMock()
        ft = _make_fake_table()
        fake_res.Table.return_value = ft

        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res), \
             patch.object(h, "_dispatch_action",
                          return_value=MagicMock(status="executed", target_ref=None)), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: action):
            resp = h.handler(event, None)

        assert resp["statusCode"] in (200, 201, 202), (
            f"product-engineer must approve a population Finding; got {resp['statusCode']}: "
            f"{json.loads(resp['body'])}"
        )
        body = json.loads(resp["body"])
        decided_by = body.get("decided_by", {})
        assert decided_by.get("kind") == "engineer", (
            f"decided_by.kind must be 'engineer' in the response for a product-engineer "
            f"approval; got {decided_by.get('kind')!r}. "
            "Mutation: use 'operator' → FAILS."
        )
        assert decided_by.get("id") == _ENGINEER_CLAIMS["sub"], (
            f"decided_by.id must be the caller's sub; "
            f"got {decided_by.get('id')!r}. "
        )
        # Also assert on the DDB Item — the stored record must carry decided_by.kind: engineer.
        stored_items = list(ft._store.values())
        assert stored_items, "An Action must have been written to DDB"
        stored = stored_items[0]
        stored_decided_by = stored.get("decided_by", {})
        assert stored_decided_by.get("kind") == "engineer", (
            f"stored Action.decided_by.kind must be 'engineer'; "
            f"got {stored_decided_by.get('kind')!r}. "
            "Mutation: record 'operator' in DDB → FAILS."
        )

    def test_fleet_operator_gets_403_on_population_finding(self):
        """A fleet-operator gets 403 when attempting to approve a population Finding.

        Fix W17: uses patch.object(h, "_load_finding_by_id") so the handler can load
        the finding without real DynamoDB (handler routes 403 before any DDB write).
        spec D16: only product-engineer may decide on population-scoped items.
        """
        finding = _investigation_finding()
        event, _ = _approve_event_with_finding(finding=finding, claims=_OPERATOR_CLAIMS)
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            f"fleet-operator must receive 403 on a population Finding; "
            f"got {resp['statusCode']}."
        )

    def test_connected_services_gets_403_on_population_finding(self):
        """connected-services gets 403 on population Finding approvals (Fix W17, spec D20).

        Fix W17: uses patch.object(h, "_load_finding_by_id") so the handler loads the
        finding without real DynamoDB (handler routes 403 before any DDB write).
        Mutation: allow connected-services to approve population Findings →
        statusCode != 403, this fails.
        """
        cs_claims = {
            "sub": "cs-sub-approve-0001",
            "cognito:groups": ["connected-services"],
            "custom:fleetIds": f"{_FLEET_ID}",
        }
        finding = _investigation_finding()
        event, _ = _approve_event_with_finding(finding=finding, claims=cs_claims)
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            "connected-services must receive 403 on population Finding approve (spec D20). "
            "Mutation: allow connected-services → 2xx, this fails."
        )

    def test_platform_admin_without_engineer_gets_403_on_population_finding(self):
        """platform-admin without product-engineer gets 403 (Fix W17, spec D16).

        Fix W17: uses patch.object(h, "_load_finding_by_id") + stubs _get_ddb and
        _dispatch_action so no real AWS call is attempted regardless of what standing
        the current handler grants platform-admin.
        The likely wrong implementation reuses Stage A's rule that platform-admin
        decides everywhere. This test catches that.
        Mutation: allow platform-admin via Stage A rule → 2xx, this fails.
        """
        admin_claims = {
            "sub": "admin-sub-approve-0001",
            "cognito:groups": ["platform-admin"],
            "custom:fleetIds": f"{_FLEET_ID},fleet-meridian-02",
        }
        finding = _investigation_finding()
        event, _ = _approve_event_with_finding(finding=finding, claims=admin_claims)
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=MagicMock()), \
             patch.object(h, "_dispatch_action", return_value={"status": "executed"}):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            "platform-admin (without product-engineer) must receive 403 on "
            "population Finding approve (spec D16). "
            "Mutation: treat platform-admin as deciding everywhere → 2xx, this fails."
        )


# ============================================================================
# Item 10c — option_id or notes changed → overridden
# ============================================================================

class TestPopulationApproveOverride:
    """Changing option_id → overridden; changing notes alone does not."""

    def test_different_option_id_marks_decision_overridden(self):
        """Choosing a different option_id than the agent's recommendation → overridden.

        Route-level test (C3 fix, G3F1.5): goes through lambda_handler, not the
        deleted _check_override_status helper.

        Mutation: remove the option_id-override path → decision = 'approved' → FAILS.
        """
        finding = _investigation_finding(option_id="opt-001")
        event, _ = _approve_event_with_finding(
            finding=finding,
            body={
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": _INVESTIGATION_ID,
                    "pattern_id": _PATTERN_ID,
                    "option_id": "opt-002",   # different from recommendation
                    "notes": "",
                },
            },
            claims=_ENGINEER_CLAIMS,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=MagicMock()), \
             patch.object(h, "_dispatch_action",
                          return_value=MagicMock(status="executed", target_ref=None)), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: action):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"Engineer approve with changed option_id must get 200; got {resp['statusCode']}"
        )
        body = json.loads(resp["body"])
        assert body.get("decision") == "overridden", (
            "Changing option_id from the recommendation must mark the decision "
            "'overridden' (contract § 2.7). "
            "Mutation: remove option_id-override path → 'approved' → FAILS."
        )

    def test_changed_notes_alone_does_not_mark_overridden(self):
        """Changing notes alone does not make the decision overridden (contract § 2.7).

        Route-level test (C3 fix, G3F1.5): goes through lambda_handler, not the
        deleted _check_override_status helper.

        Mutation: remove the notes-exemption → decision = 'overridden' → FAILS.
        """
        finding = _investigation_finding(option_id="opt-001")
        event, _ = _approve_event_with_finding(
            finding=finding,
            body={
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": _INVESTIGATION_ID,
                    "pattern_id": _PATTERN_ID,
                    "option_id": "opt-001",   # same as recommendation
                    "notes": "Extra context from engineer review.",  # different — notes only
                },
            },
            claims=_ENGINEER_CLAIMS,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=MagicMock()), \
             patch.object(h, "_dispatch_action",
                          return_value=MagicMock(status="executed", target_ref=None)), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: action):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body.get("decision") != "overridden", (
            "Changing only notes must not mark the decision 'overridden' "
            "(contract § 2.7: only option_id change makes it overridden). "
            "Mutation: remove notes-exemption → 'overridden' → FAILS."
        )
        # Also verify that option_id change IS overridden (positive control)
        finding2 = _investigation_finding(option_id="opt-001")
        ev2, _ = _approve_event_with_finding(
            finding=finding2,
            body={
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": _INVESTIGATION_ID,
                    "pattern_id": _PATTERN_ID,
                    "option_id": "opt-002",   # different
                    "notes": "",
                },
            },
            claims=_ENGINEER_CLAIMS,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding2), \
             patch.object(h, "_get_ddb", return_value=MagicMock()), \
             patch.object(h, "_dispatch_action",
                          return_value=MagicMock(status="executed", target_ref=None)), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: action):
            resp2 = h.handler(ev2, None)
        assert resp2["statusCode"] == 200
        body2 = json.loads(resp2["body"])
        assert body2.get("decision") == "overridden", (
            "Changing option_id must still mark the decision 'overridden'."
        )


# ============================================================================
# Item 10d — investigation_id or pattern_id different → 400
# ============================================================================

class TestPopulationApproveServerSet:
    """Server-set parameters investigation_id and pattern_id: different body value → 400."""

    def test_different_investigation_id_returns_400(self):
        """A body investigation_id that differs from the Finding's server-set value → 400.

        Fix W17: uses patch.object(h, "_load_finding_by_id") instead of a string patch
        (the root conftest evicts sys.modules["handler"] before each test, so
        patch("handler._load_finding_by_id") patches a fresh module object, not h).
        The 400 must come from the server-set params check (error names
        'investigation_id'), not from the earlier action_kind validation gate.
        """
        finding = _investigation_finding(investigation_id=_INVESTIGATION_ID)
        event, _ = _approve_event_with_finding(
            finding=finding,
            body={
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": "inv-WRONG",   # mismatch
                    "pattern_id": _PATTERN_ID,
                    "option_id": _OPTION_ID,
                },
            },
            claims=_ENGINEER_CLAIMS,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400, (
            f"A mismatched investigation_id must return 400; got {resp['statusCode']}."
        )
        body = json.loads(resp.get("body") or "{}")
        assert "investigation_id" in body.get("error", ""), (
            f"400 must be from the server-set params check (error names 'investigation_id'), "
            f"not from an earlier action_kind gate. Got error: {body.get('error')!r}"
        )

    def test_different_pattern_id_returns_400(self):
        """A body pattern_id that differs from the Finding's server-set value → 400.

        Fix W17: uses patch.object(h, "_load_finding_by_id") instead of a string patch
        (the root conftest evicts sys.modules["handler"] before each test).
        The 400 must come from the server-set params check (error names 'pattern_id'),
        not from the earlier action_kind validation gate.
        """
        finding = _investigation_finding(pattern_id=_PATTERN_ID)
        event, _ = _approve_event_with_finding(
            finding=finding,
            body={
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": _INVESTIGATION_ID,
                    "pattern_id": "pat-WRONG",   # mismatch
                    "option_id": _OPTION_ID,
                },
            },
            claims=_ENGINEER_CLAIMS,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400, (
            f"A mismatched pattern_id must return 400; got {resp['statusCode']}."
        )
        body = json.loads(resp.get("body") or "{}")
        assert "pattern_id" in body.get("error", ""), (
            f"400 must be from the server-set params check (error names 'pattern_id'), "
            f"not from an earlier action_kind gate. Got error: {body.get('error')!r}"
        )


# ============================================================================
# Item 13 — Stage D findings: no fleet_id when population-scoped;
#            every Stage D agent_id starts with "agent-fi-"
# ============================================================================

class TestStageDFindingInvariants:
    """Stage D findings carry no fleet_id when population-scoped; agent_id starts with agent-fi-."""

    def test_investigation_finding_has_no_fleet_id(self):
        """quality.investigation and quality.issue Findings carry no fleet_id.

        Contract § 7 fail-closed state: population-scoped items carry no fleet_id,
        so no fleet queue shows them (spec D11). The iOS route exclusion and push-broker
        skip rely on the agent_id prefix, not the fleet_id.
        """
        from agents.fi_quality.write import build_investigation_finding

        finding = build_investigation_finding(
            investigation_id=_INVESTIGATION_ID,
            pattern_id=_PATTERN_ID,
            bundle={"pattern": {"code_family": "P0AA6"}, "candidates": []},
            root_cause={"candidate_id": "cand-001", "failure_mode_id": "fm-001"},
            options=[],
            computed_at="2026-10-05T06:00:00Z",
        )
        assert "fleet_id" not in finding, (
            "quality.investigation Finding must not carry fleet_id "
            "(spec D11 / contract § 7 fail-closed state)."
        )
        assert "scope" in finding
        assert "population_id" in finding["scope"], (
            "Population-scoped Finding must carry scope.population_id."
        )
        assert "fleet_id" not in finding["scope"], (
            "scope must not contain fleet_id for a population-scoped Finding."
        )

    def test_fleet_health_finding_population_scoped_has_no_fleet_id(self):
        """A population-scoped fleet.health.service Finding carries no fleet_id.

        This tests the property described in item 13: no Stage D Finding carries
        fleet_id when population-scoped. Fleet-health Findings are VIN-scoped and
        do carry a fleet_id (via the writer projecting the vehicle's fleet), but a
        Finding constructed without a resolved fleet must carry no fleet_id.
        """
        from agents.fi_fleet_health.write import build_finding_no_fleet

        finding = build_finding_no_fleet(vin=_VIN)
        assert "fleet_id" not in finding, (
            "A fleet-health Finding with no resolved fleet must carry no fleet_id."
        )

    def test_fleet_health_agent_id_starts_with_agent_fi(self):
        """fleet.health.service Finding agent_id starts with 'agent-fi-'."""
        from agents.fi_fleet_health.write import AGENT_ID as HEALTH_AGENT_ID

        assert HEALTH_AGENT_ID.startswith("agent-fi-"), (
            f"Fleet-health agent_id must start with 'agent-fi-'; got {HEALTH_AGENT_ID!r}. "
            "The iOS exclusion and push-broker skip rely on this prefix (Stage C D13)."
        )
        assert HEALTH_AGENT_ID == "agent-fi-fleet-health", (
            f"Expected exactly 'agent-fi-fleet-health'; got {HEALTH_AGENT_ID!r}."
        )

    def test_quality_agent_id_starts_with_agent_fi(self):
        """quality.investigation Finding agent_id starts with 'agent-fi-'."""
        from agents.fi_quality.write import AGENT_ID as QUALITY_AGENT_ID

        assert QUALITY_AGENT_ID.startswith("agent-fi-"), (
            f"Quality agent_id must start with 'agent-fi-'; got {QUALITY_AGENT_ID!r}. "
            "The push-broker skip and iOS exclusion rely on this prefix."
        )
        assert QUALITY_AGENT_ID == "agent-fi-quality", (
            f"Expected exactly 'agent-fi-quality'; got {QUALITY_AGENT_ID!r}."
        )

    def test_agent_fi_prefix_mutation_guard(self):
        """Mutation guard: changing the agent_id prefix would break this assertion."""
        from agents.fi_fleet_health.write import AGENT_ID as HEALTH_AGENT_ID
        from agents.fi_quality.write import AGENT_ID as QUALITY_AGENT_ID

        # Mutation: AGENT_ID = "agent-2-fleet-health"
        assert not HEALTH_AGENT_ID.startswith("agent-2-"), (
            "Mutation guard: if the prefix is 'agent-2-' the broker skip won't fire."
        )
        assert not QUALITY_AGENT_ID.startswith("agent-2-"), (
            "Mutation guard: if the prefix is 'agent-2-' the iOS exclusion won't fire."
        )
