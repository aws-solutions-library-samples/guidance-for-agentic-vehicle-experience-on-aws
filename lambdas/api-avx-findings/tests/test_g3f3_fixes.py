"""G3F3.1 Fix Group G3-3 tests for lambdas/api-avx-findings/handler.py.

Covers:
  - c W3 (design answer 9): supplying a dealer_id the recommendation lacks
    is 'approved', not 'overridden'. Probe: probe_p0_no_rooftop.py in
    /tmp/2026-10-05-fi-fleet-health-stage-d-g3-rev3c/ confirmed the route
    returned 'overridden' before the fix.
"""
from __future__ import annotations

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup — consistent with the rest of the api-avx-findings test files
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
# c W3: p0 booking with no rooftop → operator supplies dealer_id → approved
# ---------------------------------------------------------------------------

class TestG3F3P0NoRooftopApproved:
    """G3F3.1 (c W3): supplying a dealer_id the recommendation lacks is 'approved'.

    Design answer 9: "The recommendation carries no dealer_id; the operator's
    body supplies one, and filling a value the recommendation lacks is approved,
    not overridden."

    Probe from /tmp/.../rev3c/probe_p0_no_rooftop.py confirmed the pre-fix
    route returned decision='overridden' when the operator supplied a rooftop
    for a p0 item with no candidate rooftop in the recommendation.

    Mutation guard: remove the `rec_val is not None` check in
    _check_server_set_params → supplying dealer_id for a null recommendation
    becomes 'overridden' → test_null_rec_dealer_id_operator_supplies_approved
    fails.
    """

    def test_null_rec_dealer_id_operator_supplies_approved(self):
        """book_fleet_service with dealer_id absent from recommendation + body supplies → approved."""
        finding = {
            "scope": {"vin": "MRDVEH00000000017"},
            "severity": "p0",
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "parameters": {
                    "vehicle_vin": "MRDVEH00000000017",
                    "needs": [{"need_id": "fault:P0AA6", "labor_op": "LAB-ELE-07"}],
                    # dealer_id is ABSENT — no candidate rooftop for this p0 item
                    "urgency": "this_week",
                },
            },
        }
        body_params = {
            "dealer_id": "dealer-meridian-001",  # operator supplies a rooftop
            "urgency": "this_week",
        }
        params, error, decision = h._check_server_set_params(
            finding, "book_fleet_service", body_params
        )
        assert error is None, f"Expected no error; got {error!r}"
        assert decision == "approved", (
            f"Supplying dealer_id for a null recommendation must be 'approved'; "
            f"got {decision!r}. Design answer 9. "
            "Mutation: remove `rec_val is not None` check → 'overridden' → fails."
        )
        assert params.get("dealer_id") == "dealer-meridian-001", (
            f"dealer_id must be set from body; got {params.get('dealer_id')!r}."
        )

    def test_present_rec_dealer_id_changed_is_overridden(self):
        """Changing dealer_id from an existing non-null recommendation → overridden.

        Unchanged behavior: the new `rec_val is not None` guard must not affect
        the existing 'overridden' path when the recommendation HAS a dealer_id.

        Mutation guard: if the `rec_val is not None` check applies unconditionally
        → changing an existing dealer_id incorrectly becomes 'approved' → fails.
        """
        finding = {
            "scope": {"vin": "MRDVEH00000000017"},
            "severity": "attention",
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "parameters": {
                    "vehicle_vin": "MRDVEH00000000017",
                    "needs": [{"need_id": "fault:P0A7F", "labor_op": "LAB-ELE-07"}],
                    "dealer_id": "dealer-meridian-001",  # recommendation HAS a value
                    "urgency": "this_month",
                },
            },
        }
        body_params = {
            "dealer_id": "dealer-meridian-002",  # operator picks a different rooftop
            "urgency": "this_month",
        }
        params, error, decision = h._check_server_set_params(
            finding, "book_fleet_service", body_params
        )
        assert error is None, f"Expected no error; got {error!r}"
        assert decision == "overridden", (
            f"Changing an existing dealer_id must still be 'overridden'; "
            f"got {decision!r}. Mutation: remove rec_val None check entirely → "
            "existing recommendation dealer_id becomes 'approved' when changed → fails."
        )
        assert params.get("dealer_id") == "dealer-meridian-002"

    def test_null_rec_dealer_id_body_omits_returns_400(self):
        """When recommendation has no dealer_id and body also omits it, still 400.

        The _check_server_set_params post-fill check requires dealer_id for
        book_fleet_service. Omitting it is always 400 regardless of the
        recommendation.
        """
        finding = {
            "scope": {"vin": "MRDVEH00000000017"},
            "severity": "p0",
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "parameters": {
                    "vehicle_vin": "MRDVEH00000000017",
                    "needs": [{"need_id": "fault:P0AA6", "labor_op": "LAB-ELE-07"}],
                    # dealer_id absent from recommendation
                    "urgency": "this_week",
                },
            },
        }
        body_params = {
            # dealer_id omitted from body too
            "urgency": "this_week",
        }
        params, error, decision = h._check_server_set_params(
            finding, "book_fleet_service", body_params
        )
        assert error is not None, (
            "Omitting dealer_id entirely must return an error; "
            "book_fleet_service always requires a dealer_id to proceed."
        )
        assert "dealer_id" in (error or ""), f"Error must mention dealer_id; got {error!r}"



