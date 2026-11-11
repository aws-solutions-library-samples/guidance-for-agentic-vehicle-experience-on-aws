"""Unit tests for lambdas/api-triage/vehicle_scope.py — DS-1.

Pins the scope contract from issues/2026-10-05-bidi-and-triage-accept-any-vehicle/report.md:

* Vehicle-bound caller (custom:vehicleId): only the VIN of record for that vehicle.
  Malformed claim → deny; lookup failure / no VIN → 503-class error.
* Fleet-bound caller (custom:fleetIds): body VIN's row(s) must all have fleetId in list.
  Empty fleet list → deny; no rows → deny; any row out-of-scope → deny.
* Neither claim → unchanged (STATUS_UNCHANGED, ok=True).

Mutations verified (each must turn a test red — see build-notes.md):
  (1) Drop the pin: accept any VIN for vehicle-bound → test_vehicle_bound_blocks_other_vin FAILS
  (2) VIN case mismatch treated as denial: pass case-insensitive mismatch →
      test_vehicle_bound_passes_case_insensitive FAILS
  (3) Accept on failed row read: STATUS_ERROR → STATUS_PASS →
      test_vehicle_bound_lookup_error_returns_503_class FAILS
  (4) Empty fleet list unrestricted: [] → pass →
      test_fleet_bound_empty_fleet_ids_deny FAILS
  (5) Only one vin-index row in scope: partial pass →
      test_fleet_bound_one_row_out_of_scope_deny FAILS
  (6) Skip check on malformed claim: malformed → pass →
      test_vehicle_bound_malformed_claim_deny FAILS
  (7) See CDK test for grant widening.

Run:
    python3 -m pytest lambdas/api-triage/tests/test_triage_vehicle_scope.py -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# Load vehicle_scope from the lambda directory (not the agents one).
_TRIAGE_DIR = os.path.join(os.path.dirname(__file__), "..")
if _TRIAGE_DIR not in sys.path:
    sys.path.insert(0, _TRIAGE_DIR)

import triage_vehicle_scope as vs  # noqa: E402

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

CLAIM_VID = "VEH-TEST-0015"        # surrogate id — distinct from the VIN (spec D1)
CLAIM_VIN = "MRDN0000000000015"
OTHER_VIN = "MRDN0000000000099"
FLEET_A = "FLEET-MERIDIAN-ONBOARD"
FLEET_B = "FLEET-MERIDIAN-OFFBOARD"
TABLE = "cms-staging-storage-vehicles"

VEHICLE_CLAIMS = {
    "sub": "driver-sub",
    "custom:vehicleId": CLAIM_VID,
}
FLEET_CLAIMS_A = {
    "sub": "fleet-sub",
    "custom:fleetIds": FLEET_A,
}
NO_CLAIMS = {
    "sub": "staff-sub",
}


# ------------------------------------------------------------------
# Helpers: fake DDB clients
# ------------------------------------------------------------------

def _ddb_vehicle_bound(vehicle_id=CLAIM_VID, vin=CLAIM_VIN):
    """Returns a low-level DynamoDB client stub for GetItem."""
    client = MagicMock()
    client.get_item.return_value = {
        "Item": {"vin": {"S": vin}},
    }
    return client


def _ddb_fleet_bound(rows: list[dict], pages: int = 1):
    """A low-level client stub shaped like the real table.

    ``rows`` are the vehicles carrying the VIN, with their ``fleetId``. The real
    ``vin-index`` is KEYS_ONLY: a Query returns only ``vehicleId`` and ``vin``,
    so ``fleetId`` is served only by ``GetItem`` on the table. ``pages`` splits
    the Query result across that many pages (LastEvaluatedKey).
    """
    client = MagicMock()
    index_items = [{"vehicleId": r["vehicleId"], "vin": {"S": CLAIM_VIN}} for r in rows]
    if pages <= 1 or not index_items:
        client.query.return_value = {"Items": index_items}
    else:
        size = max(1, -(-len(index_items) // pages))
        chunks = [index_items[i:i + size] for i in range(0, len(index_items), size)]
        responses = []
        for n, chunk in enumerate(chunks):
            resp = {"Items": chunk}
            if n < len(chunks) - 1:
                resp["LastEvaluatedKey"] = {"vehicleId": chunk[-1]["vehicleId"], "vin": {"S": CLAIM_VIN}}
            responses.append(resp)
        client.query.side_effect = responses
    by_id = {r["vehicleId"]["S"]: r for r in rows}

    def _get_item(**kwargs):
        row = by_id.get(kwargs["Key"]["vehicleId"]["S"])
        return {"Item": {"fleetId": row["fleetId"]}} if row and "fleetId" in row else {}

    client.get_item.side_effect = _get_item
    return client


def _ddb_error():
    """Raises on any call."""
    client = MagicMock()
    client.get_item.side_effect = RuntimeError("table unavailable")
    client.query.side_effect = RuntimeError("table unavailable")
    return client


def _ddb_empty_item():
    """GetItem returns no Item (vehicle not found)."""
    client = MagicMock()
    client.get_item.return_value = {}
    return client


def _ddb_no_vin():
    """GetItem returns an item without a vin attribute."""
    client = MagicMock()
    client.get_item.return_value = {"Item": {"vehicleId": {"S": CLAIM_VID}}}
    return client


# ==================================================================
# Vehicle-bound (custom:vehicleId) tests
# ==================================================================

class TestVehicleBoundCaller:

    def test_matching_vin_passes(self):
        r = vs.check(VEHICLE_CLAIMS, CLAIM_VIN, _ddb_vehicle_bound(), TABLE)
        assert r.ok
        assert r.status == vs.STATUS_PASS

    def test_matching_vin_passes_case_insensitive(self):
        """Mutation (2): case-insensitive comparison must pass."""
        r = vs.check(VEHICLE_CLAIMS, CLAIM_VIN.lower(), _ddb_vehicle_bound(), TABLE)
        assert r.ok, "VIN comparison must be case-insensitive"

    def test_other_vin_is_denied(self):
        """Mutation (1): drop the pin → this test would FAIL."""
        r = vs.check(VEHICLE_CLAIMS, OTHER_VIN, _ddb_vehicle_bound(), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_DENY_VIN_MISMATCH
        assert "vehicle not in caller scope" in r.detail

    def test_malformed_claim_is_denied(self):
        """Mutation (6): skip check on malformed → this test would FAIL."""
        bad_claims = {**VEHICLE_CLAIMS, "custom:vehicleId": "VEH MRDN 0015"}
        r = vs.check(bad_claims, CLAIM_VIN, _ddb_error(), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_DENY_MALFORMED_CLAIM
        # Error client is never called — malformed fails before the lookup.

    @pytest.mark.parametrize("bad", ["x" * 65, "VEH/../0015", "'; drop"])
    def test_other_malformed_claim_shapes_denied(self, bad):
        r = vs.check({**VEHICLE_CLAIMS, "custom:vehicleId": bad}, CLAIM_VIN, MagicMock(), TABLE)
        assert r.status == vs.STATUS_DENY_MALFORMED_CLAIM

    def test_lookup_error_returns_error_status(self):
        """Mutation (3): accept on failed read → this test would FAIL."""
        r = vs.check(VEHICLE_CLAIMS, CLAIM_VIN, _ddb_error(), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_ERROR

    def test_vehicle_not_found_returns_error(self):
        r = vs.check(VEHICLE_CLAIMS, CLAIM_VIN, _ddb_empty_item(), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_ERROR

    def test_no_vin_on_row_returns_error(self):
        r = vs.check(VEHICLE_CLAIMS, CLAIM_VIN, _ddb_no_vin(), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_ERROR

    def test_get_item_called_with_correct_key(self):
        ddb = _ddb_vehicle_bound()
        vs.check(VEHICLE_CLAIMS, CLAIM_VIN, ddb, TABLE)
        ddb.get_item.assert_called_once_with(
            TableName=TABLE,
            Key={"vehicleId": {"S": CLAIM_VID}},
            ProjectionExpression="vin",
        )


# ==================================================================
# Fleet-bound (custom:fleetIds) tests
# ==================================================================

class TestFleetBoundCaller:

    def test_single_fleet_row_in_scope_passes(self):
        rows = [{"vehicleId": {"S": "VEH-001"}, "fleetId": {"S": FLEET_A}}]
        r = vs.check(FLEET_CLAIMS_A, CLAIM_VIN, _ddb_fleet_bound(rows), TABLE)
        assert r.ok
        assert r.status == vs.STATUS_PASS

    def test_multiple_rows_all_in_scope_passes(self):
        rows = [
            {"vehicleId": {"S": "VEH-001"}, "fleetId": {"S": FLEET_A}},
            {"vehicleId": {"S": "VEH-002"}, "fleetId": {"S": FLEET_A}},
        ]
        r = vs.check(FLEET_CLAIMS_A, CLAIM_VIN, _ddb_fleet_bound(rows), TABLE)
        assert r.ok

    def test_one_row_out_of_scope_denied(self):
        """Mutation (5): partial pass → this test would FAIL."""
        rows = [
            {"vehicleId": {"S": "VEH-001"}, "fleetId": {"S": FLEET_A}},
            {"vehicleId": {"S": "VEH-002"}, "fleetId": {"S": FLEET_B}},
        ]
        r = vs.check(FLEET_CLAIMS_A, CLAIM_VIN, _ddb_fleet_bound(rows), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_DENY_NOT_IN_FLEET

    def test_no_rows_denied(self):
        r = vs.check(FLEET_CLAIMS_A, CLAIM_VIN, _ddb_fleet_bound([]), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_DENY_NOT_IN_FLEET

    def test_empty_fleet_ids_denied(self):
        """Mutation (4): empty fleet list → pass → this test would FAIL."""
        claims = {**FLEET_CLAIMS_A, "custom:fleetIds": ""}
        r = vs.check(claims, CLAIM_VIN, _ddb_fleet_bound([]), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_DENY_NOT_IN_FLEET

    def test_csv_fleet_ids_parsed(self):
        """CSV custom:fleetIds are split correctly."""
        claims = {**FLEET_CLAIMS_A, "custom:fleetIds": f"{FLEET_A},{FLEET_B}"}
        rows = [{"vehicleId": {"S": "VEH-001"}, "fleetId": {"S": FLEET_B}}]
        r = vs.check(claims, CLAIM_VIN, _ddb_fleet_bound(rows), TABLE)
        assert r.ok

    def test_json_fleet_ids_parsed(self):
        claims = {**FLEET_CLAIMS_A, "custom:fleetIds": json.dumps([FLEET_A, FLEET_B])}
        rows = [{"vehicleId": {"S": "VEH-001"}, "fleetId": {"S": FLEET_B}}]
        r = vs.check(claims, CLAIM_VIN, _ddb_fleet_bound(rows), TABLE)
        assert r.ok

    def test_fleet_lookup_error_returns_error(self):
        r = vs.check(FLEET_CLAIMS_A, CLAIM_VIN, _ddb_error(), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_ERROR

    def test_query_called_with_correct_args(self):
        """The VIN is found through vin-index, and fleetId through GetItem on the
        table, because the KEYS_ONLY index does not carry fleetId."""
        rows = [{"vehicleId": {"S": "VEH-001"}, "fleetId": {"S": FLEET_A}}]
        ddb = _ddb_fleet_bound(rows)
        vs.check(FLEET_CLAIMS_A, CLAIM_VIN, ddb, TABLE)
        ddb.query.assert_called_once_with(
            TableName=TABLE,
            IndexName="vin-index",
            KeyConditionExpression="vin = :vin",
            ExpressionAttributeValues={":vin": {"S": CLAIM_VIN}},
        )
        ddb.get_item.assert_called_once_with(
            TableName=TABLE,
            Key={"vehicleId": {"S": "VEH-001"}},
            ProjectionExpression="fleetId",
        )

    def test_every_query_page_is_checked(self):
        """An out-of-scope row on a later page still denies."""
        rows = [
            {"vehicleId": {"S": "VEH-001"}, "fleetId": {"S": FLEET_A}},
            {"vehicleId": {"S": "VEH-002"}, "fleetId": {"S": FLEET_B}},
        ]
        r = vs.check(FLEET_CLAIMS_A, CLAIM_VIN, _ddb_fleet_bound(rows, pages=2), TABLE)
        assert r.status == vs.STATUS_DENY_NOT_IN_FLEET

    def test_row_without_fleet_id_denied(self):
        rows = [{"vehicleId": {"S": "VEH-001"}}]
        r = vs.check(FLEET_CLAIMS_A, CLAIM_VIN, _ddb_fleet_bound(rows), TABLE)
        assert r.status == vs.STATUS_DENY_NOT_IN_FLEET

    def test_fleet_get_item_error_returns_error(self):
        rows = [{"vehicleId": {"S": "VEH-001"}, "fleetId": {"S": FLEET_A}}]
        ddb = _ddb_fleet_bound(rows)
        ddb.get_item.side_effect = RuntimeError("boom")
        r = vs.check(FLEET_CLAIMS_A, CLAIM_VIN, ddb, TABLE)
        assert r.status == vs.STATUS_ERROR

    def test_blank_segment_in_csv_not_a_valid_fleet_id(self):
        """A claim of ',' or ' , ' parses to no valid fleet ids → deny."""
        claims = {**FLEET_CLAIMS_A, "custom:fleetIds": ","}
        r = vs.check(claims, CLAIM_VIN, _ddb_fleet_bound([]), TABLE)
        assert not r.ok
        assert r.status == vs.STATUS_DENY_NOT_IN_FLEET


# ==================================================================
# Neither-claim caller
# ==================================================================

class TestClaimShapes:
    @pytest.mark.parametrize("raw", [12345, 3.5, True, {"a": 1}])
    def test_non_string_fleet_claim_does_not_raise(self, raw):
        r = vs.check({"sub": "x", "custom:fleetIds": raw}, CLAIM_VIN, _ddb_fleet_bound([]), TABLE)
        assert not r.ok


class TestNeitherClaimCaller:

    def test_unchanged_when_no_vehicle_claims(self):
        r = vs.check(NO_CLAIMS, CLAIM_VIN, MagicMock(), TABLE)
        assert r.ok
        assert r.status == vs.STATUS_UNCHANGED

    def test_no_ddb_call_when_no_vehicle_claims(self):
        ddb = MagicMock()
        vs.check(NO_CLAIMS, CLAIM_VIN, ddb, TABLE)
        ddb.get_item.assert_not_called()
        ddb.query.assert_not_called()

    def test_blank_vehicle_id_claim_is_bound_and_denied(self):
        """A present-but-blank claim binds the caller: it must not fall through
        to the unscoped branch."""
        claims = {**VEHICLE_CLAIMS, "custom:vehicleId": "  "}
        r = vs.check(claims, CLAIM_VIN, MagicMock(), TABLE)
        assert r.status == vs.STATUS_DENY_MALFORMED_CLAIM

    def test_scoped_caller_without_table_name_is_refused(self):
        r = vs.check(VEHICLE_CLAIMS, CLAIM_VIN, MagicMock(), "")
        assert r.status == vs.STATUS_ERROR
        r = vs.check(FLEET_CLAIMS_A, CLAIM_VIN, MagicMock(), "")
        assert r.status == vs.STATUS_ERROR

    def test_unscoped_caller_without_table_name_is_unchanged(self):
        assert vs.check(NO_CLAIMS, CLAIM_VIN, MagicMock(), "").status == vs.STATUS_UNCHANGED

    def test_non_string_body_vin_does_not_raise(self):
        r = vs.check(VEHICLE_CLAIMS, 12345, _ddb_vehicle_bound(), TABLE)
        assert r.status == vs.STATUS_DENY_VIN_MISMATCH


# ==================================================================
# VehicleScopeOutcome.ok helper
# ==================================================================

class TestOutcomeOkProperty:
    def test_pass_is_ok(self):
        assert vs.VehicleScopeOutcome(vs.STATUS_PASS).ok is True

    def test_unchanged_is_ok(self):
        assert vs.VehicleScopeOutcome(vs.STATUS_UNCHANGED).ok is True

    def test_deny_vin_mismatch_not_ok(self):
        assert vs.VehicleScopeOutcome(vs.STATUS_DENY_VIN_MISMATCH).ok is False

    def test_deny_not_in_fleet_not_ok(self):
        assert vs.VehicleScopeOutcome(vs.STATUS_DENY_NOT_IN_FLEET).ok is False

    def test_deny_malformed_not_ok(self):
        assert vs.VehicleScopeOutcome(vs.STATUS_DENY_MALFORMED_CLAIM).ok is False

    def test_error_not_ok(self):
        assert vs.VehicleScopeOutcome(vs.STATUS_ERROR).ok is False


# ==================================================================
# Handler integration: vehicle scope is called and returns correct HTTP codes
# ==================================================================

class TestHandlerVehicleScopeIntegration:
    """Thin integration tests — the handler calls vehicle_scope.check() and maps
    status to the correct HTTP response codes."""

    @pytest.fixture
    def h(self, monkeypatch):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
        monkeypatch.setenv("CLASSIFIER_FUNCTION_ARN", "arn:aws:lambda:us-west-2:123456789012:function:clf")
        monkeypatch.setenv("DEFAULT_TENANT_ID", "meridian")
        monkeypatch.setenv("CMS_VEHICLES_TABLE", TABLE)
        spec = importlib.util.spec_from_file_location(
            "api_triage_handler_vs",
            os.path.join(os.path.dirname(__file__), "..", "handler.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _event(self, claims, body=None):
        return {
            "requestContext": {"authorizer": {"claims": claims}},
            "body": json.dumps(body or {"vin": CLAIM_VIN}),
        }

    def test_vehicle_bound_correct_vin_passes_to_classifier(self, h, monkeypatch):
        import io
        import triage_vehicle_scope as vs_mod
        monkeypatch.setattr(
            vs_mod, "check",
            lambda *a, **kw: vs_mod.VehicleScopeOutcome(vs_mod.STATUS_PASS),
        )
        fake_classifier = MagicMock()
        fake_classifier.invoke.return_value = {
            "Payload": io.BytesIO(json.dumps({
                "classification": "P2", "sessionId": "s", "decidedAt": "t", "latencyMs": 1,
            }).encode())
        }
        monkeypatch.setattr(h, "_lambda", fake_classifier)
        resp = h.handler(self._event(VEHICLE_CLAIMS), None)
        assert resp["statusCode"] == 200

    def test_classifier_gets_the_vin_of_record_not_the_callers_casing(self, h, monkeypatch):
        """Review cycle 1: a lowercased body VIN passes the case-insensitive check,
        but the classifier must key its decision row on the VIN of record."""
        import io
        fake_classifier = MagicMock()
        fake_classifier.invoke.return_value = {
            "Payload": io.BytesIO(json.dumps({
                "classification": "P2", "sessionId": "s", "decidedAt": "t", "latencyMs": 1,
            }).encode())
        }
        monkeypatch.setattr(h, "_lambda", fake_classifier)
        monkeypatch.setattr(h, "_get_ddb", lambda: _ddb_vehicle_bound())
        resp = h.handler(self._event(VEHICLE_CLAIMS, {"vin": CLAIM_VIN.lower()}), None)
        assert resp["statusCode"] == 200
        sent = json.loads(fake_classifier.invoke.call_args.kwargs["Payload"])
        assert sent["vin"] == CLAIM_VIN

    def test_vehicle_bound_wrong_vin_returns_403(self, h, monkeypatch):
        import triage_vehicle_scope as vs_mod
        monkeypatch.setattr(
            vs_mod, "check",
            lambda *a, **kw: vs_mod.VehicleScopeOutcome(vs_mod.STATUS_DENY_VIN_MISMATCH, "vehicle not in caller scope"),
        )
        resp = h.handler(self._event(VEHICLE_CLAIMS), None)
        assert resp["statusCode"] == 403
        assert "vehicle not in caller scope" in resp["body"]

    def test_vehicle_scope_error_returns_503(self, h, monkeypatch):
        import triage_vehicle_scope as vs_mod
        monkeypatch.setattr(
            vs_mod, "check",
            lambda *a, **kw: vs_mod.VehicleScopeOutcome(vs_mod.STATUS_ERROR, "table unavailable"),
        )
        resp = h.handler(self._event(VEHICLE_CLAIMS), None)
        assert resp["statusCode"] == 503
        assert "vehicle scope unavailable" in resp["body"]

    def test_malformed_claim_returns_403(self, h, monkeypatch):
        import triage_vehicle_scope as vs_mod
        monkeypatch.setattr(
            vs_mod, "check",
            lambda *a, **kw: vs_mod.VehicleScopeOutcome(vs_mod.STATUS_DENY_MALFORMED_CLAIM, "malformed"),
        )
        resp = h.handler(self._event(VEHICLE_CLAIMS), None)
        assert resp["statusCode"] == 403

    def test_no_vehicle_table_env_refuses_a_scoped_caller(self, h, monkeypatch):
        """CMS_VEHICLES_TABLE unset must not turn the check off: a scoped caller
        gets 503 and the classifier is never called. (Before the fix it was
        skipped and the call went through, which was fail-open.)"""
        import io
        monkeypatch.delenv("CMS_VEHICLES_TABLE", raising=False)
        # Re-load module so env var is fresh
        monkeypatch.setenv("CLASSIFIER_FUNCTION_ARN", "arn:aws:lambda:us-west-2:123456789012:function:clf")
        spec = importlib.util.spec_from_file_location(
            "api_triage_handler_no_table",
            os.path.join(os.path.dirname(__file__), "..", "handler.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fake_classifier = MagicMock()
        fake_classifier.invoke.return_value = {
            "Payload": io.BytesIO(json.dumps({
                "classification": "P2", "sessionId": "s", "decidedAt": "t", "latencyMs": 1,
            }).encode())
        }
        module._lambda = fake_classifier
        resp = module.handler(self._event(VEHICLE_CLAIMS), None)
        assert resp["statusCode"] == 503
        fake_classifier.invoke.assert_not_called()
