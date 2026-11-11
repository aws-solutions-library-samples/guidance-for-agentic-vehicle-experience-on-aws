"""Route-level tests for the four task 3.6 fixes in handler.py.

Fix #1 — Notes exemption for record_engineering_decision
  Contract § 2.7 / § 4: changing ``notes`` alone on ``record_engineering_decision``
  does NOT make the decision ``overridden``.  Changing ``option_id`` still does.
  Stage C's ``submit_warranty_claim`` notes-change STAYS overridden.

Fix #2 — Decline population branch
  Spec D11: ``product-engineer`` may also decline a population-scoped Finding
  (``decided_by.kind: engineer``).  Fleet groups (``fleet-operator``) get 403.

Fix #3 — Kind/scope coupling
  400 before any write when:
  (a) population-scoped Finding + non-engineering action_kind (approve or decline)
  (b) engineering action_kind + non-population-scoped Finding (approve)

Fix #4 — ground_vehicle fault form fault_codes validation
  400 when the recommendation's ``fault_codes`` is not a non-empty list of
  non-blank strings.

Every test goes through ``lambda_handler`` / ``_approve_finding`` / ``_decline_finding``;
no helper-function-only tests.

Mutation targets are documented inline; each was verified in a git-archive copy.

Run:
    .venv/bin/python -m pytest lambdas/api-avx-findings/tests/test_t36_fixes.py -q
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

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
# Shared fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"
_FLEET_ID = "fleet-meridian-01"
_INVESTIGATION_ID = "inv-aaaa-bbbb-cccc-0001"
_PATTERN_ID = "pat-001-P0AA6-plant-CGA"
_OPTION_ID = "opt-001"
_FAULT_CODE = "P0420"


def _claims_engineer(**extra):
    return {"sub": "eng-sub-t36-0001", "cognito:groups": ["product-engineer"], **extra}


def _claims_operator(**extra):
    return {
        "sub": "op-sub-t36-0001",
        "cognito:groups": ["fleet-operator"],
        "custom:fleetIds": _FLEET_ID,
        **extra,
    }


def _event(method, resource, path_params=None, claims=None, body=None) -> dict:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params or {},
        "queryStringParameters": {},
        "requestContext": {"authorizer": {"claims": claims or _claims_engineer()}},
        "body": json.dumps(body) if body else None,
    }


def _approve_event(finding_id, body, claims=None) -> dict:
    return _event(
        "POST",
        "/findings/{finding_id}/approve",
        path_params={"finding_id": finding_id},
        claims=claims,
        body=body,
    )


def _decline_event(finding_id, body=None, claims=None) -> dict:
    return _event(
        "POST",
        "/findings/{finding_id}/decline",
        path_params={"finding_id": finding_id},
        claims=claims,
        body=body,
    )


def _decode(resp) -> dict:
    return json.loads(resp["body"])


def _make_ccf():
    return type("ConditionalCheckFailedException", (ClientError,), {})


def _fake_table():
    _CCF = _make_ccf()

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
                               "Message": "The conditional request failed"}},
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


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Block DDB and dispatch for every test unless the test patches them itself."""
    monkeypatch.setattr(h, "_dispatch_action",
                        lambda action, claims: MagicMock(status="executed", target_ref=None),
                        raising=False)
    monkeypatch.setattr(h, "_record_dispatch_result",
                        lambda action, result: action,
                        raising=False)

    def _no_find(*_a, **_k):
        raise AssertionError("test did not patch _load_finding_by_id")

    fake_res = MagicMock()
    fake_res.Table.return_value = _fake_table()
    monkeypatch.setattr(h, "_load_finding_by_id", _no_find, raising=False)
    monkeypatch.setattr(h, "_get_ddb", lambda: fake_res, raising=False)
    monkeypatch.setattr(h, "_ddb", fake_res, raising=False)


# ---------------------------------------------------------------------------
# Shared finding builders
# ---------------------------------------------------------------------------

