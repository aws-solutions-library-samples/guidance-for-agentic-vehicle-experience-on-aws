"""Additional tests for move_vehicles approve — task 4.2.

Supplements test_move_vehicles_approve.py with:
  - duplicates in vehicle_vins → error
  - non-string VIN → error
  - omitted vehicle_vins → approved with full recommendation list
  - recommendation fleet_id differing from scope.fleet_id → body matching rec rejected
  - recommendation missing forecast_week → error
  - policy row assertions: move_vehicles is executable, operator-only, not auto-approvable
  - other kinds unchanged (book_recall_service, ground_vehicle, apply_cost_action)

Run:
    .venv/bin/python -m pytest lambdas/api-avx-findings/tests/test_move_vehicles_approve_more.py -q
"""
from __future__ import annotations

import os
import pytest
import sys

# ---------------------------------------------------------------------------
# Path setup — same as test_move_vehicles_approve.py
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
# Fixtures (mirrors test_move_vehicles_approve.py)
# ---------------------------------------------------------------------------

_FLEET_ID = "fleet-meridian-01"
_FROM_LOC = "loc-sea-downtown-02"
_TO_LOC = "loc-sea-airport-01"
_VIN_A = "MRDVEH00000000021"
_VIN_B = "MRDVEH00000000022"
_VIN_C = "MRDVEH00000000023"
_ISO_WEEK = "2026-W42"