class TestCycle4ApprovedRuleOnlyForFleetBookingDealer:
    """Cycle 4 a W4 (user 2026-10-09): the 'filling a missing value is approved' rule is
    only for ``book_fleet_service``'s ``dealer_id``. Filling any other missing value on
    that kind is ``overridden``. (Stage C's ``test_approve_fix_f5``
    ``test_control_body_supplies_dealer_is_overridden`` pins the other-kind side.)

    Mutant: drop ``key == "dealer_id"`` from the rule -> filling a missing ``urgency``
    becomes 'approved' -> FAILS.
    """

    def test_filling_another_missing_value_is_overridden(self):
        finding = {
            "scope": {"vin": "MRDVEH00000000017"},
            "severity": "attention",
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "parameters": {
                    "vehicle_vin": "MRDVEH00000000017",
                    "needs": [{"need_id": "fault:P0A7F", "labor_op": "LAB-ELE-07"}],
                    "dealer_id": "dealer-meridian-001",
                    # urgency ABSENT from the recommendation
                },
            },
        }
        body_params = {"dealer_id": "dealer-meridian-001", "urgency": "this_month"}
        params, error, decision = h._check_server_set_params(
            finding, "book_fleet_service", body_params)
        assert error is None, f"Expected no error; got {error!r}"
        assert params.get("urgency") == "this_month"
        assert decision == "overridden", (
            f"Filling a missing urgency must be 'overridden'; got {decision!r}")



class TestCycle5ApprovedRuleWarrantyNotes:
    """Cycle 5 a W3: the third reachable 'filling a missing value' case. An operator's
    ``notes`` on a ``submit_warranty_claim`` whose recommendation has none (empty model
    narrative) is ``overridden``: the approved rule is only for ``book_fleet_service``'s
    ``dealer_id``. Mutant ``b-rule-denylist`` (the rule as a denylist of the two pinned
    pairs) makes it 'approved': FAILS.
    """

    def test_filling_missing_warranty_notes_is_overridden(self):
        vin = "MRDVEH00000000017"
        finding = {
            "scope": {"vin": vin}, "severity": "attention",
            "recommended_action": {
                "action_kind": "submit_warranty_claim",
                "parameters": {"vehicle_vin": vin, "ro_id": "ro-1", "line_no": "1",
                               "dealer_id": "dealer-1", "labor_op": "LO-1"},
            },
        }
        params, error, decision = h._check_server_set_params(
            finding, "submit_warranty_claim", {"notes": "operator note"})
        assert error is None, f"Expected no error; got {error!r}"
        assert params.get("notes") == "operator note"
        assert decision == "overridden", f"got {decision!r}"



class TestP0UrgencyAfterFill:
    """Security cycle 1 W1: a p0 booking reaches DMS only with urgency 'this_week', even
    when the body omits urgency and the recommendation's value drifted. Mutant: drop the
    post-fill check -> 'this_month' is approved -> FAILS. Control: a correct p0
    recommendation with the body omitting urgency is still approved."""

    @staticmethod
    def _finding(rec_urgency):
        params = {
            "vehicle_vin": "MRDVEH00000000017",
            "needs": [{"need_id": "fault:P0AA6", "labor_op": "LAB-ELE-07"}],
            "dealer_id": "dealer-meridian-001",
        }
        if rec_urgency is not None:
            params["urgency"] = rec_urgency
        return {"scope": {"vin": "MRDVEH00000000017"}, "severity": "p0",
                "recommended_action": {"action_kind": "book_fleet_service",
                                       "parameters": params}}

    def test_control_correct_p0_rec_with_body_omitting_urgency_is_approved(self):
        params, error, decision = h._check_server_set_params(
            self._finding("this_week"), "book_fleet_service", {"dealer_id": "dealer-meridian-001"})
        assert error is None, error
        assert params["urgency"] == "this_week"
        assert decision == "approved"

    @pytest.mark.parametrize("rec_urgency", ["this_month", "next_visit", None])
    def test_drifted_p0_rec_is_refused_when_body_omits_urgency(self, rec_urgency):
        params, error, decision = h._check_server_set_params(
            self._finding(rec_urgency), "book_fleet_service", {"dealer_id": "dealer-meridian-001"})
        assert error == "parameters.urgency: p0 items require urgency 'this_week'", (error, params)
