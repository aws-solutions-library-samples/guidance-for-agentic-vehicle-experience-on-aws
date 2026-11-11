"""
DS-4 tests: P0 callback placed before chat, correlation id, chatContactFlowId,
p0CallbackPlaced attribute, chatContactFlowId projection guard.

Mutation verification targets (verified after implementation):
    M1: swap call order (chat before call)
        → test_call_is_placed_before_chat FAILS
    M2: use after-callback flow when call FAILED (not only when it succeeded)
        → test_after_callback_flow_only_when_call_succeeded FAILS
    M3: read chatContactFlowId without isinstance string guard
        → test_chat_contact_flow_id_non_string_not_projected FAILS
    M4: remove dry-run guard from connect-p0-call-first.py
        → test_script_dry_run_guard_blocks_write_calls FAILS
    M5: remove identity guard from connect-p0-call-first.py
        → test_identity_guard_refuses_wrong_account FAILS

Run:
    .venv/bin/python -m pytest lambdas/api-escalate/tests/test_ds4_call_first.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Path wiring — same pattern as test_handler.py
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = Path(__file__).resolve().parent.parent
_LAMBDAS_ROOT = _LAMBDA_ROOT.parent
_REPO_ROOT = _LAMBDAS_ROOT.parent

for _p in (str(_LAMBDA_ROOT), str(_LAMBDAS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("TENANT_CONFIG_TABLE", "test-tenant-config")
os.environ.setdefault("CMS_STAGE", "test")
os.environ.setdefault("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
os.environ.setdefault("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")


def _load_handler():
    mod_name = "escalate_handler"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    handler_path = _LAMBDA_ROOT / "handler.py"
    spec = importlib.util.spec_from_file_location(mod_name, handler_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load_handler()

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_CONNECT_RESPONSE = {
    "ContactId": "chat-contact-001",
    "ParticipantId": "participant-001",
    "ParticipantToken": "token-abc",
}


def _event(body: dict, tenant_id: str = "t1") -> dict:
    return {
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenantId": tenant_id}
            }
        },
        "body": json.dumps(body),
    }


def _decode(resp: dict) -> dict:
    return json.loads(resp["body"])


def _p0_body(**extra) -> dict:
    body = {
        "severity": "P0",
        "vin": "1HGBH41JXMN000024",
        "vehicleId": "VEH-001",
        "reason": "brake failure",
        "triageSessionId": "sess-p0",
        "driverId": "DRV-1",
    }
    body.update(extra)
    return body


def _cfg_with_callback(chat_flow: str | None = None, callback_flow="cb-flow-001") -> dict:
    """Tenant config with callback configured, optionally with chatContactFlowId."""
    cfg: dict[str, Any] = {
        "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/inst-1",
        "instanceId": "inst-1",
        "instanceRegion": "us-east-1",
        "contactFlowId": "chat-flow-normal",
        "queueArn": "arn:aws:connect:us-east-1:111122223333:instance/inst-1/queue/q1",
        "autoRequestRoadsideOnP0": False,
        "autoOpenHumanChatOnP0": False,
        "callbackFlowId": callback_flow,
        "callbackQueueId": "q-cb",
        "callbackSourcePhoneNumber": None,
    }
    if chat_flow is not None:
        cfg["chatContactFlowId"] = chat_flow
    return cfg


# ---------------------------------------------------------------------------
# TestDS4CallOrder — M1 mutation target
# ---------------------------------------------------------------------------


class TestDS4CallOrder:
    """DS-4: outbound call must be placed BEFORE the chat contact is created.

    Mutation M1: swap the two calls (chat first, call second) → this test FAILS.
    """

    def _run(self, call_succeeds: bool, chat_flow: str | None = None):
        """Run handler with P0 callback configured. Returns (resp, connect_mock).

        `connect_mock` receives BOTH start_outbound_voice_contact and
        start_chat_contact calls so we can check the order.
        """
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        if call_succeeds:
            connect_mock.start_outbound_voice_contact.return_value = {"ContactId": "call-001"}
        else:
            from botocore.exceptions import ClientError
            connect_mock.start_outbound_voice_contact.side_effect = ClientError(
                {"Error": {"Code": "InternalServiceError", "Message": "fail"}},
                "StartOutboundVoiceContact",
            )

        with patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100"), \
             patch.object(h, "_load_tenant_connect_config",
                          return_value=_cfg_with_callback(chat_flow=chat_flow)), \
             patch.object(h, "_get_connect", return_value=connect_mock), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            resp = h.handler(_event(_p0_body()), None)
        return resp, connect_mock

    def test_call_is_placed_before_chat(self):
        """M1: swap call order → this test FAILS.

        Verifies that start_outbound_voice_contact appears earlier in
        connect_mock.method_calls than start_chat_contact.
        """
        _, connect_mock = self._run(call_succeeds=True)

        call_names = [c[0] for c in connect_mock.method_calls]
        assert "start_outbound_voice_contact" in call_names, (
            "start_outbound_voice_contact must be called (the outbound call)"
        )
        assert "start_chat_contact" in call_names, (
            "start_chat_contact must be called (the human handoff)"
        )
        assert call_names.index("start_outbound_voice_contact") < call_names.index("start_chat_contact"), (
            "The outbound call must be placed BEFORE the chat is created. "
            "M1 mutation (swap order) should make this assertion fail."
        )

    def test_call_is_placed_before_chat_even_when_call_fails(self):
        """Even when the outbound call raises, the attempt must precede the chat."""
        _, connect_mock = self._run(call_succeeds=False)

        call_names = [c[0] for c in connect_mock.method_calls]
        assert "start_outbound_voice_contact" in call_names
        assert "start_chat_contact" in call_names
        assert call_names.index("start_outbound_voice_contact") < call_names.index("start_chat_contact")

    def test_response_is_200_and_has_contact_id(self):
        """Positive control: end-to-end handler returns 200 with contactId."""
        resp, _ = self._run(call_succeeds=True)
        assert resp["statusCode"] == 200
        body = _decode(resp)
        assert body["contactId"] == "chat-contact-001"

    def test_non_p0_path_makes_no_outbound_call(self):
        """The non-callback path (P1, or P0 without callback config) must not change."""
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        # P1 — no callback path at all
        cfg_no_cb = {
            "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/inst-1",
            "instanceId": "inst-1", "instanceRegion": "us-east-1",
            "contactFlowId": "chat-flow-normal", "queueArn": "q",
            "autoRequestRoadsideOnP0": False, "autoOpenHumanChatOnP0": False,
            "callbackFlowId": None, "callbackQueueId": None,
            "callbackSourcePhoneNumber": None, "chatContactFlowId": None,
        }
        body = {"severity": "P1", "vin": "1HGBH41JXMN000024",
                "vehicleId": "VEH-001", "reason": "oil low"}
        with patch.object(h, "_load_tenant_connect_config", return_value=cfg_no_cb), \
             patch.object(h, "_get_connect", return_value=connect_mock):
            resp = h.handler(_event(body), None)

        assert resp["statusCode"] == 200
        connect_mock.start_outbound_voice_contact.assert_not_called()
        connect_mock.start_chat_contact.assert_called_once()


# ---------------------------------------------------------------------------
# TestDS4CorrelationId
# ---------------------------------------------------------------------------


class TestDS4CorrelationId:
    """DS-4: a correlation id is minted before either contact so both can be tied together.

    The id used for the outbound call (escalation_id in _initiate_callback) must
    NOT be the chat's ContactId (which doesn't exist yet at call time) and must
    equal the p0CallbackEscalationId attribute on the chat.
    """

    def _run_and_capture(self, call_returns=True):
        """Returns (resp, captured_escalation_id, chat_attributes)."""
        captured: dict = {}

        original_initiate = h._initiate_callback

        def _mock_initiate(*, escalation_id, **kw):
            captured["escalation_id"] = escalation_id
            return call_returns

        connect_mock = MagicMock()
        connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        with patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100"), \
             patch.object(h, "_load_tenant_connect_config",
                          return_value=_cfg_with_callback(chat_flow="after-cb-flow")), \
             patch.object(h, "_get_connect", return_value=connect_mock), \
             patch.object(h, "_initiate_callback", side_effect=_mock_initiate), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            resp = h.handler(_event(_p0_body()), None)

        chat_attrs = connect_mock.start_chat_contact.call_args.kwargs.get("Attributes", {})
        return resp, captured.get("escalation_id"), chat_attrs

    def test_correlation_id_is_not_the_chat_contact_id(self):
        """The escalationId on the call must be set BEFORE the chat contact exists.

        If it were the chat's ContactId, the call could not have been placed first
        — contact IDs are generated by Connect and returned only from StartChatContact.
        """
        _, esc_id, _ = self._run_and_capture()
        assert esc_id is not None
        # The chat contact id is "chat-contact-001" (from _CONNECT_RESPONSE)
        assert esc_id != "chat-contact-001", (
            "escalationId on the call must NOT be the chat's ContactId (which "
            "didn't exist yet when the call was placed)"
        )

    def test_correlation_id_appears_on_the_chat_as_attribute(self):
        """The same id used for the call appears on the chat as p0CallbackEscalationId."""
        _, esc_id, attrs = self._run_and_capture()
        assert esc_id is not None
        assert attrs.get("p0CallbackEscalationId") == esc_id, (
            "p0CallbackEscalationId on the chat must equal the escalationId "
            "used for the outbound call"
        )

    def test_correlation_id_is_a_uuid(self):
        """The correlation id is a UUID (not some other format)."""
        import uuid
        _, esc_id, _ = self._run_and_capture()
        # Should parse as a UUID without raising
        parsed = uuid.UUID(esc_id)
        assert str(parsed) == esc_id

    def test_correlation_id_absent_on_non_callback_path(self):
        """p0CallbackEscalationId must NOT be set when there is no callback configured."""
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        cfg_no_cb = {
            "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/inst-1",
            "instanceId": "inst-1", "instanceRegion": "us-east-1",
            "contactFlowId": "chat-flow-normal", "queueArn": "q",
            "autoRequestRoadsideOnP0": False, "autoOpenHumanChatOnP0": False,
            "callbackFlowId": None, "callbackQueueId": None,
            "callbackSourcePhoneNumber": None, "chatContactFlowId": None,
        }
        body = {"severity": "P0", "vin": "1HGBH41JXMN000024",
                "vehicleId": "VEH-001", "reason": "brakes"}
        with patch.object(h, "_load_tenant_connect_config", return_value=cfg_no_cb), \
             patch.object(h, "_get_connect", return_value=connect_mock), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            resp = h.handler(_event(body), None)

        attrs = connect_mock.start_chat_contact.call_args.kwargs.get("Attributes", {})
        assert "p0CallbackEscalationId" not in attrs, (
            "p0CallbackEscalationId must only be set when a callback was attempted"
        )


# ---------------------------------------------------------------------------
# TestDS4ChatFlowChoice — M2 mutation target
# ---------------------------------------------------------------------------


class TestDS4ChatFlowChoice:
    """DS-4: chat uses chatContactFlowId ONLY when the call succeeded AND key is set.

    Mutation M2: use chatContactFlowId even when the call failed
    → test_after_callback_flow_only_when_call_succeeded FAILS.
    """

    def _run(self, call_succeeds: bool, chat_flow_in_cfg: str | None):
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        if call_succeeds:
            connect_mock.start_outbound_voice_contact.return_value = {"ContactId": "vc-1"}
        else:
            from botocore.exceptions import ClientError
            connect_mock.start_outbound_voice_contact.side_effect = ClientError(
                {"Error": {"Code": "Error", "Message": "fail"}},
                "StartOutboundVoiceContact",
            )

        with patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100"), \
             patch.object(h, "_load_tenant_connect_config",
                          return_value=_cfg_with_callback(chat_flow=chat_flow_in_cfg)), \
             patch.object(h, "_get_connect", return_value=connect_mock), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            h.handler(_event(_p0_body()), None)

        return connect_mock.start_chat_contact.call_args.kwargs["ContactFlowId"]

    def test_after_callback_flow_used_when_call_succeeded_and_key_set(self):
        """Call succeeded + chatContactFlowId configured → use the after-callback flow."""
        flow_used = self._run(call_succeeds=True, chat_flow_in_cfg="after-cb-flow-999")
        assert flow_used == "after-cb-flow-999", (
            "When call succeeded and chatContactFlowId is set, "
            "the chat must use the after-callback flow"
        )

    # MUTATION M2: change `if callback_initiated and ...` to `if cfg.get(...)`
    # (use the flow regardless of whether the call succeeded) → this test FAILS.
    def test_after_callback_flow_only_when_call_succeeded(self):
        """M2: use after-callback flow even when call failed → this test FAILS.

        When the call fails, the chat MUST use the normal flow, not the
        after-callback flow, so the driver still reaches a human promptly.
        """
        flow_used = self._run(call_succeeds=False, chat_flow_in_cfg="after-cb-flow-999")
        assert flow_used == "chat-flow-normal", (
            "When the call FAILED, the chat must use the normal flow (chat-flow-normal), "
            "not the after-callback delayed flow. "
            "M2 mutation (use flow regardless of call result) should make this fail."
        )

    def test_normal_flow_used_when_chatContactFlowId_not_set(self):
        """When chatContactFlowId is not configured, normal flow is used even on call success."""
        flow_used = self._run(call_succeeds=True, chat_flow_in_cfg=None)
        assert flow_used == "chat-flow-normal"

    def test_normal_flow_used_when_no_callback_configured(self):
        """Non-P0-callback path: normal flow is used."""
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        cfg_no_cb = {
            "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/inst-1",
            "instanceId": "inst-1", "instanceRegion": "us-east-1",
            "contactFlowId": "chat-flow-normal", "queueArn": "q",
            "autoRequestRoadsideOnP0": False, "autoOpenHumanChatOnP0": False,
            "callbackFlowId": None, "callbackQueueId": None,
            "callbackSourcePhoneNumber": None, "chatContactFlowId": "after-cb-flow-x",
        }
        body = {"severity": "P1", "vin": "1HGBH41JXMN000024",
                "vehicleId": "VEH-001", "reason": "oil"}
        with patch.object(h, "_load_tenant_connect_config", return_value=cfg_no_cb), \
             patch.object(h, "_get_connect", return_value=connect_mock), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            h.handler(_event(body), None)

        flow_used = connect_mock.start_chat_contact.call_args.kwargs["ContactFlowId"]
        assert flow_used == "chat-flow-normal", (
            "Non-callback P1 path must always use the normal flow"
        )


# ---------------------------------------------------------------------------
# TestDS4P0CallbackPlacedAttribute
# ---------------------------------------------------------------------------


class TestDS4P0CallbackPlacedAttribute:
    """DS-4: p0CallbackPlaced="true"/"false" attribute on the chat."""

    def _run(self, call_succeeds: bool) -> dict:
        """Run handler and return the Attributes dict from start_chat_contact."""
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        if call_succeeds:
            connect_mock.start_outbound_voice_contact.return_value = {"ContactId": "vc-1"}
        else:
            from botocore.exceptions import ClientError
            connect_mock.start_outbound_voice_contact.side_effect = ClientError(
                {"Error": {"Code": "E", "Message": "fail"}},
                "StartOutboundVoiceContact",
            )

        with patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100"), \
             patch.object(h, "_load_tenant_connect_config",
                          return_value=_cfg_with_callback(chat_flow="after-cb")), \
             patch.object(h, "_get_connect", return_value=connect_mock), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            h.handler(_event(_p0_body()), None)

        return connect_mock.start_chat_contact.call_args.kwargs.get("Attributes", {})

    def test_p0_callback_placed_true_when_call_succeeds(self):
        """Mutation: flip 'true'/'false' → FAILS."""
        attrs = self._run(call_succeeds=True)
        assert attrs.get("p0CallbackPlaced") == "true", (
            "p0CallbackPlaced must be the string 'true' when the call succeeded"
        )

    def test_p0_callback_placed_false_when_call_fails(self):
        """Mutation: hardcode 'true' → FAILS."""
        attrs = self._run(call_succeeds=False)
        assert attrs.get("p0CallbackPlaced") == "false", (
            "p0CallbackPlaced must be the string 'false' when the call failed"
        )

    def test_p0_callback_placed_is_a_string(self):
        """Connect attributes must be strings, not booleans."""
        for result in (True, False):
            attrs = self._run(call_succeeds=result)
            val = attrs.get("p0CallbackPlaced")
            assert isinstance(val, str), (
                f"p0CallbackPlaced must be a string, not {type(val).__name__!r}"
            )

    def test_p0_callback_placed_absent_when_no_callback_configured(self):
        """No callback path → p0CallbackPlaced must not appear."""
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
        cfg_no_cb = {
            "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/inst-1",
            "instanceId": "inst-1", "instanceRegion": "us-east-1",
            "contactFlowId": "chat-flow-normal", "queueArn": "q",
            "autoRequestRoadsideOnP0": False, "autoOpenHumanChatOnP0": False,
            "callbackFlowId": None, "callbackQueueId": None,
            "callbackSourcePhoneNumber": None, "chatContactFlowId": None,
        }
        body = {"severity": "P0", "vin": "1HGBH41JXMN000024",
                "vehicleId": "VEH-001", "reason": "check engine"}
        with patch.object(h, "_load_tenant_connect_config", return_value=cfg_no_cb), \
             patch.object(h, "_get_connect", return_value=connect_mock), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            h.handler(_event(body), None)

        attrs = connect_mock.start_chat_contact.call_args.kwargs.get("Attributes", {})
        assert "p0CallbackPlaced" not in attrs, (
            "p0CallbackPlaced must only appear when a callback was attempted"
        )


# ---------------------------------------------------------------------------
# TestDS4ChatContactFlowIdProjection — M3 mutation target
# ---------------------------------------------------------------------------


class TestDS4ChatContactFlowIdProjection:
    """DS-4: chatContactFlowId projected from escalation.callback with string guard.

    Mutation M3: remove isinstance guard (allow non-string through)
    → test_non_string_chat_contact_flow_id_is_treated_as_absent FAILS.
    """

    def _cfg_from(self, escalation: dict) -> dict | None:
        """Call _load_tenant_connect_config via a fake DDB with the given escalation block."""
        tenant_row = {
            "tenantId": "t", "version": "1.1.0",
            "channels": {"connect": {
                "instanceArn": "arn:aws:connect:us-east-1:123456789012:instance/inst-1",
                "contactFlowId": "chat-flow",
            }},
            "escalation": escalation,
        }

        class _Tenants:
            def Table(self, _n):
                return self
            def get_item(self, Key):
                if Key["version"] == "current":
                    return {"Item": {"activeVersion": "1.1.0"}}
                return {"Item": tenant_row}

        with patch.object(h, "_get_ddb", return_value=_Tenants()):
            return h._load_tenant_connect_config("t")

    def test_string_chat_contact_flow_id_is_projected(self):
        """Mutation M3: drop the key from the return dict → FAILS."""
        cfg = self._cfg_from({"callback": {
            "contactFlowId": "cb-flow",
            "chatContactFlowId": "after-cb-flow-123",
        }})
        assert cfg is not None
        assert cfg["chatContactFlowId"] == "after-cb-flow-123", (
            "chatContactFlowId must be projected from escalation.callback"
        )

    def test_absent_chat_contact_flow_id_is_none(self):
        """No chatContactFlowId key → projected as None."""
        cfg = self._cfg_from({"callback": {"contactFlowId": "cb-flow"}})
        assert cfg is not None
        assert cfg["chatContactFlowId"] is None

    @pytest.mark.parametrize("bad_value", [
        42,           # integer
        3.14,         # float
        True,         # bool
        ["id"],       # list
        {"id": "x"},  # dict
        None,         # explicit None
    ])
    def test_non_string_chat_contact_flow_id_is_treated_as_absent(self, bad_value):
        """M3: remove isinstance guard → non-strings pass through → FAILS.

        A non-string chatContactFlowId must be treated as absent (None),
        just like callback_queue_arn's guard prevents AttributeError on
        non-string queueArn values. A malformed field must not break
        /escalate for every severity.
        """
        cfg = self._cfg_from({"callback": {
            "contactFlowId": "cb-flow",
            "chatContactFlowId": bad_value,
        }})
        assert cfg is not None, "Config must not be None with bad chatContactFlowId"
        assert cfg["chatContactFlowId"] is None, (
            f"chatContactFlowId={bad_value!r} (type {type(bad_value).__name__}) "
            "must be treated as absent (None), not passed through. "
            "M3 mutation (remove isinstance guard) should make this fail."
        )
        # Must not break the normal chat flow
        assert cfg["callbackFlowId"] == "cb-flow"

    def test_no_callback_block_gives_none_for_chat_flow_id(self):
        """No callback block at all → chatContactFlowId is None."""
        cfg = self._cfg_from({})
        assert cfg is not None
        assert cfg["chatContactFlowId"] is None


# ---------------------------------------------------------------------------
# TestConnectScriptDryRunGuard — M4 mutation target
# ---------------------------------------------------------------------------


class TestConnectScriptDryRunGuard:
    """DS-4 script: dry run cannot make write calls.

    Guard is installed on the botocore session BEFORE any client is created.
    Test uses a real client with dummy creds and AWS_ENDPOINT_URL pointing at
    a port that is not listening (127.0.0.1:1) — so if the guard is missing,
    the test gets a connection error, not DryRunBlocked.

    Mutation M4: remove the guard → test gets ConnectionError → test FAILS.
    """

    @pytest.fixture(autouse=True)
    def _load_script(self):
        """Load the connect-p0-call-first script under a unique module name."""
        mod_name = "_ds4_connect_script"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        script_path = _REPO_ROOT / "scripts" / "connect-p0-call-first.py"
        spec = importlib.util.spec_from_file_location(mod_name, script_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        self.script = mod

    def test_dry_run_guard_blocks_write_calls(self, monkeypatch):
        """M4: remove the guard → write call reaches the network → test FAILS.

        Creates a real Connect client via the script's session with dummy
        credentials and AWS_ENDPOINT_URL=http://127.0.0.1:1 so any network
        call fails immediately. The guard must prevent even reaching the network
        for write operations. Read operations (DescribeQueue etc.) are allowed
        through by the guard.
        """
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

        client = self.script.make_connect_client(region="us-east-1")

        with pytest.raises(self.script.DryRunBlocked):
            client.create_queue(
                InstanceId="inst-1",
                Name="test-queue",
                HoursOfOperationId="hours-id-1",
            )

    def test_dry_run_blocks_associate_routing_profile(self, monkeypatch):
        """All write operations are blocked in dry run."""
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

        client = self.script.make_connect_client(region="us-east-1")

        with pytest.raises(self.script.DryRunBlocked):
            client.associate_routing_profile_queues(
                InstanceId="inst-1",
                RoutingProfileId="rp-1",
                QueueConfigs=[{
                    "QueueReference": {"QueueId": "q-1", "Channel": "CHAT"},
                    "Priority": 1,
                    "Delay": 60,
                }],
            )

    def test_dry_run_blocks_create_contact_flow(self, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

        client = self.script.make_connect_client(region="us-east-1")

        with pytest.raises(self.script.DryRunBlocked):
            client.create_contact_flow(
                InstanceId="inst-1",
                Name="test-flow",
                Type="CONTACT_FLOW",
                Content='{"Version":"2019-10-30","StartAction":"id","Actions":[]}',
            )


# ---------------------------------------------------------------------------
# TestConnectScriptIdentityGuard — M5 mutation target
# ---------------------------------------------------------------------------


class TestConnectScriptIdentityGuard:
    """DS-4 script: identity guard refuses wrong account/instance.

    Mutation M5: remove the identity guard → wrong-account run proceeds → test FAILS.
    """

    @pytest.fixture(autouse=True)
    def _load_script(self):
        mod_name = "_ds4_connect_script_id"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        script_path = _REPO_ROOT / "scripts" / "connect-p0-call-first.py"
        spec = importlib.util.spec_from_file_location(mod_name, script_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        self.script = mod

    def test_identity_guard_refuses_wrong_account(self):
        with pytest.raises(self.script.IdentityMismatch):
            self.script.check_identity(actual_account="999999999999", expected_account="111122223333")

    def test_identity_guard_passes_the_stated_account(self):
        self.script.check_identity(actual_account="111122223333", expected_account="111122223333")

    @pytest.mark.parametrize("actual,expected", [("", "111122223333"), ("111122223333", ""), ("", "")])
    def test_identity_guard_refuses_empty_values(self, actual, expected):
        with pytest.raises(self.script.IdentityMismatch):
            self.script.check_identity(actual_account=actual, expected_account=expected)

    def test_account_is_required_on_the_command_line(self):
        with pytest.raises(SystemExit):
            self.script._parse_args(["--instance", "inst-1"])

    def test_main_checks_identity_before_any_connect_call(self, monkeypatch):
        monkeypatch.setattr(self.script, "_get_caller_account", lambda: "999999999999")
        made = []
        monkeypatch.setattr(self.script, "make_connect_client", lambda region: made.append(region))
        with pytest.raises(self.script.IdentityMismatch):
            self.script.main(["--account", "111122223333", "--instance", "inst-1"])
        assert made == []

    def test_no_account_id_is_kept_in_the_script(self):
        import re as _re
        text = (_REPO_ROOT / "scripts" / "connect-p0-call-first.py").read_text()
        assert not _re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text)


class TestConnectScriptGuardIsAnAllowlist:
    """Dry run admits only Describe/List/Get/Search, whatever the operation."""

    @pytest.fixture(autouse=True)
    def _load_script(self, monkeypatch):
        mod_name = "_ds4_connect_script_allow"
        sys.modules.pop(mod_name, None)
        script_path = _REPO_ROOT / "scripts" / "connect-p0-call-first.py"
        spec = importlib.util.spec_from_file_location(mod_name, script_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        self.script = mod
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    def test_a_write_not_named_anywhere_is_still_blocked(self):
        client = self.script.make_connect_client(region="us-east-1")
        with pytest.raises(self.script.DryRunBlocked):
            client.update_queue_name(InstanceId="inst-1", QueueId="q-1", Name="x")
        with pytest.raises(self.script.DryRunBlocked):
            client.update_routing_profile_concurrency(
                InstanceId="inst-1", RoutingProfileId="rp-1",
                MediaConcurrencies=[{"Channel": "CHAT", "Concurrency": 1}],
            )

    def test_a_read_passes_the_guard_and_reaches_the_network(self):
        """Positive control: a read is not refused by the guard (it fails on the
        unreachable endpoint instead), so the write tests above are not vacuous."""
        import botocore.exceptions as bexc
        client = self.script.make_connect_client(region="us-east-1")
        with pytest.raises(bexc.EndpointConnectionError):
            client.describe_queue(InstanceId="inst-1", QueueId="q-1")

    def test_main_dry_run_client_refuses_writes(self, monkeypatch):
        """The client main() builds is the guarded one: a write attempted from
        the plan step is refused."""
        monkeypatch.setattr(self.script, "_get_caller_account", lambda: "111122223333")
        monkeypatch.setattr(self.script, "_find_instance_id", lambda c, n: "inst-1")

        def _plan(connect, instance_id, delay):
            connect.create_queue(InstanceId=instance_id, Name="x", HoursOfOperationId="h")

        monkeypatch.setattr(self.script, "plan", _plan)
        with pytest.raises(self.script.DryRunBlocked):
            self.script.main(["--account", "111122223333", "--instance", "inst-1"])


# ---------------------------------------------------------------------------
# TestConnectScriptFlowContentSwap
# ---------------------------------------------------------------------------


class TestConnectScriptFlowContentSwap:
    """DS-4 script: flow content swap refuses 0 or >1 ARN matches."""

    @pytest.fixture(autouse=True)
    def _load_script(self):
        mod_name = "_ds4_connect_script_fc"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        script_path = _REPO_ROOT / "scripts" / "connect-p0-call-first.py"
        spec = importlib.util.spec_from_file_location(mod_name, script_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        self.script = mod

    OLD_ARN = "arn:aws:connect:us-east-1:123456789012:instance/inst-1/queue/q-old"
    NEW_ARN = "arn:aws:connect:us-east-1:123456789012:instance/inst-1/queue/q-new"

    def _flow_content(self, arns: list[str]) -> str:
        """Build a fake flow Content JSON with the given queue ARNs."""
        actions = [
            {"Identifier": f"step-{i}", "Parameters": {"QueueId": arn}}
            for i, arn in enumerate(arns)
        ]
        return json.dumps({"Version": "2019-10-30", "Actions": actions})

    def test_exactly_one_match_is_swapped(self):
        content = self._flow_content([self.OLD_ARN])
        result = self.script.swap_queue_arn(content, self.OLD_ARN, self.NEW_ARN)
        assert self.NEW_ARN in result
        assert self.OLD_ARN not in result

    def test_zero_matches_raises(self):
        content = self._flow_content(["arn:aws:connect:us-east-1:1:instance/i/queue/other"])
        with pytest.raises(self.script.FlowSwapError):
            self.script.swap_queue_arn(content, self.OLD_ARN, self.NEW_ARN)

    def test_multiple_matches_raises(self):
        content = self._flow_content([self.OLD_ARN, self.OLD_ARN])
        with pytest.raises(self.script.FlowSwapError):
            self.script.swap_queue_arn(content, self.OLD_ARN, self.NEW_ARN)


# ---------------------------------------------------------------------------
# Review cycle 1 (driver-safety batch): chat failure after a placed call,
# bounded Connect client, flow swap validated before any write
# ---------------------------------------------------------------------------


class TestChatFailureAfterPlacedCall:
    def _run(self, call_succeeds: bool):
        from botocore.exceptions import ClientError
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "chat down"}}, "StartChatContact",
        )
        if call_succeeds:
            connect_mock.start_outbound_voice_contact.return_value = {"ContactId": "call-001"}
        else:
            connect_mock.start_outbound_voice_contact.side_effect = ClientError(
                {"Error": {"Code": "InternalServiceError", "Message": "fail"}}, "StartOutboundVoiceContact",
            )
        with patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100"), \
             patch.object(h, "_load_tenant_connect_config", return_value=_cfg_with_callback()), \
             patch.object(h, "_get_connect", return_value=connect_mock), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            return h.handler(_event(_p0_body()), None)

    def test_502_says_the_call_was_placed(self):
        resp = self._run(call_succeeds=True)
        assert resp["statusCode"] == 502
        assert _decode(resp)["callbackInitiated"] is True

    def test_502_says_no_call_when_it_failed(self):
        resp = self._run(call_succeeds=False)
        assert resp["statusCode"] == 502
        assert _decode(resp)["callbackInitiated"] is False


def test_connect_client_is_bounded():
    """The call now runs before the chat; a stalled call must not eat the 15 s."""
    cfg = h._CONNECT_CONFIG
    assert cfg.connect_timeout <= 2 and cfg.read_timeout <= 8
    assert cfg.retries == {"total_max_attempts": 2}
    h._connect = None
    client = h._get_connect("us-east-1")
    assert client.meta.config.read_timeout == cfg.read_timeout
    h._connect = None


class TestConnectScriptApplyOrder:
    @pytest.fixture(autouse=True)
    def _load_script(self):
        mod_name = "_ds4_connect_script_apply"
        sys.modules.pop(mod_name, None)
        script_path = _REPO_ROOT / "scripts" / "connect-p0-call-first.py"
        spec = importlib.util.spec_from_file_location(mod_name, script_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        self.script = mod

    def _connect(self, flow_content: str):
        c = MagicMock()
        self.script.plan = lambda connect, iid, d: {
            "source_queue_id": "q-src", "source_queue_arn": "arn:q-src",
            "source_flow_id": "f-src", "routing_profile_id": "rp-1", "chat_priority": 1,
        }
        c.describe_contact_flow.return_value = {"ContactFlow": {"Content": flow_content}}
        c.describe_queue.return_value = {"Queue": {"HoursOfOperationId": "h-1"}}
        c.create_queue.return_value = {"QueueId": "q-new", "QueueArn": "arn:q-new"}
        c.create_contact_flow.return_value = {"ContactFlowId": "f-new", "ContactFlowArn": "arn:f-new"}
        return c

    def test_flow_swap_refused_before_any_write(self):
        """Review cycle 1 W5: a flow without the queue ARN refuses with nothing created."""
        c = self._connect('{"Actions": []}')
        with pytest.raises(self.script.FlowSwapError):
            self.script.apply_changes(c, "inst-1", 60)
        c.create_queue.assert_not_called()
        c.associate_routing_profile_queues.assert_not_called()
        c.create_contact_flow.assert_not_called()

    def test_apply_prints_the_flow_id_for_the_tenant_key(self, capsys):
        c = self._connect('{"QueueId": "arn:q-src"}')
        out = self.script.apply_changes(c, "inst-1", 60)
        assert out["new_flow_id"] == "f-new"
        assert '"chatContactFlowId": "f-new"' in capsys.readouterr().out

    def test_rollback_without_a_flow_id_deletes_the_queue(self, monkeypatch):
        c = MagicMock()
        monkeypatch.setattr(self.script, "_find_routing_profile_by_name", lambda *a: {"Id": "rp-1"})
        c.disassociate_routing_profile_queues.side_effect = RuntimeError("not associated")
        self.script.rollback(c, "inst-1", "q-new", None)
        c.delete_contact_flow.assert_not_called()
        c.delete_queue.assert_called_once_with(InstanceId="inst-1", QueueId="q-new")


class TestCycle2EscalateFixes:
    def _run_transport_failure(self):
        from botocore.exceptions import EndpointConnectionError
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.side_effect = EndpointConnectionError(endpoint_url="https://x")
        connect_mock.start_outbound_voice_contact.return_value = {"ContactId": "call-001"}
        with patch.object(h, "_verified_p0_decision", return_value=True), \
             patch.object(h, "_vehicle_matches_vin", return_value=True), \
             patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
             patch.object(h, "_resolve_driver_phone", return_value="+15555550100"), \
             patch.object(h, "_load_tenant_connect_config", return_value=_cfg_with_callback()), \
             patch.object(h, "_get_connect", return_value=connect_mock), \
             patch.object(h, "_queue_rsa_action", return_value=None):
            return h.handler(_event(_p0_body()), None)

    def test_transport_error_502_says_the_call_was_placed(self):
        """Review cycle 2 M3."""
        resp = self._run_transport_failure()
        assert resp["statusCode"] == 502
        assert _decode(resp)["callbackInitiated"] is True

    def test_client_error_502_puts_the_call_first_with_its_correlation_id(self):
        resp = TestChatFailureAfterPlacedCall()._run(call_succeeds=True)
        body = _decode(resp)
        assert list(body)[0] == "callbackInitiated"
        assert body["p0CallbackEscalationId"]

    def test_the_call_has_its_own_tighter_client(self):
        cfg = h._CALLBACK_CONNECT_CONFIG
        assert cfg.connect_timeout <= 1 and cfg.read_timeout <= 3
        assert cfg.retries == {"total_max_attempts": 1}
        h._connect = None
        h._callback_connect = None
        chat, call = h._get_connect("us-east-1"), h._get_connect("us-east-1", callback=True)
        assert chat is not call
        assert call.meta.config.read_timeout == cfg.read_timeout
        assert chat.meta.config.read_timeout == h._CONNECT_CONFIG.read_timeout
        h._connect = None
        h._callback_connect = None

    def test_initiate_callback_uses_the_callback_client(self):
        calls = []

        def fake_get_connect(region=None, *, callback=False):
            calls.append(callback)
            return MagicMock()

        with patch.object(h, "_get_connect", side_effect=fake_get_connect):
            h._initiate_callback(
                instance_id="i", instance_region="us-east-1", contact_flow_id="f",
                driver_phone="+15555550100", escalation_id="e", queue_id="q",
                source_phone_number=None,
            )
        assert calls == [True]




# ---------------------------------------------------------------------------
# Review cycle 3 W2: every 502 after the call point reports the call truthfully
# ---------------------------------------------------------------------------


def _chat_failure(kind: str):
    from botocore.exceptions import ClientError, EndpointConnectionError
    if kind == "client_error":
        return {"side_effect": ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "chat down"}}, "StartChatContact")}
    if kind == "transport_error":
        return {"side_effect": EndpointConnectionError(endpoint_url="https://x")}
    return {"return_value": {"ContactId": "", "ParticipantId": "", "ParticipantToken": ""}}


@pytest.mark.parametrize("kind", ["client_error", "transport_error", "incomplete_response"])
@pytest.mark.parametrize("call_succeeds", [True, False])
def test_every_chat_failure_reports_whether_the_call_was_placed(kind, call_succeeds):
    from botocore.exceptions import ClientError
    connect_mock = MagicMock()
    for attr, value in _chat_failure(kind).items():
        setattr(connect_mock.start_chat_contact, attr, value)
    if call_succeeds:
        connect_mock.start_outbound_voice_contact.return_value = {"ContactId": "call-001"}
    else:
        connect_mock.start_outbound_voice_contact.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "fail"}}, "StartOutboundVoiceContact")
    with patch.object(h, "_verified_p0_decision", return_value=True), \
         patch.object(h, "_vehicle_matches_vin", return_value=True), \
         patch.object(h, "_caller_assigned_driver", return_value="DRV-1"), \
         patch.object(h, "_resolve_driver_phone", return_value="+15555550100"), \
         patch.object(h, "_load_tenant_connect_config", return_value=_cfg_with_callback()), \
         patch.object(h, "_get_connect", return_value=connect_mock), \
         patch.object(h, "_queue_rsa_action", return_value=None):
        resp = h.handler(_event(_p0_body()), None)
    assert resp["statusCode"] == 502
    assert _decode(resp)["callbackInitiated"] is call_succeeds