def _pop_finding_engineering(
    action_kind="record_engineering_decision",
    option_id=_OPTION_ID,
    notes="",
):
    """Population-scoped quality.investigation Finding with engineering rec."""
    return {
        "finding_id": "FND-t36-pop-0001",
        "finding_kind": "quality.investigation",
        "agent_id": "agent-fi-quality",
        "scope": {"population_id": "meridian-motors"},
        "recommended_action": {
            "action_kind": action_kind,
            "target_system": "adp",
            "parameters": {
                "investigation_id": _INVESTIGATION_ID,
                "pattern_id": _PATTERN_ID,
                "option_id": option_id,
                "notes": notes,
            },
        },
    }


def _fleet_finding_ack():
    """Fleet-scoped FI Finding with non-engineering rec (acknowledge_only)."""
    return {
        "finding_id": "FND-t36-fleet-0001",
        "finding_kind": "health.tires",
        "agent_id": "agent-fi-fleet-health",
        "scope": {"fleet_id": _FLEET_ID},
        "fleet_id": _FLEET_ID,
        "evidence": [],
        "expires_at": "2099-12-31T00:00:00Z",
        "recommended_action": {
            "action_kind": "acknowledge_only",
            "target_system": "none",
            "parameters": {},
        },
    }


def _vin_finding_ground_fault(fault_codes=None):
    """VIN-scoped fleet.health.service Finding with ground_vehicle fault-form rec."""
    if fault_codes is None:
        fault_codes = [_FAULT_CODE]
    return {
        "finding_id": "FND-t36-vin-gnd-0001",
        "finding_kind": "fleet.health.service",
        "agent_id": "agent-fi-fleet-health",
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "fleet_id": _FLEET_ID,
        "vehicle_id": "MRDN0000000000015",
        "evidence": [],
        "expires_at": "2099-12-31T00:00:00Z",
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {
                "vehicle_vin": _VIN,
                "fault_codes": fault_codes,
            },
        },
    }


# ============================================================================
# Fix #1 — Notes exemption for record_engineering_decision
# ============================================================================

class TestFix1NotesExemption:
    """Changing notes alone on record_engineering_decision must NOT mark decision overridden.

    Mutation target: remove the `action_kind == "record_engineering_decision" and key == "notes"`
    guard in the body_overridable loop → notes change marks overridden → the
    ``test_notes_change_alone_stays_approved`` assertion fails.
    """

    def test_notes_change_alone_stays_approved(self):
        """ROUTE LEVEL: changing notes alone on record_engineering_decision → decision='approved'.

        Fix #1 mutation: remove the notes exemption → decision becomes 'overridden' → FAILS.
        """
        finding = _pop_finding_engineering(option_id=_OPTION_ID, notes="original notes")
        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": _INVESTIGATION_ID,
                    "pattern_id": _PATTERN_ID,
                    "option_id": _OPTION_ID,       # same as rec
                    "notes": "engineer adds context",  # different — notes only
                },
            },
            claims=_claims_engineer(),
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"engineer notes-only change must approve; got {resp['statusCode']}: "
            f"{_decode(resp)}"
        )
        body = _decode(resp)
        assert body.get("decision") == "approved", (
            f"Notes-only change must NOT mark decision='overridden'. Got {body.get('decision')!r}. "
            "Mutation: remove notes exemption → 'overridden' → FAILS."
        )

    def test_option_id_change_still_marks_overridden(self):
        """Positive control: changing option_id from rec → decision='overridden' (§ 2.7)."""
        finding = _pop_finding_engineering(option_id="opt-rec", notes="")
        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": _INVESTIGATION_ID,
                    "pattern_id": _PATTERN_ID,
                    "option_id": "opt-DIFFERENT",   # changed
                    "notes": "",
                },
            },
            claims=_claims_engineer(),
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200
        assert _decode(resp).get("decision") == "overridden", (
            "option_id change must still mark decision='overridden'"
        )

    def test_notes_change_on_submit_warranty_claim_stays_overridden(self):
        """Stage C submit_warranty_claim notes change STAYS overridden (not affected by fix).

        Mutation: extend the notes exemption to submit_warranty_claim → 'approved' → FAILS.
        """
        finding = {
            "finding_id": "FND-t36-wc-0001",
            "finding_kind": "fleet.warranty.claim_candidate",
            "agent_id": "agent-fi-recall-warranty",
            "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
            "fleet_id": _FLEET_ID,
            "vehicle_id": "MRDN0000000000015",
            "evidence": [],
            "expires_at": "2099-12-31T00:00:00Z",
            "recommended_action": {
                "action_kind": "submit_warranty_claim",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "ro_id": "ro-test-9001",
                    "line_no": "1",
                    "dealer_id": "dealer-seattle",
                    "labor_op": "LO-EV-BRAKE-001",
                    "notes": "original note",
                },
            },
        }
        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": "submit_warranty_claim",
                "target_system": "dms",
                "parameters": {
                    "notes": "engineer changes the note",  # different from rec
                },
            },
            claims=_claims_operator(),
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"submit_warranty_claim must approve; got {resp['statusCode']}: {_decode(resp)}"
        )
        assert _decode(resp).get("decision") == "overridden", (
            "Notes change on submit_warranty_claim must stay 'overridden' (Stage C unchanged). "
            "Mutation: extend exemption to submit_warranty_claim → 'approved' → FAILS."
        )


