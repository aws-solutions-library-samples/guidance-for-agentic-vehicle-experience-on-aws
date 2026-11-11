"""Tests for _check_server_set_params task 3.2 behaviour changes.

Four tests, one per rule introduced in task 3.2. Each test has a must-succeed
control leg followed by a failure-assertion leg.  Mutation targets are documented
inline; each was verified against a git-archive copy in scratch before commit.
"""
from __future__ import annotations

import os
import sys

# Minimal env so the handler module can be imported without live AWS.
os.environ.setdefault("AVX_FINDINGS_TABLE", "vsa-test-avx-findings")
os.environ.setdefault("AVX_ACTIONS_TABLE", "vsa-test-avx-actions")
os.environ.setdefault("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import handler as h  # noqa: E402

_VIN = "MRDVEH00000000015"
_FLEET_ID = "fleet-meridian-01"
_CAMPAIGN_ID = "MRD-2024-001"
_RO_ID = "ro-test-5001"
_LINE_NO = "1"


def _recall_finding(scope_vin: str = _VIN) -> dict:
    """book_recall_service finding with configurable scope vin."""
    return {
        "finding_id": "AVX-FND-g3-0001",
        "finding_kind": "fleet.recall.vehicle",
        "agent_id": "agent-fi-recall-warranty",
        "severity": "p0",
        "fleet_id": _FLEET_ID,
        "scope": {"vin": scope_vin, "fleet_id": _FLEET_ID},
        "recommended_action": {
            "action_kind": "book_recall_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": scope_vin,
                "recall_id": _CAMPAIGN_ID,
                "dealer_id": "dealer-seattle",
            },
        },
    }


def _warranty_finding_no_key(missing_key: str | None = None) -> dict:
    """submit_warranty_claim finding; omit missing_key from rec.parameters."""
    rec_params = {
        "vehicle_vin": _VIN,
        "ro_id": _RO_ID,
        "line_no": _LINE_NO,
        "dealer_id": "dealer-seattle",
        "labor_op": "LO-EV-BRAKE-001",
        "fault_code": "C0035",
        "notes": "Initial writer notes.",
    }
    if missing_key:
        del rec_params[missing_key]
    return {
        "finding_id": "AVX-FND-g3-0002",
        "finding_kind": "fleet.warranty.claim_candidate",
        "agent_id": "agent-fi-recall-warranty",
        "severity": "soon",
        "fleet_id": _FLEET_ID,
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "recommended_action": {
            "action_kind": "submit_warranty_claim",
            "target_system": "dms",
            "parameters": rec_params,
        },
    }


def _ground_finding() -> dict:
    """ground_vehicle finding (body_overridable=())."""
    return {
        "finding_id": "AVX-FND-g3-0003",
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
                "recall_id": _CAMPAIGN_ID,
            },
        },
    }


# ---------------------------------------------------------------------------
# Rule 1 — vehicle_vin is taken from scope.vin, not from rec.parameters
# ---------------------------------------------------------------------------

class TestRule1VinFromScope:
    """_check_server_set_params must source vehicle_vin from finding.scope.vin.

    Mutation target: remove ``if not scope_vin: return …`` so a finding with an
    empty scope never triggers the error → the failure-assertion leg would pass.
    """

    def test_control_scope_vin_used(self):
        """Control: scope.vin present and matching rec; body omits vehicle_vin → success."""
        params, err, decision = h._check_server_set_params(
            finding=_recall_finding(),
            action_kind="book_recall_service",
            body_parameters={},
        )
        assert err is None, f"Unexpected error: {err!r}"
        assert params["vehicle_vin"] == _VIN

    def test_scope_vin_missing_returns_error(self):
        """Failure: finding with no scope.vin → error naming parameters.vehicle_vin.

        Mutant: comment out ``if not scope_vin: return …`` → this assertion fails.
        """
        finding_no_scope = _recall_finding()
        finding_no_scope["scope"] = {}          # vin absent from scope
        params, err, decision = h._check_server_set_params(
            finding=finding_no_scope,
            action_kind="book_recall_service",
            body_parameters={},
        )
        assert err is not None, (
            "scope.vin missing must return an error. "
            "Rule 1 mutant: removing the scope_vin check causes this assertion to fail."
        )
        assert "parameters.vehicle_vin" in err, (
            f"Error must name parameters.vehicle_vin. Got: {err!r}"
        )


# ---------------------------------------------------------------------------
# Rule 2 — server-set key absent in recommendation → error (except fault_code)
# ---------------------------------------------------------------------------

