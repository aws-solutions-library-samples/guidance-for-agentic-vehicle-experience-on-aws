"""Red tests: Stage C executor behaviour for book_recall_service, submit_warranty_claim,
and ground_vehicle — task 1.4 item 10.

Contract § 4, `docs/fi-recall-warranty-contract.md` § 1.7 and § 2.7.

  10a. DmsExecutor (book_recall_service): the request body sent to DMS carries `recall_id`.
  10b. DmsExecutor (submit_warranty_claim):
        - 201 → ExecuteResult.status == executed
        - 202 → ExecuteResult.status == executed  (also executed per decisions.md 2026-10-05 call 9)
        - 422 → ExecuteResult.status == declined / failure_kind == target_refused  (refused)
        - 400 → ExecuteResult.status == failed   / failure_kind == executor_terminal_error
  10c. CmsExecutor (ground_vehicle):
        - 2xx → ExecuteResult.status == executed
        - 4xx → ExecuteResult.status == declined / failure_kind == target_refused  (refused)
        - 5xx exhausted retries → ExecuteResult.status == failed / executor_exhausted_retries

Every new-behaviour test is xfail(strict=True, reason="FI stage C task 1.4"). The DmsExecutor
currently does not send `recall_id` for `book_recall_service`, and `submit_warranty_claim` is
not executable yet (policy.py `executable=False`). CmsExecutor refuses all kinds. All assertions
therefore fail.

Grounding route: POST /api/v1/fleet-intelligence/groundings (decisions.md 2026-10-05 call 9).
DMS base URL: the API root (executor appends /api/dms/fleet/…); decisions.md 2026-10-05 call 9.
Claim 201 and 202 map to `executed` (decisions.md 2026-10-05 call 9).

All not-yet-built or not-yet-wired imports are inside test bodies. This file lives in
`agents/executors/tests/` and is governed by the package conftest.py (`_no_real_aws_clients`),
so no real boto3 call can occur. CSV custom:fleetIds in claims fixtures.

Run:
    .venv/bin/python -m pytest agents/executors/tests/test_stage_c_kinds.py -q
"""
from __future__ import annotations

import pytest
import responses as rsps  # type: ignore[import]
from unittest.mock import patch

from agents.executors.base import ActionStatus

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"
_CAMPAIGN_ID = "MRD-2024-001"
_RO_ID = "ro-stc-8001"
_LINE_NO = "3"
_ACTION_UUID = "aabbccdd-eeff-4000-8000-001122334455"

#: Claims with CSV custom:fleetIds (task constraint).
_OPERATOR_CLAIMS: dict = {
    "sub": "op-sub-stc-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": "fleet-meridian-01,fleet-meridian-02",
    "token": "Bearer.fake.jwt",
}

#: DMS API root only — executor appends /api/dms/fleet/…
#: (decisions.md 2026-10-05 call 9: "the DMS executor's base URL is the API root")
_DMS_BASE = "http://dms-staging.test"
_DMS_PREFIX = f"{_DMS_BASE}/api/dms/fleet"

#: CMS API endpoint
_CMS_ENDPOINT = "https://cms-staging.test"


def _make_dms_executor():
    """Build a DmsExecutor with stubbed collaborators."""
    from agents.executors.dms import DmsExecutor
    from agents.executors.base import RetryPolicy
    from unittest.mock import MagicMock

    stub_ssm = MagicMock()
    stub_ssm.get_parameter.side_effect = Exception("stubbed SSM")
    return DmsExecutor(
        enqueue_ios_owner=MagicMock(),
        events_client=MagicMock(),
        cloudwatch_client=MagicMock(),
        ssm_client=stub_ssm,
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
    )


def _make_cms_executor():
    """Build a Stage-C CmsExecutor with stubbed collaborators."""
    from agents.executors.cms import CmsExecutor
    from agents.executors.base import RetryPolicy

    return CmsExecutor(
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
    )


# ---------------------------------------------------------------------------
# 10a — book_recall_service body carries recall_id
# ---------------------------------------------------------------------------

class TestBookRecallServiceBody:
    """The DMS request body for book_recall_service must include recall_id (§ 1.7)."""

    @rsps.activate
    def test_booking_body_carries_recall_id(self):
        """The POST body sent to DMS must contain recall_id set from the Finding's campaign."""
        import json

        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/repair-orders",
            json={"ro_id": "ro-dms-9001"},
            status=201,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "book_recall_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "recall_id": _CAMPAIGN_ID,
                    "dealer_id": "dealer-seattle",
                },
            }
            executor.dispatch(action, _OPERATOR_CLAIMS)

        assert len(rsps.calls) == 1
        sent_body = json.loads(rsps.calls[0].request.body)
        assert "recall_id" in sent_body, (
            "The DMS request body must carry recall_id for book_recall_service "
            "(contract § 1.7); it was not sent."
        )
        assert sent_body["recall_id"] == _CAMPAIGN_ID

    @rsps.activate
    def test_booking_body_recall_id_equals_finding_campaign_id(self):
        """recall_id in the DMS body equals the campaign from the Finding's parameters."""
        import json

        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/repair-orders",
            json={"ro_id": "ro-dms-9002"},
            status=201,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "book_recall_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "recall_id": "MRD-2024-002",  # a different campaign
                    "dealer_id": "dealer-portland",
                },
            }
            executor.dispatch(action, _OPERATOR_CLAIMS)
        sent_body = json.loads(rsps.calls[0].request.body)
        assert sent_body.get("recall_id") == "MRD-2024-002"


