"""T1.6a — p0Callback in the /escalate 200 body.

Tests that handler.py adds p0Callback to the 200 response:
  - p0Callback.placed is True when the outbound call succeeded.
  - p0Callback.placed is False when the call was attempted but failed.
  - p0Callback.maskedNumber is "•••-•••-<last4>" only when placed=True.
  - p0Callback is absent when no callback was configured (non-callback path).
  - The full driver phone number is NEVER present anywhere in the 200 body.

Mutation targets (verified green→red after implementation):
  M1: placed=False → maskedNumber still appears
      → test_masked_number_absent_when_not_placed FAILS
  M2: p0Callback sent even when will_place_call is False (no callback path)
      → test_p0callback_absent_on_non_callback_path FAILS
  M3: full phone number leaked (e.g. maskedNumber = phone)
      → test_full_number_not_in_body FAILS
  M4: placed hardcoded to True regardless of callback_initiated
      → test_placed_false_when_call_fails FAILS
  M5: maskedNumber uses wrong last-N (not 4 digits)
      → test_masked_number_format FAILS

Run (from the repo root, with .venv active):
    .venv/bin/python -m pytest lambdas/api-escalate/test_p0callback_body.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors test_ds4_call_first.py)
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = Path(__file__).resolve().parent.parent  # lambdas/api-escalate
_LAMBDAS_ROOT = _LAMBDA_ROOT.parent                   # lambdas/

for _p in (str(_LAMBDA_ROOT), str(_LAMBDAS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("TENANT_CONFIG_TABLE", "test-tenant-config")
os.environ.setdefault("CMS_STAGE", "test")
os.environ.setdefault("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
os.environ.setdefault("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")


def _load_handler():
    mod_name = "escalate_handler_p0cb"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    handler_path = _LAMBDA_ROOT / "handler.py"
    spec = importlib.util.spec_from_file_location(mod_name, handler_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


h = _load_handler()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONNECT_RESPONSE = {
    "ContactId": "chat-contact-p0cb-001",
    "ParticipantId": "participant-p0cb-001",
    "ParticipantToken": "token-p0cb-abc",
}
_DRIVER_PHONE = "+15555550199"  # last 4 digits: 0199


def _event(body: dict, tenant_id: str = "t1") -> dict:
    return {
        "requestContext": {
            "authorizer": {"claims": {"custom:tenantId": tenant_id}}
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


def _cfg_with_callback() -> dict:
    return {
        "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/inst-1",
        "instanceId": "inst-1",
        "instanceRegion": "us-east-1",
        "contactFlowId": "chat-flow-normal",
        "queueArn": "q",
        "autoRequestRoadsideOnP0": False,
        "autoOpenHumanChatOnP0": False,
        "callbackFlowId": "cb-flow-001",
        "callbackQueueId": "q-cb",
        "callbackSourcePhoneNumber": None,
        "chatContactFlowId": None,
    }


def _cfg_no_callback() -> dict:
    return {
        "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/inst-1",
        "instanceId": "inst-1",
        "instanceRegion": "us-east-1",
        "contactFlowId": "chat-flow-normal",
        "queueArn": "q",
        "autoRequestRoadsideOnP0": False,
        "autoOpenHumanChatOnP0": False,
        "callbackFlowId": None,
        "callbackQueueId": None,
        "callbackSourcePhoneNumber": None,
        "chatContactFlowId": None,
    }


def _run_handler(
    call_succeeds: bool,
    cfg: dict | None = None,
    phone: str = _DRIVER_PHONE,
) -> dict:
    """Run handler with a P0 body and callback configured. Returns decoded body."""
    if cfg is None:
        cfg = _cfg_with_callback()

    connect_mock = MagicMock()
    connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()
    if call_succeeds:
        connect_mock.start_outbound_voice_contact.return_value = {"ContactId": "call-001"}
    else:
        from botocore.exceptions import ClientError
        connect_mock.start_outbound_voice_contact.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "call failed"}},
            "StartOutboundVoiceContact",
        )

    with (
        patch.object(h, "_verified_p0_decision", return_value=True),
        patch.object(h, "_vehicle_matches_vin", return_value=True),
        patch.object(h, "_caller_assigned_driver", return_value="DRV-1"),
        patch.object(h, "_resolve_driver_phone", return_value=phone),
        patch.object(h, "_load_tenant_connect_config", return_value=cfg),
        patch.object(h, "_get_connect", return_value=connect_mock),
        patch.object(h, "_queue_rsa_action", return_value=None),
    ):
        resp = h.handler(_event(_p0_body()), None)

    assert resp["statusCode"] == 200, f"Expected 200, got {resp['statusCode']}: {resp['body']}"
    return _decode(resp)


# ---------------------------------------------------------------------------
# Tests: p0Callback present when callback path was attempted
# ---------------------------------------------------------------------------

class TestP0CallbackPlaced:
    """p0Callback.placed reflects whether the outbound call was accepted."""

    def test_placed_true_when_call_succeeds(self):
        body = _run_handler(call_succeeds=True)
        assert "p0Callback" in body, "p0Callback must be in the 200 body on callback path"
        assert body["p0Callback"]["placed"] is True, (
            "p0Callback.placed must be True when Connect accepted the call. "
            "Mutation M4 (hardcode False) should make this FAIL."
        )

    def test_placed_false_when_call_fails(self):
        """M4: hardcode placed=True → this FAILS."""
        body = _run_handler(call_succeeds=False)
        assert "p0Callback" in body
        assert body["p0Callback"]["placed"] is False, (
            "p0Callback.placed must be False when the outbound call failed. "
            "Mutation M4 (hardcode True) should make this FAIL."
        )

    def test_placed_is_a_bool(self):
        """placed must be a JSON boolean, not a string."""
        for succeeds in (True, False):
            body = _run_handler(call_succeeds=succeeds)
            val = body["p0Callback"]["placed"]
            assert isinstance(val, bool), (
                f"placed must be bool, got {type(val).__name__!r}"
            )


# ---------------------------------------------------------------------------
# Tests: maskedNumber format and presence
# ---------------------------------------------------------------------------

class TestMaskedNumber:
    """maskedNumber present only when placed=True, correct format, last-4 correct."""

    def test_masked_number_present_when_placed(self):
        body = _run_handler(call_succeeds=True)
        assert "maskedNumber" in body["p0Callback"], (
            "maskedNumber must be in p0Callback when placed=True"
        )

    def test_masked_number_absent_when_not_placed(self):
        """M1: maskedNumber appears when placed=False → this FAILS."""
        body = _run_handler(call_succeeds=False)
        assert "maskedNumber" not in body["p0Callback"], (
            "maskedNumber must NOT be in p0Callback when placed=False. "
            "Mutation M1 should make this FAIL."
        )

    def test_masked_number_format_bullet(self):
        """maskedNumber starts with bullet-bullet-bullet dash pattern."""
        body = _run_handler(call_succeeds=True)
        mn = body["p0Callback"]["maskedNumber"]
        # Should be "•••-•••-XXXX"
        assert mn.startswith("\u2022\u2022\u2022-\u2022\u2022\u2022-"), (
            f"maskedNumber {mn!r} must start with •••-•••-"
        )

    def test_masked_number_last4_correct(self):
        """M5: wrong number of digits (e.g. last-3 or last-5) → this FAILS."""
        body = _run_handler(call_succeeds=True, phone="+15555550199")
        mn = body["p0Callback"]["maskedNumber"]
        suffix = mn.split("-")[-1]
        assert suffix == "0199", (
            f"maskedNumber suffix must be the last 4 digits '0199', got {suffix!r}. "
            "Mutation M5 should make this FAIL."
        )

    @pytest.mark.parametrize("phone,expected_last4", [
        ("+15555550100", "0100"),
        ("+442071234567", "4567"),
        ("+61299999999",  "9999"),
        ("+1-800-555-0101", "0101"),   # phone with dashes (still digits-only extract)
    ])
    def test_last4_across_formats(self, phone: str, expected_last4: str):
        body = _run_handler(call_succeeds=True, phone=phone)
        mn = body["p0Callback"]["maskedNumber"]
        suffix = mn.split("-")[-1]
        assert suffix == expected_last4, (
            f"For phone {phone!r}, last4 must be {expected_last4!r}, got {suffix!r}"
        )


# ---------------------------------------------------------------------------
# Tests: p0Callback absent on non-callback path
# ---------------------------------------------------------------------------

class TestP0CallbackAbsent:
    """p0Callback must not appear when no callback is configured."""

    def test_p0callback_absent_on_non_callback_path(self):
        """M2: p0Callback sent when will_place_call=False → this FAILS."""
        body = _run_handler(call_succeeds=True, cfg=_cfg_no_callback())
        assert "p0Callback" not in body, (
            "p0Callback must be absent when callbackFlowId is not configured. "
            "Mutation M2 should make this FAIL."
        )

    def test_p0callback_absent_on_p1(self):
        """Non-P0 severity: no callback path whatsoever."""
        connect_mock = MagicMock()
        connect_mock.start_chat_contact.return_value = _CONNECT_RESPONSE.copy()

        body = {"severity": "P1", "vin": "1HGBH41JXMN000024",
                "vehicleId": "VEH-001", "reason": "oil low"}
        with (
            patch.object(h, "_load_tenant_connect_config", return_value=_cfg_with_callback()),
            patch.object(h, "_get_connect", return_value=connect_mock),
            patch.object(h, "_queue_rsa_action", return_value=None),
        ):
            resp = h.handler(_event(body), None)

        decoded = _decode(resp)
        assert resp["statusCode"] == 200
        assert "p0Callback" not in decoded, (
            "p0Callback must be absent for non-P0 severity"
        )


# ---------------------------------------------------------------------------
# Tests: full phone number never leaked
# ---------------------------------------------------------------------------

class TestNoPhoneLeak:
    """The full driver phone number must not appear anywhere in the 200 body."""

    def test_full_number_not_in_body(self):
        """M3: maskedNumber = full phone → this FAILS."""
        phone = "+15559876543"
        body = _run_handler(call_succeeds=True, phone=phone)
        body_str = json.dumps(body)
        # The raw phone and the digit sequence both must be absent.
        digits_only = "".join(c for c in phone if c.isdigit())
        assert phone not in body_str, (
            f"Full phone {phone!r} must not appear in the response body. "
            "Mutation M3 should make this FAIL."
        )
        # Also check the E.164 digit string (minus last 4) is absent.
        prefix_digits = digits_only[:-4]
        assert prefix_digits not in body_str, (
            f"Phone prefix digits {prefix_digits!r} must not appear in body. "
            "Mutation M3 should make this FAIL."
        )
