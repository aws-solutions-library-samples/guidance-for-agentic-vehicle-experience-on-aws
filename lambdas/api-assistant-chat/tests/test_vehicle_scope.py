"""Fix Group 18: a vehicle-bound caller chats about the vehicle in its claim.

Spec 2026-09-23-cvx-escalation-recommend-not-act, F18.1. The properties under
test, each checked on the runtime payload the handler actually sends:

* With `custom:vehicleId` in the verified claims, the payload's `vehicleId` is
  the claim's and its `vin` is the one the CMS vehicles table holds.
* A client-sent `vin` or `vehicleId` that doesn't match never reaches the payload.
* If the VIN can't be read, the payload has no `vin`: the body's is not a fallback.
* Without the claim, body values pass through and no table read happens.

Run: python3 -m pytest lambdas/api-assistant-chat/tests/test_vehicle_scope.py -v
"""

from __future__ import annotations

import json
import os
import sys

import boto3
import pytest
from botocore.stub import Stubber

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

import vehicle_scope  # noqa: E402

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test"
CLAIM_VID = "VEH-MRDN-0015"
CLAIM_VIN = "MRDN0000000000015"
OTHER_VID = "VEH-MRDN-0003"
OTHER_VIN = "MRDN0000000000003"

DRIVER_CLAIMS = {
    "sub": "driver-sub",
    "custom:tenantId": "meridian",
    "custom:vehicleId": CLAIM_VID,
    "custom:driverId": "DRV-MRDN-0015",
    "cognito:groups": "vehicle-owner",
}
FLEET_CLAIMS = {
    "sub": "fleet-sub",
    "custom:tenantId": "meridian-fleet",
    "cognito:groups": "fleet-operator",
}


# ── harness ──────────────────────────────────────────────────────────────────


class _Events:
    def register_first(self, *_a, **_k):
        pass

    def unregister(self, *_a, **_k):
        pass


class _Meta:
    events = _Events()


class _Client:
    """Fake bedrock-agentcore client that records the payload it was sent."""

    def __init__(self):
        self.meta = _Meta()
        self.payloads: list[dict] = []

    def invoke_agent_runtime(self, **kw):
        self.payloads.append(json.loads(kw["payload"]))
        body = json.dumps({"result": "ok", "sessionId": "s"}).encode()

        class _S:
            def read(self_inner):
                return body

        return {"response": _S()}


@pytest.fixture
def harness(monkeypatch):
    import handler as h
    import session_cap

    session_cap.clear_all()
    client = _Client()
    lookups: list[str] = []

    def fake_lookup(vehicle_id):
        lookups.append(vehicle_id)
        return {CLAIM_VID: CLAIM_VIN, OTHER_VID: OTHER_VIN}.get(vehicle_id)

    monkeypatch.setattr(h, "_agentcore_client", client)
    monkeypatch.setattr(h, "_lookup_vin", fake_lookup)
    monkeypatch.setenv("CVX_RUNTIME_ARN", RUNTIME_ARN)

    def call(claims, **body_extra):
        body = {"prompt": "My brake warning light just came on.", "sessionId": "sess-veh-1"}
        body.update(body_extra)
        event = {
            "requestContext": {"authorizer": {"claims": claims}},
            "headers": {"Authorization": "Bearer fake.id.token"},
            "body": json.dumps(body),
        }
        resp = h.handler(event, None)
        assert resp["statusCode"] == 200, resp
        return client.payloads[-1]

    yield h, call, lookups
    session_cap.clear_all()


# ── claim-bound caller ───────────────────────────────────────────────────────