# ---------------------------------------------------------------------------
# 10b — submit_warranty_claim response codes
# ---------------------------------------------------------------------------

class TestSubmitWarrantyClaimResponseCodes:
    """DMS warranty-claims route: 201/202 → executed; 422 → refused; 400 → failed (§ 2.7).

    decisions.md 2026-10-05 call 9: claim 201 and 202 map to `executed`.
    """

    @rsps.activate
    def test_201_accepted_maps_to_executed(self):
        """DMS 201 → ExecuteResult executed (decisions.md call 9)."""
        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/warranty-claims",
            json={"claim_id": "clm-001", "status": "Submitted"},
            status=201,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "submit_warranty_claim",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "ro_id": _RO_ID,
                    "line_no": _LINE_NO,
                    "dealer_id": "dealer-seattle",
                    "labor_op": "LO-EV-BRAKE-001",
                    "fault_code": "C0035",
                    "notes": "Brake caliper replaced.",
                },
            }
            result = executor.dispatch(action, _OPERATOR_CLAIMS)
        assert result.status == ActionStatus.EXECUTED

    @rsps.activate
    def test_202_accepted_maps_to_executed(self):
        """DMS 202 (async accepted) → ExecuteResult executed (decisions.md call 9)."""
        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/warranty-claims",
            json={"claim_id": "clm-002", "status": "Validated"},
            status=202,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "submit_warranty_claim",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "ro_id": _RO_ID,
                    "line_no": _LINE_NO,
                    "dealer_id": "dealer-seattle",
                    "labor_op": "LO-EV-BRAKE-001",
                    "fault_code": "C0035",
                    "notes": "Brake caliper replaced.",
                },
            }
            result = executor.dispatch(action, _OPERATOR_CLAIMS)
        assert result.status == ActionStatus.EXECUTED

    @rsps.activate
    def test_422_maps_to_refused(self):
        """DMS 422 (coverage denied) → ExecuteResult declined / target_refused (refused)."""
        from agents.executors.base import FailureKind

        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/warranty-claims",
            json={"reason": "COVERAGE_DENIED"},
            status=422,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "submit_warranty_claim",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "ro_id": _RO_ID,
                    "line_no": _LINE_NO,
                    "dealer_id": "dealer-seattle",
                    "labor_op": "LO-EV-BRAKE-001",
                    "fault_code": "C0035",
                    "notes": "Brake caliper.",
                },
            }
            result = executor.dispatch(action, _OPERATOR_CLAIMS)
        assert result.status == ActionStatus.DECLINED
        assert result.failure_kind == FailureKind.TARGET_REFUSED

    @rsps.activate
    def test_400_maps_to_failed(self):
        """DMS 400 (bad request) → ExecuteResult failed / executor_terminal_error."""
        from agents.executors.base import FailureKind

        rsps.add(
            rsps.POST,
            f"{_DMS_PREFIX}/warranty-claims",
            json={"error": "malformed_request"},
            status=400,
        )
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            executor = _make_dms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "submit_warranty_claim",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "ro_id": _RO_ID,
                    "line_no": _LINE_NO,
                    "dealer_id": "dealer-seattle",
                    "labor_op": "LO-EV-BRAKE-001",
                    "fault_code": "C0035",
                },
            }
            result = executor.dispatch(action, _OPERATOR_CLAIMS)
        assert result.status == ActionStatus.FAILED
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


# ---------------------------------------------------------------------------
# 10c — CmsExecutor ground_vehicle response codes
# ---------------------------------------------------------------------------

