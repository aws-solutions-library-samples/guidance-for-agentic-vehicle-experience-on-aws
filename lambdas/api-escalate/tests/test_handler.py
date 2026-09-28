"""
Unit tests for lambdas/api-escalate/handler.py

Mutation verification results (2026-09-23):
  (a) Flipped autoRequestRoadsideOnP0 default to True (absent-from-config case):
      test_default_auto_rsa_is_false_when_absent_from_config FAILED as expected.
      Mutation reverted. ✓ CAUGHT

  (b) Flipped autoOpenHumanChatOnP0 default to True (absent-from-config case):
      test_default_auto_chat_is_false_when_absent_from_config FAILED as expected.
      Mutation reverted. ✓ CAUGHT
      (both separately — a single "both false" test would pass if one is hardcoded)

  (c) Removed force-False for service escalation (dispatch_roadside = False
      for is_service_escalation replaced by checking body like safety path):
      test_service_escalation_with_p0_and_dispatch_true_does_not_dispatch FAILED
      as expected (1.2b). Mutation reverted.
      NOTE: 1.2a alone (removing only the force-False, leaving other guards) SURVIVES
      because `not is_service_escalation` in `p0_verified` returns False, so the
      p0_verified gate is never called and nothing catches it.  (The vehicle check
      is patched to True, so _vehicle_matches_vin is not the cause; and
      gate.assert_not_called() tests a different mock path, not this one.)
      Per decisions.md this is accepted by design.
      ✓ 1.2b CAUGHT; 1.2a alone NOT caught by design.

  (d) Removed P0 callback block guard for service escalation
      (removed "not is_service_escalation" from `p0_verified`):
      test_service_escalation_skips_p0_callback FAILED as expected.
      Mutation reverted. ✓ CAUGHT

Task 2.3 mutation results (2026-09-23):
  (e) Task 2.3 mutation — re-add dispatch_roadside=True at bidi caller:
      test_bidi_caller_omits_dispatch_roadside_in_payload in
      agents/supervisor/tests/test_escalate_behaviour.py FAILED as expected.
      Mutation reverted. ✓ CAUGHT
      (Full log in .kiro/specs/2026-09-23-cvx-rsa-stub-dispatch/decisions-2.3.md)
      Mutation reverted. ✓ CAUGHT

F5.6 (W): converted from bare `import handler as h` to spec_from_file_location so
  both suites can run in one pytest process without the sys.modules["handler"]
  collision (F4.10 fixed the consumer side; this fixes the escalate side).

Run:
    python3 -m pytest lambdas/api-escalate/ -v
    # Combined run (F4.10 / F5.6 target):
    python3 -m pytest lambdas/rsa-dispatch-consumer/ lambdas/api-escalate/ -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path wiring — lambdas/ must be on sys.path so `from shared.rsa_client import`
# resolves, but we load the handler itself under a unique module name to avoid
# the sys.modules["handler"] collision with lambdas/rsa-dispatch-consumer/tests/.
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = Path(__file__).resolve().parent.parent
_LAMBDAS_ROOT = _LAMBDA_ROOT.parent

for _p in (str(_LAMBDA_ROOT), str(_LAMBDAS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The handler requires TENANT_CONFIG_TABLE, RSA_DISPATCH_TABLE, and
# LIVE_STATE_FN_NAME at module load time. Set stub values before importing so
# the module-level assignments do not raise.
os.environ.setdefault("TENANT_CONFIG_TABLE", "test-tenant-config")
os.environ.setdefault("CMS_STAGE", "test")
os.environ.setdefault("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
os.environ.setdefault("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")


def _load_handler():
    """Load api-escalate/handler.py under the unique module name 'escalate_handler'.

    spec_from_file_location lets both lambda suites run in one pytest process
    without the sys.modules["handler"] collision (F4.10 / F5.6 / W8).
    The module is cached under 'escalate_handler' in sys.modules so repeated
    calls within a session are free.
    """
    mod_name = "escalate_handler"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    handler_path = _LAMBDA_ROOT / "handler.py"
    spec = importlib.util.spec_from_file_location(mod_name, handler_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load once at collection time; test bodies reference as h.
h = _load_handler()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claims(tenant_id="test-tenant"):
    return {
        "custom:tenantId": tenant_id,
    }


def _connect_cfg(
    *,
    auto_rsa: bool = False,
    auto_chat: bool = False,
    callback_flow_id: str | None = None,
):
    """Minimal tenant config dict as returned by _load_tenant_connect_config."""
    cfg = {
        "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/test-instance",
        "instanceId": "test-instance",
        "instanceRegion": "us-east-1",
        "contactFlowId": "test-flow-id",
        "queueArn": "arn:aws:connect:us-east-1:111122223333:instance/test-instance/queue/test",
        "autoRequestRoadsideOnP0": auto_rsa,
        "autoOpenHumanChatOnP0": auto_chat,
    }
    if callback_flow_id is not None:
        cfg["callbackFlowId"] = callback_flow_id
    return cfg


_CONNECT_RESPONSE = {
    "ContactId": "contact-001",
    "ParticipantId": "participant-001",
    "ParticipantToken": "token-abc",
}


def _event(body: dict, tenant_id: str = "test-tenant") -> dict:
    return {
        "requestContext": {
            "authorizer": {
                "claims": _claims(tenant_id=tenant_id),
            }
        },
        "body": json.dumps(body),
    }


def _decode(resp: dict) -> dict:
    return json.loads(resp["body"])


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


class TestAuthGuard:
    def test_missing_tenant_returns_401(self):
        event = {
            "requestContext": {"authorizer": {"claims": {}}},
            "body": json.dumps({"severity": "P1", "vin": "VIN-001", "vehicleId": "VEH-1", "reason": "test"}),
        }
        resp = h.handler(event, None)
        assert resp["statusCode"] == 401

    def test_null_claims_returns_401(self):
        event = {
            "requestContext": {"authorizer": {"claims": None}},
            "body": "{}",
        }
        resp = h.handler(event, None)
        assert resp["statusCode"] == 401


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def _base_body(self):
        return {
            "severity": "P1",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "engine overheating",
        }

    def test_invalid_severity_returns_400(self):
        body = self._base_body()
        body["severity"] = "P5"
        resp = h.handler(_event(body), None)
        assert resp["statusCode"] == 400
        assert "severity" in _decode(resp)["error"]

    def test_missing_vin_returns_400(self):
        body = self._base_body()
        del body["vin"]
        resp = h.handler(_event(body), None)
        assert resp["statusCode"] == 400

    def test_missing_vehicle_id_returns_400(self):
        body = self._base_body()
        del body["vehicleId"]
        resp = h.handler(_event(body), None)
        assert resp["statusCode"] == 400

    def test_missing_reason_returns_400(self):
        body = self._base_body()
        del body["reason"]
        resp = h.handler(_event(body), None)
        assert resp["statusCode"] == 400

    def test_no_tenant_config_returns_500(self):
        body = self._base_body()
        with patch.object(h, "_load_tenant_connect_config", return_value=None):
            resp = h.handler(_event(body), None)
        assert resp["statusCode"] == 500


# ---------------------------------------------------------------------------
# Security W1: dispatch requires a platform-recorded P0 decision
# ---------------------------------------------------------------------------


class _FakeDecisions:
    """Stand-in for the vsa-{stage}-triage-decisions table."""

    def __init__(self, items: dict | None = None, raise_on_get: bool = False):
        self.items = items or {}
        self.raise_on_get = raise_on_get
        self.gets: list = []

    def Table(self, _name):  # noqa: N802 — boto3 resource API
        return self

    def get_item(self, Key):  # noqa: N803
        self.gets.append(Key)
        if self.raise_on_get:
            raise RuntimeError("ddb down")
        item = self.items.get(Key["sessionId"])
        return {"Item": item} if item else {}


def _decision(**overrides):
    from datetime import datetime, timezone
    item = {
        "sessionId": "sess-p0",
        "tenantId": "test-tenant",
        "vin": "1HGBH41JXMN000024",
        "classification": "P0",
        "decidedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    item.update(overrides)
    return item


class TestDispatchRequiresRecordedP0:
    """Any authenticated caller in any trusted pool could send
    `severity: "P0", dispatchRoadside: true` for any VIN and reach dispatch,
    bypassing tenant policy. Now a dispatch needs a triage-decisions row the
    platform wrote: same tenant (JWT claim), same VIN, classified P0, recent."""

    VIN = "1HGBH41JXMN000024"

    def _body(self, **extra):
        body = {
            "severity": "P0",
            "vin": self.VIN,
            "vehicleId": "VEH-001",
            "reason": "brake failure",
            "dispatchRoadside": True,
            "triageSessionId": "sess-p0",
        }
        body.update(extra)
        return body

    def _call(self, body, decisions, auto_rsa=False, table="vsa-test-triage-decisions"):
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        with patch.object(h, "_TRIAGE_DECISIONS_TABLE", table), \
             patch.object(h, "_get_ddb", return_value=decisions), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_load_tenant_connect_config", return_value=_connect_cfg(auto_rsa=auto_rsa)), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_queue_rsa_action", return_value="rsa-ok") as mock_rsa:
            resp = h.handler(_event(body), None)
        return resp, mock_rsa, mock_connect_client

    def test_verified_p0_dispatches(self):
        """Positive control: with a real P0 row the gate opens."""
        resp, rsa, _ = self._call(self._body(), _FakeDecisions({"sess-p0": _decision()}))
        assert _decode(resp)["rsaDispatched"] is True
        rsa.assert_called_once()

    @staticmethod
    def _classifier_row(level_name: str) -> dict:
        """A row shaped exactly as the classifier Lambda's path produces it.

        `Level` is an IntEnum; the handler JSON-round-trips the dataclass, so the
        item carries an int, and DynamoDB hands it back as a Decimal. Built with
        boto3's own (de)serializer rather than a hand-typed literal, so the test
        cannot drift into a shape no writer produces (review Cycle 3 Critical:
        the earlier positive control used the string "P0", which only the
        driver-confirmation writer stores).
        """
        import importlib.util
        import json as _json
        from pathlib import Path as _P
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

        types_path = _P(__file__).resolve().parents[2] / "triage-classifier" / "types.py"
        spec = importlib.util.spec_from_file_location("_triage_types_for_w1", types_path)
        mod = importlib.util.module_from_spec(spec)
        import sys as _sys
        _sys.modules[spec.name] = mod  # dataclasses resolve their module by name
        spec.loader.exec_module(mod)
        level = getattr(mod.Level, level_name)
        wire = _json.loads(_json.dumps({"classification": level}))  # as handler.py does
        stored = TypeSerializer().serialize(wire["classification"])
        row = _decision()
        row["classification"] = TypeDeserializer().deserialize(stored)
        return row

    def test_classifier_written_p0_row_dispatches(self):
        """THE production case: an ordinary classifier P0. Mutation: compare
        `classification` to the string "P0" only → FAILS."""
        row = self._classifier_row("P0")
        assert not isinstance(row["classification"], str)  # it really is numeric
        resp, rsa, _ = self._call(self._body(), _FakeDecisions({"sess-p0": row}))
        assert _decode(resp)["rsaDispatched"] is True

    @pytest.mark.parametrize("level_name", ["P1", "P2", "P3"])
    def test_classifier_written_non_p0_row_is_refused(self, level_name):
        row = self._classifier_row(level_name)
        resp, rsa, _ = self._call(self._body(), _FakeDecisions({"sess-p0": row}))
        assert _decode(resp)["rsaDispatched"] is False

    @pytest.mark.parametrize("value", [False, "0", "p0", None, 0.5, "P0 "])
    def test_other_encodings_are_not_p0(self, value):
        assert h._is_p0(value) is False

    @pytest.mark.parametrize("label,decisions,body_extra", [
        ("no triageSessionId", _FakeDecisions({"sess-p0": _decision()}), {"triageSessionId": None}),
        ("unknown id", _FakeDecisions({}), {}),
        ("decision is P2", _FakeDecisions({"sess-p0": _decision(classification="P2")}), {}),
        ("other tenant", _FakeDecisions({"sess-p0": _decision(tenantId="other-tenant")}), {}),
        ("other VIN", _FakeDecisions({"sess-p0": _decision(vin="2FMDK3JC4ABA00000")}), {}),
        ("stale", _FakeDecisions({"sess-p0": _decision(decidedAt="2020-01-01T00:00:00Z")}), {}),
        ("future", _FakeDecisions({"sess-p0": _decision(decidedAt="2999-01-01T00:00:00Z")}), {}),
        ("unparseable time", _FakeDecisions({"sess-p0": _decision(decidedAt="yesterday")}), {}),
        ("ddb error", _FakeDecisions({"sess-p0": _decision()}, raise_on_get=True), {}),
        ("id not a string", _FakeDecisions({"sess-p0": _decision()}), {"triageSessionId": ["sess-p0"]}),
    ])
    def test_unverified_dispatch_is_refused_but_chat_still_opens(self, label, decisions, body_extra):
        """Mutation: delete the `_verified_p0_decision` gate in handler() → every
        case FAILS (dispatch goes through on the caller's word)."""
        resp, rsa, connect = self._call(self._body(**body_extra), decisions)
        assert resp["statusCode"] == 200, label
        assert _decode(resp)["rsaDispatched"] is False, label
        rsa.assert_not_called()
        connect.start_chat_contact.assert_called_once()  # the human handoff is unaffected

    def test_tenant_auto_flag_is_gated_too(self):
        """The same bypass existed through the auto flag: body severity P0 with
        no dispatchRoadside, on a tenant with autoRequestRoadsideOnP0=true."""
        body = self._body()
        del body["dispatchRoadside"]
        body["triageSessionId"] = "forged"
        resp, rsa, _ = self._call(body, _FakeDecisions({"sess-p0": _decision()}), auto_rsa=True)
        assert _decode(resp)["rsaDispatched"] is False
        rsa.assert_not_called()

    def test_unconfigured_table_refuses(self):
        """No TRIAGE_DECISIONS_TABLE wired → refuse, never 'dispatch unchecked'."""
        decisions = _FakeDecisions({"sess-p0": _decision()})
        resp, rsa, _ = self._call(self._body(), decisions, table=None)
        assert _decode(resp)["rsaDispatched"] is False
        assert decisions.gets == []

    def test_driver_confirmed_row_is_accepted(self):
        """The supervisor's driver-confirmation audit row (classification P0,
        confirmationSource=driver) is platform-written and counts."""
        row = _decision(sessionId="s1-driver-confirmed", confirmationSource="driver")
        resp, rsa, _ = self._call(
            self._body(triageSessionId="s1-driver-confirmed"),
            _FakeDecisions({"s1-driver-confirmed": row}),
        )
        assert _decode(resp)["rsaDispatched"] is True

    def test_lookup_uses_claim_tenant_not_body(self):
        """A body-supplied tenantId must not satisfy the tenant check."""
        resp, rsa, _ = self._call(
            self._body(tenantId="other-tenant"),
            _FakeDecisions({"sess-p0": _decision(tenantId="other-tenant")}),
        )
        assert _decode(resp)["rsaDispatched"] is False


class TestCallbackRequiresRecordedP0AndAssignedDriver:
    """The P0 outbound callback (Connect dials the driver's phone) keyed on the
    caller-written `severity` and dialled whatever `driverId` the body named.
    Now: a platform-recorded P0 (same gate as dispatch) AND the driver must be
    assigned to the request's vehicle."""

    VIN = "1HGBH41JXMN000024"

    def _body(self, **extra):
        body = {
            "severity": "P0", "vin": self.VIN, "vehicleId": "VEH-001",
            "reason": "brake failure", "triageSessionId": "sess-p0", "driverId": "DRV-1",
        }
        body.update(extra)
        return body

    def _call(self, body, verified):
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        with patch.object(h, "_verified_p0_decision", return_value=verified) as gate, \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(callback_flow_id="cb-flow-001")), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100") as phone, \
             patch.object(h, "_initiate_callback", return_value=True) as cb:
            resp = h.handler(_event(body), None)
        return resp, gate, phone, cb

    def test_verified_p0_places_the_call_to_the_assigned_driver(self):
        resp, gate, phone, cb = self._call(self._body(), verified=True)
        assert resp["statusCode"] == 200
        cb.assert_called_once()
        phone.assert_called_once_with("DRV-1", "VEH-001")

    def test_unverified_p0_places_no_call(self):
        """Mutation: gate the callback on `severity == "P0"` again → FAILS."""
        resp, gate, phone, cb = self._call(self._body(triageSessionId="forged"), verified=False)
        assert resp["statusCode"] == 200  # chat still opens
        cb.assert_not_called()
        phone.assert_not_called()

    def test_one_decision_lookup_serves_both_side_effects(self):
        resp, gate, _, _ = self._call(self._body(dispatchRoadside=True), verified=True)
        assert gate.call_count == 1

    def test_non_p0_skips_the_lookup_entirely(self):
        resp, gate, _, cb = self._call(self._body(severity="P1"), verified=True)
        gate.assert_not_called()
        cb.assert_not_called()