# ============================================================================
# Fix #2 — Decline population branch
# ============================================================================

class TestFix2DeclinePopulation:
    """product-engineer may decline a population-scoped Finding (decided_by.kind: engineer).

    Mutation target: remove the population branch from _decline_finding (or replace
    product-engineer check with fleet-operator check) → the
    ``test_engineer_can_decline_population_finding`` assertion fails.
    """

    def test_engineer_can_decline_population_finding(self):
        """ROUTE LEVEL: product-engineer gets 200 declining a population Finding (spec D11).

        Mutation: remove the population branch → 403 (no standing found) → FAILS.
        """
        finding = _pop_finding_engineering()
        event = _decline_event(finding["finding_id"], claims=_claims_engineer())
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"product-engineer must get 200 declining a population Finding; "
            f"got {resp['statusCode']}: {_decode(resp)}. "
            "Mutation: remove population branch → 403 → FAILS."
        )
        body = _decode(resp)
        assert body.get("decision") == "declined"
        decided_by = body.get("decided_by", {})
        assert decided_by.get("kind") == "engineer", (
            f"decided_by.kind must be 'engineer' for population decline; "
            f"got {decided_by.get('kind')!r}. "
            "Mutation: keep kind='operator' → FAILS."
        )

    def test_fleet_operator_gets_403_declining_population_finding(self):
        """fleet-operator gets 403 declining a population Finding (spec D11).

        Mutation: allow fleet-operator on population findings → 2xx → FAILS.
        """
        finding = _pop_finding_engineering()
        event = _decline_event(finding["finding_id"], claims=_claims_operator())
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 403, (
            f"fleet-operator must get 403 declining a population Finding; "
            f"got {resp['statusCode']}. "
            "Mutation: allow fleet-operator on population findings → 2xx → FAILS."
        )

    def test_fleet_operator_still_declines_fleet_finding(self):
        """Positive control: fleet-operator still gets 200 declining a fleet Finding."""
        finding = _fleet_finding_ack()
        event = _decline_event(finding["finding_id"], claims=_claims_operator())
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"fleet-operator must still decline a fleet Finding; got {resp['statusCode']}"
        )
        assert _decode(resp).get("decided_by", {}).get("kind") == "operator"

    def test_engineer_cannot_decline_fleet_finding(self):
        """product-engineer without fleet standing gets 403 on a fleet Finding.

        Mutation: let product-engineer bypass fleet standing → 200 → FAILS.
        """
        finding = _fleet_finding_ack()
        event = _decline_event(finding["finding_id"], claims=_claims_engineer())
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 403, (
            "product-engineer (no fleet standing) must not decline a fleet Finding. "
            "Mutation: let engineer bypass fleet standing → 200 → FAILS."
        )


# ============================================================================
# Fix #3 — Kind/scope coupling
# ============================================================================