class TestCmsGroundVehicle:
    """Stage C CmsExecutor: ground_vehicle 2xx → executed; 4xx → refused; 5xx → failed.

    Route: POST /api/v1/fleet-intelligence/groundings (decisions.md 2026-10-05 call 9).
    The endpoint is not VIN-scoped; the VIN is in the request body.
    """

    @rsps.activate
    def test_200_maps_to_executed(self):
        """CMS 200 OK on ground_vehicle → ExecuteResult executed (decisions.md call 9)."""
        rsps.add(
            rsps.POST,
            f"{_CMS_ENDPOINT}/api/v1/fleet-intelligence/groundings",
            json={"grounding_id": "grd-001"},
            status=200,
        )
        with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
            executor = _make_cms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
                "parameters": {
                    "vehicle_vin": _VIN,
                    "recall_id": _CAMPAIGN_ID,
                },
            }
            result = executor.dispatch(action, _OPERATOR_CLAIMS)
        assert result.status == ActionStatus.EXECUTED

    @rsps.activate
    def test_4xx_maps_to_refused(self):
        """CMS 4xx on ground_vehicle → ExecuteResult declined / target_refused (refused)."""
        from agents.executors.base import FailureKind

        rsps.add(
            rsps.POST,
            f"{_CMS_ENDPOINT}/api/v1/fleet-intelligence/groundings",
            json={"error": "vehicle_already_grounded"},
            status=409,
        )
        with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
            executor = _make_cms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
                "parameters": {
                    "vehicle_vin": _VIN,
                    "recall_id": _CAMPAIGN_ID,
                },
            }
            result = executor.dispatch(action, _OPERATOR_CLAIMS)
        assert result.status == ActionStatus.DECLINED
        assert result.failure_kind == FailureKind.TARGET_REFUSED

    @rsps.activate
    def test_5xx_exhausted_retries_maps_to_failed(self):
        """CMS 5xx (repeated) → ExecuteResult failed / executor_exhausted_retries."""
        from agents.executors.base import FailureKind

        # Three 503s — exceeds retry_count=2 (0-indexed: attempts 0, 1, 2).
        for _ in range(3):
            rsps.add(
                rsps.POST,
                f"{_CMS_ENDPOINT}/api/v1/fleet-intelligence/groundings",
                json={"error": "service_unavailable"},
                status=503,
            )

        with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
            executor = _make_cms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
                "parameters": {
                    "vehicle_vin": _VIN,
                    "recall_id": _CAMPAIGN_ID,
                },
            }
            result = executor.dispatch(action, _OPERATOR_CLAIMS)
        assert result.status == ActionStatus.FAILED
        assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES

    @rsps.activate
    def test_ground_vehicle_forwards_operator_token(self):
        """Authorization header must be present for ground_vehicle (operator token forwarded)."""
        rsps.add(
            rsps.POST,
            f"{_CMS_ENDPOINT}/api/v1/fleet-intelligence/groundings",
            json={},
            status=200,
        )
        with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
            executor = _make_cms_executor()
            action = {
                "action_id": f"AVX-ACT-{_ACTION_UUID}",
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {"vehicle_vin": _VIN, "recall_id": _CAMPAIGN_ID},
            }
            executor.dispatch(action, _OPERATOR_CLAIMS)
        assert len(rsps.calls) == 1, "expected exactly one HTTP call to groundings"
        auth = rsps.calls[0].request.headers.get("Authorization", "")
        assert auth, "Authorization header must be present for ground_vehicle"
        assert "Bearer" in auth, f"Authorization must be a Bearer token, got: {auth!r}"

    def test_cms_executor_still_refuses_non_ground_vehicle_kinds(self):
        """Not xfail: this tests existing behaviour that already holds, not new Stage C code.
        Stage A's CmsExecutor takes no constructor arguments; Stage C extends it.
        Using CmsExecutor() directly rather than _make_cms_executor() (which passes
        Stage-C args) so this test passes against the current Stage-A implementation.
        """
        from agents.executors.base import FailureKind
        from agents.executors.cms import CmsExecutor

        executor = CmsExecutor()
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "exempt_recall",
            "target_system": "cms",
            "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
            "parameters": {"vehicle_vin": _VIN},
        }
        result = executor.dispatch(action, _OPERATOR_CLAIMS)
        # exempt_recall is still not executable; the executor must refuse it. (This was
        # move_vehicles until FI stage E task 4.3 made moves executable, 2026-10-10.)
        assert result.status == ActionStatus.DECLINED
        assert result.failure_kind == FailureKind.TARGET_REFUSED



@rsps.activate
def test_warranty_claim_int_line_no_is_sent_not_dropped():
    """G3-6: an integer line_no (DMS's own type) is dispatched, not refused as missing.

    Mutation guard: the str-only check in `_warranty_claim_body` returned None for an int,
    so the claim became a MissingDispatchParameter terminal error and nothing reached DMS.
    """
    import json as _json

    rsps.add(
        rsps.POST,
        f"{_DMS_PREFIX}/warranty-claims",
        json={"claim_id": "clm-002", "status": "Submitted"},
        status=201,
    )
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
        executor = _make_dms_executor()
        action = {
            "action_id": f"AVX-ACT-{_ACTION_UUID}",
            "action_kind": "submit_warranty_claim",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": _VIN,
                "ro_id": _RO_ID,
                "line_no": 3,
                "dealer_id": "dealer-seattle",
                "labor_op": "LO-EV-BRAKE-001",
            },
        }
        result = executor.dispatch(action, _OPERATOR_CLAIMS)
    assert result.status == ActionStatus.EXECUTED
    assert len(rsps.calls) == 1
    assert _json.loads(rsps.calls[0].request.body)["line_no"] == "3"
