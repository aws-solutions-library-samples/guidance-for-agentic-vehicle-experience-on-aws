"""DS-1: bidi runtime session boundary enforces vehicle scope.

Drives `_establish_session` synchronously (same approach as
`test_bidi_app_jwt_verify.py`). Pins that:

* A vehicle-bound caller (custom:vehicleId in verified claims) has vehicle_id
  and vin replaced with the claim's, not the header's.
* A vehicle-bound caller with a mismatched VIN from a claim vehicle row that has
  a different VIN is still replaced (not rejected — the test is about pinning).
* A session is REFUSED (InvocationError) when the vehicles table is unreachable
  for a vehicle-bound caller.
* vin_pinned is True for vehicle-bound callers, False for neither-claim callers.
* A fleet-bound caller whose vehicle is not in their fleet is refused.

The scope module itself is unit-tested in test_supervisor_vehicle_scope.py;
these tests cover only the wiring in _establish_session.

Run:
    python3 -m pytest agents/supervisor/tests/test_bidi_app_vehicle_scope.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor import bidi_app, vehicle_scope as vs
from agents.supervisor.bidi_app import InvocationError, _establish_session
from agents.supervisor.jwt_verify import ENV_VERIFY_MODE, VerifyOutcome

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN = "header.payload.signature"
CLAIM_VID = "VEH-TEST-0015"        # surrogate id — distinct from the VIN (spec D1)
CLAIM_VIN = "MRDN0000000000015"
OTHER_VID = "VEH-OTHER-0001"
OTHER_VIN = "OTHER0000000000001"
FLEET_A = "FLEET-MERIDIAN-ONBOARD"

_VERIFIED_CLAIMS = {
    "sub": "driver-sub",
    "custom:vehicleId": CLAIM_VID,
    "custom:driverId": "DRV-MRDN-0015",
    "token_use": "id",
}
_FLEET_CLAIMS = {
    "sub": "fleet-sub",
    "custom:fleetIds": FLEET_A,
    "token_use": "id",
}

SUCCESS = VerifyOutcome(True, "verified", sub="driver-sub", claims=_VERIFIED_CLAIMS)
SUCCESS_FLEET = VerifyOutcome(True, "verified", sub="fleet-sub", claims=_FLEET_CLAIMS)
FAILURE = VerifyOutcome(False, "bad_signature")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(vid: str | None = OTHER_VID, vin: str | None = OTHER_VIN) -> tuple:
    """Build a (websocket, context) pair with the given header vehicle values."""
    headers: dict = {
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": TOKEN,
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian",
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": vin or "",
    }
    if vid:
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId"] = vid

    ctx = RequestContext(session_id="sid", request_headers=headers, request=None)
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("receive_json should not be called"))
    return ws, ctx


def _run(monkeypatch, outcome: VerifyOutcome, scope_result: vs.BidiVehicleScope,
         mode: str = "enforce") -> object:
    """Run _establish_session with the given JWT outcome and scope result."""
    monkeypatch.setenv(ENV_VERIFY_MODE, mode)
    monkeypatch.setenv("CMS_VEHICLES_TABLE", "cms-staging-storage-vehicles")
    monkeypatch.setattr(bidi_app, "verify_id_token", lambda *a, **kw: outcome)

    def fake_resolve(claims, header_vehicle_id, header_vin, header_driver_id,
                     lookup_row, claims_verified=False):
        return scope_result

    monkeypatch.setattr(vs, "resolve", fake_resolve)

    ws, ctx = _ctx()
    return asyncio.run(_establish_session(ws, ctx, "sid"))


# ---------------------------------------------------------------------------
# Wiring tests
# ---------------------------------------------------------------------------

class TestVehicleScopeWiring:

    def test_vehicle_bound_vehicle_id_pinned_from_scope(self, monkeypatch):
        """The session's vehicle_id must come from the scope result, not the header."""
        scope = vs.BidiVehicleScope(
            status=vs.STATUS_PASS, vehicle_id=CLAIM_VID, vin=CLAIM_VIN,
            driver_id="DRV-MRDN-0015", vin_pinned=True,
        )
        session = _run(monkeypatch, SUCCESS, scope)
        assert session.vehicle_id == CLAIM_VID
        assert session.vin == CLAIM_VIN

    def test_vehicle_bound_vin_pinned_flag_set(self, monkeypatch):
        scope = vs.BidiVehicleScope(
            status=vs.STATUS_PASS, vehicle_id=CLAIM_VID, vin=CLAIM_VIN,
            driver_id=None, vin_pinned=True,
        )
        session = _run(monkeypatch, SUCCESS, scope)
        assert session.vin_pinned is True

    def test_unchanged_caller_vin_not_pinned(self, monkeypatch):
        scope = vs.BidiVehicleScope(
            status=vs.STATUS_UNCHANGED, vehicle_id=OTHER_VID, vin=OTHER_VIN,
            driver_id=None, vin_pinned=False,
        )
        session = _run(monkeypatch, SUCCESS, scope)
        assert session.vin_pinned is False

    def test_driver_id_from_scope(self, monkeypatch):
        scope = vs.BidiVehicleScope(
            status=vs.STATUS_PASS, vehicle_id=CLAIM_VID, vin=CLAIM_VIN,
            driver_id="DRV-SCOPE-01", vin_pinned=True,
        )
        session = _run(monkeypatch, SUCCESS, scope)
        assert session.driver_id == "DRV-SCOPE-01"

    def test_scope_deny_raises_invocation_error(self, monkeypatch):
        scope = vs.BidiVehicleScope(
            status=vs.STATUS_DENY, vehicle_id=None, vin=None,
            driver_id=None, vin_pinned=False,
            detail="vehicle not in caller scope",
        )
        with pytest.raises(InvocationError) as exc:
            _run(monkeypatch, SUCCESS, scope)
        assert "vehicle not in caller scope" in str(exc.value).lower() or "vehicle scope" in str(exc.value).lower()

    def test_scope_error_raises_invocation_error(self, monkeypatch):
        scope = vs.BidiVehicleScope(
            status=vs.STATUS_ERROR, vehicle_id=None, vin=None,
            driver_id=None, vin_pinned=False,
            detail="vehicles table unavailable (RuntimeError)",
        )
        with pytest.raises(InvocationError):
            _run(monkeypatch, SUCCESS, scope)

    def test_scope_resolve_called_with_verified_claims_on_enforce(self, monkeypatch):
        """When JWT succeeds in enforce mode, verified claims are passed to scope."""
        monkeypatch.setenv(ENV_VERIFY_MODE, "enforce")
        monkeypatch.setenv("CMS_VEHICLES_TABLE", "cms-staging-storage-vehicles")
        monkeypatch.setattr(bidi_app, "verify_id_token", lambda *a, **kw: SUCCESS)

        received: list[dict] = []

        def record_resolve(claims, header_vehicle_id, header_vin, header_driver_id,
                           lookup_row, claims_verified=False):
            received.append({"claims": claims, "verified": claims_verified})
            return vs.BidiVehicleScope(
                status=vs.STATUS_UNCHANGED, vehicle_id=OTHER_VID, vin=OTHER_VIN,
                driver_id=None, vin_pinned=False,
            )

        monkeypatch.setattr(vs, "resolve", record_resolve)
        ws, ctx = _ctx()
        asyncio.run(_establish_session(ws, ctx, "sid"))

        assert received, "resolve() must be called"
        assert received[0]["claims"].get("custom:vehicleId") == CLAIM_VID, \
            "verified claims must include the vehicleId claim"
        assert received[0]["verified"] is True

    def test_scope_resolve_called_with_unverified_claims_on_off(self, monkeypatch):
        """In off mode, claims come from an unverified decode. Scope still runs."""
        monkeypatch.setenv(ENV_VERIFY_MODE, "off")
        monkeypatch.setenv("CMS_VEHICLES_TABLE", "cms-staging-storage-vehicles")

        received: list[dict] = []

        def record_resolve(claims, header_vehicle_id, header_vin, header_driver_id,
                           lookup_row, claims_verified=False):
            received.append({"verified": claims_verified})
            return vs.BidiVehicleScope(
                status=vs.STATUS_UNCHANGED, vehicle_id=OTHER_VID, vin=OTHER_VIN,
                driver_id=None, vin_pinned=False,
            )

        monkeypatch.setattr(vs, "resolve", record_resolve)
        ws, ctx = _ctx()
        asyncio.run(_establish_session(ws, ctx, "sid"))

        assert received
        assert received[0]["verified"] is False