class TestFix3KindScopeCoupling:
    """Engineering kinds only on population Findings; non-engineering only on others.

    Mutation targets:
    (a) remove the pop+non-engineering guard → test_pop_finding_non_engineering_kind_is_400
        gets 200 instead of 400.
    (b) remove the non-pop+engineering guard → test_fleet_finding_engineering_kind_is_400
        gets 200 instead of 400.
    """

    # ── approve route ────────────────────────────────────────────────────────

    def test_pop_finding_non_engineering_kind_returns_400_on_approve(self):
        """ROUTE LEVEL (approve): pop Finding + non-engineering action_kind → 400.

        Mutation: remove the population + non-engineering kind guard → 403 or 200 → FAILS.
        """
        finding = _pop_finding_engineering()
        # Override finding's recommended action to a non-engineering kind
        finding_copy = dict(finding)
        finding_copy["recommended_action"] = {
            "action_kind": "acknowledge_only",
            "target_system": "none",
            "parameters": {},
        }
        event = _approve_event(
            finding_copy["finding_id"],
            body={
                "action_kind": "acknowledge_only",
                "target_system": "none",
                "parameters": {},
            },
            claims=_claims_engineer(),
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding_copy):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"population Finding + non-engineering action_kind must return 400; "
            f"got {resp['statusCode']}. "
            "Mutation: remove guard → 403 or 200 → FAILS."
        )

    def test_fleet_finding_engineering_kind_returns_400_on_approve(self):
        """ROUTE LEVEL (approve): non-population Finding + engineering action_kind → 400.

        Mutation: remove the fleet+engineering kind guard → 200 → FAILS.
        """
        # Fleet finding, but try to approve with an engineering kind
        finding = _fleet_finding_ack()
        finding_copy = dict(finding)
        finding_copy["recommended_action"] = {
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "parameters": {
                "investigation_id": _INVESTIGATION_ID,
                "pattern_id": _PATTERN_ID,
                "option_id": _OPTION_ID,
                "notes": "",
            },
        }
        event = _approve_event(
            finding_copy["finding_id"],
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
            claims=_claims_operator(),
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding_copy):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"non-population Finding + engineering action_kind must return 400; "
            f"got {resp['statusCode']}. "
            "Mutation: remove guard → 403 or 200 → FAILS."
        )

    def test_pop_finding_engineering_kind_not_400_on_approve(self):
        """Positive control (approve): pop Finding + engineering action_kind → not 400."""
        finding = _pop_finding_engineering()
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
            claims=_claims_engineer(),
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] != 400, (
            f"population Finding + engineering kind must not return 400; got {resp['statusCode']}"
        )

    # ── decline route ────────────────────────────────────────────────────────

    def test_pop_finding_non_engineering_rec_returns_400_on_decline(self):
        """ROUTE LEVEL (decline): pop Finding with non-engineering rec action_kind → 400.

        Mutation: remove the pop+non-engineering guard in _decline_finding → 200 → FAILS.
        """
        finding = _pop_finding_engineering()
        finding_copy = dict(finding)
        finding_copy["recommended_action"] = {
            "action_kind": "acknowledge_only",
            "target_system": "none",
            "parameters": {},
        }
        event = _decline_event(finding_copy["finding_id"], claims=_claims_engineer())
        with patch.object(h, "_load_finding_by_id", return_value=finding_copy):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"pop Finding with non-engineering rec must return 400 on decline; "
            f"got {resp['statusCode']}. "
            "Mutation: remove guard → 200 → FAILS."
        )

    def test_fleet_finding_engineering_rec_returns_400_on_decline(self):
        """ROUTE LEVEL (decline): fleet Finding with engineering rec action_kind → 400.

        Mutation: remove the fleet+engineering guard in _decline_finding → 200 → FAILS.
        """
        finding = _fleet_finding_ack()
        finding_copy = dict(finding)
        finding_copy["recommended_action"] = {
            "action_kind": "record_engineering_decision",
            "target_system": "adp",
            "parameters": {
                "investigation_id": _INVESTIGATION_ID,
                "pattern_id": _PATTERN_ID,
                "option_id": _OPTION_ID,
                "notes": "",
            },
        }
        event = _decline_event(finding_copy["finding_id"], claims=_claims_operator())
        with patch.object(h, "_load_finding_by_id", return_value=finding_copy):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"fleet Finding with engineering rec must return 400 on decline; "
            f"got {resp['statusCode']}. "
            "Mutation: remove guard → 200 → FAILS."
        )

    def test_fleet_finding_non_engineering_rec_not_400_on_decline(self):
        """Positive control (decline): fleet Finding + non-engineering rec → not 400."""
        finding = _fleet_finding_ack()
        event = _decline_event(finding["finding_id"], claims=_claims_operator())
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] != 400, (
            f"fleet Finding + non-engineering kind must not return 400 on decline; "
            f"got {resp['statusCode']}"
        )


