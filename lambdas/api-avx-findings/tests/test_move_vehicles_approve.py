"""Red tests: move_vehicles approve — server-set params, subset rule, VIN validation.

Spec D11, contract § 4. Task 4.2.

All tests are xfail(strict=True, reason="fi-rebalancing-stage-e task 4.2").

Three properties to guard:

  1. Server-set fields refused when they differ (fleet_id, from_location_id,
     forecast_week, and the predicted figures are server-set; a body value
     that differs must return 400).

  2. vehicle_vins must be a non-empty subset of the recommendation's list;
     a strict subset makes the decision 'overridden'.

  3. A VIN outside the recommendation's list returns 400.

The tests call ``handler._check_server_set_params(finding, "move_vehicles", body)``
(Layer 1 isolation), following the test_server_set_params.py pattern.
move_vehicles is not in _check_server_set_params's per-kind table yet → the
function raises KeyError or returns the wrong result → each test fails → xfail.

Layer 2: one test exercises the full approve route to pin that the helper is
actually wired in.

Run:
    .venv/bin/python -m pytest lambdas/api-avx-findings/tests/test_move_vehicles_approve.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup — same pattern as test_server_set_params.py
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

import handler as h  # noqa: E402  — handler.py exists; safe at module level

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FLEET_ID = "fleet-meridian-01"
_FROM_LOC = "loc-sea-downtown-02"
_TO_LOC = "loc-sea-airport-01"
_VIN_A = "MRDVEH00000000021"
_VIN_B = "MRDVEH00000000022"
_VIN_C = "MRDVEH00000000023"
_ISO_WEEK = "2026-W42"
_ACTION_ID = "AVX-ACT-e2000000-0000-4000-8000-000000000002"

_OPERATOR_CLAIMS = {
    "sub": "op-sub-move-approve-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": f"{_FLEET_ID},fleet-meridian-02",
}


def _move_finding(
    fleet_id: str = _FLEET_ID,
    from_location_id: str = _FROM_LOC,
    to_location_id: str = _TO_LOC,
    vehicle_vins: list[str] | None = None,
    forecast_week: str = _ISO_WEEK,
    transfer_cost_usd: float = 84.06,
    predicted_hours_gain: float = 35.0,
    predicted_turnaways_avoided: int = 2,
) -> dict:
    """Return a minimal fleet.rebalance.move Finding fixture."""
    vins = vehicle_vins if vehicle_vins is not None else [_VIN_A, _VIN_B]
    return {
        "finding_id": "AVX-FND-move-approve-0001",
        "finding_kind": "fleet.rebalance.move",
        "agent_id": "agent-fi-rebalancing",
        "severity": "soon",
        "fleet_id": fleet_id,
        "scope": {"fleet_id": fleet_id},
        "location": {
            "from_location_id": from_location_id,
            "to_location_id": to_location_id,
        },
        "subject_key": f"{from_location_id}/{to_location_id}/{forecast_week}",
        "recommended_action": {
            "action_kind": "move_vehicles",
            "target_system": "cms",
            "parameters": {
                "fleet_id": fleet_id,
                "from_location_id": from_location_id,
                "to_location_id": to_location_id,
                "vehicle_vins": vins,
                "forecast_week": forecast_week,
                "transfer_cost_usd": transfer_cost_usd,
                "predicted_on_rent_hours_gain": predicted_hours_gain,
                "predicted_turnaways_avoided": predicted_turnaways_avoided,
            },
        },
    }


# ---------------------------------------------------------------------------
# Layer 1: _check_server_set_params for move_vehicles
# ---------------------------------------------------------------------------


class TestMoveVehiclesServerSetParams:
    """Server-set fields for move_vehicles are refused when the body differs."""

    @pytest.mark.parametrize("field", ["fleet_id", "from_location_id", "to_location_id"])
    def test_recommendation_that_disagrees_with_the_finding_is_refused(self, field):
        """Security S3 (Group 4 security cycle 1): a recommendation naming a different fleet or
        location than the Finding's own scope/location is refused, even with an empty body.

        A matching Finding is approved first (control), so the refusal is this rule's.
        """
        _, ok_err, _ = h._check_server_set_params(
            finding=_move_finding(), action_kind="move_vehicles", body_parameters={})
        assert ok_err is None, ok_err
        finding = _move_finding()
        finding["recommended_action"]["parameters"][field] = "drifted-value-99"
        _, err, _ = h._check_server_set_params(
            finding=finding, action_kind="move_vehicles", body_parameters={})
        assert err is not None and field in err and "does not match the Finding" in err, err

    def test_different_fleet_id_returns_error(self):
        """fleet_id is server-set: a different body value must return an error.

        Mutation: remove fleet_id from the move_vehicles server-set list → different
        fleet_id passes without error → this assertion fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(),
            action_kind="move_vehicles",
            body_parameters={
                "fleet_id": "fleet-different-99",  # differs from server value
                "vehicle_vins": [_VIN_A],
            },
        )
        assert err is not None, (
            "fleet_id is server-set for move_vehicles; a different body value must error."
        )

    def test_different_from_location_id_returns_error(self):
        """from_location_id is server-set: a different body value must return an error.

        Mutation: remove from_location_id from the server-set list → body override accepted
        → no error → this assertion fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(),
            action_kind="move_vehicles",
            body_parameters={
                "from_location_id": "loc-sea-wrong-09",  # differs
                "vehicle_vins": [_VIN_A],
            },
        )
        assert err is not None, (
            "from_location_id is server-set; a different body value must error."
        )

    def test_different_forecast_week_returns_error(self):
        """forecast_week is server-set: a different body value must return an error.

        Mutation: remove forecast_week from the server-set list → body can change the
        week → no error → this assertion fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(),
            action_kind="move_vehicles",
            body_parameters={
                "forecast_week": "2026-W99",  # differs
                "vehicle_vins": [_VIN_A],
            },
        )
        assert err is not None, (
            "forecast_week is server-set; a different body value must error."
        )

    def test_different_to_location_id_returns_error(self):
        """to_location_id is server-set: a different body value must return an error.

        Mutation: remove to_location_id from the server-set list → caller can redirect
        the move to a different destination → no error → assertion fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(),
            action_kind="move_vehicles",
            body_parameters={
                "to_location_id": "loc-sea-wrong-destination",  # differs
                "vehicle_vins": [_VIN_A],
            },
        )
        assert err is not None, (
            "to_location_id is server-set; a different body value must error."
        )

    def test_different_transfer_cost_usd_returns_error(self):
        """transfer_cost_usd is server-set (pre-scale): a mismatching body value must error.

        Mutation: remove transfer_cost_usd from the server-set list → caller can
        supply a false cost → no error → assertion fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(transfer_cost_usd=84.06),
            action_kind="move_vehicles",
            body_parameters={
                "transfer_cost_usd": 1.00,  # differs from 84.06
                "vehicle_vins": [_VIN_A, _VIN_B],
            },
        )
        assert err is not None, (
            "transfer_cost_usd is server-set; a different body value must error."
        )

    def test_transfer_cost_usd_check_uses_pre_scale_basis(self):
        """transfer_cost_usd is compared against the pre-scale rec value, not post-scale.

        Contract § 4: for a strict subset, the scaled basis is ``rec.transfer_cost_usd × scale``.
        A body that echoes back the *post-scale* value must therefore error (it matches the
        post-scale basis but not the pre-scale rec).

        Mutation N6b: a handler that compares against ``rec.transfer_cost_usd × scale``
        (post-scale) would accept ``42.03`` for a 1-of-2 subset and NOT error → this
        assertion fails → N6b caught.

        Mutation N6a (positive control / pre-scale): a handler that compares against
        ``rec.transfer_cost_usd`` (pre-scale) would reject ``42.03 ≠ 84.06`` and return an
        error → assertion passes → N6a is the correct behaviour and this test enforces it.
        """
        # 1-of-2 subset; post-scale value for transfer_cost_usd = 84.06 × 0.5 = 42.03.
        # The pre-scale rec value is still 84.06.  A body that echoes the post-scale
        # figure (42.03) must be rejected because the server-set check compares against the
        # unscaled recommendation.
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(vehicle_vins=[_VIN_A, _VIN_B], transfer_cost_usd=84.06),
            action_kind="move_vehicles",
            body_parameters={
                "transfer_cost_usd": 42.03,  # post-scale value; differs from pre-scale 84.06
                "vehicle_vins": [_VIN_A],    # strict subset → scale = 0.5
            },
        )
        assert err is not None, (
            "A body transfer_cost_usd matching the post-scale figure must error: the "
            "comparison basis is pre-scale (rec.parameters.transfer_cost_usd = 84.06). "
            "Mutation N6b (post-scale compare): 42.03 == 84.06×0.5 passes → no error → fails."
        )

        # Acceptance half: a body that echoes back the pre-scale rec value must be accepted.
        # N9 (reject on presence): rejects 84.06 regardless of value → err2 is not None
        # → assert err2 is None fails → N9 caught.
        _, err2, _ = h._check_server_set_params(
            finding=_move_finding(vehicle_vins=[_VIN_A, _VIN_B], transfer_cost_usd=84.06),
            action_kind="move_vehicles",
            body_parameters={
                "transfer_cost_usd": 84.06,  # pre-scale rec value — must be accepted
                "vehicle_vins": [_VIN_A],    # strict subset → scale = 0.5
            },
        )
        assert err2 is None, (
            "A body transfer_cost_usd matching the pre-scale rec value (84.06) must be "
            "accepted (contract § 4: comparison basis is pre-scale). "
            "Mutation N9 (reject on presence): returns error for 84.06 → err2 not None → fails."
        )

    def test_different_predicted_on_rent_hours_gain_returns_error(self):
        """predicted_on_rent_hours_gain is server-set: a mismatching body value must error.

        Mutation: remove predicted_on_rent_hours_gain from server-set list → approval
        UI shows a false gain figure → no error → assertion fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(predicted_hours_gain=35.0),
            action_kind="move_vehicles",
            body_parameters={
                "predicted_on_rent_hours_gain": 9999.0,  # differs
                "vehicle_vins": [_VIN_A, _VIN_B],
            },
        )
        assert err is not None, (
            "predicted_on_rent_hours_gain is server-set; a different body value must error."
        )

    def test_different_predicted_turnaways_avoided_returns_error(self):
        """predicted_turnaways_avoided is server-set: a mismatching body value must error.

        Mutation: remove predicted_turnaways_avoided from server-set list → approval
        UI shows a false turn-away reduction figure → no error → assertion fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(predicted_turnaways_avoided=2),
            action_kind="move_vehicles",
            body_parameters={
                "predicted_turnaways_avoided": 0,  # differs from 2
                "vehicle_vins": [_VIN_A, _VIN_B],
            },
        )
        assert err is not None, (
            "predicted_turnaways_avoided is server-set; a different body value must error."
        )