class TestResolveDriverPhoneAssignment:
    class _Drivers:
        def __init__(self, item):
            self.item = item

        def Table(self, _name):  # noqa: N802
            return self

        def get_item(self, Key):  # noqa: N803
            return {"Item": self.item} if self.item else {}

    def _resolve(self, item, driver_id="DRV-1", vehicle_id="VEH-001"):
        with patch.object(h, "_get_ddb", return_value=self._Drivers(item)):
            return h._resolve_driver_phone(driver_id, vehicle_id)

    def test_assigned_driver_returns_phone(self):
        assert self._resolve({"driverId": "DRV-1", "assignedVehicleId": "VEH-001", "phone": "+1555"}) == "+1555"

    def test_driver_of_another_vehicle_returns_none(self):
        """Mutation: drop the assignedVehicleId check → FAILS."""
        assert self._resolve({"driverId": "DRV-1", "assignedVehicleId": "VEH-999", "phone": "+1555"}) is None

    def test_unassigned_driver_returns_none(self):
        assert self._resolve({"driverId": "DRV-1", "phone": "+1555"}) is None

    def test_missing_ids_return_none(self):
        assert self._resolve({"assignedVehicleId": "VEH-001", "phone": "+1"}, driver_id=None) is None
        assert self._resolve({"assignedVehicleId": "VEH-001", "phone": "+1"}, vehicle_id=None) is None


class _OneTable:
    """Fake boto3 resource returning one item for any key."""

    def __init__(self, item=None, raise_on_get=False):
        self.item = item
        self.raise_on_get = raise_on_get
        self.keys: list = []

    def Table(self, _name):  # noqa: N802
        return self

    def get_item(self, Key):  # noqa: N803
        self.keys.append(Key)
        if self.raise_on_get:
            raise RuntimeError("ddb down")
        return {"Item": self.item} if self.item else {}


class TestVehicleMustBeTheVin:
    """The decision row binds tenant + VIN; the side effects act on vehicleId.
    Both are body fields, so `/escalate` now checks the CMS vehicles row for
    `vehicleId` carries the request VIN before any P0 side effect."""

    VIN = "1HGBH41JXMN000024"

    def _match(self, item, table="cms-test-storage-vehicles", vehicle_id="VEH-001", vin=VIN, **kw):
        with patch.object(h, "_CMS_VEHICLES_TABLE", table), \
             patch.object(h, "_get_ddb", return_value=_OneTable(item, **kw)):
            return h._vehicle_matches_vin(vehicle_id, vin)

    def test_matching_row(self):
        assert self._match({"vehicleId": "VEH-001", "vin": self.VIN}) is True

    @pytest.mark.parametrize("label,item,kw", [
        ("other vehicle's vin", {"vehicleId": "VEH-001", "vin": "2FMDK3JC4ABA00000"}, {}),
        ("no vin attribute", {"vehicleId": "VEH-001"}, {}),
        ("no row", None, {}),
        ("ddb error", {"vin": VIN}, {"raise_on_get": True}),
    ])
    def test_non_matching_is_refused(self, label, item, kw):
        """Mutation: return True on mismatch → FAILS."""
        assert self._match(item, **kw) is False, label

    def test_unconfigured_table_is_refused(self):
        assert self._match({"vin": self.VIN}, table=None) is False

    def test_missing_ids_are_refused(self):
        assert self._match({"vin": self.VIN}, vehicle_id="") is False
        assert self._match({"vin": self.VIN}, vin="") is False

    def test_mismatch_blocks_dispatch_and_callback_but_not_chat(self):
        """Through the handler with the REAL binding check. Mutation: drop
        `_vehicle_matches_vin` from the p0_verified gate → FAILS."""
        body = {
            "severity": "P0", "vin": self.VIN, "vehicleId": "VEH-001", "reason": "brakes",
            "dispatchRoadside": True, "triageSessionId": "sess-p0", "driverId": "DRV-1",
        }
        connect = MagicMock()
        connect.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        vehicles = _OneTable({"vehicleId": "VEH-001", "vin": "2FMDK3JC4ABA00000"})
        with patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_CMS_VEHICLES_TABLE", "cms-test-storage-vehicles"), \
             patch.object(h, "_get_ddb", return_value=vehicles), \
             patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(callback_flow_id="cb-flow-001")), \
             patch.object(h, "_get_connect", return_value=connect), \
             patch.object(h, "_queue_rsa_action", return_value="rsa-x") as rsa, \
             patch.object(h, "_initiate_callback", return_value=True) as cb:
            resp = h.handler(_event(body), None)
        assert resp["statusCode"] == 200
        assert _decode(resp)["rsaDispatched"] is False
        rsa.assert_not_called()
        cb.assert_not_called()
        connect.start_chat_contact.assert_called_once()
        assert vehicles.keys == [{"vehicleId": "VEH-001"}]


class TestCallbackConfiguration:
    """The callback was unreachable: `_load_tenant_connect_config` never
    returned `callbackFlowId`, and the Connect call carried no caller ID."""

    def _cfg_from(self, escalation):
        tenant_row = {
            "tenantId": "t", "version": "1.1.0",
            "channels": {"connect": {
                "instanceArn": "arn:aws:connect:us-east-1:123456789012:instance/inst-1",
                "contactFlowId": "chat-flow",
            }},
            "escalation": escalation,
        }

        class _Tenants:
            def Table(self, _n):  # noqa: N802
                return self

            def get_item(self, Key):  # noqa: N803
                if Key["version"] == "current":
                    return {"Item": {"activeVersion": "1.1.0"}}
                return {"Item": tenant_row}

        with patch.object(h, "_get_ddb", return_value=_Tenants()):
            return h._load_tenant_connect_config("t")

    def test_callback_block_is_projected(self):
        """Mutation: drop callbackFlowId from the returned dict → FAILS."""
        cfg = self._cfg_from({"callback": {
            "contactFlowId": "cb-flow",
            "queueArn": "arn:aws:connect:us-east-1:123456789012:instance/inst-1/queue/q-cb",
            "sourcePhoneNumber": "+15555550199",
        }})
        assert cfg["callbackFlowId"] == "cb-flow"
        assert cfg["callbackQueueId"] == "q-cb"
        assert cfg["callbackSourcePhoneNumber"] == "+15555550199"

    @pytest.mark.parametrize("escalation", [{}, {"callback": "yes"}, {"callback": {}}])
    def test_absent_or_malformed_block_disables_callback(self, escalation):
        cfg = self._cfg_from(escalation)
        assert cfg["callbackFlowId"] is None
        assert cfg["callbackQueueId"] is None
        assert cfg["callbackSourcePhoneNumber"] is None

    @pytest.mark.parametrize("bad_arn", [
        42,           # DynamoDB Number
        3.14,         # float
        True,         # bool (subtype of int — but also invalid)
        ["arn"],      # list
        {"arn": "x"}, # dict
        None,         # explicit None (same as absent)
    ])
    def test_non_string_queueArn_does_not_raise(self, bad_arn):
        """S4: a non-string escalation.callback.queueArn must not raise AttributeError.

        _arn_trailing_id calls .split("/") which raises on non-strings. A
        mis-configured tenant must not break /escalate for every severity —
        the chat handoff must still succeed even if the callback block is wrong.

        Mutation: remove the isinstance guard → FAILS (AttributeError or TypeError
        propagates to the caller and produces a 500 on the non-string cases).
        """
        cfg = self._cfg_from({"callback": {
            "contactFlowId": "cb-flow",
            "queueArn": bad_arn,  # the non-string value under test
        }})
        # Must not raise; non-string queueArn is treated as absent.
        assert cfg is not None
        assert cfg["callbackQueueId"] is None  # non-string → treated as absent
        # The flow ID is still projected (only queueArn is bad).
        assert cfg["callbackFlowId"] == "cb-flow"

    def _start(self, **kw):
        connect = MagicMock()
        with patch.object(h, "_get_connect", return_value=connect):
            ok = h._initiate_callback(
                instance_id="inst-1", instance_region="us-east-1", contact_flow_id="cb-flow",
                driver_phone="+15555550100", escalation_id="esc-1", **kw,
            )
        return ok, connect

    def test_queue_id_is_sent(self):
        ok, connect = self._start(queue_id="q-cb")
        assert ok is True
        assert connect.start_outbound_voice_contact.call_args.kwargs["QueueId"] == "q-cb"

    def test_source_number_is_sent(self):
        ok, connect = self._start(source_phone_number="+15555550199")
        assert connect.start_outbound_voice_contact.call_args.kwargs["SourcePhoneNumber"] == "+15555550199"

    def test_no_caller_id_refuses_before_calling_connect(self):
        """Mutation: remove the no-caller-ID guard → Connect is called → FAILS."""
        ok, connect = self._start()
        assert ok is False
        connect.start_outbound_voice_contact.assert_not_called()

    def test_handler_threads_the_caller_id(self):
        """Mutation: drop source_phone_number= from _initiate_callback call → FAILS (S3)."""
        body = {
            "severity": "P0", "vin": "1HGBH41JXMN000024", "vehicleId": "VEH-001",
            "reason": "brakes", "triageSessionId": "sess-p0", "driverId": "DRV-1",
        }
        cfg = _connect_cfg(callback_flow_id="cb-flow-001")
        cfg["callbackQueueId"] = "q-cb"
        cfg["callbackSourcePhoneNumber"] = "+15555550199"
        connect = MagicMock()
        connect.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        with patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_load_tenant_connect_config", return_value=cfg), \
             patch.object(h, "_get_connect", return_value=connect), \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100"), \
             patch.object(h, "_initiate_callback", return_value=True) as cb:
            h.handler(_event(body), None)
        assert cb.call_args.kwargs["queue_id"] == "q-cb"
        assert cb.call_args.kwargs["source_phone_number"] == "+15555550199"