# ============================================================================
# Fix #4 — ground_vehicle fault form fault_codes validation
# ============================================================================

class TestFix4FaultCodesValidation:
    """Recommendation's fault_codes must be a non-empty list of non-blank strings.

    Mutation target: remove the fault_codes validation block from
    _check_server_set_params → test_empty_list_returns_400 and
    test_blank_string_returns_400 fail (get 200 instead of 400).
    """

    def _approve_ground_fault(self, fault_codes, claims=None):
        """Helper: approve a ground_vehicle fault-form Finding via the route."""
        finding = _vin_finding_ground_fault(fault_codes=fault_codes)
        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {"vehicle_vin": _VIN},
            },
            claims=claims or _claims_operator(),
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            return h.handler(event, None)

    def test_valid_fault_codes_list_approves(self):
        """Positive control: non-empty list of non-blank strings → approves (not 400)."""
        resp = self._approve_ground_fault(fault_codes=["P0420", "P0300"])
        assert resp["statusCode"] != 400, (
            f"valid fault_codes list must not return 400; got {resp['statusCode']}"
        )

    def test_empty_list_returns_400(self):
        """ROUTE LEVEL: fault_codes=[] in recommendation → 400 before any write.

        Mutation: remove the non-empty check → 200 (empty fault_codes written) → FAILS.
        """
        resp = self._approve_ground_fault(fault_codes=[])
        assert resp["statusCode"] == 400, (
            f"empty fault_codes list must return 400; got {resp['statusCode']}. "
            "Mutation: remove non-empty check → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "fault_codes" in error, (
            f"400 error must name 'fault_codes'; got {error!r}"
        )

    def test_blank_string_element_returns_400(self):
        """ROUTE LEVEL: fault_codes containing a blank string → 400 before any write.

        Mutation: remove the non-blank-strings check → 200 (blank code written) → FAILS.
        """
        resp = self._approve_ground_fault(fault_codes=["P0420", "   "])
        assert resp["statusCode"] == 400, (
            f"fault_codes with a blank element must return 400; got {resp['statusCode']}. "
            "Mutation: remove non-blank check → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "fault_codes" in error, (
            f"400 error must name 'fault_codes'; got {error!r}"
        )

    def test_non_list_fault_codes_returns_400(self):
        """ROUTE LEVEL: fault_codes that is a string (not a list) → 400 before any write.

        Mutation: remove the isinstance(fault_codes, list) check → 200 → FAILS.
        """
        resp = self._approve_ground_fault(fault_codes="P0420")
        assert resp["statusCode"] == 400, (
            f"fault_codes as a string must return 400; got {resp['statusCode']}. "
            "Mutation: remove list-type check → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "fault_codes" in error

    def test_no_ddb_write_on_invalid_fault_codes(self):
        """No DDB write occurs when fault_codes is invalid."""
        finding = _vin_finding_ground_fault(fault_codes=[])
        fake_table = _fake_table()
        fake_res = MagicMock()
        fake_res.Table.return_value = fake_table
        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {"vehicle_vin": _VIN},
            },
            claims=_claims_operator(),
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400
        # No items stored
        assert not fake_table._store, (
            "No DDB row must be written when fault_codes validation fails"
        )
