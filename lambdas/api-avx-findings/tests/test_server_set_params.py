"""Red tests: server-set parameters per kind on the approve route (contract § 4 v2.6, spec D11).

Task 1.4 item 9.

Contract § 3 of `docs/fi-recall-warranty-contract.md` defines per-kind server-set rules:

  book_recall_service:
    server-set: vehicle_vin, recall_id    (body must omit or match → 400 otherwise)
    body may differ: dealer_id            (changing it makes the decision `overridden`)

  ground_vehicle:
    server-set: vehicle_vin, recall_id    (nothing body-changeable)

  submit_warranty_claim:
    server-set: vehicle_vin, ro_id, line_no, dealer_id, labor_op, fault_code
    body may differ: notes                (changing it makes the decision `overridden`)

decisions.md 2026-10-05 call 3 (override rule): Server-set parameters must be omitted or
equal (400 otherwise), are filled in after the comparison, and never make a decision
`overridden`; only overridable fields (`dealer_id`; `notes`) can.

Two test layers:
  Layer 1 (16 tests): handler._check_server_set_params — a pure helper that will be wired
    into the approve route. Tests call it directly to pin the property under isolation.
  Layer 2 (2 tests): the approve route itself — pins that the helper is actually wired in
    and a mismatched server-set param returns HTTP 400 from the approve endpoint.

All tests are xfail(strict=True, reason="FI stage C task 1.4"). The function does not exist
yet, so every test raises AttributeError inside the test body → xfailed. No full-handler
invocation for layer 1: these tests never trigger executor paths, so no AWS calls occur.

Run:
    .venv/bin/python -m pytest lambdas/api-avx-findings/tests/test_server_set_params.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup — let handler import succeed so the module-level import of
# handler does not error at collection time.
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

# Import handler at module level so collection succeeds.
import handler as h  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"
_FLEET_ID = "fleet-meridian-01"
_CAMPAIGN_ID = "MRD-2024-001"
_RO_ID = "ro-test-5001"
_LINE_NO = "1"

#: Operator claims with CSV custom:fleetIds (task constraint).
_OPERATOR_CLAIMS = {
    "sub": "op-sub-aaaa-bbbb-cccc-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": f"{_FLEET_ID},fleet-meridian-02",
}


def _recall_finding(campaign_id: str = _CAMPAIGN_ID) -> dict:
    return {
        "finding_id": "AVX-FND-ssparams-0001",
        "finding_kind": "fleet.recall.vehicle",
        "agent_id": "agent-fi-recall-warranty",
        "severity": "p0",
        "fleet_id": _FLEET_ID,
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "recommended_action": {
            "action_kind": "book_recall_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": _VIN,
                "recall_id": campaign_id,
                "dealer_id": "dealer-seattle",
            },
        },
    }


def _warranty_finding() -> dict:
    return {
        "finding_id": "AVX-FND-ssparams-0002",
        "finding_kind": "fleet.warranty.claim_candidate",
        "agent_id": "agent-fi-recall-warranty",
        "severity": "soon",
        "fleet_id": _FLEET_ID,
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "recommended_action": {
            "action_kind": "submit_warranty_claim",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": _VIN,
                "ro_id": _RO_ID,
                "line_no": _LINE_NO,
                "dealer_id": "dealer-seattle",
                "labor_op": "LO-EV-BRAKE-001",
                "fault_code": "C0035",
                "notes": "Initial writer notes.",
            },
        },
    }


def _ground_finding(campaign_id: str = _CAMPAIGN_ID) -> dict:
    return {
        "finding_id": "AVX-FND-ssparams-0003",
        "finding_kind": "fleet.recall.vehicle",
        "agent_id": "agent-fi-recall-warranty",
        "severity": "p0",
        "fleet_id": _FLEET_ID,
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {
                "vehicle_vin": _VIN,
                "recall_id": campaign_id,
            },
        },
    }


# ---------------------------------------------------------------------------
# Layer 1: _check_server_set_params helper (pure function)
# ---------------------------------------------------------------------------

# --- book_recall_service — server-set: vehicle_vin, recall_id ---

class TestBookRecallServiceServerSetParams:
    """handler._check_server_set_params validates recall_id as server-set for book_recall_service."""

    def test_different_recall_id_returns_error(self):
        """recall_id is server-set: a different body value must return an error."""
        params, err, decision = h._check_server_set_params(
            finding=_recall_finding(),
            action_kind="book_recall_service",
            body_parameters={
                "vehicle_vin": _VIN,
                "recall_id": "MRD-9999-999",  # differs from server value
                "dealer_id": "dealer-seattle",
            },
        )
        assert err is not None, "expected error for mismatched recall_id"

    def test_matching_recall_id_passes(self):
        """recall_id equal to the server value is accepted (omit-or-match rule)."""
        params, err, decision = h._check_server_set_params(
            finding=_recall_finding(),
            action_kind="book_recall_service",
            body_parameters={
                "vehicle_vin": _VIN,
                "recall_id": _CAMPAIGN_ID,  # exact match
                "dealer_id": "dealer-seattle",
            },
        )
        assert err is None

    def test_omitted_recall_id_passes_decision_approved(self):
        """recall_id omitted from body → no error; decision is 'approved' (call 3).

        Server-set params never make a decision 'overridden'; omitting them fills them in
        and the decision is 'approved' (decisions.md 2026-10-05 call 3).
        """
        params, err, decision = h._check_server_set_params(
            finding=_recall_finding(),
            action_kind="book_recall_service",
            body_parameters={
                "vehicle_vin": _VIN,
                # recall_id omitted
                "dealer_id": "dealer-seattle",
            },
        )
        assert err is None
        assert params.get("recall_id") == _CAMPAIGN_ID
        assert decision == "approved", (
            f"Omitting a server-set param must yield 'approved', not 'overridden'. "
            f"Got {decision!r}. decisions.md 2026-10-05 call 3."
        )

    def test_different_dealer_id_yields_overridden_decision(self):
        """dealer_id differs → no error, but decision == 'overridden'."""
        params, err, decision = h._check_server_set_params(
            finding=_recall_finding(),
            action_kind="book_recall_service",
            body_parameters={
                "vehicle_vin": _VIN,
                "recall_id": _CAMPAIGN_ID,
                "dealer_id": "dealer-portland",  # operator chose different rooftop
            },
        )
        assert err is None
        assert decision == "overridden"

    def test_changed_dealer_id_alone_yields_overridden_decision(self):
        """Only dealer_id in the body, changed → 'overridden'; server-set fields are filled in.

        Guards against a rule that needs a server-set field in the body before an overridable
        change counts (decisions.md 2026-10-05 call 3: overridable fields alone decide it).
        """
        params, err, decision = h._check_server_set_params(
            finding=_recall_finding(),
            action_kind="book_recall_service",
            body_parameters={"dealer_id": "dealer-portland"},
        )
        assert err is None
        assert params.get("vehicle_vin") == _VIN
        assert params.get("recall_id") == _CAMPAIGN_ID
        assert decision == "overridden"

    def test_matching_dealer_id_yields_approved_decision(self):
        """dealer_id matching recommended value → decision == 'approved'."""
        params, err, decision = h._check_server_set_params(
            finding=_recall_finding(),
            action_kind="book_recall_service",
            body_parameters={
                "vehicle_vin": _VIN,
                "recall_id": _CAMPAIGN_ID,
                "dealer_id": "dealer-seattle",  # matches recommended
            },
        )
        assert err is None
        assert decision == "approved"

    def test_omitted_dealer_id_gets_filled_from_recommendation(self):
        """Omitted dealer_id is filled from the recommendation; decision is 'approved'.

        decisions.md call 10: an omitted overridable field takes the recommendation's
        value and is not an override.

        N10 boundary: a helper that skips the fill-in leaves params["dealer_id"] absent.
        N11 boundary: a helper treating omission as override returns 'overridden' instead.
        """
        params, err, decision = h._check_server_set_params(
            finding=_recall_finding(),
            action_kind="book_recall_service",
            body_parameters={
                "vehicle_vin": _VIN,
                "recall_id": _CAMPAIGN_ID,
                # dealer_id omitted — should be filled from rec
            },
        )
        assert err is None, "Omitted dealer_id must not return an error."
        assert params.get("dealer_id") == "dealer-seattle", (
            f"Omitted dealer_id must be filled from the recommendation. "
            f"Got {params.get('dealer_id')!r}. "
            "N10 check: a helper that skips fill-in leaves it absent."
        )
        assert decision == "approved", (
            f"Omitted overridable field must yield 'approved', not 'overridden'. "
            f"Got {decision!r}. "
            "N11 check: treating omission as override (None != rec_val) gives 'overridden'."
        )


# --- ground_vehicle — server-set: vehicle_vin, recall_id (nothing body-changeable) ---

class TestGroundVehicleServerSetParams:

    def test_different_recall_id_returns_error(self):
        """recall_id is server-set on ground_vehicle: a different value must error."""
        params, err, decision = h._check_server_set_params(
            finding=_ground_finding(),
            action_kind="ground_vehicle",
            body_parameters={
                "vehicle_vin": _VIN,
                "recall_id": "MRD-9999-999",  # wrong campaign
            },
        )
        assert err is not None

    def test_empty_body_passes_server_fills_all_decision_approved(self):
        """Empty body parameters → server fills vehicle_vin and recall_id; decision 'approved'.

        Neither field is body-changeable, so omitting both yields 'approved' (call 3).
        """
        params, err, decision = h._check_server_set_params(
            finding=_ground_finding(),
            action_kind="ground_vehicle",
            body_parameters={},
        )
        assert err is None
        assert params.get("vehicle_vin") == _VIN
        assert params.get("recall_id") == _CAMPAIGN_ID
        assert decision == "approved", (
            f"Omitting server-set params must yield 'approved'. Got {decision!r}."
        )

    def test_ground_vehicle_has_no_overrideable_fields(self):
        """ground_vehicle has no body-changeable fields (contract § 3, 'Body may differ': nothing).

        A mismatched server-set field (recall_id) must return an error (the server-set rule).
        Extra non-server-set fields (notes, dealer_id) are ignored; they do NOT make the
        decision 'overridden' because ground_vehicle has no overridable fields.
        decisions.md call 10: only a body value that differs from the recommendation makes
        the decision 'overridden'; for ground_vehicle nothing is overridable, so the decision
        is always 'approved' when server-set params are correct.
        """
        # Mismatched server-set field → error.
        params, err, decision = h._check_server_set_params(
            finding=_ground_finding(),
            action_kind="ground_vehicle",
            body_parameters={"recall_id": "MRD-9999-999"},
        )
        assert err is not None, (
            "ground_vehicle: mismatched server-set recall_id must return an error."
        )

    def test_ground_vehicle_extra_field_does_not_make_decision_overridden(self):
        """A non-server-set body field on ground_vehicle does not cause 'overridden'.

        ground_vehicle has no overridable fields (contract § 3: 'Body may differ: nothing').
        Per decisions.md call 10: only a body value that differs from the recommendation
        makes the decision 'overridden'; a field that doesn't appear in the recommendation
        at all is not an 'overridable' field, so it cannot produce 'overridden'.
        Concretely: sending notes='driver note' in the body must yield decision='approved'.
        """
        params, err, decision = h._check_server_set_params(
            finding=_ground_finding(),
            action_kind="ground_vehicle",
            body_parameters={
                "vehicle_vin": _VIN,
                "recall_id": _CAMPAIGN_ID,
                "notes": "driver note — not a ground_vehicle field",  # genuinely not in kind
            },
        )
        assert err is None, (
            "A non-server-set field on ground_vehicle must not return 400."
        )
        assert decision == "approved", (
            f"ground_vehicle has no overridable fields; extra body fields must not yield "
            f"'overridden'. Got {decision!r}."
        )

    def test_both_recall_id_and_fault_codes_in_recommendation_returns_error(self):
        """A recommendation with both recall_id AND fault_codes returns an error.

        Contract § 4 / fi-fleet-health-contract.md § 4: exactly one of the two forms
        is present; having both is a schema error.

        Mutation guard: removing the 'rec_has_recall and rec_has_faults' check lets both
        forms through, causing this test to fail.
        """
        finding_both_forms = {
            "finding_id": "AVX-FND-ground-both-forms",
            "finding_kind": "fleet.health.service",
            "agent_id": "agent-fi-fleet-health",
            "severity": "p0",
            "fleet_id": _FLEET_ID,
            "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "recall_id": _CAMPAIGN_ID,   # Stage C form
                    "fault_codes": ["P0AA6"],    # Stage D form — both present → invalid
                },
            },
        }
        params, err, decision = h._check_server_set_params(
            finding=finding_both_forms,
            action_kind="ground_vehicle",
            body_parameters={},
        )
        assert err is not None, (
            "A recommendation with both recall_id and fault_codes must return an error "
            "(contract § 4: exactly one of the two forms). "
            "Mutation: remove the both-present guard → err is None → this fails."
        )

    def test_fault_form_with_fault_codes_passes(self):
        """Stage D fault form: fault_codes in the recommendation is accepted.

        Contract § 4 / fi-fleet-health-contract.md § 4: fault_codes (Stage D fault
        form) is server-set; a body that matches or omits it is accepted.
        """
        finding_fault_form = {
            "finding_id": "AVX-FND-ground-fault-form",
            "finding_kind": "fleet.health.service",
            "agent_id": "agent-fi-fleet-health",
            "severity": "p0",
            "fleet_id": _FLEET_ID,
            "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "fault_codes": ["P0AA6"],   # Stage D fault form
                },
            },
        }
        params, err, decision = h._check_server_set_params(
            finding=finding_fault_form,
            action_kind="ground_vehicle",
            body_parameters={},
        )
        assert err is None, f"Stage D fault form should pass: {err!r}"
        assert params.get("fault_codes") == ["P0AA6"], (
            "fault_codes must be filled from the recommendation."
        )
        assert "recall_id" not in params, "recall_id must not appear in fault form params."
        assert decision == "approved"


# --- submit_warranty_claim — server-set: vin, ro_id, line_no, dealer_id, labor_op, fault_code ---

class TestSubmitWarrantyClaimServerSetParams:

    def test_different_ro_id_returns_error(self):
        """ro_id is server-set: a different value must error."""
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding(),
            action_kind="submit_warranty_claim",
            body_parameters={
                "vehicle_vin": _VIN,
                "ro_id": "ro-9999",  # wrong RO
                "line_no": _LINE_NO,
                "dealer_id": "dealer-seattle",
                "labor_op": "LO-EV-BRAKE-001",
                "fault_code": "C0035",
            },
        )
        assert err is not None

    def test_different_dealer_id_returns_error_for_warranty(self):
        """dealer_id is server-set for submit_warranty_claim: a different value must error.

        M3 guard: removing dealer_id from the server-set list must fail this test.
        Unlike book_recall_service (where dealer_id is overrideable), for warranty claims
        the dealer_id is fixed (the RO's rooftop).
        """
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding(),
            action_kind="submit_warranty_claim",
            body_parameters={
                "vehicle_vin": _VIN,
                "ro_id": _RO_ID,
                "line_no": _LINE_NO,
                "dealer_id": "dealer-portland",  # differs — server-set for warranty
                "labor_op": "LO-EV-BRAKE-001",
                "fault_code": "C0035",
            },
        )
        assert err is not None, (
            "dealer_id is server-set for submit_warranty_claim (not overrideable). "
            "A different value must return an error. M3 guard."
        )

    def test_different_line_no_returns_error_for_warranty(self):
        """line_no is server-set for submit_warranty_claim: a different value must error.

        M3 guard: removing line_no from the server-set list must fail this test.
        """
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding(),
            action_kind="submit_warranty_claim",
            body_parameters={
                "vehicle_vin": _VIN,
                "ro_id": _RO_ID,
                "line_no": "99",  # wrong line — server-set
                "dealer_id": "dealer-seattle",
                "labor_op": "LO-EV-BRAKE-001",
                "fault_code": "C0035",
            },
        )
        assert err is not None, (
            "line_no is server-set for submit_warranty_claim. "
            "A different value must return an error. M3 guard."
        )

    def test_different_labor_op_returns_error(self):
        """labor_op is server-set: a different value must error."""
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding(),
            action_kind="submit_warranty_claim",
            body_parameters={
                "vehicle_vin": _VIN,
                "ro_id": _RO_ID,
                "line_no": _LINE_NO,
                "dealer_id": "dealer-seattle",
                "labor_op": "LO-DIFFERENT-999",  # differs
                "fault_code": "C0035",
            },
        )
        assert err is not None

    def test_different_fault_code_returns_error(self):
        """fault_code is server-set: a different value must error."""
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding(),
            action_kind="submit_warranty_claim",
            body_parameters={
                "vehicle_vin": _VIN,
                "ro_id": _RO_ID,
                "line_no": _LINE_NO,
                "dealer_id": "dealer-seattle",
                "labor_op": "LO-EV-BRAKE-001",
                "fault_code": "P0420",  # differs from C0035
            },
        )
        assert err is not None

    def test_different_notes_yields_overridden_decision(self):
        """notes is body-changeable: different notes → no error, decision='overridden'."""
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding(),
            action_kind="submit_warranty_claim",
            body_parameters={
                "vehicle_vin": _VIN,
                "ro_id": _RO_ID,
                "line_no": _LINE_NO,
                "dealer_id": "dealer-seattle",
                "labor_op": "LO-EV-BRAKE-001",
                "fault_code": "C0035",
                "notes": "Operator added a different note.",  # changed
            },
        )
        assert err is None
        assert decision == "overridden"

    def test_only_changed_notes_in_body_is_overridden_and_server_fills_the_rest(self):
        """Body with only a changed notes field → server fills every server-set param; 'overridden'.

        Call 3 (decisions.md 2026-10-05): server-set params are filled in and never cause
        `overridden`; an overridable field (`notes`) that differs from the recommendation does.
        The omitted server-set fields must not be what decides it (see the empty-body test).
        """
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding(),
            action_kind="submit_warranty_claim",
            body_parameters={"notes": "Only the allowed field."},
        )
        assert err is None
        assert params.get("vehicle_vin") == _VIN
        assert params.get("ro_id") == _RO_ID
        assert decision == "overridden", (
            f"notes differs from the recommendation (an overridable field) → 'overridden'. "
            f"Got {decision!r}. decisions.md 2026-10-05 call 3."
        )

    def test_omitted_notes_gets_filled_from_recommendation_approved(self):
        """Omitted notes is filled from the recommendation; decision is 'approved'.

        decisions.md call 10: an omitted overridable field takes the recommendation's
        value and is not an override.

        N10 boundary: skipping the fill-in leaves params['notes'] absent.
        N11 boundary: treating omission as override (None != rec_val) gives 'overridden'.
        """
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding(),
            action_kind="submit_warranty_claim",
            body_parameters={
                "vehicle_vin": _VIN,
                "ro_id": _RO_ID,
                "line_no": _LINE_NO,
                "dealer_id": "dealer-seattle",
                "labor_op": "LO-EV-BRAKE-001",
                "fault_code": "C0035",
                # notes omitted — should be filled from rec
            },
        )
        assert err is None, "Omitted notes must not return an error."
        assert params.get("notes") == "Initial writer notes.", (
            f"Omitted notes must be filled from the recommendation. "
            f"Got {params.get('notes')!r}. "
            "N10 check: skipping fill-in leaves it absent."
        )
        assert decision == "approved", (
            f"Omitted overridable field must yield 'approved', not 'overridden'. "
            f"Got {decision!r}. "
            "N11 check: None != rec_val without the guard gives 'overridden'."
        )


# ---------------------------------------------------------------------------
# Layer 2: approve route wiring — the 400 must come from the route
# ---------------------------------------------------------------------------

class TestApproveRouteServerSetValidation:
    """The approve route must call _check_server_set_params and return 400 on mismatch.

    These tests go through handler.approve_finding() (or the Lambda handler event
    dispatch) to pin that _check_server_set_params is actually wired in, not just
    present as a helper.

    All tests are xfail(strict=True, reason="FI stage C task 1.4") because Stage C
    handler code is not implemented yet.
    """

    def _approve_event(self, finding_id: str, body: dict, claims: dict) -> dict:
        """Minimal API Gateway event for POST /findings/{finding_id}/approve."""
        return {
            "httpMethod": "POST",
            "resource": "/findings/{finding_id}/approve",
            "path": f"/findings/{finding_id}/approve",
            "pathParameters": {"finding_id": finding_id},
            "headers": {"Authorization": "Bearer fake-jwt"},
            "requestContext": {
                "authorizer": {"claims": claims},
            },
            "body": json.dumps(body),
        }

    def test_approve_with_mismatched_recall_id_returns_400(self):
        """Approve with a mismatched recall_id returns HTTP 400 from the route.

        The handler loads the Finding through _load_finding_by_id (handler.py:920).
        The route is dispatched via event['resource']; without it handler() returns 404.
        Reviewer two-line fix: set resource in the event and patch _load_finding_by_id.

        The 400 must name the server-set parameter (parameters.recall_id), not a generic
        "not executable" 400 that fires before _check_server_set_params is reached.
        """
        from unittest.mock import MagicMock, patch
        import json as _json

        event = self._approve_event(
            finding_id="AVX-FND-ssparams-0001",
            body={
                "action_kind": "book_recall_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "recall_id": "MRD-WRONG-999",  # server-set mismatch
                    "dealer_id": "dealer-seattle",
                },
            },
            claims=_OPERATOR_CLAIMS,
        )
        with patch.object(h, "_load_finding_by_id", return_value=_recall_finding()), \
             patch.object(h, "_get_ddb", return_value=MagicMock()), \
             patch.object(h, "_dispatch_action", return_value={"status": "executed"}):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"Expected 400 for mismatched server-set recall_id, got {resp['statusCode']}. "
            "_check_server_set_params may not be wired into _approve_finding."
        )
        body = _json.loads(resp.get("body") or "{}")
        assert "recall_id" in body.get("error", ""), (
            f"400 must be from _check_server_set_params (error names 'recall_id'), "
            f"not from an earlier 'not executable' gate. Got error: {body.get('error')!r}"
        )

    def test_approve_with_mismatched_warranty_dealer_id_returns_400(self):
        """Approve with a mismatched dealer_id for submit_warranty_claim returns HTTP 400.

        The handler loads the Finding through _load_finding_by_id; the route is dispatched
        via event['resource']. Reviewer two-line fix: set resource, patch _load_finding_by_id.

        The 400 must name the server-set parameter (parameters.dealer_id), not a generic
        "not executable" 400 that fires before _check_server_set_params is reached.
        """
        from unittest.mock import MagicMock, patch
        import json as _json

        event = self._approve_event(
            finding_id="AVX-FND-ssparams-0002",
            body={
                "action_kind": "submit_warranty_claim",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "ro_id": _RO_ID,
                    "line_no": _LINE_NO,
                    "dealer_id": "dealer-wrong",  # server-set mismatch
                    "labor_op": "LO-EV-BRAKE-001",
                    "fault_code": "C0035",
                },
            },
            claims=_OPERATOR_CLAIMS,
        )
        with patch.object(h, "_load_finding_by_id", return_value=_warranty_finding()), \
             patch.object(h, "_get_ddb", return_value=MagicMock()), \
             patch.object(h, "_dispatch_action", return_value={"status": "executed"}):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"Expected 400 for mismatched dealer_id on submit_warranty_claim, "
            f"got {resp['statusCode']}. M3 guard (approve-route layer)."
        )
        body = _json.loads(resp.get("body") or "{}")
        assert "dealer_id" in body.get("error", ""), (
            f"400 must be from _check_server_set_params (error names 'dealer_id'), "
            f"not from an earlier gate. Got error: {body.get('error')!r}"
        )


    def test_approve_book_recall_body_empty_uses_helper_output(self):
        """Approve with body {} for book_recall_service: helper fills params, decision approved.

        W1: the route must use _check_server_set_params' parameters and decision outputs,
        not just its error. With body {}, the helper fills every server-set and overridable
        field; the route uses those filled params and 'approved' as the decision.

        N10 boundary: helper skips fill-in → Action missing dealer_id.
        N11 boundary: helper counts omission as override → decision 'overridden'.
        N12 boundary: route discards helper output → parameters empty or wrong.
        """
        from unittest.mock import MagicMock, patch
        import json as _json

        captured_action: list = []

        def capture_dispatch(action, claims):
            captured_action.append(action)
            m = MagicMock()
            m.status = "executed"
            m.target_ref = None
            return m

        event = self._approve_event(
            finding_id="AVX-FND-ssparams-0001",
            body={
                "action_kind": "book_recall_service",
                "target_system": "dms",
                "parameters": {},  # all omitted — server fills everything
            },
            claims=_OPERATOR_CLAIMS,
        )
        mock_ddb = MagicMock()
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}
        mock_table.meta.client.exceptions.ConditionalCheckFailedException = Exception
        mock_table.update_item.return_value = {
            "Attributes": {"action_id": "test", "status": "executed"}
        }
        mock_ddb.Table.return_value = mock_table

        with patch.object(h, "_load_finding_by_id", return_value=_recall_finding()), \
             patch.object(h, "_get_ddb", return_value=mock_ddb), \
             patch.object(h, "_dispatch_action", side_effect=capture_dispatch):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"book_recall_service with body {{}} must succeed (server fills all params). "
            f"Got {resp['statusCode']}. Body: {resp.get('body', '')[:300]!r}"
        )
        assert len(captured_action) == 1, "Dispatch must have been called once."
        action = captured_action[0]
        rec_params = _recall_finding()["recommended_action"]["parameters"]
        assert action.get("parameters", {}).get("dealer_id") == rec_params["dealer_id"], (
            f"Action parameters must include dealer_id from the recommendation. "
            f"Got: {action.get('parameters')!r}. "
            "N10 check: skipping fill-in leaves dealer_id absent."
        )
        assert action.get("decision") == "approved", (
            f"Omitting overridable fields must yield decision='approved'. "
            f"Got {action.get('decision')!r}. "
            "N11 check: treating omission as override gives 'overridden'."
        )

    def test_approve_warranty_claim_body_empty_uses_helper_output(self):
        """Approve with body {} for submit_warranty_claim: helper fills params, decision approved.

        W1: the route uses the helper's parameters (all server-set + notes filled from rec)
        and decision='approved' (notes omitted = not an override per call 10).

        N10 boundary: helper skips fill-in → Action missing notes.
        N11 boundary: helper counts omission as override → decision 'overridden'.
        N12 boundary: route discards helper output → wrong parameters or status.
        """
        from unittest.mock import MagicMock, patch
        import json as _json

        captured_action: list = []

        def capture_dispatch(action, claims):
            captured_action.append(action)
            m = MagicMock()
            m.status = "executed"
            m.target_ref = None
            return m

        event = self._approve_event(
            finding_id="AVX-FND-ssparams-0002",
            body={
                "action_kind": "submit_warranty_claim",
                "target_system": "dms",
                "parameters": {},  # all omitted
            },
            claims=_OPERATOR_CLAIMS,
        )
        mock_ddb = MagicMock()
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}
        mock_table.meta.client.exceptions.ConditionalCheckFailedException = Exception
        mock_table.update_item.return_value = {
            "Attributes": {"action_id": "test", "status": "executed"}
        }
        mock_ddb.Table.return_value = mock_table

        with patch.object(h, "_load_finding_by_id", return_value=_warranty_finding()), \
             patch.object(h, "_get_ddb", return_value=mock_ddb), \
             patch.object(h, "_dispatch_action", side_effect=capture_dispatch):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"submit_warranty_claim with body {{}} must succeed. "
            f"Got {resp['statusCode']}. Body: {resp.get('body', '')[:300]!r}"
        )
        assert len(captured_action) == 1, "Dispatch must have been called once."
        action = captured_action[0]
        rec_params = _warranty_finding()["recommended_action"]["parameters"]
        assert action.get("parameters", {}).get("notes") == rec_params["notes"], (
            f"Action parameters must include notes from the recommendation. "
            f"Got: {action.get('parameters', {}).get('notes')!r}. "
            "N10 check: skipping fill-in of overridable notes leaves it absent."
        )
        assert action.get("decision") == "approved", (
            f"Omitting overridable fields must yield decision='approved'. "
            f"Got {action.get('decision')!r}. "
            "N11 check: treating omission as override gives 'overridden'."
        )


# ---------------------------------------------------------------------------
# Security review area B cycle 1, Critical: vehicle_vin is server-set on every Stage C kind
# (spec D11). Every other test sends the server's own VIN, so a helper that trusted a body
# vehicle_vin passed them all; an approve naming vehicle B would then dispatch against B.
# ---------------------------------------------------------------------------

_OTHER_VIN = "MRDVEH00000000099"

_KIND_FINDINGS = [
    ("book_recall_service", _recall_finding),
    ("ground_vehicle", _ground_finding),
    ("submit_warranty_claim", _warranty_finding),
]


class TestVehicleVinServerSet:

    @pytest.mark.parametrize("action_kind,make_finding", _KIND_FINDINGS)
    def test_different_vehicle_vin_returns_error(self, action_kind, make_finding):
        """Body equals the recommendation in every field except vehicle_vin → error."""
        finding = make_finding()
        body = dict(finding["recommended_action"]["parameters"])
        assert body["vehicle_vin"] == _VIN
        body["vehicle_vin"] = _OTHER_VIN
        params, err, decision = h._check_server_set_params(
            finding=finding, action_kind=action_kind, body_parameters=body,
        )
        assert err is not None, f"{action_kind}: a body vehicle_vin for another vehicle must be refused"

    @pytest.mark.parametrize("action_kind,make_finding", _KIND_FINDINGS)
    def test_omitted_vehicle_vin_is_filled_from_the_server(self, action_kind, make_finding):
        """vehicle_vin omitted → filled with the Finding's own VIN, not an override."""
        finding = make_finding()
        body = {k: v for k, v in finding["recommended_action"]["parameters"].items()
                if k != "vehicle_vin"}
        params, err, decision = h._check_server_set_params(
            finding=finding, action_kind=action_kind, body_parameters=body,
        )
        assert err is None
        assert params["vehicle_vin"] == _VIN
        assert decision == "approved"
