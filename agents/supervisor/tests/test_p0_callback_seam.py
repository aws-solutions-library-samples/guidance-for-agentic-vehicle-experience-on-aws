"""The P0 callback notice across the real seam (review 2026-11-06 cycle 1 W4, task F1.5).

Spec 2026-11-05-avx-proactive-p0-alert § CB: the app shows "Meridian fleet support is
calling you now" only when api-escalate actually placed the P0 callback.

``test_callback_ringing_notice.py`` patches ``call_escalate_endpoint`` with a return
value that already holds ``p0Callback``, so dropping the field in the real function
shipped green (review mutant G). Here nothing between the Lambda and the wire event is
stubbed except the HTTP transport:

  real api-escalate ``handler`` (Connect and lookups mocked, as in
  ``lambdas/api-escalate/tests/test_p0callback_body.py``) → its real response body →
  ``requests.post`` stub → real ``call_escalate_endpoint`` → real
  ``_run_escalate_in_background`` → the events sent on the WebSocket.

502 rule (decided 2026-11-06, ``decisions.md`` F1.5): a 502 whose body says
``callbackInitiated: true`` (the call was placed, the chat failed) sends NO ringing
notice. The driver is told by the failed ``escalation`` event, which carries
``callbackInitiated`` and the user-approved message "… a support agent is calling your
phone now." (2026-10-05). Only the 200 body's ``p0Callback.placed`` drives the notice.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import agents.supervisor.bidi_app as bidi
import agents.supervisor.tools.escalate as escalate_mod
from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

_REPO = Path(__file__).resolve().parents[3]
_ESCALATE_DIR = _REPO / "lambdas" / "api-escalate"
_DRIVER_PHONE = "+15555550199"
_MASKED = "\u2022\u2022\u2022-\u2022\u2022\u2022-0199"


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("TENANT_CONFIG_TABLE", "test-tenant-config")
    monkeypatch.setenv("CMS_STAGE", "test")
    monkeypatch.setenv("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
    monkeypatch.setenv("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")
    for p in (str(_ESCALATE_DIR), str(_ESCALATE_DIR.parent)):
        if p not in sys.path:
            monkeypatch.syspath_prepend(p)
    name = "escalate_handler_p0_seam"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, _ESCALATE_DIR / "handler.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


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


def _lambda_response(h, *, call_succeeds: bool, chat_succeeds: bool = True) -> dict:
    """The real api-escalate handler's response for a P0 with a callback armed."""
    connect = MagicMock()
    if chat_succeeds:
        connect.start_chat_contact.return_value = {
            "ContactId": "chat-contact-seam-001",
            "ParticipantId": "participant-seam-001",
            "ParticipantToken": "token-seam-abc",
        }
    else:
        connect.start_chat_contact.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "chat failed"}},
            "StartChatContact",
        )
    if call_succeeds:
        connect.start_outbound_voice_contact.return_value = {"ContactId": "call-seam-001"}
    else:
        connect.start_outbound_voice_contact.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "call failed"}},
            "StartOutboundVoiceContact",
        )
    body = {
        "severity": "P0",
        "vin": "1HGBH41JXMN000024",
        "vehicleId": "VEH-0099",
        "reason": "brake failure",
        "triageSessionId": "sess-p0",
        "driverId": "DRV-1",
    }
    event = {
        "requestContext": {"authorizer": {"claims": {"custom:tenantId": "t1"}}},
        "body": json.dumps(body),
    }
    with (
        patch.object(h, "_verified_p0_decision", return_value=True),
        patch.object(h, "_vehicle_matches_vin", return_value=True),
        patch.object(h, "_caller_assigned_driver", return_value="DRV-1"),
        patch.object(h, "_resolve_driver_phone", return_value=_DRIVER_PHONE),
        patch.object(h, "_load_tenant_connect_config", return_value=_cfg_with_callback()),
        patch.object(h, "_get_connect", return_value=connect),
        patch.object(h, "_queue_rsa_action", return_value=None),
    ):
        return h.handler(event, None)


class _Resp:
    """The parts of ``requests.Response`` that ``call_escalate_endpoint`` reads."""

    def __init__(self, lambda_resp: dict):
        self.status_code = lambda_resp["statusCode"]
        self.text = lambda_resp["body"]

    def json(self):
        return json.loads(self.text)


def _wire_events(lambda_resp: dict) -> list[dict]:
    config = Config.from_env()
    ctx = SessionContext(
        tenant_id=config.tenant_id,
        session_id="cb-seam-001",
        jwt="fake-jwt",
        persona_id="fleet_driver",
        vin="1HGBH41JXMN000024",
        vehicle_id="VEH-0099",
    )
    ctx.triage_classification = "P0"
    ws = AsyncMock()

    async def _go():
        with patch.object(escalate_mod.requests, "post", return_value=_Resp(lambda_resp)) as post:
            await bidi._run_escalate_in_background(
                websocket=ws,
                ctx=ctx,
                config=config,
                user_utterance="brake failure",
                booking_result=None,
                classification="P0",
            )
            assert post.call_count == 1, "the real call_escalate_endpoint must have run"

    asyncio.run(_go())
    return [c.args[0] for c in ws.send_json.call_args_list]


def _ringing(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "platform.callback.ringing"]


def test_placed_call_reaches_the_ringing_notice_with_the_masked_number(handler):
    resp = _lambda_response(handler, call_succeeds=True)
    assert resp["statusCode"] == 200
    events = _wire_events(resp)
    ringing = _ringing(events)
    assert ringing == [
        {
            "type": "platform.callback.ringing",
            "callerName": "Meridian fleet support",
            "maskedNumber": _MASKED,
        }
    ]
    # Before the escalation event, and the full number appears nowhere on the wire.
    types = [e.get("type") for e in events]
    assert types.index("platform.callback.ringing") < types.index("escalation")
    assert "5555550199" not in json.dumps(events)


def test_failed_call_sends_no_ringing_notice(handler):
    resp = _lambda_response(handler, call_succeeds=False)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["p0Callback"]["placed"] is False
    events = _wire_events(resp)
    assert _ringing(events) == []
    assert any(e.get("type") == "escalation" and e.get("status") == "initiated" for e in events)


def test_502_with_the_call_placed_sends_no_ringing_but_says_the_phone_is_ringing(handler):
    resp = _lambda_response(handler, call_succeeds=True, chat_succeeds=False)
    assert resp["statusCode"] == 502
    assert json.loads(resp["body"])["callbackInitiated"] is True
    events = _wire_events(resp)
    assert _ringing(events) == []
    failed = [e for e in events if e.get("type") == "escalation"]
    assert len(failed) == 1
    assert failed[0]["status"] == "failed"
    assert failed[0]["callbackInitiated"] is True
    assert "a support agent is calling your phone now" in failed[0]["message"]