class TestClaimBoundCaller:
    def test_no_body_vehicle_gets_the_claim_vehicle_and_its_vin(self, harness):
        _, call, lookups = harness
        payload = call(DRIVER_CLAIMS)
        assert payload["vehicleId"] == CLAIM_VID
        assert payload["vin"] == CLAIM_VIN
        assert lookups == [CLAIM_VID]

    def test_mismatched_body_vehicle_id_is_dropped(self, harness):
        _, call, _ = harness
        payload = call(DRIVER_CLAIMS, vehicleId=OTHER_VID)
        assert payload["vehicleId"] == CLAIM_VID
        assert payload["vin"] == CLAIM_VIN

    def test_mismatched_body_vin_is_dropped(self, harness):
        _, call, _ = harness
        payload = call(DRIVER_CLAIMS, vin=OTHER_VIN)
        assert payload["vin"] == CLAIM_VIN
        assert OTHER_VIN not in json.dumps(payload)

    def test_both_body_values_mismatched_neither_reaches_the_payload(self, harness):
        _, call, lookups = harness
        payload = call(DRIVER_CLAIMS, vin=OTHER_VIN, vehicleId=OTHER_VID)
        assert (payload["vin"], payload["vehicleId"]) == (CLAIM_VIN, CLAIM_VID)
        # The body's vehicle is never looked up either.
        assert lookups == [CLAIM_VID]

    def test_matching_body_vin_in_another_case_still_uses_the_table_vin(self, harness):
        _, call, _ = harness
        payload = call(DRIVER_CLAIMS, vin=CLAIM_VIN.lower(), vehicleId=CLAIM_VID)
        assert payload["vin"] == CLAIM_VIN

    def test_lookup_failure_sends_no_vin_and_not_the_body_vin(self, harness, monkeypatch):
        h, call, _ = harness

        def boom(_vid):
            raise RuntimeError("ddb down")

        monkeypatch.setattr(h, "_lookup_vin", boom)
        payload = call(DRIVER_CLAIMS, vin=OTHER_VIN, vehicleId=OTHER_VID)
        assert "vin" not in payload
        assert payload["vehicleId"] == CLAIM_VID
        assert OTHER_VIN not in json.dumps(payload)

    def test_vehicle_row_without_a_vin_sends_no_vin(self, harness, monkeypatch):
        h, call, _ = harness
        monkeypatch.setattr(h, "_lookup_vin", lambda _vid: None)
        payload = call(DRIVER_CLAIMS, vin=OTHER_VIN)
        assert "vin" not in payload
        assert payload["vehicleId"] == CLAIM_VID

    @pytest.mark.parametrize("bad", ["VEH MRDN 0015", "x" * 65, "VEH/../0015", "'; drop"])
    def test_malformed_claim_is_not_used_and_the_body_is_still_dropped(self, harness, bad):
        _, call, lookups = harness
        payload = call({**DRIVER_CLAIMS, "custom:vehicleId": bad}, vin=OTHER_VIN, vehicleId=OTHER_VID)
        assert "vin" not in payload
        assert "vehicleId" not in payload
        assert lookups == []


# ── caller without the claim: unchanged behaviour ────────────────────────────


class TestCallerWithoutClaim:
    def test_body_values_pass_through(self, harness):
        _, call, _ = harness
        payload = call(FLEET_CLAIMS, vin=OTHER_VIN, vehicleId=OTHER_VID)
        assert (payload["vin"], payload["vehicleId"]) == (OTHER_VIN, OTHER_VID)

    def test_no_table_read(self, harness):
        _, call, lookups = harness
        call(FLEET_CLAIMS, vin=OTHER_VIN, vehicleId=OTHER_VID)
        call(FLEET_CLAIMS)
        assert lookups == []

    def test_empty_claim_counts_as_no_claim(self, harness):
        _, call, lookups = harness
        payload = call({**FLEET_CLAIMS, "custom:vehicleId": "  "}, vin=OTHER_VIN)
        assert payload["vin"] == OTHER_VIN
        assert lookups == []

    def test_non_string_body_values_do_not_raise(self, harness):
        _, call, _ = harness
        payload = call(FLEET_CLAIMS, vin=12345, vehicleId=["x"])
        assert payload["vin"] == "12345"


# ── the log line names the source, never the token ───────────────────────────


def test_log_line_records_the_vehicle_source_and_the_drop(harness, caplog):
    _, call, _ = harness
    caplog.set_level("INFO")
    call(DRIVER_CLAIMS, vehicleId=OTHER_VID)
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("chat: sub="))
    assert "vehicle_src=claim " in line
    assert "body_vehicle_dropped=True" in line
    assert "fake.id.token" not in line


# ── resolve() directly ───────────────────────────────────────────────────────


class TestResolve:
    def test_sources(self):
        lk = {CLAIM_VID: CLAIM_VIN}.get
        assert vehicle_scope.resolve(DRIVER_CLAIMS, None, None, lk).source == "claim"
        assert vehicle_scope.resolve(DRIVER_CLAIMS, None, None, lambda _v: None).source == "claim-unresolved"
        assert vehicle_scope.resolve({"custom:vehicleId": "a b"}, None, None, lk).source == "claim-invalid"
        assert vehicle_scope.resolve(FLEET_CLAIMS, "V", None, lk).source == "body"
        assert vehicle_scope.resolve(FLEET_CLAIMS, None, None, lk).source == "none"

    def test_body_dropped_flag(self):
        lk = {CLAIM_VID: CLAIM_VIN}.get
        assert vehicle_scope.resolve(DRIVER_CLAIMS, None, None, lk).body_dropped is False
        assert vehicle_scope.resolve(DRIVER_CLAIMS, CLAIM_VIN, CLAIM_VID, lk).body_dropped is False
        assert vehicle_scope.resolve(DRIVER_CLAIMS, OTHER_VIN, None, lk).body_dropped is True
        assert vehicle_scope.resolve(DRIVER_CLAIMS, None, OTHER_VID, lk).body_dropped is True
        # A body VIN that can't be checked counts as dropped.
        assert vehicle_scope.resolve(DRIVER_CLAIMS, CLAIM_VIN, None, lambda _v: None).body_dropped is True