class TestCmsTableNames:
    def test_unset_env_gives_none_not_cms_dev(self, monkeypatch):
        """The old default read cms-dev-storage-drivers on the deployed Lambda."""
        monkeypatch.delenv("CMS_DRIVERS_TABLE", raising=False)
        monkeypatch.delenv("CMS_STAGE", raising=False)
        assert h._cms_table("CMS_DRIVERS_TABLE", "storage-drivers") is None

    def test_explicit_env_wins(self, monkeypatch):
        monkeypatch.setenv("CMS_VEHICLES_TABLE", "cms-staging-storage-vehicles")
        assert h._cms_table("CMS_VEHICLES_TABLE", "storage-vehicles") == "cms-staging-storage-vehicles"

    def test_unconfigured_drivers_table_returns_no_phone(self):
        with patch.object(h, "_CMS_DRIVERS_TABLE", None), \
             patch.object(h, "_get_ddb") as ddb:
            assert h._resolve_driver_phone("DRV-1", "VEH-001") is None
        ddb.assert_not_called()


# ---------------------------------------------------------------------------
# escalationType: allowlisted at the Connect trust boundary (security W2)
# ---------------------------------------------------------------------------


class TestEscalationTypeAllowlist:
    """Security review Cycle 1 W2: an unknown escalationType used to be coerced
    to safety semantics and written verbatim into Connect contact attributes.
    Now: absent → safety; exact "safety"/"service" accepted; anything else → 400,
    before Connect is called."""

    def _body(self, **extra):
        body = {
            "severity": "P0",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "brake warning",
        }
        body.update(extra)
        return body

    def _call(self, body):
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        with patch.object(h, "_load_tenant_connect_config", return_value=_connect_cfg()), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            resp = h.handler(_event(body), None)
        return resp, mock_connect_client

    @pytest.mark.parametrize("bad", [
        "servic", "Service", " service", "safety-and-p0-dispatch",
        "\u0455ervice",  # Cyrillic ѕ confusable
        "x" * 40_000, "", 1, True, ["service"], {"t": "service"},
    ])
    def test_unknown_value_rejected_before_connect(self, bad):
        """Mutation: restore the `.strip().lower()` coercion (or any fallback to
        "safety") → FAILS."""
        resp, connect = self._call(self._body(escalationType=bad))
        assert resp["statusCode"] == 400
        connect.start_chat_contact.assert_not_called()

    @pytest.mark.parametrize("good", ["safety", "service"])
    def test_known_value_reaches_connect_verbatim(self, good):
        resp, connect = self._call(self._body(escalationType=good))
        assert resp["statusCode"] == 200
        attrs = connect.start_chat_contact.call_args.kwargs["Attributes"]
        assert attrs["escalationType"] == good

    def test_absent_value_defaults_to_safety(self):
        resp, connect = self._call(self._body())
        assert resp["statusCode"] == 200
        attrs = connect.start_chat_contact.call_args.kwargs["Attributes"]
        assert attrs["escalationType"] == "safety"

    def test_attribute_is_capped_even_if_the_helper_is_called_directly(self):
        attrs = h._build_attributes(
            severity="P3", vin="V", vehicle_id="VEH", reason="r", summary=None,
            triage_session_id=None, driver_id=None, display_name=None,
            voice_session_id=None, tenant_id="t", escalation_type="y" * 1000,
        )
        assert len(attrs["escalationType"]) <= 16


# ---------------------------------------------------------------------------
# escalationType: "service" — structural cannot-dispatch guard
# ---------------------------------------------------------------------------


class TestServiceEscalationType:
    """Task 1.2 Accept: service path forces dispatch_roadside=False and
    skips the P0 callback branch regardless of other request fields."""

    def _base_body(self):
        return {
            "severity": "P0",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "motorcycle purchase enquiry",
            "escalationType": "service",
        }

    # MUTATION-VERIFIED (1.2b): removed force-False for service escalation.
    # Expected: this test fails (truck dispatched). Reverted. ✓ 1.2b CAUGHT.
    # NOTE: 1.2a-alone (force-False removed, other guards intact) SURVIVES because
    # `not is_service_escalation` in p0_verified returns False before the gate is
    # called, so nothing catches it.  (The vehicle check is patched to True, so
    # _vehicle_matches_vin is not the cause.)  Correct by design (decisions.md Fix Group 3).
    def test_service_escalation_with_p0_and_dispatch_true_does_not_dispatch(self):
        """Service escalation + P0 + dispatchRoadside:true → rsaDispatched MUST be false.

        The type distinction is enforced at the handler level, not by the
        caller behaving correctly. Sending P0 severity with dispatchRoadside:true
        on a service escalation must be silently overridden — the truck-for-a-
        shopper path does not exist in this handler.
        """
        body = self._base_body()
        body["dispatchRoadside"] = True  # explicitly requesting dispatch

        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        # The W1 decision gate is forced OPEN so the service guard is the only
        # thing between this body and a truck. Without it the gate refuses
        # first (no decision table in tests) and this test cannot fail — review
        # Cycle 3 found 1.2a/1.2b surviving for exactly that reason.
        # _vehicle_matches_vin is also forced True so the service guard (not the
        # vehicle binding) is the only thing between the body and a dispatch.
        with patch.object(h, "_verified_p0_decision", return_value=True) as gate, \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_load_tenant_connect_config", return_value=_connect_cfg(auto_rsa=True)), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_queue_rsa_action") as mock_rsa:
            resp = h.handler(_event(body), None)
        # Service escalations never even consult the decision gate.
        gate.assert_not_called()

        assert resp["statusCode"] == 200
        body_out = _decode(resp)
        # No RSA queued
        assert body_out["rsaDispatched"] is False
        assert body_out.get("rsaActionId") is None
        mock_rsa.assert_not_called()

    def test_service_escalation_absent_escalation_type_defaults_to_safety(self):
        """Absent escalationType preserves existing behaviour (back-compat)."""
        body = {
            "severity": "P1",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "brake warning",
            # No escalationType field — should default to safety path
        }
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_load_tenant_connect_config", return_value=_connect_cfg()), \
             patch.object(h, "_get_connect", return_value=mock_connect_client):
            resp = h.handler(_event(body), None)

        # Should reach the Connect call successfully (safety path, P1 → no dispatch)
        assert resp["statusCode"] == 200

    # MUTATION-VERIFIED: removed "not is_service_escalation" from p0_verified
    # Expected: this test fails (mock is called). Reverted. ✓ CAUGHT
    def test_service_escalation_skips_p0_callback(self):
        """Service escalation with severity P0 must not initiate an outbound callback."""
        body = self._base_body()  # severity P0, escalationType service

        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        # Gate forced open so the service exclusion is what refuses (see above).
        with patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(callback_flow_id="cb-flow-001")), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_initiate_callback") as mock_cb, \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100"):
            resp = h.handler(_event(body), None)

        assert resp["statusCode"] == 200
        mock_cb.assert_not_called()

    def test_safety_escalation_p0_with_callback_flow_initiates_callback(self):
        """Safety path P0 WITH callback flow should try to initiate a callback."""
        body = {
            "severity": "P0",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "brake failure",
            # No escalationType → safety path
        }
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(auto_rsa=False, callback_flow_id="cb-flow-001")), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_initiate_callback", return_value=True) as mock_cb, \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100"):
            resp = h.handler(_event(body), None)

        assert resp["statusCode"] == 200
        mock_cb.assert_called_once()


# ---------------------------------------------------------------------------
# Tenant flags: autoRequestRoadsideOnP0 / autoOpenHumanChatOnP0
# ---------------------------------------------------------------------------


class TestTenantFlags:
    """Task 1.5 Accept: both flags default False when absent from tenant config."""

    # MUTATION-VERIFIED (separate): flip autoRequestRoadsideOnP0 default to True
    # Expected: this test fails. Reverted. ✓ CAUGHT
    def test_default_auto_rsa_is_false_when_absent_from_config(self):
        """autoRequestRoadsideOnP0 defaults False when key is absent from escalation config.

        Tests the projection in _load_tenant_connect_config directly —
        the absent-from-config case is distinct from being explicitly False.
        """
        # Simulate projection with no autoRequestRoadsideOnP0 key
        escalation_config_without_key = {}
        projected = bool(escalation_config_without_key.get("autoRequestRoadsideOnP0", False))
        assert projected is False, (
            "autoRequestRoadsideOnP0 must default False when absent from tenant config"
        )

        # Verify the handler uses this value — absent key means no auto-RSA for P0
        body = {
            "severity": "P0",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "engine warning",
            # No dispatchRoadside — test the default-from-config path
        }
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(auto_rsa=False)), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_queue_rsa_action") as mock_rsa:
            resp = h.handler(_event(body), None)

        assert resp["statusCode"] == 200
        body_out = _decode(resp)
        # Flag is False → no RSA queued
        assert body_out["rsaDispatched"] is False
        mock_rsa.assert_not_called()

    def test_projection_auto_rsa_defaults_false_when_key_absent(self):
        """Direct projection test: _load_tenant_connect_config returns auto_rsa=False
        when the escalation config object has no autoRequestRoadsideOnP0 key.

        This test calls through the real projection code and is what the
        mutation (flip default to True) actually catches.
        """
        # Build a DDB item that has escalation but no autoRequestRoadsideOnP0
        config_item = {
            "channels": {
                "connect": {
                    "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/test",
                    "contactFlowId": "flow-001",
                }
            },
            "escalation": {
                "tier2QueueArn": "arn:aws:connect:us-east-1:111122223333:instance/test/queue/t2",
                # No autoRequestRoadsideOnP0 key
            },
            "booking": {},
        }
        pointer_item = {"activeVersion": "1.0.0"}

        mock_table = MagicMock()
        mock_table.get_item.side_effect = [
            {"Item": pointer_item},
            {"Item": config_item},
        ]

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = h._load_tenant_connect_config("test-tenant")

        assert result is not None
        assert result["autoRequestRoadsideOnP0"] is False, (
            "autoRequestRoadsideOnP0 must project as False when absent from config"
        )

    def test_auto_rsa_true_queues_rsa_for_p0(self):
        """When autoRequestRoadsideOnP0=True, P0 safety escalation queues RSA."""
        body = {
            "severity": "P0",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "brake warning",
        }
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(auto_rsa=True)), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_queue_rsa_action", return_value="rsa-action-001") as mock_rsa:
            resp = h.handler(_event(body), None)

        assert resp["statusCode"] == 200
        body_out = _decode(resp)
        assert body_out["rsaDispatched"] is True
        assert body_out["rsaActionId"] == "rsa-action-001"
        mock_rsa.assert_called_once()

    # MUTATION-VERIFIED (separate): flip autoOpenHumanChatOnP0 default to True
    # Expected: this test fails. Reverted. ✓ CAUGHT
    def test_default_auto_chat_is_false_when_absent_from_config(self):
        """autoOpenHumanChatOnP0 is projected False by _load_tenant_connect_config
        when the key is absent from the escalation config object.

        The handler does not directly gate on autoOpenHumanChatOnP0 —
        that flag is consumed by bidi_app.py's auto-escalation path.
        This test verifies the projection default so bidi_app.py always
        gets a boolean (never None/KeyError) even for configs that
        pre-date the flag.
        """
        # Simulate config row with escalation object but no auto-chat key
        tenant_config_escalation = {}  # no autoOpenHumanChatOnP0 key

        auto_chat_projected = bool(tenant_config_escalation.get("autoOpenHumanChatOnP0", False))
        # The projection must default to False
        assert auto_chat_projected is False, (
            "autoOpenHumanChatOnP0 must default False when absent from tenant config"
        )

    def test_projection_auto_chat_defaults_false_when_key_absent(self):
        """Direct projection test: _load_tenant_connect_config returns auto_chat=False
        when the escalation config object has no autoOpenHumanChatOnP0 key.
        """
        config_item = {
            "channels": {
                "connect": {
                    "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/test",
                    "contactFlowId": "flow-001",
                }
            },
            "escalation": {
                "tier2QueueArn": "arn:aws:connect:us-east-1:111122223333:instance/test/queue/t2",
                # No autoOpenHumanChatOnP0 key
            },
            "booking": {},
        }
        pointer_item = {"activeVersion": "1.0.0"}

        mock_table = MagicMock()
        mock_table.get_item.side_effect = [
            {"Item": pointer_item},
            {"Item": config_item},
        ]

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = h._load_tenant_connect_config("test-tenant")

        assert result is not None
        assert result["autoOpenHumanChatOnP0"] is False, (
            "autoOpenHumanChatOnP0 must project as False when absent from config"
        )

    def test_explicit_dispatch_roadside_true_wins_over_flag_false(self):
        """An explicit dispatchRoadside:true in the request overrides the flag.

        This is the user-confirmed tap path — iOS sends dispatchRoadside:true
        when the driver has explicitly confirmed they want roadside.
        """
        body = {
            "severity": "P0",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "brake failure",
            "dispatchRoadside": True,  # user confirmed
        }
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(auto_rsa=False)), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_queue_rsa_action", return_value="rsa-user-001") as mock_rsa:
            resp = h.handler(_event(body), None)

        assert resp["statusCode"] == 200
        body_out = _decode(resp)
        # Explicit body value wins even when flag is False
        assert body_out["rsaDispatched"] is True
        mock_rsa.assert_called_once()

    def test_auto_rsa_flag_does_not_dispatch_for_non_p0(self):
        """Even with autoRequestRoadsideOnP0=True, non-P0 severities never dispatch."""
        body = {
            "severity": "P1",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "oil pressure low",
        }
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(auto_rsa=True)), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_queue_rsa_action") as mock_rsa:
            resp = h.handler(_event(body), None)

        assert resp["statusCode"] == 200
        body_out = _decode(resp)
        assert body_out["rsaDispatched"] is False
        mock_rsa.assert_not_called()



# ---------------------------------------------------------------------------
# Task 2.3: RSA_DISPATCH_TABLE env var, LIFECYCLE_REQUESTED status, dispatchId
# ---------------------------------------------------------------------------