# ---------------------------------------------------------------------------
# Layer 1: vehicle_vins subset rule
# ---------------------------------------------------------------------------


class TestMoveVehiclesSubsetRule:
    """vehicle_vins must be a non-empty subset; a strict subset yields 'overridden'."""

    def test_strict_subset_yields_overridden(self):
        """Sending a strict subset of vehicle_vins makes decision == 'overridden', and
        the three predicted figures and transfer cost are scaled by the subset share.

        Warning W5 fix: the scaled values are asserted so contract § 4's
        ``scale = len(body_vins) / len(rec_vins)`` rule is load-bearing.  A 1-of-2 subset
        must halve the three figures; an implementation that returns the full recommended
        values fails here.

        Mutation: return 'approved' for any non-empty subset → decision wrong → fails.
        Mutation: return full values without scaling → scaled assertions fail → fails.
        """
        params, err, decision = h._check_server_set_params(
            finding=_move_finding(vehicle_vins=[_VIN_A, _VIN_B]),
            action_kind="move_vehicles",
            body_parameters={"vehicle_vins": [_VIN_A]},  # strict subset: one of two
        )
        assert err is None, "A strict subset is not an error."
        assert decision == "overridden", (
            f"A strict subset must yield decision='overridden', got {decision!r}. "
            "Mutation: return 'approved' for any non-empty subset → wrong decision → fails."
        )
        # scale = 1/2; check all three scaled fields.
        assert params is not None, "params must be returned with decision='overridden'."
        _TOL = 0.02
        assert abs(params.get("transfer_cost_usd", float("nan")) - 42.03) <= _TOL, (
            f"transfer_cost_usd must be scaled by 0.5: 84.06 → 42.03, "
            f"got {params.get('transfer_cost_usd')!r}. "
            "Mutation: return unscaled value → 84.06 ≠ 42.03 → fails."
        )
        assert abs(params.get("predicted_on_rent_hours_gain", float("nan")) - 17.50) <= _TOL, (
            f"predicted_on_rent_hours_gain must be scaled by 0.5: 35.0 → 17.5, "
            f"got {params.get('predicted_on_rent_hours_gain')!r}."
        )
        assert params.get("predicted_turnaways_avoided") == 1, (
            f"predicted_turnaways_avoided must be scaled (floor): 2 × 0.5 = 1, "
            f"got {params.get('predicted_turnaways_avoided')!r}."
        )

    def test_exact_vehicle_vins_yields_approved(self):
        """Sending exactly the recommendation's vehicle_vins yields decision == 'approved'.

        Positive control: if strict_subset also yields 'approved', this test distinguishes
        them (subset → overridden; exact match → approved).
        """
        params, err, decision = h._check_server_set_params(
            finding=_move_finding(vehicle_vins=[_VIN_A, _VIN_B]),
            action_kind="move_vehicles",
            body_parameters={"vehicle_vins": [_VIN_A, _VIN_B]},  # exact match
        )
        assert err is None
        assert decision == "approved", (
            f"Exact vehicle_vins must yield 'approved', got {decision!r}."
        )

    def test_empty_vehicle_vins_returns_error(self):
        """An empty vehicle_vins list is not a valid subset and must return an error.

        Contract § 4: 'body vehicle_vins must be a non-empty subset → 400 otherwise'.
        Mutation: allow empty list → no error → assertion fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(),
            action_kind="move_vehicles",
            body_parameters={"vehicle_vins": []},
        )
        assert err is not None, (
            "An empty vehicle_vins list must return an error (contract § 4). "
            "Mutation: allow empty list → no error → this fails."
        )

    def test_vin_outside_recommendation_list_returns_400_error(self):
        """A VIN not in the recommendation's list must return an error.

        contract § 4: vehicle_vins must be a subset of the recommendation's list.
        Mutation: only check that vins are non-empty, not that they are in the list
        → foreign VIN accepted → assertion fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(vehicle_vins=[_VIN_A, _VIN_B]),
            action_kind="move_vehicles",
            body_parameters={
                "vehicle_vins": [_VIN_C],  # VIN_C not in recommendation
            },
        )
        assert err is not None, (
            "A VIN outside the recommendation's list must return an error. "
            "Mutation: remove the in-list check → foreign VIN accepted → this fails."
        )