# ── the real get_item call, through boto3's own validation ────────────────────


def test_lookup_vin_issues_the_expected_get_item(monkeypatch):
    """MagicMock would accept a positional or misshapen call; Stubber does not."""
    import handler as h

    monkeypatch.setenv("CMS_VEHICLES_TABLE", "cms-staging-storage-vehicles")
    resource = boto3.resource(
        "dynamodb", region_name="us-west-2",
        aws_access_key_id="x", aws_secret_access_key="x",
    )
    monkeypatch.setattr(h, "_ddb_resource", resource)
    with Stubber(resource.meta.client) as stub:
        stub.add_response(
            "get_item",
            {"Item": {"vin": {"S": CLAIM_VIN}}},
            {
                "TableName": "cms-staging-storage-vehicles",
                "Key": {"vehicleId": CLAIM_VID},
                "ProjectionExpression": "vin",
            },
        )
        stub.add_response(
            "get_item",
            {},
            {
                "TableName": "cms-staging-storage-vehicles",
                "Key": {"vehicleId": "VEH-NONE"},
                "ProjectionExpression": "vin",
            },
        )
        assert h._lookup_vin(CLAIM_VID) == CLAIM_VIN
        assert h._lookup_vin("VEH-NONE") is None
        stub.assert_no_pending_responses()


def test_lookup_vin_raises_when_the_table_is_not_configured(monkeypatch):
    import handler as h

    monkeypatch.delenv("CMS_VEHICLES_TABLE", raising=False)
    with pytest.raises(RuntimeError):
        h._lookup_vin(CLAIM_VID)



# ── review Cycle 1 W3: the runtime is told the session is pinned ───────────────


class TestVehicleBoundFlag:
    def test_claim_bound_caller_sets_vehicle_bound(self, harness):
        _, call, _ = harness
        assert call(DRIVER_CLAIMS)["vehicleBound"] is True

    def test_unresolved_claim_still_sets_vehicle_bound(self, harness, monkeypatch):
        h, call, _ = harness
        monkeypatch.setattr(h, "_lookup_vin", lambda _v: None)
        assert call(DRIVER_CLAIMS)["vehicleBound"] is True

    def test_malformed_claim_still_sets_vehicle_bound(self, harness):
        _, call, _ = harness
        assert call({**DRIVER_CLAIMS, "custom:vehicleId": "a b"})["vehicleBound"] is True

    def test_caller_without_claim_has_no_flag(self, harness):
        _, call, _ = harness
        assert "vehicleBound" not in call(FLEET_CLAIMS, vin=OTHER_VIN, vehicleId=OTHER_VID)

    def test_client_cannot_set_the_flag(self, harness):
        _, call, _ = harness
        assert "vehicleBound" not in call(FLEET_CLAIMS, vehicleBound=True)


# ── review Cycle 1 W1: the lookup's time budget is pinned ─────────────────────


def test_ddb_client_timeouts_and_attempts(monkeypatch):
    """At most two attempts of 1 s connect + 1 s read: ~4 s in the worst case."""
    import handler as h

    monkeypatch.setattr(h, "_ddb_resource", None)
    cfg = h._get_ddb().meta.client.meta.config
    assert cfg.connect_timeout == 1
    assert cfg.read_timeout == 1
    assert cfg.retries["total_max_attempts"] == 2
    assert "max_attempts" not in cfg.retries
    monkeypatch.setattr(h, "_ddb_resource", None)


def test_a_hung_table_endpoint_gets_exactly_two_attempts():
    """Behavioural check of _DDB_CONFIG: an endpoint that accepts and never answers."""
    import socket
    import threading
    import time

    import handler as h
    from botocore.exceptions import ReadTimeoutError

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    conns = []

    def accept():
        srv.settimeout(6)
        try:
            while True:
                c, _ = srv.accept()
                conns.append(c)
        except OSError:
            pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    resource = boto3.resource(
        "dynamodb", region_name="us-west-2", endpoint_url=f"http://127.0.0.1:{port}",
        aws_access_key_id="x", aws_secret_access_key="x", config=h._DDB_CONFIG,
    )
    start = time.monotonic()
    with pytest.raises(ReadTimeoutError):
        resource.Table("t").get_item(Key={"vehicleId": "V"})
    elapsed = time.monotonic() - start
    srv.close()
    for c in conns:
        c.close()
    assert len(conns) == 2
    assert elapsed < 4