# ---------------------------------------------------------------------------
# End to end: real scope module, real botocore client under a Stubber
# ---------------------------------------------------------------------------
#
# The wiring tests above fake `resolve`, so they cannot see the lookup itself.
# These drive `_establish_session` -> `vehicle_scope.resolve` -> the GetItem,
# with the environment the deployed runtime actually has: CMS_STAGE set,
# CMS_VEHICLES_TABLE NOT set (staging bidi v75, 2026-10-05). A first draft read
# CMS_VEHICLES_TABLE with no default and would have refused every vehicle-bound
# session in staging while all the faked-resolve tests stayed green.

import boto3 as _boto3_for_stub  # noqa: E402
from botocore.stub import Stubber  # noqa: E402


def _stubbed_client(monkeypatch, expected_table: str, item: dict | None):
    client = _boto3_for_stub.client(
        "dynamodb", region_name="us-west-2",
        aws_access_key_id="x", aws_secret_access_key="x",
    )
    stub = Stubber(client)
    resp = {"Item": item} if item is not None else {}
    stub.add_response(
        "get_item", resp,
        {"TableName": expected_table, "Key": {"vehicleId": {"S": CLAIM_VID}},
         "ProjectionExpression": "vin, fleetId"},
    )
    stub.activate()
    monkeypatch.setattr(bidi_app, "_get_scope_ddb_client", lambda: client)
    return stub