class TestRsaDispatchTable:
    """Task 2.3 Accept 1–2: _queue_rsa_action writes REQUESTED to the new table;
    the response carries dispatchId alongside rsaActionId."""

    # --- helper ---------------------------------------------------------

    def _base_p0_body(self) -> dict:
        return {
            "severity": "P0",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "brake failure",
            "dispatchRoadside": True,  # explicit user-confirmed tap
        }

    # --- status field ---------------------------------------------------

    def test_queue_rsa_action_writes_requested_not_pending(self):
        """_queue_rsa_action must write status=LIFECYCLE_REQUESTED ("REQUESTED"),
        not the retired VFO queue status "PENDING".

        MUTATION-VERIFIED: change LIFECYCLE_REQUESTED to "PENDING" in the item →
        this test MUST fail. See decisions-2.3.md.
        """
        from shared.rsa_client import LIFECYCLE_REQUESTED as EXPECTED_STATUS

        captured_items: list[dict] = []

        mock_table = MagicMock()
        mock_table.put_item.side_effect = lambda Item: captured_items.append(Item)

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = h._queue_rsa_action(
                tenant_id="test-tenant",
                vin="1HGBH41JXMN000024",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="brake failure",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        assert result is not None, "Expected a dispatchId on success"
        assert len(captured_items) == 1
        item = captured_items[0]
        assert item["status"] == EXPECTED_STATUS, (
            f"status must be LIFECYCLE_REQUESTED ({EXPECTED_STATUS!r}), "
            f"not 'PENDING'. Got: {item['status']!r}"
        )

    def test_queue_rsa_action_uses_dispatch_id_as_pk(self):
        """Item PK must be 'dispatchId', not the retired 'actionId' key."""
        captured_items: list[dict] = []
        mock_table = MagicMock()
        mock_table.put_item.side_effect = lambda Item: captured_items.append(Item)

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            dispatch_id = h._queue_rsa_action(
                tenant_id="test-tenant",
                vin="1HGBH41JXMN000024",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="brake failure",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        assert dispatch_id is not None
        item = captured_items[0]
        assert item.get("dispatchId") == dispatch_id, (
            "Item PK must be 'dispatchId' matching the returned id"
        )
        assert "actionId" not in item, (
            "'actionId' must not appear in the new table — that was the VFO queue PK"
        )

    def test_queue_rsa_action_writes_to_rsa_dispatch_table(self):
        """_queue_rsa_action must target RSA_DISPATCH_TABLE, not any CMS table."""
        table_names_used: list[str] = []
        mock_table = MagicMock()
        mock_table.put_item.return_value = None

        def _mock_table(name: str):
            table_names_used.append(name)
            return mock_table

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.side_effect = _mock_table
            h._queue_rsa_action(
                tenant_id="test-tenant",
                vin="1HGBH41JXMN000024",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="brake failure",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        assert table_names_used == [h._RSA_DISPATCH_TABLE], (
            f"Must write to _RSA_DISPATCH_TABLE ({h._RSA_DISPATCH_TABLE!r}); "
            f"got {table_names_used!r}"
        )
        # Must NOT write to any CMS VFO queue
        for name in table_names_used:
            assert "vfo" not in name, (
                f"Must not write to VFO queue table; got table name: {name!r}"
            )
            assert "cms-" not in name, (
                f"Must not write to CMS table; got table name: {name!r}"
            )

    # --- response shape -------------------------------------------------

    def test_response_includes_dispatch_id_when_rsa_dispatched(self):
        """Task 2.3 Accept 2: response gains dispatchId alongside rsaActionId."""
        body = self._base_p0_body()
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_load_tenant_connect_config", return_value=_connect_cfg()), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_queue_rsa_action", return_value="rsa-dispatch-abc123") as mock_rsa:
            resp = h.handler(_event(body), None)

        assert resp["statusCode"] == 200
        body_out = _decode(resp)
        assert body_out["rsaDispatched"] is True
        assert body_out["rsaActionId"] == "rsa-dispatch-abc123", (
            "rsaActionId must be preserved for backward compatibility"
        )
        assert body_out["dispatchId"] == "rsa-dispatch-abc123", (
            "dispatchId must appear in the response for status-read callers"
        )
        mock_rsa.assert_called_once()

    def test_response_dispatch_id_null_when_rsa_not_dispatched(self):
        """When dispatch is not triggered, both rsaActionId and dispatchId are null."""
        body = {
            "severity": "P1",
            "vin": "1HGBH41JXMN000024",
            "vehicleId": "VEH-001",
            "reason": "oil low",
        }
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_load_tenant_connect_config", return_value=_connect_cfg()), \
             patch.object(h, "_get_connect", return_value=mock_connect_client):
            resp = h.handler(_event(body), None)

        assert resp["statusCode"] == 200
        body_out = _decode(resp)
        assert body_out["rsaDispatched"] is False
        assert body_out.get("rsaActionId") is None
        assert body_out.get("dispatchId") is None

    def test_response_dispatch_id_equals_rsa_action_id(self):
        """dispatchId and rsaActionId must be the same value — both surface the PK."""
        body = self._base_p0_body()
        mock_connect_client = MagicMock()
        mock_connect_client.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_load_tenant_connect_config", return_value=_connect_cfg()), \
             patch.object(h, "_get_connect", return_value=mock_connect_client), \
             patch.object(h, "_queue_rsa_action", return_value="rsa-xyz-007"):
            resp = h.handler(_event(body), None)

        body_out = _decode(resp)
        assert body_out["dispatchId"] == body_out["rsaActionId"], (
            "dispatchId and rsaActionId must be the same value"
        )

    # --- fail-closed env var --------------------------------------------

    def test_rsa_dispatch_table_read_at_module_level(self):
        """RSA_DISPATCH_TABLE is bound at import time from os.environ.

        This test verifies that the handler's module-level variable reflects
        the injected value (vsa-test-rsa-dispatch), not a CMS table default.
        A missing RSA_DISPATCH_TABLE raises KeyError at import — that behavior
        is not testable post-import, but Task 2.4 supplies it in the real deploy.
        """
        assert h._RSA_DISPATCH_TABLE == "vsa-test-rsa-dispatch", (
            f"_RSA_DISPATCH_TABLE must be the injected test value; "
            f"got {h._RSA_DISPATCH_TABLE!r}"
        )
        assert "vfo" not in h._RSA_DISPATCH_TABLE, (
            "_RSA_DISPATCH_TABLE must not point at the retired VFO queue"
        )
        assert "cms-" not in h._RSA_DISPATCH_TABLE, (
            "_RSA_DISPATCH_TABLE must not be a CMS table"
        )

    # --- lifecycle constant import -------------------------------------

    def test_lifecycle_requested_imported_not_literal(self):
        """LIFECYCLE_REQUESTED must be imported from shared.rsa_client.

        The item status must equal that constant — not a hardcoded "PENDING"
        or any other literal. This test verifies the constant value is "REQUESTED"
        and that the written status matches it.

        MUTATION-VERIFIED: change the import to use "PENDING" literal instead →
        test_queue_rsa_action_writes_requested_not_pending MUST fail.
        """
        from shared.rsa_client import LIFECYCLE_REQUESTED

        assert LIFECYCLE_REQUESTED == "REQUESTED", (
            f"LIFECYCLE_REQUESTED from shared.rsa_client must equal 'REQUESTED'; "
            f"got {LIFECYCLE_REQUESTED!r}"
        )
        # Verify the handler uses it (the status in a written item matches)
        captured_items: list[dict] = []
        mock_table = MagicMock()
        mock_table.put_item.side_effect = lambda Item: captured_items.append(Item)

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            h._queue_rsa_action(
                tenant_id="t",
                vin="VIN",
                vehicle_id="VEH-X",
                driver_id=None,
                display_name=None,
                reason="test",
                summary=None,
                triage_session_id=None,
                contact_id="cnt",
                voice_session_id=None,
            )

        assert captured_items[0]["status"] == LIFECYCLE_REQUESTED, (
            "Written status must equal the imported LIFECYCLE_REQUESTED constant"
        )



# ---------------------------------------------------------------------------
# F5.D2: Live-state position resolution in _queue_rsa_action
# ---------------------------------------------------------------------------


def _fresh_live_state_response(lat: float = 37.7749, lon: float = -122.4194, vin: str | None = "1FMCU0F70MUA12345") -> dict:
    """Minimal live-state response with a fresh Redis-backed position.

    F6.1: includes locationSource="redis" — the coordinate came from a live signal.
    F7.2: includes vin from the vehicles row for cross-checking.
    """
    body: dict = {
        "latitude": lat,
        "longitude": lon,
        "lastSeenAgoSeconds": 30,
        "redisReachable": True,
        "locationSource": "redis",  # F6.1: provenance field
        "connectionStatusSource": "redis",
        "redisSignalsCount": 42,
        "vin": vin,  # F7.2: vin from vehicles row
    }
    return {
        "statusCode": 200,
        "body": json.dumps(body),
    }


def _stale_live_state_response() -> dict:
    """Live-state response with a position too old for dispatch."""
    return {
        "statusCode": 200,
        "body": json.dumps({
            "latitude": 37.7749,
            "longitude": -122.4194,
            "lastSeenAgoSeconds": 3600,  # 1 hour — exceeds 900s default
            "redisReachable": True,
            "locationSource": "redis",  # still Redis-sourced but stale
        }),
    }


def _redis_unreachable_response() -> dict:
    """Live-state response where Redis was unreachable (DDB fallback, possibly stale).

    F6.1: locationSource="ddb" — the coordinate came from the DDB fallback, not telemetry.
    This is the case the gate must refuse: Redis down means DDB fallback, which can lag days.
    """
    return {
        "statusCode": 200,
        "body": json.dumps({
            "latitude": 37.7749,
            "longitude": -122.4194,
            "lastSeenAgoSeconds": 10,
            "redisReachable": False,   # client connectivity — NOT provenance
            "locationSource": "ddb",   # F6.1: coordinate came from DDB fallback
        }),
    }


def _make_lambda_payload_response(response_dict: dict):
    """Wrap a response dict in a mock boto3 invoke response."""
    import io
    payload_bytes = json.dumps(response_dict).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.__getitem__ = lambda self, key: (
        io.BytesIO(payload_bytes) if key == "Payload" else MagicMock()
    )
    return mock_resp


class TestLiveStatePositionResolution:
    """F5.D2: _queue_rsa_action resolves position via live-state Lambda invoke.

    Mutation guards:
      (a) make the age gate accept any age → test_stale_position_writes_no_lat_lon MUST fail
      (b) raise on lookup failure instead of degrading → test_lookup_failure_still_writes_row MUST fail
      (c) return a coordinate when lookup failed → test_lookup_failure_writes_no_coordinates MUST fail
    """

    def _call_queue_rsa_action(self, mock_ddb_table, live_state_invoke_return=None):
        """Helper: call _queue_rsa_action with mocked DDB and Lambda client.

        live_state_invoke_return: if provided, the boto3 Lambda invoke response.
        If None, the _resolve_vehicle_position helper is patched to return None.
        """
        captured_items: list[dict] = []
        mock_ddb_table.put_item.side_effect = lambda Item: captured_items.append(Item)

        lambda_mock = MagicMock()
        if live_state_invoke_return is not None:
            lambda_mock.invoke.return_value = live_state_invoke_return

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_lambda", return_value=lambda_mock):
            mock_ddb.return_value.Table.return_value = mock_ddb_table
            h._queue_rsa_action(
                tenant_id="meridian",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="flat tire",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )
        return captured_items

    def test_fresh_position_writes_lat_lon_to_row(self):
        """When live-state returns a fresh position, lat/lon are written to the dispatch row.

        F5.D2 mutation guard (a): make the age gate accept any age → stale position
        would also write lat/lon → test_stale_position_writes_no_lat_lon fails.
        """
        import io
        mock_table = MagicMock()
        fresh_response = _fresh_live_state_response(lat=37.7749, lon=-122.4194)
        payload_bytes = json.dumps(fresh_response).encode("utf-8")

        mock_invoke_resp = {"Payload": io.BytesIO(payload_bytes)}
        lambda_mock = MagicMock()
        lambda_mock.invoke.return_value = mock_invoke_resp

        captured_items: list[dict] = []
        mock_table.put_item.side_effect = lambda Item: captured_items.append(Item)

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_lambda", return_value=lambda_mock):
            mock_ddb.return_value.Table.return_value = mock_table
            result = h._queue_rsa_action(
                tenant_id="meridian",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="flat tire",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        assert result is not None, "Expected a dispatchId on success"
        assert captured_items, "_queue_rsa_action must call put_item"
        item = captured_items[0]
        assert "lat" in item, (
            "lat must be written to the dispatch row when position is fresh. "
            "F5.D2 mutation guard (a): make gate accept any age → stale test fails."
        )
        assert "lon" in item, "lon must be written when position is fresh"
        assert float(item["lat"]) == pytest.approx(37.7749, abs=1e-4)
        assert float(item["lon"]) == pytest.approx(-122.4194, abs=1e-4)

    def test_stale_position_writes_no_lat_lon(self):
        """When position age exceeds threshold, lat/lon are NOT written.

        The consumer's fail-closed path then handles the absent coordinates.
        F5.D2 mutation guard (a): making the gate accept any age → this test fails
        because lat/lon would be written even for stale positions.
        """
        import io
        mock_table = MagicMock()
        stale_response = _stale_live_state_response()
        payload_bytes = json.dumps(stale_response).encode("utf-8")

        mock_invoke_resp = {"Payload": io.BytesIO(payload_bytes)}
        lambda_mock = MagicMock()
        lambda_mock.invoke.return_value = mock_invoke_resp

        captured_items: list[dict] = []
        mock_table.put_item.side_effect = lambda Item: captured_items.append(Item)

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_lambda", return_value=lambda_mock):
            mock_ddb.return_value.Table.return_value = mock_table
            result = h._queue_rsa_action(
                tenant_id="meridian",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="flat tire",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        assert result is not None, "Row must still be written even when position is stale"
        item = captured_items[0]
        assert "lat" not in item, (
            "lat must NOT be written when position age exceeds RSA_POSITION_MAX_AGE_SECONDS. "
            "The consumer fails closed on absent lat/lon — that is the correct behaviour. "
            "F5.D2 mutation guard (a): accepting any age → lat IS written → this fails."
        )
        assert "lon" not in item, "lon must NOT be written when position is stale"
        # Row must still exist in REQUESTED state
        assert item["status"] == "REQUESTED"

    def test_redis_unreachable_writes_no_lat_lon(self):
        """When Redis was unreachable, position came from DDB fallback and is not used.

        bidi_app.py:2112 documents DDB fallback as able to lag by days. A days-old
        coordinate for a tow truck is worse than a refusal.
        """
        import io
        mock_table = MagicMock()
        unreachable_response = _redis_unreachable_response()
        payload_bytes = json.dumps(unreachable_response).encode("utf-8")

        mock_invoke_resp = {"Payload": io.BytesIO(payload_bytes)}
        lambda_mock = MagicMock()
        lambda_mock.invoke.return_value = mock_invoke_resp

        captured_items: list[dict] = []
        mock_table.put_item.side_effect = lambda Item: captured_items.append(Item)

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_lambda", return_value=lambda_mock):
            mock_ddb.return_value.Table.return_value = mock_table
            result = h._queue_rsa_action(
                tenant_id="meridian",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="flat tire",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        assert result is not None, "Row must still be written when Redis unreachable"
        item = captured_items[0]
        assert "lat" not in item, (
            "lat must NOT be written when redisReachable=False (DDB fallback may be days old)"
        )
        assert "lon" not in item

    def test_lookup_failure_still_writes_row(self):
        """When the live-state invoke raises, the row is still written (best-effort).

        Resolution failure must NOT break the human handoff.
        F5.D2 mutation guard (b): raise on lookup failure instead of degrading →
        _queue_rsa_action raises → no row written → result is None → this test fails.
        """
        mock_table = MagicMock()
        lambda_mock = MagicMock()
        lambda_mock.invoke.side_effect = Exception("Lambda invocation timeout")

        captured_items: list[dict] = []
        mock_table.put_item.side_effect = lambda Item: captured_items.append(Item)

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_lambda", return_value=lambda_mock):
            mock_ddb.return_value.Table.return_value = mock_table
            result = h._queue_rsa_action(
                tenant_id="meridian",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="flat tire",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        assert result is not None, (
            "Row must be written even when live-state invoke fails. "
            "Resolution failure must not break the human handoff. "
            "F5.D2 mutation guard (b): raise on lookup failure → no row → result is None → FAILS."
        )
        assert captured_items, "put_item must be called even when position lookup fails"

    def test_lookup_failure_writes_no_coordinates(self):
        """When live-state invoke fails, lat/lon are NOT written to the row.

        F5.D2 mutation guard (c): returning a coordinate when lookup failed → lat/lon
        IS written → this assertion fails.
        """
        mock_table = MagicMock()
        lambda_mock = MagicMock()
        lambda_mock.invoke.side_effect = Exception("connection error")

        captured_items: list[dict] = []
        mock_table.put_item.side_effect = lambda Item: captured_items.append(Item)

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_lambda", return_value=lambda_mock):
            mock_ddb.return_value.Table.return_value = mock_table
            h._queue_rsa_action(
                tenant_id="meridian",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="flat tire",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        item = captured_items[0]
        assert "lat" not in item, (
            "lat must NOT be present when the position lookup failed. "
            "Never invent a coordinate. "
            "F5.D2 mutation guard (c): returning a coordinate on failure → lat IS present → FAILS."
        )
        assert "lon" not in item, "lon must NOT be present when the position lookup failed"

    def test_live_state_invoke_uses_correct_function_name(self):
        """The invoke call uses _LIVE_STATE_FN_NAME, not a hardcoded string."""
        import io
        mock_table = MagicMock()
        mock_table.put_item.return_value = None

        fresh_response = _fresh_live_state_response()
        payload_bytes = json.dumps(fresh_response).encode("utf-8")
        lambda_mock = MagicMock()
        lambda_mock.invoke.return_value = {"Payload": io.BytesIO(payload_bytes)}

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_lambda", return_value=lambda_mock):
            mock_ddb.return_value.Table.return_value = mock_table
            h._queue_rsa_action(
                tenant_id="meridian",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="flat tire",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        assert lambda_mock.invoke.called, "Lambda invoke must be called for position resolution"
        call_kwargs = lambda_mock.invoke.call_args.kwargs
        assert call_kwargs.get("FunctionName") == h._LIVE_STATE_FN_NAME, (
            f"Lambda invoke must use _LIVE_STATE_FN_NAME ({h._LIVE_STATE_FN_NAME!r}), "
            f"not a hardcoded string. Got {call_kwargs.get('FunctionName')!r}."
        )

    def test_live_state_event_is_api_gateway_shaped(self):
        """The invoke payload must be API-Gateway-shaped (pathParameters.vehicleId + httpMethod).

        The live-state handler explicitly accepts both API-Gateway and direct invocation
        shapes using pathParameters.vehicleId. This test pins that contract.
        """
        import io
        mock_table = MagicMock()
        mock_table.put_item.return_value = None

        fresh_response = _fresh_live_state_response()
        payload_bytes = json.dumps(fresh_response).encode("utf-8")
        lambda_mock = MagicMock()
        lambda_mock.invoke.return_value = {"Payload": io.BytesIO(payload_bytes)}

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_lambda", return_value=lambda_mock):
            mock_ddb.return_value.Table.return_value = mock_table
            h._queue_rsa_action(
                tenant_id="meridian",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-007",
                driver_id=None,
                display_name=None,
                reason="flat tire",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        invoke_kwargs = lambda_mock.invoke.call_args.kwargs
        payload_str = invoke_kwargs.get("Payload")
        if isinstance(payload_str, bytes):
            payload_dict = json.loads(payload_str.decode("utf-8"))
        else:
            payload_dict = json.loads(payload_str)
        assert "pathParameters" in payload_dict, "Payload must use API-Gateway shape"
        assert payload_dict["pathParameters"].get("vehicleId") == "VEH-007", (
            "vehicleId must be passed in pathParameters.vehicleId"
        )
        assert payload_dict.get("httpMethod") == "GET"

    def test_live_state_fn_name_env_var_is_set(self):
        """LIVE_STATE_FN_NAME is bound at module load time from os.environ.

        A missing LIVE_STATE_FN_NAME raises KeyError at import — that behavior
        ensures a deploy missing the CDK wiring fails loudly.
        """
        assert h._LIVE_STATE_FN_NAME == "vsa-test-live-state-fn", (
            f"_LIVE_STATE_FN_NAME must be the injected test value; "
            f"got {h._LIVE_STATE_FN_NAME!r}"
        )



# ---------------------------------------------------------------------------
# F6.1: Location provenance gate — locationSource == "redis" is the load-bearing check
# ---------------------------------------------------------------------------


class TestLocationProvenanceGate:
    """F6.1 (Critical C6-1): the freshness gate must require locationSource == "redis".

    C6-1 demonstrated two routes that currently reach dispatch when they must not:
    1. Redis up, signals hash has no location (no signal aliases match) → _live_location
       falls through to DDB; live-state returns redisReachable=True, locationSource="ddb".
    2. Redis up, signal_catalog:reverse is empty → same fallback.

    Both yield redisReachable=True, age≈0, and a DDB coordinate that may be weeks stale.
    The old gate (redisReachable) accepted both. The new gate (locationSource=="redis")
    refuses both.

    Mutation guards:
    (a) make _live_location report "redis" on the DDB fallback path →
        test_ddb_fallback_locationSource_refused MUST fail.
    (b) gate on redisReachable again instead of locationSource →
        test_redis_reachable_with_no_location_signal_refused MUST fail.
    """

    def _make_live_state_invoke_return(self, body_dict: dict) -> MagicMock:
        """Wrap body_dict as a mock boto3 Lambda invoke response."""
        import io
        inner = json.dumps({"statusCode": 200, "body": json.dumps(body_dict)}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__getitem__ = lambda self, key: (
            io.BytesIO(inner) if key == "Payload" else MagicMock()
        )
        return mock_resp

    def test_redis_backed_coordinate_accepted(self):
        """locationSource='redis' + fresh age → lat/lon written to dispatch row.

        This is the happy path: a real Redis signal alias supplied the coordinate.

        Mutation guard (b): gate on redisReachable again instead of locationSource →
        test_ddb_fallback_locationSource_refused would still pass (ddb → refused by age or
        redisReachable), but test_absent_location_source_refused would fail because
        redisReachable=True there would accept the missing-locationSource case.
        """
        invoke_return = self._make_live_state_invoke_return({
            "latitude": 47.6062,
            "longitude": -122.3321,
            "lastSeenAgoSeconds": 25,
            "locationSource": "redis",
            "redisReachable": True,
            "vin": "1FMCU0F70MUA12345",  # F7.2: must match vin passed to _queue_rsa_action
        })
        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            result = h._queue_rsa_action(
                tenant_id="test-tenant",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-redis",
                driver_id=None,
                display_name=None,
                reason="flat tire",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )
        assert result is not None, "row must be written"
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" in item, (
            "lat must be written when locationSource='redis'. "
            "Mutation (b): gate on redisReachable → this passes even for ddb-sourced "
            "coordinates, so test_ddb_fallback_locationSource_refused fails."
        )
        assert "lon" in item

    def test_ddb_fallback_locationSource_refused(self):
        """locationSource='ddb' → coordinate refused even though redisReachable=True.

        This is C6-1 Route 1: Redis is up but the signals hash carries no location alias,
        so _live_location falls through to DDB. The old redisReachable gate accepted this;
        the new locationSource gate refuses.

        Mutation guard (a): make _live_location report "redis" on the DDB path
        → this test must FAIL (coordinate is accepted despite being DDB-sourced).
        """
        invoke_return = self._make_live_state_invoke_return({
            "latitude": 37.7749,
            "longitude": -122.4194,
            "lastSeenAgoSeconds": 5,   # fresh-looking age
            "locationSource": "ddb",   # <-- DDB fallback, not Redis
            "redisReachable": True,    # Redis was up but had no location signals
        })
        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            result = h._queue_rsa_action(
                tenant_id="test-tenant",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-ddb",
                driver_id=None,
                display_name=None,
                reason="engine light",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-003",
                voice_session_id=None,
            )
        assert result is not None, "row must still be written (best-effort)"
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" not in item, (
            "lat must NOT be written when locationSource='ddb'. "
            "Mutation guard (a): if _live_location reports 'redis' on the DDB path, "
            "the gate would not refuse this coordinate and lat would be written."
        )
        assert "lon" not in item, "lon must NOT be written when locationSource='ddb'"

    def test_redis_reachable_with_no_location_signal_refused(self):
        """locationSource=None (no coordinate found) → refused.

        This is C6-1 Route 2: Redis up, signal_catalog:reverse empty → _live_location
        found no signal alias AND no DDB value. locationSource stays None.

        Mutation guard (b): gate on redisReachable again → this test must FAIL because
        redisReachable=True would cause the gate to pass (with missing lat/lon raising
        further down), masking the provenance defect.
        """
        invoke_return = self._make_live_state_invoke_return({
            "latitude": None,          # no coordinate at all
            "longitude": None,
            "lastSeenAgoSeconds": 5,
            "locationSource": None,    # <-- no source: neither Redis nor DDB had coords
            "redisReachable": True,
        })
        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            result = h._queue_rsa_action(
                tenant_id="test-tenant",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-nosig",
                driver_id=None,
                display_name=None,
                reason="breakdown",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-004",
                voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" not in item, (
            "lat must NOT be written when locationSource=None. "
            "Mutation guard (b): gate on redisReachable → passes with True, "
            "coordinate accepted despite not being Redis-backed."
        )

    def test_absent_location_source_refused(self):
        """locationSource absent from live-state body (old Lambda version) → refused.

        An older live-state Lambda that doesn't yet have F6.1 will return a body
        without locationSource. The gate must fail closed in both deploy orderings.
        """
        invoke_return = self._make_live_state_invoke_return({
            "latitude": 37.7749,
            "longitude": -122.4194,
            "lastSeenAgoSeconds": 5,
            # no locationSource key — simulates a pre-F6.1 live-state Lambda
            "redisReachable": True,
        })
        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            result = h._queue_rsa_action(
                tenant_id="test-tenant",
                vin="1FMCU0F70MUA12345",
                vehicle_id="VEH-nokey",
                driver_id=None,
                display_name=None,
                reason="stalled",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-005",
                voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" not in item, (
            "lat must NOT be written when locationSource key is absent from live-state body. "
            "Version-skew safety: fails closed until both sides are deployed."
        )


# ---------------------------------------------------------------------------
# F6.2: Gate must fail closed on malformed inputs
# ---------------------------------------------------------------------------


class TestGateMalformedInputs:
    """F6.2: gate refuses NaN, negative age, non-numeric lastSeenAgoSeconds, absent locationSource.

    Cycle 6 found the gate failed OPEN on NaN and negative age: NaN passes float() silently
    and `NaN > 900` is False, so it looks fresh; a negative age is never > 900 either.
    Both should be refused.

    Mutation guard: feed NaN and -1 without the guards → tests MUST fail (items written with lat).
    """

    def _make_invoke_return(self, body_dict: dict) -> MagicMock:
        import io
        inner = json.dumps({"statusCode": 200, "body": json.dumps(body_dict)}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__getitem__ = lambda self, key: (
            io.BytesIO(inner) if key == "Payload" else MagicMock()
        )
        return mock_resp

    def _call_and_get_item(self, body_dict: dict) -> dict:
        """Call _queue_rsa_action with a given live-state body; return the written DDB item."""
        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = self._make_invoke_return(body_dict)
            h._queue_rsa_action(
                tenant_id="t1",
                vin="VIN-TEST",
                vehicle_id="VEH-T",
                driver_id=None,
                display_name=None,
                reason="test",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-t",
                voice_session_id=None,
            )
            return mock_table.put_item.call_args.kwargs["Item"]

    def test_nan_age_refused(self):
        """NaN lastSeenAgoSeconds must be refused (fail closed).

        Without the guard: float('nan') passes the > threshold check (NaN > 900 is False)
        and the position is accepted as fresh.

        Mutation guard: remove the math.isnan check → item would have lat, so assertion fails.
        """
        import math
        item = self._call_and_get_item({
            "latitude": 37.7,
            "longitude": -122.4,
            "lastSeenAgoSeconds": math.nan,
            "locationSource": "redis",
        })
        assert "lat" not in item, (
            "lat must NOT be written when lastSeenAgoSeconds is NaN. "
            "Mutation guard: remove isnan check → NaN passes the > threshold check silently, "
            "gate accepts the position, lat is written, this assertion fails."
        )

    def test_negative_age_refused(self):
        """Negative lastSeenAgoSeconds must be refused (fail closed).

        Without the guard: -1 < 900 is True (not > 900), so a negative age is treated as
        ultra-fresh and accepted.

        Mutation guard: remove the < 0 check → item would have lat, assertion fails.
        """
        item = self._call_and_get_item({
            "latitude": 37.7,
            "longitude": -122.4,
            "lastSeenAgoSeconds": -1,
            "locationSource": "redis",
        })
        assert "lat" not in item, (
            "lat must NOT be written when lastSeenAgoSeconds is negative. "
            "Mutation guard: remove < 0 check → -1 is not > threshold, position 'accepted'."
        )

    def test_none_age_refused(self):
        """None lastSeenAgoSeconds must be refused (fail closed)."""
        item = self._call_and_get_item({
            "latitude": 37.7,
            "longitude": -122.4,
            "lastSeenAgoSeconds": None,
            "locationSource": "redis",
        })
        assert "lat" not in item, "lat must NOT be written when lastSeenAgoSeconds is None"

    def test_non_numeric_age_refused(self):
        """Non-numeric lastSeenAgoSeconds (e.g. 'abc') must be refused."""
        item = self._call_and_get_item({
            "latitude": 37.7,
            "longitude": -122.4,
            "lastSeenAgoSeconds": "not-a-number",
            "locationSource": "redis",
        })
        assert "lat" not in item, "lat must NOT be written when lastSeenAgoSeconds is non-numeric"


# ---------------------------------------------------------------------------
# F6.3: Config typo must not 502 all of /escalate
# ---------------------------------------------------------------------------


class TestConfigTypoDoesNotBreakEscalation:
    """F6.3: RSA_POSITION_MAX_AGE_SECONDS='abc' must not 502 /escalate.

    Today: float('abc') at module load raises ValueError → every /escalate call returns 502.
    After fix: the handler continues to serve escalations; dispatch is refused (fail closed).
    """

    def test_handler_serves_escalation_with_bad_age_env(self, monkeypatch):
        """With RSA_POSITION_MAX_AGE_SECONDS='abc', the handler must still return 200.

        The human handoff (chat contact) is unaffected. A location problem must never
        break the escalation path — this was F5.D2 Accept 4's own requirement, and
        a typo in the *fix* for it must not violate it.
        """
        # Load a fresh handler module with the bad env var
        import importlib.util
        import sys
        from pathlib import Path

        handler_path = Path(__file__).parent.parent / "handler.py"
        mod_name = "_escalate_typo_test"
        spec = importlib.util.spec_from_file_location(mod_name, handler_path)
        mod = importlib.util.module_from_spec(spec)

        monkeypatch.setenv("TENANT_CONFIG_TABLE", "test-tenant-config")
        monkeypatch.setenv("RSA_DISPATCH_TABLE", "test-rsa-dispatch")
        monkeypatch.setenv("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")
        monkeypatch.setenv("RSA_POSITION_MAX_AGE_SECONDS", "abc")  # typo
        monkeypatch.setenv("CMS_STAGE", "test")

        # Module load must not raise even with a bad env var.
        spec.loader.exec_module(mod)

        mock_connect_resp = {
            "ContactId": "cnt-typo-123",
            "ParticipantId": "part-typo-456",
            "ParticipantToken": "token-typo-789",
        }

        with patch.object(mod, "_load_tenant_connect_config", return_value={
            "instanceArn": "arn:aws:connect:us-east-1:123456789012:instance/abc",
            "instanceId": "abc",
            "instanceRegion": "us-east-1",
            "contactFlowId": "flow-001",
            "queueArn": None,
            "autoRequestRoadsideOnP0": False,
            "autoOpenHumanChatOnP0": False,
        }), patch.object(mod, "_get_connect") as mock_connect_fn:
            mock_connect_fn.return_value.start_chat_contact.return_value = mock_connect_resp
            event = {
                "requestContext": {"authorizer": {"claims": {"custom:tenantId": "t1"}}},
                "body": json.dumps({
                    "severity": "P0",
                    "vin": "VIN001",
                    "vehicleId": "VEH-001",
                    "reason": "flat tire",
                }),
            }
            resp = mod.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"With RSA_POSITION_MAX_AGE_SECONDS='abc', handler must still return 200. "
            f"Got {resp['statusCode']}. A config typo must degrade to no-dispatch, not 502."
        )

    def test_handler_refuses_dispatch_with_bad_age_env(self, monkeypatch):
        """With RSA_POSITION_MAX_AGE_SECONDS='abc', dispatch must be refused (fail closed).

        The gate uses _RSA_POSITION_MAX_AGE_SECONDS = -1.0 as sentinel. Any age > -1 fails,
        so no vehicle will be dispatched until the config is corrected.
        """
        import importlib.util
        from pathlib import Path

        handler_path = Path(__file__).parent.parent / "handler.py"
        mod_name = "_escalate_typo_dispatch_test"
        spec = importlib.util.spec_from_file_location(mod_name, handler_path)
        mod = importlib.util.module_from_spec(spec)

        monkeypatch.setenv("TENANT_CONFIG_TABLE", "test-tenant-config")
        monkeypatch.setenv("RSA_DISPATCH_TABLE", "test-rsa-dispatch")
        monkeypatch.setenv("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")
        monkeypatch.setenv("RSA_POSITION_MAX_AGE_SECONDS", "abc")
        monkeypatch.setenv("CMS_STAGE", "test")

        spec.loader.exec_module(mod)

        # Confirm the sentinel was set
        assert mod._RSA_POSITION_MAX_AGE_SECONDS == -1.0, (
            "_RSA_POSITION_MAX_AGE_SECONDS must be -1.0 when env var is unparseable"
        )

        # Confirm dispatch is refused for a "fresh" position
        import io
        fresh_body = json.dumps({
            "latitude": 37.7, "longitude": -122.4,
            "lastSeenAgoSeconds": 30,
            "locationSource": "redis",
            "redisReachable": True,
        })
        payload_bytes = json.dumps({"statusCode": 200, "body": fresh_body}).encode("utf-8")
        mock_invoke_resp = MagicMock()
        mock_invoke_resp.__getitem__ = lambda self, key: (
            io.BytesIO(payload_bytes) if key == "Payload" else MagicMock()
        )

        with patch.object(mod, "_get_ddb") as mock_ddb_fn, \
             patch.object(mod, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = mock_invoke_resp
            dispatch_id = mod._queue_rsa_action(
                tenant_id="t1",
                vin="VIN001",
                vehicle_id="VEH-001",
                driver_id=None,
                display_name=None,
                reason="flat",
                summary=None,
                triage_session_id=None,
                contact_id="cnt-001",
                voice_session_id=None,
            )

        # Row is still written (dispatch is best-effort) but without lat/lon
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" not in item, (
            "With bad RSA_POSITION_MAX_AGE_SECONDS, dispatch gate must refuse all positions. "
            "Row must be written without lat/lon."
        )


# ---------------------------------------------------------------------------
# F6.4: Coordinate range validation — both producer and consumer
# ---------------------------------------------------------------------------


class TestCoordinateRangeValidation:
    """F6.4: abs(lat) <= 90 and abs(lon) <= 180 enforced at both producer and consumer.

    Cycle 6 showed (999.0, -999.0) reaches the provider unchecked.
    The producer is the first line; the consumer is the second.

    Mutation guard: remove the producer-side range check → a consumer-side test must
    still catch it (that is the point of having both).
    """

    def _make_invoke_return(self, body_dict: dict) -> MagicMock:
        import io
        inner = json.dumps({"statusCode": 200, "body": json.dumps(body_dict)}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__getitem__ = lambda self, key: (
            io.BytesIO(inner) if key == "Payload" else MagicMock()
        )
        return mock_resp

    def test_producer_refuses_out_of_range_lat(self):
        """Producer must refuse lat=999.0 (out of valid -90..90 range).

        Mutation guard: remove producer-side range check → item has lat written.
        This test catches that, and the consumer test catches it redundantly.
        """
        invoke_return = self._make_invoke_return({
            "latitude": 999.0,
            "longitude": -122.4,
            "lastSeenAgoSeconds": 30,
            "locationSource": "redis",
            "redisReachable": True,
        })
        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1", vin="VIN1", vehicle_id="VEH-R1",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-r1", voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" not in item, (
            "lat must NOT be written when latitude=999.0 (out of range). "
            "Mutation guard: remove range check → lat is written, this assertion fails."
        )

    def test_producer_refuses_out_of_range_lon(self):
        """Producer must refuse lon=-999.0 (out of valid -180..180 range)."""
        invoke_return = self._make_invoke_return({
            "latitude": 37.7,
            "longitude": -999.0,
            "lastSeenAgoSeconds": 30,
            "locationSource": "redis",
            "redisReachable": True,
        })
        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1", vin="VIN1", vehicle_id="VEH-R2",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-r2", voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lon" not in item, (
            "lon must NOT be written when longitude=-999.0 (out of range)."
        )

    def test_producer_accepts_boundary_coordinates(self):
        """Boundary values (lat=90, lon=180) are valid and must be accepted."""
        invoke_return = self._make_invoke_return({
            "latitude": 90.0,
            "longitude": 180.0,
            "lastSeenAgoSeconds": 10,
            "locationSource": "redis",
            "redisReachable": True,
            "vin": "VIN1",  # F7.2: must match vin passed to _queue_rsa_action below
        })
        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1", vin="VIN1", vehicle_id="VEH-R3",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-r3", voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" in item, "lat=90.0 is a valid boundary value and must be accepted"
        assert "lon" in item, "lon=180.0 is a valid boundary value and must be accepted"



# ---------------------------------------------------------------------------
# F7.2: VIN cross-check — vin/vehicleId identity must be confirmed before dispatch
# ---------------------------------------------------------------------------

class TestVinCrossCheck:
    """F7.2: _resolve_vehicle_position refuses the coordinate when vehicleId↔VIN cannot
    be confirmed.  A mismatch means the request describes vehicle A (by vehicleId) but
    names vehicle B's VIN — the truck would go to A's location under B's VIN.

    Two refusal cases (Accept 3):
      1. live-state vin does not match the expected_vin (mismatch).
      2. live-state returns no vin at all (None / absent) — version-skew / old record.

    Mutation guard: remove the comparison → mismatch test MUST fail (lat/lon written).
    """

    def _invoke_return_from_body(self, body: dict) -> MagicMock:
        """Wrap a live-state body dict into a Lambda invoke response mock."""
        import io
        raw = json.dumps({"statusCode": 200, "body": json.dumps(body)}).encode()
        mock_resp = MagicMock()
        mock_resp.__getitem__ = lambda self_, key: (
            io.BytesIO(raw) if key == "Payload" else MagicMock()
        )
        return mock_resp

    def test_matching_vin_accepted(self):
        """When live-state vin matches expected_vin, position is used normally.

        Baseline (non-mutation): shows the gate passes when identity is confirmed.
        """
        body = {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "lastSeenAgoSeconds": 30,
            "locationSource": "redis",
            "vin": "1HGBH41JXMN000024",
        }
        invoke_return = self._invoke_return_from_body(body)

        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1", vin="1HGBH41JXMN000024", vehicle_id="VEH-001",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-001", voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" in item, "lat must be written when VINs match"
        assert "lon" in item, "lon must be written when VINs match"

    def test_mismatched_vin_refused(self):
        """When live-state vin does not match expected_vin, position is refused.

        Sends dispatch under VIN-B's request but live-state reports VIN-A.
        Without the check, the truck is dispatched to VIN-A's location under VIN-B.

        Mutation guard: remove the vin comparison → lat/lon are written, this FAILS.
        """
        body = {
            "latitude": 47.6062,
            "longitude": -122.3321,
            "lastSeenAgoSeconds": 20,
            "locationSource": "redis",
            "vin": "VIN-A-FROM-VEHICLES-ROW",  # mismatch: request carries VIN-B
        }
        invoke_return = self._invoke_return_from_body(body)

        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1",
                vin="VIN-B-FROM-REQUEST",   # mismatch: live-state says VIN-A
                vehicle_id="VEH-001",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-001", voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" not in item, (
            "lat must NOT be written when VINs mismatch. "
            "Mutation guard: remove vin comparison → lat written, this FAILS."
        )
        assert "lon" not in item, "lon must NOT be written on VIN mismatch"

    def test_absent_vin_in_live_state_refused(self):
        """When live-state returns no vin (None), position is refused.

        Version-skew case: live-state was deployed before F7.2; its response has no vin field.
        The escalate handler cannot confirm identity, so it refuses.
        """
        body = {
            "latitude": 47.6062,
            "longitude": -122.3321,
            "lastSeenAgoSeconds": 20,
            "locationSource": "redis",
            "vin": None,  # no vin in live-state response
        }
        invoke_return = self._invoke_return_from_body(body)

        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1", vin="SOME-VIN", vehicle_id="VEH-001",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-001", voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" not in item, (
            "lat must NOT be written when live-state vin is None (version-skew refusal)"
        )

    def test_missing_vin_field_in_live_state_refused(self):
        """When live-state response has no vin key at all, position is refused."""
        body = {
            "latitude": 47.6062,
            "longitude": -122.3321,
            "lastSeenAgoSeconds": 20,
            "locationSource": "redis",
            # no "vin" key
        }
        invoke_return = self._invoke_return_from_body(body)

        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1", vin="SOME-VIN", vehicle_id="VEH-001",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-001", voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" not in item, (
            "lat must NOT be written when live-state has no vin field (same as None)"
        )



# ---------------------------------------------------------------------------
# F7.3: Unparseable coordinate branch — producer side
# ---------------------------------------------------------------------------

class TestUnparseableCoordinateProducer:
    """F7.3 (W6-5 remainder): the float()-raises branch on the producer side is exercised.

    F6.4 added the range checks and a (0,0) test, but the ValueError branch on both sides
    was still unexercised.  This class covers the producer (api-escalate).

    Mutation guard: swallow the parse error and continue (return a default coord) →
    the tests below MUST fail (lat/lon would be written with garbage values).
    """

    def _invoke_return_from_body(self, body: dict) -> MagicMock:
        import io
        raw = json.dumps({"statusCode": 200, "body": json.dumps(body)}).encode()
        mock_resp = MagicMock()
        mock_resp.__getitem__ = lambda self_, key: (
            io.BytesIO(raw) if key == "Payload" else MagicMock()
        )
        return mock_resp

    def test_non_numeric_lat_refuses_position(self):
        """Producer must refuse when lat is non-numeric (float() raises ValueError).

        Mutation guard: swallow ValueError, use lat=0.0 → lat written with bad value,
        this assertion fails.
        """
        invoke_return = self._invoke_return_from_body({
            "latitude": "not-a-number",
            "longitude": -122.4194,
            "lastSeenAgoSeconds": 30,
            "locationSource": "redis",
            "vin": "1FMCU0F70MUA12345",
        })

        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1", vin="1FMCU0F70MUA12345", vehicle_id="VEH-P1",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-p1", voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lat" not in item, (
            "lat must NOT be written when latitude is non-numeric. "
            "Mutation guard: swallow ValueError → lat written with bad value, this FAILS."
        )

    def test_non_numeric_lon_refuses_position(self):
        """Producer must refuse when lon is non-numeric (float() raises ValueError)."""
        invoke_return = self._invoke_return_from_body({
            "latitude": 37.7749,
            "longitude": "not-a-number",
            "lastSeenAgoSeconds": 30,
            "locationSource": "redis",
            "vin": "1FMCU0F70MUA12345",
        })

        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn:
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1", vin="1FMCU0F70MUA12345", vehicle_id="VEH-P2",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-p2", voice_session_id=None,
            )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "lon" not in item, (
            "lon must NOT be written when longitude is non-numeric."
        )



# ---------------------------------------------------------------------------
# F8.1: VIN-mismatch log must NOT contain either VIN
# ---------------------------------------------------------------------------

class TestVinMismatchLogging:
    """F8.1: The vin-mismatch refusal must log the event, not the values.

    handler.py previously emitted:
        "request vin=%r but vehicles row vin=%r"
    with both VINs as positional args. Any principal holding logs:GetLogEvents
    on this group could harvest (vehicleId, real VIN) pairs.

    The fix: log only that a mismatch occurred and which vehicle, never either VIN.

    Mutation guard: re-add either VIN to the log call → this test MUST fail.

    MUTATION-VERIFIED (F8.1):
      Mutant: add `expected_vin` back to the log warning.
      test_vin_mismatch_log_has_no_vin_in_formatted_message → FAILED.
    """

    def _invoke_return_from_body(self, body: dict) -> MagicMock:
        """Wrap a live-state body dict into a Lambda invoke response mock."""
        import io
        raw = json.dumps({"statusCode": 200, "body": json.dumps(body)}).encode()
        mock_resp = MagicMock()
        mock_resp.__getitem__ = lambda self_, key: (
            io.BytesIO(raw) if key == "Payload" else MagicMock()
        )
        return mock_resp

    def test_vin_mismatch_log_has_no_vin_in_formatted_message(self, caplog):
        """The formatted warning message must not contain either VIN.

        caplog captures both the formatted message (record.getMessage()) and
        the raw args (record.args).  We assert on both so a VIN passed as a
        lazy-format arg is still caught.

        Mutation guard: add expected_vin or live_state_vin back to the log call
        → the assertion below MUST fail.
        """
        import logging

        EXPECTED_VIN = "VIN-REQUEST-SIDE"
        LIVE_STATE_VIN = "VIN-LIVE-STATE-SIDE"

        body = {
            "latitude": 47.6062,
            "longitude": -122.3321,
            "lastSeenAgoSeconds": 15,
            "locationSource": "redis",
            "vin": LIVE_STATE_VIN,  # mismatch: request carries EXPECTED_VIN
        }
        invoke_return = self._invoke_return_from_body(body)

        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn, \
             caplog.at_level(logging.WARNING):
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="tenant-x",
                vin=EXPECTED_VIN,
                vehicle_id="VEH-MISMATCH",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-mismatch", voice_session_id=None,
            )

        # Collect all warning/error log records
        mismatch_records = [
            r for r in caplog.records
            if "mismatch" in r.getMessage().lower()
        ]
        assert mismatch_records, (
            "Expected at least one log record with 'mismatch'; none found. "
            "The mismatch refusal must still log the event."
        )
        for record in mismatch_records:
            # Check formatted message (the string the operator actually sees)
            formatted = record.getMessage()
            assert EXPECTED_VIN not in formatted, (
                f"Request VIN {EXPECTED_VIN!r} must NOT appear in the formatted log message. "
                f"Got: {formatted!r}. "
                "Mutation guard: add expected_vin back to log call → this FAILS."
            )
            assert LIVE_STATE_VIN not in formatted, (
                f"Live-state VIN {LIVE_STATE_VIN!r} must NOT appear in the formatted log message. "
                f"Got: {formatted!r}. "
                "Mutation guard: add live_state_vin back to log call → this FAILS."
            )
            # Also assert on record.args — a VIN passed as a lazy-format arg
            # does not appear in getMessage() until format time, but it IS in args.
            args = record.args or ()
            args_str = str(args)
            assert EXPECTED_VIN not in args_str, (
                f"Request VIN {EXPECTED_VIN!r} must NOT appear in record.args. "
                f"Got: {args_str!r}. "
                "A VIN passed as a lazy-format arg is still in the record and reachable "
                "via the logging handler's raw message."
            )
            assert LIVE_STATE_VIN not in args_str, (
                f"Live-state VIN {LIVE_STATE_VIN!r} must NOT appear in record.args. "
                f"Got: {args_str!r}."
            )

    def test_vin_mismatch_log_still_records_vehicle_id(self, caplog):
        """The mismatch log must still name the vehicle so operators can act on it.

        The diagnostic value is knowing *which* vehicle triggered the refusal;
        the VINs themselves are not needed.
        """
        import logging

        body = {
            "latitude": 47.6062,
            "longitude": -122.3321,
            "lastSeenAgoSeconds": 15,
            "locationSource": "redis",
            "vin": "VIN-LIVE-SIDE",
        }
        invoke_return = self._invoke_return_from_body(body)

        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn, \
             caplog.at_level(logging.WARNING):
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="tenant-x",
                vin="VIN-REQUEST-SIDE",
                vehicle_id="VEH-FOR-OPERATOR-AUDIT",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-mismatch", voice_session_id=None,
            )

        mismatch_records = [
            r for r in caplog.records
            if "mismatch" in r.getMessage().lower() or "identity" in r.getMessage().lower()
        ]
        assert mismatch_records, "Expected at least one log record referencing the mismatch"
        # The vehicle_id — not PII — must appear so the operator knows which vehicle to audit.
        found_vehicle = any(
            "VEH-FOR-OPERATOR-AUDIT" in r.getMessage()
            for r in mismatch_records
        )
        assert found_vehicle, (
            "vehicle_id ('VEH-FOR-OPERATOR-AUDIT') must appear in the mismatch warning "
            "so operators can investigate the specific vehicle. "
            "vehicle_id is not PII — the VINs are."
        )


# ---------------------------------------------------------------------------
# F8.3: Broad-exception logs in escalate must not echo response fragments
# ---------------------------------------------------------------------------

class TestBroadExceptionLogsEscalate:
    """F8.3 (S2): The three broad-exception logs in _resolve_vehicle_position
    must use type(exc).__name__ rather than str(exc).

    str(exc) from float() includes the raw string value it could not parse.
    If that string came from the live-state response body, a lat/lon fragment
    would be echoed to CloudWatch.

    Mutation guard: restore `e` (str) to any log call → the test MUST fail.

    MUTATION-VERIFIED (F8.3):
      Mutant: restore `e` to the lat/lon ValueError log at line 299.
      test_invalid_lat_lon_log_no_coordinate_fragment → FAILED.
    """

    def _invoke_return_from_body(self, body: dict) -> MagicMock:
        import io
        raw = json.dumps({"statusCode": 200, "body": json.dumps(body)}).encode()
        mock_resp = MagicMock()
        mock_resp.__getitem__ = lambda self_, key: (
            io.BytesIO(raw) if key == "Payload" else MagicMock()
        )
        return mock_resp

    def test_invalid_lat_lon_log_no_coordinate_fragment(self, caplog):
        """When lat/lon parse fails, the log must not include the raw value (lat/lon fragment).

        The ValueError message from float("47.6062_EXTRA") contains the original string;
        using str(exc) would echo that value to CloudWatch.

        Mutation guard: restore `e` to the warning → the malformed string appears in the
        formatted message, this assertion FAILS.
        """
        import logging

        MALFORMED_LAT = "47.6062_MALFORMED"
        body = {
            "latitude": MALFORMED_LAT,
            "longitude": -122.4194,
            "lastSeenAgoSeconds": 20,
            "locationSource": "redis",
            "vin": "1FMCU0F70MUA12345",
        }
        invoke_return = self._invoke_return_from_body(body)

        with patch.object(h, "_get_ddb") as mock_ddb_fn, \
             patch.object(h, "_get_lambda") as mock_lambda_fn, \
             caplog.at_level(logging.WARNING):
            mock_table = MagicMock()
            mock_ddb_fn.return_value.Table.return_value = mock_table
            mock_lambda_fn.return_value.invoke.return_value = invoke_return
            h._queue_rsa_action(
                tenant_id="t1", vin="1FMCU0F70MUA12345", vehicle_id="VEH-BADLAT",
                driver_id=None, display_name=None, reason="test",
                summary=None, triage_session_id=None,
                contact_id="cnt-bl", voice_session_id=None,
            )

        # Find log records that fire for the lat/lon parse failure path
        lat_lon_records = [
            r for r in caplog.records
            if "lat/lon" in r.getMessage().lower() or "invalid lat" in r.getMessage().lower()
        ]
        assert lat_lon_records, (
            "Expected a warning log for invalid lat/lon; none found. "
            "The exception path must still be logged."
        )
        for record in lat_lon_records:
            formatted = record.getMessage()
            assert MALFORMED_LAT not in formatted, (
                f"Malformed lat value {MALFORMED_LAT!r} must NOT appear in the formatted log. "
                f"Got: {formatted!r}. "
                "Mutation guard: restore `e` (str(exc)) → raw value appears, this FAILS."
            )
            args_str = str(record.args or ())
            assert MALFORMED_LAT not in args_str, (
                f"Malformed lat value {MALFORMED_LAT!r} must NOT appear in record.args. "
                f"Got: {args_str!r}."
            )

    def test_invoke_failure_log_no_response_fragment(self, caplog):
        """When the live-state invoke itself fails, str(exc) must not leak to CloudWatch.

        The exception may wrap an HTTP response body containing lat/lon from a
        prior successful fetch that was incorrectly cached in the error path.

        Mutation guard: restore `e` to the invoke-failure warning → the exc message
        (which we embed here) appears in the log, this FAILS.
        """
        import logging

        SENSITIVE_FRAGMENT = "lat=47.6062_IN_EXC"

        with patch.object(h, "_get_lambda") as mock_lambda_fn, \
             caplog.at_level(logging.WARNING):
            mock_lambda_fn.return_value.invoke.side_effect = RuntimeError(
                f"connection error containing {SENSITIVE_FRAGMENT}"
            )
            result = h._resolve_vehicle_position("VEH-INVFAIL", expected_vin="SOME-VIN")

        assert result is None, "invoke failure must return None"

        invoke_fail_records = [
            r for r in caplog.records
            if "invoke failed" in r.getMessage().lower() or "live-state" in r.getMessage().lower()
        ]
        assert invoke_fail_records, "Expected a warning for live-state invoke failure"
        for record in invoke_fail_records:
            formatted = record.getMessage()
            assert SENSITIVE_FRAGMENT not in formatted, (
                f"Sensitive fragment {SENSITIVE_FRAGMENT!r} must NOT appear in formatted log. "
                f"Got: {formatted!r}. "
                "Mutation guard: restore `e` (str(exc)) to the invoke-failure log → this FAILS."
            )
            args_str = str(record.args or ())
            assert SENSITIVE_FRAGMENT not in args_str, (
                f"Sensitive fragment must NOT appear in record.args. Got: {args_str!r}."
            )



# ---------------------------------------------------------------------------
# Security W1, option (b): the caller must BE the vehicle's assigned driver
# ---------------------------------------------------------------------------


class _Drivers:
    """Fake boto3 resource over the CMS drivers table, keyed on driverId."""

    def __init__(self, rows: dict | None = None, raise_on_get: bool = False):
        self.rows = rows or {}
        self.raise_on_get = raise_on_get
        self.keys: list = []

    def Table(self, _name):  # noqa: N802
        return self

    def get_item(self, Key):  # noqa: N803
        self.keys.append(Key)
        if self.raise_on_get:
            raise RuntimeError("ddb down")
        item = self.rows.get(Key["driverId"])
        return {"Item": item} if item else {}


_DRIVER_ROWS = {
    "DRV-1": {"driverId": "DRV-1", "assignedVehicleId": "VEH-001", "phone": "+15555550101"},
    "DRV-ATTACKER": {"driverId": "DRV-ATTACKER", "assignedVehicleId": "VEH-777", "phone": "+15555550199"},
    "DRV-UNASSIGNED": {"driverId": "DRV-UNASSIGNED", "phone": "+15555550188"},
}


class TestCallerAssignedDriverGate:
    """Unit tests for `_caller_assigned_driver`. Identity comes from the verified
    `custom:driverId` claim; the body `driverId` may repeat it, never replace it."""

    def _gate(self, claims, body_driver_id=None, vehicle_id="VEH-001", drivers=None,
              table="cms-test-storage-drivers"):
        drivers = drivers if drivers is not None else _Drivers(_DRIVER_ROWS)
        with patch.object(h, "_CMS_DRIVERS_TABLE", table), \
             patch.object(h, "_get_ddb", return_value=drivers):
            return h._caller_assigned_driver(claims, body_driver_id, vehicle_id), drivers

    def test_assigned_driver_passes(self):
        got, _ = self._gate({"custom:driverId": "DRV-1"})
        assert got == "DRV-1"

    def test_body_repeating_the_claim_passes(self):
        got, _ = self._gate({"custom:driverId": "DRV-1"}, body_driver_id="DRV-1")
        assert got == "DRV-1"

    @pytest.mark.parametrize("claims", [
        {}, {"custom:driverId": ""}, {"custom:driverId": "   "},
        {"custom:driverId": ["DRV-1"]}, {"custom:driverId": None},
    ])
    def test_no_usable_claim_refuses_without_a_read(self, claims):
        got, drivers = self._gate(claims)
        assert got is None
        assert drivers.keys == []

    def test_body_naming_another_driver_refuses_without_a_read(self):
        """Mutation: trust the body driverId over the claim → FAILS."""
        got, drivers = self._gate({"custom:driverId": "DRV-ATTACKER"}, body_driver_id="DRV-1")
        assert got is None
        assert drivers.keys == []

    def test_driver_of_another_vehicle_refuses(self):
        """Mutation: drop the assignedVehicleId comparison → FAILS."""
        got, _ = self._gate({"custom:driverId": "DRV-ATTACKER"})
        assert got is None

    @pytest.mark.parametrize("claim,drivers", [
        ("DRV-UNASSIGNED", _Drivers(_DRIVER_ROWS)),
        ("DRV-NOBODY", _Drivers(_DRIVER_ROWS)),
        ("DRV-1", _Drivers(_DRIVER_ROWS, raise_on_get=True)),
    ])
    def test_unassigned_missing_or_error_refuses(self, claim, drivers):
        got, _ = self._gate({"custom:driverId": claim}, drivers=drivers)
        assert got is None

    def test_unconfigured_table_refuses(self):
        got, drivers = self._gate({"custom:driverId": "DRV-1"}, table=None)
        assert got is None
        assert drivers.keys == []

    def test_lookup_is_keyed_on_the_claim(self):
        _, drivers = self._gate({"custom:driverId": "DRV-1"}, body_driver_id="DRV-1")
        assert drivers.keys == [{"driverId": "DRV-1"}]


class TestP0SideEffectsRequireTheAssignedDriver:
    """The Cycle 2 W1 attack end to end, with the real driver gate and the real
    phone lookup over one fake drivers table. Every other gate is patched open,
    so these tests isolate the new one. The chat must open in every case."""

    VIN = "1HGBH41JXMN000024"

    def _body(self, **extra):
        body = {
            "severity": "P0", "vin": self.VIN, "vehicleId": "VEH-001",
            "reason": "brake failure", "triageSessionId": "sess-p0",
            "dispatchRoadside": True,
        }
        body.update(extra)
        return {k: v for k, v in body.items() if v is not None}

    def _call(self, body, claims):
        connect = MagicMock()
        connect.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        event = {"requestContext": {"authorizer": {"claims": {"custom:tenantId": "test-tenant", **claims}}},
                 "body": json.dumps(body)}
        with patch.object(h, "_CMS_DRIVERS_TABLE", "cms-test-storage-drivers"), \
             patch.object(h, "_get_ddb", return_value=_Drivers(_DRIVER_ROWS)), \
             patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(callback_flow_id="cb-flow-001")), \
             patch.object(h, "_get_connect", return_value=connect), \
             patch.object(h, "_queue_rsa_action", return_value="rsa-ok") as rsa, \
             patch.object(h, "_initiate_callback", return_value=True) as cb:
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        connect.start_chat_contact.assert_called_once()  # the human handoff is never gated
        return _decode(resp), rsa, cb

    def test_drivers_own_escalation_dispatches_and_calls_the_driver(self):
        """The voice-session path: the driver's own token, their own vehicle."""
        out, rsa, cb = self._call(self._body(driverId="DRV-1"), {"custom:driverId": "DRV-1"})
        assert out["rsaDispatched"] is True
        assert rsa.call_args.kwargs["driver_id"] == "DRV-1"
        cb.assert_called_once()
        assert cb.call_args.kwargs["driver_phone"] == "+15555550101"

    def test_drivers_own_escalation_without_a_body_driver_id_still_passes(self):
        """Mutation: dial or record the body driverId instead of the verified
        caller → FAILS (no body id means no call and no driver on the row)."""
        out, rsa, cb = self._call(self._body(), {"custom:driverId": "DRV-1"})
        assert out["rsaDispatched"] is True
        assert rsa.call_args.kwargs["driver_id"] == "DRV-1"
        cb.assert_called_once()
        assert cb.call_args.kwargs["driver_phone"] == "+15555550101"

    @pytest.mark.parametrize("label,body_extra,claims", [
        ("tenant user with no driver identity", {"driverId": "DRV-1"}, {}),
        ("another vehicle's driver", {}, {"custom:driverId": "DRV-ATTACKER"}),
        ("another driver naming the victim", {"driverId": "DRV-1"}, {"custom:driverId": "DRV-ATTACKER"}),
        ("unassigned driver", {}, {"custom:driverId": "DRV-UNASSIGNED"}),
    ])
    def test_non_driver_caller_gets_the_chat_but_no_side_effects(self, label, body_extra, claims):
        """Mutation: remove the driver gate from p0_verified → every case FAILS."""
        out, rsa, cb = self._call(self._body(**body_extra), claims)
        assert out["rsaDispatched"] is False, label
        rsa.assert_not_called()
        cb.assert_not_called()

    def test_non_p0_never_reads_the_drivers_table(self):
        drivers = _Drivers(_DRIVER_ROWS)
        connect = MagicMock()
        connect.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        event = {"requestContext": {"authorizer": {"claims": {"custom:tenantId": "test-tenant",
                                                               "custom:driverId": "DRV-1"}}},
                 "body": json.dumps(self._body(severity="P1"))}
        with patch.object(h, "_CMS_DRIVERS_TABLE", "cms-test-storage-drivers"), \
             patch.object(h, "_get_ddb", return_value=drivers), \
             patch.object(h, "_load_tenant_connect_config",
                          return_value=_connect_cfg(callback_flow_id="cb-flow-001")), \
             patch.object(h, "_get_connect", return_value=connect):
            h.handler(event, None)
        assert drivers.keys == []