class TestRule2MissingRecValue:
    """_check_server_set_params must refuse a missing recommendation value for server-set keys.

    Mutation target: remove the ``if (rec_val is None or rec_val == "") and key != "fault_code"``
    guard so a finding with ro_id absent from the recommendation silently sets None → the
    failure-assertion leg would pass without an error.
    """

    def test_control_rec_value_present(self):
        """Control: rec has ro_id → success; params["ro_id"] filled from rec."""
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding_no_key(),    # all keys present
            action_kind="submit_warranty_claim",
            body_parameters={},
        )
        assert err is None, f"Unexpected error: {err!r}"
        assert params["ro_id"] == _RO_ID

    def test_missing_ro_id_in_rec_returns_error(self):
        """Failure: recommendation missing ro_id → error naming parameters.ro_id.

        Mutant: remove the missing-rec-value guard → no error returned → this assertion fails.
        """
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding_no_key("ro_id"),  # ro_id absent from rec
            action_kind="submit_warranty_claim",
            body_parameters={},
        )
        assert err is not None, (
            "recommendation missing ro_id must return an error. "
            "Rule 2 mutant: removing the missing-rec guard causes this assertion to fail."
        )
        assert "parameters.ro_id" in err, (
            f"Error must name parameters.ro_id. Got: {err!r}"
        )


# ---------------------------------------------------------------------------
# Rule 3 — unknown body keys are dropped, not passed through
# ---------------------------------------------------------------------------

class TestRule3UnknownKeysDropped:
    """_check_server_set_params must not pass unknown body keys through to params.

    Mutation target: change ``params: dict = {}`` back to ``params = dict(body_parameters)``
    so the unknown key survives into the returned dict → the failure-assertion fails.
    """

    def test_control_known_keys_present(self):
        """Control: body with only valid server-set keys → success and those keys in params."""
        params, err, decision = h._check_server_set_params(
            finding=_ground_finding(),
            action_kind="ground_vehicle",
            body_parameters={"vehicle_vin": _VIN, "recall_id": _CAMPAIGN_ID},
        )
        assert err is None, f"Unexpected error: {err!r}"
        assert params["vehicle_vin"] == _VIN
        assert params["recall_id"] == _CAMPAIGN_ID

    def test_unknown_key_dropped_from_params(self):
        """Failure: unknown body key must be absent from returned params.

        Mutant: initialise params from body_parameters instead of {} → unknown key
        survives → this assertion fails.
        """
        params, err, decision = h._check_server_set_params(
            finding=_ground_finding(),
            action_kind="ground_vehicle",
            body_parameters={
                "vehicle_vin": _VIN,
                "recall_id": _CAMPAIGN_ID,
                "unknown_extra_key": "should-be-dropped",   # not in server_set or body_overridable
            },
        )
        assert err is None, "Unknown key must not cause a 400 error."
        assert "unknown_extra_key" not in params, (
            "Unknown body key must be silently dropped from the returned params. "
            "Rule 3 mutant: using dict(body_parameters) causes this assertion to fail."
        )


# ---------------------------------------------------------------------------
# Rule 4 — overridable values are validated (notes ≤ 1000 chars)
# ---------------------------------------------------------------------------

class TestRule4OverridableValidation:
    """_check_server_set_params must validate body-overridable values.

    Mutation target: remove the ``len(body_val) > 1000`` check for notes so an
    oversized notes value is accepted without error → the failure-assertion fails.
    """

    def test_control_valid_notes_accepted(self):
        """Control: notes within 1000 chars → success; decision is overridden."""
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding_no_key(),
            action_kind="submit_warranty_claim",
            body_parameters={"notes": "x" * 100},
        )
        assert err is None, f"Unexpected error for valid notes: {err!r}"
        assert params["notes"] == "x" * 100

    def test_notes_over_1000_chars_returns_error(self):
        """Failure: notes > 1000 chars → error naming parameters.notes.

        Mutant: remove the len > 1000 guard → no error → this assertion fails.
        """
        params, err, decision = h._check_server_set_params(
            finding=_warranty_finding_no_key(),
            action_kind="submit_warranty_claim",
            body_parameters={"notes": "x" * 1001},
        )
        assert err is not None, (
            "notes longer than 1000 characters must return an error. "
            "Rule 4 mutant: removing the length check causes this assertion to fail."
        )
        assert "parameters.notes" in err, (
            f"Error must name parameters.notes. Got: {err!r}"
        )