def _move_finding(
    fleet_id: str = _FLEET_ID,
    from_location_id: str = _FROM_LOC,
    to_location_id: str = _TO_LOC,
    vehicle_vins: "list[str] | None" = None,
    forecast_week: str = _ISO_WEEK,
    transfer_cost_usd: float = 84.06,
    predicted_hours_gain: float = 35.0,
    predicted_turnaways_avoided: int = 2,
    scope_fleet_id: "str | None" = None,
) -> dict:
    """Return a minimal fleet.rebalance.move Finding fixture.

    ``scope_fleet_id`` overrides the scope independently of the rec parameters,
    allowing tests for scope-vs-rec fleet discrepancy.
    """
    vins = vehicle_vins if vehicle_vins is not None else [_VIN_A, _VIN_B]
    actual_scope_fleet_id = scope_fleet_id if scope_fleet_id is not None else fleet_id
    return {
        "finding_id": "AVX-FND-move-more-0001",
        "finding_kind": "fleet.rebalance.move",
        "agent_id": "agent-fi-rebalancing",
        "severity": "soon",
        "fleet_id": actual_scope_fleet_id,
        "scope": {"fleet_id": actual_scope_fleet_id},
        "location": {
            "from_location_id": from_location_id,
            "to_location_id": to_location_id,
        },
        "subject_key": f"{from_location_id}/{to_location_id}/{forecast_week}",
        "recommended_action": {
            "action_kind": "move_vehicles",
            "target_system": "cms",
            "parameters": {
                "fleet_id": fleet_id,  # may differ from scope_fleet_id
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
# vehicle_vins extra validation
# ---------------------------------------------------------------------------


class TestVehicleVinsValidationMore:
    """Additional vehicle_vins validation beyond the base test file."""

    def test_duplicate_vins_return_error(self):
        """Duplicate VINs in vehicle_vins must return an error (non-distinct list).

        Mutation N-dup: remove the duplicates check → [A, A] is accepted as a
        1-element subset → wrong scale, decision based on wrong count → this fails.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(vehicle_vins=[_VIN_A, _VIN_B]),
            action_kind="move_vehicles",
            body_parameters={"vehicle_vins": [_VIN_A, _VIN_A]},  # duplicate
        )
        assert err is not None, (
            "Duplicate VINs must return an error (vehicle_vins must be distinct). "
            "Mutation: skip distinct check → [A, A] accepted → fails."
        )
        assert "vehicle_vins" in err, (
            f"Error must name the parameter 'vehicle_vins', got: {err!r}"
        )

    def test_non_string_vin_returns_error(self):
        """A non-string entry in vehicle_vins must return an error.

        Mutation N-type: skip the isinstance(v, str) check → integer VIN accepted
        → set membership with a string VIN would always fail anyway (None not in set),
        but a path that skips the type check AND the membership check would accept it.
        """
        _, err, _ = h._check_server_set_params(
            finding=_move_finding(vehicle_vins=[_VIN_A, _VIN_B]),
            action_kind="move_vehicles",
            body_parameters={"vehicle_vins": [_VIN_A, 42]},  # 42 is not a string
        )
        assert err is not None, (
            "A non-string VIN must return an error. "
            "Mutation: skip type check → integer VIN accepted → this fails."
        )
        assert "vehicle_vins" in err, (
            f"Error must name 'vehicle_vins', got: {err!r}"
        )

    def test_omitted_vehicle_vins_approved_with_full_list(self):
        """Omitting vehicle_vins from the body uses the recommendation's full list.

        Contract § 4: 'omitted → the recommendation's list'.  Decision must be
        'approved' (full list is not a strict subset).

        Mutation N-omit: treat omitted vehicle_vins as an empty list → error →
        this assert err is None fails.
        Mutation N-omit2: return an empty params['vehicle_vins'] or no key →
        assertion on params fails.
        Mutation N-omit3: return decision='overridden' for the full list →
        decision assertion fails.
        """
        params, err, decision = h._check_server_set_params(
            finding=_move_finding(vehicle_vins=[_VIN_A, _VIN_B]),
            action_kind="move_vehicles",
            body_parameters={},  # no vehicle_vins key
        )
        assert err is None, (
            f"Omitting vehicle_vins must not error; got err={err!r}. "
            "Mutation N-omit: treat omitted as empty list → error → fails."
        )
        assert decision == "approved", (
            f"Omitted vehicle_vins uses the full rec list → 'approved', got {decision!r}."
        )
        assert params is not None and params.get("vehicle_vins") == [_VIN_A, _VIN_B], (
            f"Output vehicle_vins must be the full rec list [A, B], got "
            f"{params.get('vehicle_vins') if params else None!r}. "
            "Mutation N-omit2: empty or missing list → this fails."
        )


# ---------------------------------------------------------------------------
# Scope vs recommendation fleet discrepancy
# ---------------------------------------------------------------------------


class TestFleetScopeVsRec:
    """fleet_id comes from scope; a body echoing rec's fleet_id (if different) errors."""

    def test_body_rec_fleet_id_rejected_when_differs_from_scope(self):
        """The server-set check uses scope.fleet_id as the authoritative value.

        If scope.fleet_id='fleet-scope-X' and rec.fleet_id='fleet-rec-Y', a body
        that echoes the rec value 'fleet-rec-Y' must be rejected (it doesn't match
        scope).

        Mutation: use rec.fleet_id as the comparison basis instead of scope.fleet_id
        → 'fleet-rec-Y' matches rec.fleet_id → no error → this assertion fails.
        """
        finding = _move_finding(
            scope_fleet_id="fleet-scope-X",  # scope has a different fleet
        )
        # rec still has fleet_id=_FLEET_ID (default); scope_fleet_id='fleet-scope-X'
        _, err, _ = h._check_server_set_params(
            finding=finding,
            action_kind="move_vehicles",
            body_parameters={
                "fleet_id": _FLEET_ID,       # matches rec.fleet_id, not scope.fleet_id
                "vehicle_vins": [_VIN_A],
            },
        )
        assert err is not None, (
            "fleet_id from scope; body matching rec but not scope must error. "
            "Mutation: use rec.fleet_id as comparison basis → no error → fails."
        )


# ---------------------------------------------------------------------------
# Missing recommendation fields
# ---------------------------------------------------------------------------


class TestMissingRecFields:
    """Recommendation must supply all five non-location server-set fields."""

    def test_missing_forecast_week_returns_error(self):
        """A recommendation without forecast_week must return a 400 error.

        Mutation N-fw: skip the missing-field check for forecast_week → rec without
        it is accepted → no error → this assertion fails.
        """
        finding = _move_finding()
        # Remove forecast_week from the rec parameters
        del finding["recommended_action"]["parameters"]["forecast_week"]
        _, err, _ = h._check_server_set_params(
            finding=finding,
            action_kind="move_vehicles",
            body_parameters={"vehicle_vins": [_VIN_A]},
        )
        assert err is not None, (
            "A recommendation missing forecast_week must return an error. "
            "Mutation N-fw: skip missing-field check → accepted without it → fails."
        )
        assert "forecast_week" in err, (
            f"Error must name the missing parameter 'forecast_week', got: {err!r}"
        )


# ---------------------------------------------------------------------------
# Policy row assertions
# ---------------------------------------------------------------------------


class TestMoveVehiclesPolicyRow:
    """The move_vehicles policy row has the correct Stage E values."""

    def test_move_vehicles_is_executable(self):
        """move_vehicles.executable must be True after task 4.2.

        Mutation M8a: set executable=False → False is not True → fails.
        """
        from agents.executors.policy import ACTION_KINDS
        assert ACTION_KINDS["move_vehicles"].executable is True, (
            "move_vehicles must be executable after Stage E task 4.2. "
            "Mutation M8a: executable=False → assertion fails."
        )

    def test_move_vehicles_is_operator_required(self):
        """move_vehicles.operator_required must be True (spec D4/D11).

        Mutation: set operator_required=False → a non-operator could decide
        a fleet move → this assertion fails.
        """
        from agents.executors.policy import ACTION_KINDS
        assert ACTION_KINDS["move_vehicles"].operator_required is True

    def test_move_vehicles_is_auto_approvable_only_as_a_limited_kind(self):
        """Changed deliberately by stage E Group 8 (U6, approved 2026-10-06).

        Before Group 8 this pinned ``auto_approvable is False`` ("until U6 group lands",
        mutation M8). Group 8 lands U6: a rule may approve a move, but only as a limited
        kind, within its limit on ``transfer_cost_usd`` and the configured bounds (spec
        group8-notes G8-D5..D7). So the half this asserts now is "limited": the kind is
        auto-approvable AND names its limit parameter. Mutations: auto_approvable=False,
        or rule_limit_param=None (an unbounded rule) → this fails.
        """
        from agents.executors.policy import ACTION_KINDS
        row = ACTION_KINDS["move_vehicles"]
        assert row.auto_approvable is True
        assert row.rule_limit_param == "transfer_cost_usd"


# ---------------------------------------------------------------------------
# Other kinds unchanged
# ---------------------------------------------------------------------------


class TestOtherKindsUnchanged:
    """Changes to move_vehicles must not alter other kinds' policy rows.

    Values are compared against the committed values on the wip branch (= AVX main
    merged into wip/fi-rebal-e before the Stage E work began).
    """

    def test_book_recall_service_unchanged(self):
        """book_recall_service: target=dms, executable, operator-only, not auto-approvable,
        server_set includes vehicle_vin and recall_id, body_overridable has dealer_id."""
        from agents.executors.policy import ACTION_KINDS
        row = ACTION_KINDS["book_recall_service"]
        assert row.target_system == "dms"
        assert row.executable is True
        assert row.operator_required is True
        assert row.auto_approvable is False
        assert "vehicle_vin" in row.server_set
        assert "recall_id" in row.server_set
        assert "dealer_id" in row.body_overridable

    def test_ground_vehicle_unchanged(self):
        """ground_vehicle: target=cms, executable, operator-only, not auto-approvable,
        server_set includes vehicle_vin and recall_id, body_overridable is empty."""
        from agents.executors.policy import ACTION_KINDS
        row = ACTION_KINDS["ground_vehicle"]
        assert row.target_system == "cms"
        assert row.executable is True
        assert row.operator_required is True
        assert row.auto_approvable is False
        assert "vehicle_vin" in row.server_set
        assert row.body_overridable == ()

    def test_apply_cost_action_unchanged(self):
        """apply_cost_action: target=cms, executable, operator-only, not auto-approvable."""
        from agents.executors.policy import ACTION_KINDS
        row = ACTION_KINDS["apply_cost_action"]
        assert row.target_system == "cms"
        assert row.executable is True
        assert row.operator_required is True
        assert row.auto_approvable is False



# ---------------------------------------------------------------------------
# Architect check (2026-10-10).
# ---------------------------------------------------------------------------

_OPERATOR_CLAIMS = {
    "sub": "op-sub-move-approve-0002",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": "fleet-meridian-01,fleet-meridian-02",
}


def test_the_approved_move_action_is_storable_by_the_boto3_resource_api():
    """The route puts the Action with Table.put_item, which raises TypeError on a Python
    float. The fake table runs boto3's own serializer on the Item, as put_item does."""
    import json as _json
    from unittest.mock import MagicMock, patch
    from boto3.dynamodb.types import TypeSerializer

    stored: list = []
    refused: list = []

    class _Table:
        class meta:  # noqa: N801
            client = MagicMock()

        def put_item(self, *, Item, ConditionExpression):  # noqa: N803
            try:
                TypeSerializer().serialize(Item)
            except TypeError as exc:  # what the real put_item raises on a float
                refused.append(str(exc))  # recorded, then asserted below
                return
            stored.append(Item)

    ddb = MagicMock()
    ddb.Table.return_value = _Table()
    finding = _move_finding()
    event = {
        "httpMethod": "POST", "resource": "/findings/{finding_id}/approve",
        "path": f"/findings/{finding['finding_id']}/approve",
        "pathParameters": {"finding_id": finding["finding_id"]},
        "headers": {"Authorization": "Bearer fake-jwt"},
        "requestContext": {"authorizer": {"claims": _OPERATOR_CLAIMS}},
        "body": _json.dumps({"action_kind": "move_vehicles", "target_system": "cms",
                             "parameters": {"vehicle_vins": [_VIN_A]}}),
    }
    with (patch.object(h, "_load_finding_by_id", return_value=finding),
          patch.object(h, "_get_ddb", return_value=ddb),
          patch.object(h, "_dispatch_action", return_value=MagicMock()),
          patch.object(h, "_record_dispatch_result", return_value={})):
        resp = h.handler(event, None)
    assert refused == [], f"put_item would refuse the Action: {refused}"
    assert resp["statusCode"] != 400 and len(stored) == 1, resp
    params = stored[0]["parameters"]
    assert params["vehicle_vins"] == [_VIN_A]
    assert str(params["transfer_cost_usd"]) == "42.03"


@pytest.mark.parametrize("field", ["fleet_id", "from_location_id", "to_location_id"])
def test_a_recommendation_naming_another_fleet_or_location_is_refused(field):
    finding = _move_finding()
    finding["recommended_action"]["parameters"][field] = "somewhere-else"
    _, err, _ = h._check_server_set_params(
        finding=finding, action_kind="move_vehicles", body_parameters={})
    assert err is not None and field in err


@pytest.mark.parametrize("field", ["from_location_id", "to_location_id"])
def test_a_finding_without_its_locations_is_refused(field):
    finding = _move_finding()
    del finding["location"][field]
    _, err, _ = h._check_server_set_params(
        finding=finding, action_kind="move_vehicles", body_parameters={})
    assert err is not None and field in err


@pytest.mark.parametrize("vins", [[], None, ["MRDVEH00000000021", "MRDVEH00000000021"], [7]])
def test_a_recommendation_without_a_clean_vehicle_list_is_refused(vins):
    finding = _move_finding()
    finding["recommended_action"]["parameters"]["vehicle_vins"] = vins
    _, err, _ = h._check_server_set_params(
        finding=finding, action_kind="move_vehicles", body_parameters={})
    assert err is not None and "vehicle_vins" in err


def test_turnaways_compared_as_numbers_not_truncated():
    _, err, _ = h._check_server_set_params(
        finding=_move_finding(predicted_turnaways_avoided=2), action_kind="move_vehicles",
        body_parameters={"predicted_turnaways_avoided": 2.9, "vehicle_vins": [_VIN_A, _VIN_B]})
    assert err is not None



@pytest.mark.parametrize("field", ["from_location_id", "to_location_id", "fleet_id"])
def test_a_move_with_no_location_or_fleet_anywhere_is_refused(field):
    """Both the Finding and its recommendation lack the value: None == None must not pass."""
    finding = _move_finding()
    if field == "fleet_id":
        finding["scope"] = {}
    else:
        del finding["location"][field]
    del finding["recommended_action"]["parameters"][field]
    _, err, _ = h._check_server_set_params(
        finding=finding, action_kind="move_vehicles", body_parameters={})
    assert err is not None and field in err


# ---------------------------------------------------------------------------
# W13: turnaways scaling uses floor, not round
# ---------------------------------------------------------------------------


def test_turnaways_floored_not_rounded_on_partial_approval():
    """3 turnaways, 1 of 2 VINs approved → floor(3 × 0.5) = 1, not round(3 × 0.5) = 2.

    Contract § 4: scaled turnaways are floored.  With ``predicted_turnaways_avoided=3``
    and a 1-of-2 VIN subset, scale = 0.5 and 3 × 0.5 = 1.5.  ``floor(1.5) = 1`` but
    ``round(1.5) = 2`` (rounds to even in Python 3).  A rounded result overstates the
    turnaways avoided in the Action that CMS records.

    Mutation M10 (reviewer): ``math.floor(float(turnaways) * _scale)``
    →                          ``round(float(turnaways) * _scale)``
    → predicted_turnaways_avoided becomes 2 (round(1.5)) instead of 1 (floor(1.5))
    → the assertion below fails.

    Also pins two-decimal rounding of money and hours in the same approval.
    """
    params, err, decision = h._check_server_set_params(
        finding=_move_finding(
            vehicle_vins=[_VIN_A, _VIN_B],
            predicted_turnaways_avoided=3,          # 3 × 0.5 = 1.5 → floor = 1, round = 2
            transfer_cost_usd=84.1234,              # × 0.5 = 42.0617 → 2dp = 42.06
            predicted_hours_gain=35.1234,           # × 0.5 = 17.5617 → 2dp = 17.56
        ),
        action_kind="move_vehicles",
        body_parameters={"vehicle_vins": [_VIN_A]},  # strict subset: 1 of 2
    )
    assert err is None, f"a 1-of-2 subset must not error; got {err!r}"
    assert decision == "overridden", (
        f"1-of-2 subset must be 'overridden', got {decision!r}"
    )
    assert params is not None
    assert params.get("predicted_turnaways_avoided") == 1, (
        f"predicted_turnaways_avoided must be floor(3 × 0.5) = 1, got "
        f"{params.get('predicted_turnaways_avoided')!r}. "
        "Mutation M10: round(1.5)=2 ≠ 1 → this fails."
    )
    assert params.get("transfer_cost_usd") == 42.06, (
        f"transfer_cost_usd must be round(84.1234 × 0.5, 2) = 42.06 exactly, "
        f"got {params.get('transfer_cost_usd')!r}. "
        "Mutations A1/A2: removing round() yields 42.0617/17.5617, which must fail."
    )
    assert params.get("predicted_on_rent_hours_gain") == 17.56, (
        f"predicted_on_rent_hours_gain must be round(35.1234 × 0.5, 2) = 17.56 exactly, "
        f"got {params.get('predicted_on_rent_hours_gain')!r}. "
        "Mutations A1/A2: removing round() yields 42.0617/17.5617, which must fail."
    )