def _run_real(monkeypatch, vid=OTHER_VID, vin=OTHER_VIN):
    monkeypatch.setenv(ENV_VERIFY_MODE, "enforce")
    monkeypatch.delenv("CMS_VEHICLES_TABLE", raising=False)
    monkeypatch.setenv("CMS_STAGE", "staging")
    monkeypatch.setattr(bidi_app, "verify_id_token", lambda *a, **kw: SUCCESS)
    ws, ctx = _ctx(vid=vid, vin=vin)
    return asyncio.run(_establish_session(ws, ctx, "sid"))


class TestVehicleScopeEndToEnd:

    def test_other_vehicle_headers_are_pinned_to_the_claim_vehicle(self, monkeypatch):
        stub = _stubbed_client(
            monkeypatch, "cms-staging-storage-vehicles",
            {"vin": {"S": CLAIM_VIN}, "fleetId": {"S": FLEET_A}},
        )
        session = _run_real(monkeypatch)
        stub.assert_no_pending_responses()
        assert session.vehicle_id == CLAIM_VID
        assert session.vin == CLAIM_VIN
        assert session.vin_pinned is True
        assert session.driver_id == "DRV-MRDN-0015"

    def test_missing_claim_vehicle_row_refuses_the_session(self, monkeypatch):
        _stubbed_client(monkeypatch, "cms-staging-storage-vehicles", None)
        with pytest.raises(InvocationError):
            _run_real(monkeypatch)

    def test_claim_vehicle_row_without_vin_refuses_the_session(self, monkeypatch):
        _stubbed_client(
            monkeypatch, "cms-staging-storage-vehicles", {"fleetId": {"S": FLEET_A}},
        )
        with pytest.raises(InvocationError):
            _run_real(monkeypatch)

    def test_table_read_error_refuses_the_session(self, monkeypatch):
        client = _boto3_for_stub.client(
            "dynamodb", region_name="us-west-2",
            aws_access_key_id="x", aws_secret_access_key="x",
        )
        stub = Stubber(client)
        stub.add_client_error("get_item", "ResourceNotFoundException")
        stub.activate()
        monkeypatch.setattr(bidi_app, "_get_scope_ddb_client", lambda: client)
        with pytest.raises(InvocationError):
            _run_real(monkeypatch)

    def test_table_name_comes_from_cms_stage_when_unset(self, monkeypatch):
        monkeypatch.delenv("CMS_VEHICLES_TABLE", raising=False)
        monkeypatch.setenv("CMS_STAGE", "staging")
        assert bidi_app._cms_vehicles_table_name() == "cms-staging-storage-vehicles"