# ---------------------------------------------------------------------------
# Layer 2: approve route wiring — the 400 must come from the route
# ---------------------------------------------------------------------------


class TestMoveVehiclesApproveRouteWiring:
    """The approve route calls _check_server_set_params for move_vehicles and returns 400."""

    def _approve_event(self, finding_id: str, body: dict, claims: dict) -> dict:
        return {
            "httpMethod": "POST",
            "resource": "/findings/{finding_id}/approve",
            "path": f"/findings/{finding_id}/approve",
            "pathParameters": {"finding_id": finding_id},
            "headers": {"Authorization": "Bearer fake-jwt"},
            "requestContext": {"authorizer": {"claims": claims}},
            "body": json.dumps(body),
        }

    def test_approve_with_vin_outside_list_returns_400(self):
        """Full approve route: VIN outside recommendation list → HTTP 400.

        The 400 must originate from the server-set/subset check, not from an
        earlier 'not executable' gate (which would fire before the VIN check).
        Mutation: omit the VIN check from the route → 400 not returned for
        foreign VINs → assertion fails.
        """
        from unittest.mock import MagicMock, patch

        finding = _move_finding(vehicle_vins=[_VIN_A, _VIN_B])

        # Control leg: an in-list subset must reach dispatch. Without it, any earlier 400
        # passes the assertions below, and one did: after main gained the "action_kind
        # 'move_vehicles' is not executable in this deployment" gate, its text contains
        # "vehicle" and the test XPASSed (merge of main, 2026-10-06).
        control_event = self._approve_event(
            finding_id=finding["finding_id"],
            body={
                "action_kind": "move_vehicles",
                "target_system": "cms",
                "parameters": {"vehicle_vins": [_VIN_A]},
            },
            claims=_OPERATOR_CLAIMS,
        )
        with (
            patch.object(h, "_load_finding_by_id", return_value=finding),
            patch.object(h, "_get_ddb", return_value=MagicMock()),
            # The handler records an ExecutorResult (`audit_entry()`), so stub the recorder
            # too; a dict result would crash after dispatch and hide the control's outcome.
            patch.object(h, "_dispatch_action", return_value=MagicMock()) as dispatch,
            patch.object(h, "_record_dispatch_result", return_value={}),
        ):
            control = h.handler(control_event, None)
        assert control["statusCode"] != 400 and dispatch.called, (
            f"Control leg: an in-list subset must reach dispatch; got {control['statusCode']} "
            f"{control.get('body')!r}"
        )

        event = self._approve_event(
            finding_id=finding["finding_id"],
            body={
                "action_kind": "move_vehicles",
                "target_system": "cms",
                "parameters": {
                    "vehicle_vins": [_VIN_C],  # not in recommendation
                },
            },
            claims=_OPERATOR_CLAIMS,
        )
        with (
            patch.object(h, "_load_finding_by_id", return_value=finding),
            patch.object(h, "_get_ddb", return_value=MagicMock()),
            patch.object(h, "_dispatch_action", return_value=MagicMock()),
            patch.object(h, "_record_dispatch_result", return_value={}),
        ):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"Expected HTTP 400 for a VIN outside the recommendation list, "
            f"got {resp['statusCode']}. "
            "The route must call _check_server_set_params for move_vehicles."
        )
        body = json.loads(resp.get("body") or "{}")
        error_text = body.get("error", "") or body.get("message", "")
        assert "not executable" not in error_text and (
            _VIN_C in error_text or "vehicle_vins" in error_text
        ), (
            f"400 must come from the subset rule and name the foreign VIN or `vehicle_vins`. "
            f"Got: {error_text!r}. Mutation: a generic or gate 400 -> this fails."
        )