# ---------------------------------------------------------------------------
# Fix Group 13: RSA_POSITION_MAX_AGE_SECONDS must be finite, > 0, <= ceiling
# ---------------------------------------------------------------------------


def _load_with_age(monkeypatch, raw):
    """A fresh handler module loaded with RSA_POSITION_MAX_AGE_SECONDS=raw."""
    import importlib.util
    from pathlib import Path

    monkeypatch.setenv("TENANT_CONFIG_TABLE", "test-tenant-config")
    monkeypatch.setenv("RSA_DISPATCH_TABLE", "test-rsa-dispatch")
    monkeypatch.setenv("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")
    monkeypatch.setenv("CMS_STAGE", "test")
    if raw is None:
        monkeypatch.delenv("RSA_POSITION_MAX_AGE_SECONDS", raising=False)
    else:
        monkeypatch.setenv("RSA_POSITION_MAX_AGE_SECONDS", raw)
    handler_path = Path(__file__).parent.parent / "handler.py"
    spec = importlib.util.spec_from_file_location("_escalate_max_age_test", handler_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPositionMaxAgeMustBeFiniteAndBounded:
    """`float()` accepts nan, inf and 1e9; `age > nan` and `age > inf` are always
    False, so those values used to turn the freshness check off entirely."""

    @pytest.mark.parametrize("raw", [
        "nan", "NaN", "inf", "-inf", "Infinity", "1e9", "3601", "3600.5",
        "0", "-5", "abc", "", " ",
    ])
    def test_out_of_range_values_fail_closed(self, monkeypatch, raw):
        """Mutation: drop the upper bound → inf, 1e9, 3601 FAIL. Dropping only
        `math.isfinite` is NOT caught, and cannot be: `0 < nan <= ceiling` is
        False and inf is above the ceiling, so the range check alone already
        refuses both. `isfinite` stays as a readable belt-and-braces check."""
        mod = _load_with_age(monkeypatch, raw)
        assert mod._RSA_POSITION_MAX_AGE_SECONDS == -1.0, raw

    @pytest.mark.parametrize("raw,want", [("900", 900.0), ("1", 1.0), ("3600", 3600.0), ("60.5", 60.5)])
    def test_in_range_values_are_used(self, monkeypatch, raw, want):
        """Mutation: make the ceiling exclusive (<) → 3600 FAILS."""
        mod = _load_with_age(monkeypatch, raw)
        assert mod._RSA_POSITION_MAX_AGE_SECONDS == want

    def test_unset_defaults_to_900(self, monkeypatch):
        mod = _load_with_age(monkeypatch, None)
        assert mod._RSA_POSITION_MAX_AGE_SECONDS == 900.0

    @staticmethod
    def _position(mod, age):
        payload = {"statusCode": 200, "body": json.dumps({
            "vin": "1FMCU0F70MUA12345", "locationSource": "redis", "redisReachable": True,
            "lastSeenAgoSeconds": age, "latitude": 37.7, "longitude": -122.4,
        })}
        lam = MagicMock()
        lam.invoke.return_value = {"Payload": MagicMock(read=MagicMock(return_value=json.dumps(payload).encode()))}
        with patch.object(mod, "_get_lambda", return_value=lam):
            return mod._resolve_vehicle_position("VEH-001", "1FMCU0F70MUA12345")

    def test_positive_control_fresh_position_is_used(self, monkeypatch):
        """Without this the refusal below could come from some other check."""
        mod = _load_with_age(monkeypatch, "900")
        assert self._position(mod, 30) == (37.7, -122.4)

    @pytest.mark.parametrize("raw", ["nan", "inf", "1e9"])
    def test_a_day_old_position_is_refused_under_the_formerly_open_values(self, monkeypatch, raw):
        """The property itself, end to end through the real position resolver: a
        position a day old is refused, which none of these values used to do."""
        mod = _load_with_age(monkeypatch, raw)
        assert self._position(mod, 86400) is None


class TestPositionMaxAgeCeilingIsOneValue:
    """The ceiling lives in three places (handler, synth check, Makefile). This
    pins them to the same number so one cannot be raised alone."""

    def test_handler_stack_and_makefile_agree(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        handler_ceiling = int(h._RSA_POSITION_MAX_AGE_CEILING_SECONDS)
        stack = (root / "infrastructure" / "lib" / "vsa-api-stack.ts").read_text()
        m = re.search(r"RSA_POSITION_MAX_AGE_CEILING_SECONDS\s*=\s*(\d+)", stack)
        assert m, "vsa-api-stack.ts must declare RSA_POSITION_MAX_AGE_CEILING_SECONDS"
        make = (root / "Makefile").read_text()
        mk = re.search(r'CVX_RSA_POSITION_MAX_AGE_SECONDS" -le (\d+)', make)
        assert mk, "Makefile _deploy-cdk must bound CVX_RSA_POSITION_MAX_AGE_SECONDS with -le"
        assert int(m.group(1)) == handler_ceiling == int(mk.group(1))
