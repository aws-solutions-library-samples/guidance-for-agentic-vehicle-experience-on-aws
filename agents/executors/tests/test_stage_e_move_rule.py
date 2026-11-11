"""Rule-approved moves go to CMS's IAM-signed internal route (stage E task 8.3, U6).

Spec group8-notes G8-D8: ``decided_by.kind == "rule"`` → SigV4 (``execute-api``) to
``VSA_CMS_FI_INTERNAL_API_ENDPOINT/api/v1/fleet-intelligence/internal/moves``, never with a
bearer token; every other decider keeps the operator token path, never signed.

``requests.post`` is patched to record what would go on the wire; signing runs for real
(botocore ``SigV4Auth``) with fixed test credentials, so the Authorization header checked
here is the one AWS would verify.
"""
from __future__ import annotations

import json
import os
from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

from agents.executors.base import ActionStatus, RetryPolicy
from agents.executors.cms import CmsExecutor

_INTERNAL_BASE = "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod"
_INTERNAL_URL = f"{_INTERNAL_BASE}/api/v1/fleet-intelligence/internal/moves"
_CMS_BASE = "https://cms-staging.test"
_OPERATOR_TOKEN = "op-jwt-stage-e-0001"

_Creds = namedtuple("_Creds", "access_key secret_key token")
_CREDS = _Creds("TESTEXAMPLEACCESSKEY", "secret/EXAMPLE+KEY", "session-token-EXAMPLE")

_RULE_ACTION = {
    "action_id": "AVX-ACT-e8000000-0000-4000-8000-000000000001",
    "finding_id": "FIND-e8000000-0000-4000-8000-000000000001",
    "action_kind": "move_vehicles",
    "target_system": "cms",
    "decision": "auto_approved",
    "decided_by": {"kind": "rule", "id": "fleet-meridian-01/small-moves"},
    "parameters": {
        "fleet_id": "fleet-meridian-01",
        "from_location_id": "loc-sea-downtown-02",
        "to_location_id": "loc-sea-airport-01",
        "vehicle_vins": ["MRDVEH00000000021", "MRDVEH00000000022"],
        "forecast_week": "2026-W42",
        "transfer_cost_usd": 84.06,
        "predicted_on_rent_hours_gain": 35.0,
        "predicted_turnaways_avoided": 2,
    },
}
_OPERATOR_ACTION = dict(_RULE_ACTION, decision="approved",
                        decided_by={"kind": "operator", "id": "op-sub-1"})


def _resp(status: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    return r


def _executor(creds_calls: list | None = None, creds=_CREDS) -> CmsExecutor:
    def _creds():
        if creds_calls is not None:
            creds_calls.append(1)
        if isinstance(creds, Exception):
            raise creds
        return creds
    return CmsExecutor(sleep_fn=lambda _: None, credentials_fn=_creds,
                       retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("VSA_CMS_FI_INTERNAL_API_ENDPOINT", _INTERNAL_BASE)
    monkeypatch.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)


def _send(executor, action, claims, responses):
    with patch("agents.executors.cms.requests.post", side_effect=list(responses)) as post:
        result = executor.dispatch(action, claims)
    return result, post


# ---------------------------------------------------------------------------
# The rule path is signed, carries no token, and posts exactly what it signed
# ---------------------------------------------------------------------------

def test_a_rule_approved_move_is_signed_and_posted_to_the_internal_route(env):
    result, post = _send(_executor(), _RULE_ACTION, {}, [_resp(200)])
    assert result.status == ActionStatus.EXECUTED
    assert result.target_ref == _RULE_ACTION["action_id"]
    (call,) = post.call_args_list
    assert call.args == (_INTERNAL_URL,)
    assert call.kwargs["allow_redirects"] is False
    assert "json" not in call.kwargs
    headers = call.kwargs["headers"]
    auth = headers["Authorization"]
    assert auth.startswith(f"AWS4-HMAC-SHA256 Credential={_CREDS.access_key}/")
    assert "/us-west-2/execute-api/aws4_request" in auth
    signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
    assert {"host", "x-amz-date", "content-type", "x-amz-security-token"} <= set(signed)
    assert headers["X-Amz-Security-Token"] == _CREDS.token
    assert headers["Content-Type"] == "application/json"
    # Exactly the signer's headers and the content type: nothing else rides along, and no
    # bearer credential of any kind (G8-D8).
    assert set(headers) == {"Authorization", "X-Amz-Date", "X-Amz-Security-Token", "Content-Type"}
    assert "bearer" not in json.dumps(headers).lower()
    body = json.loads(call.kwargs["data"])
    assert body["actionId"] == _RULE_ACTION["action_id"]
    assert body["vehicle_vins"] == _RULE_ACTION["parameters"]["vehicle_vins"]


def test_the_signature_covers_exactly_the_bytes_posted(env):
    """Re-sign the posted bytes at the request's own timestamp and compare signatures."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    _, post = _send(_executor(), _RULE_ACTION, {}, [_resp(200)])
    (call,) = post.call_args_list
    sent = call.kwargs["headers"]
    req = AWSRequest(method="POST", url=_INTERNAL_URL, data=call.kwargs["data"],
                     headers={"Content-Type": "application/json"})
    req.context["timestamp"] = sent["X-Amz-Date"]
    with patch("botocore.auth.datetime") as dt:
        import datetime as _d
        dt.datetime.now.return_value = _d.datetime.strptime(sent["X-Amz-Date"], "%Y%m%dT%H%M%SZ")
        dt.datetime.utcnow.return_value = dt.datetime.now.return_value
        SigV4Auth(_CREDS, "execute-api", "us-west-2").add_auth(req)
    assert req.headers["Authorization"] == sent["Authorization"]


def test_a_token_in_the_claims_is_never_sent_on_the_rule_path(env):
    claims = {"token": _OPERATOR_TOKEN, "id_token": _OPERATOR_TOKEN}
    _, post = _send(_executor(), _RULE_ACTION, claims, [_resp(200)])
    (call,) = post.call_args_list
    assert _OPERATOR_TOKEN not in json.dumps(call.kwargs["headers"])
    assert _OPERATOR_TOKEN.encode() not in call.kwargs["data"]
    assert call.args == (_INTERNAL_URL,)


def test_the_region_signed_for_is_the_endpoints(env, monkeypatch):
    monkeypatch.setenv("VSA_CMS_FI_INTERNAL_API_ENDPOINT",
                       "https://yourapi000.execute-api.eu-west-1.amazonaws.com/prod")
    _, post = _send(_executor(), _RULE_ACTION, {}, [_resp(200)])
    assert "/eu-west-1/execute-api/aws4_request" in post.call_args.kwargs["headers"]["Authorization"]


def test_each_retry_is_signed_again(env):
    with patch("agents.executors.cms._sigv4_headers", wraps=__import__(
            "agents.executors.cms", fromlist=["_sigv4_headers"])._sigv4_headers) as sign:
        result, post = _send(_executor(), _RULE_ACTION, {}, [_resp(503), _resp(200)])
    assert result.status == ActionStatus.EXECUTED
    assert post.call_count == 2
    assert sign.call_count == 2


# ---------------------------------------------------------------------------
# Responses map as on the operator path
# ---------------------------------------------------------------------------

def test_a_cms_409_is_refused(env):
    result, post = _send(_executor(), _RULE_ACTION, {}, [_resp(409)])
    assert result.status == ActionStatus.DECLINED
    assert result.last_error_status == 409
    assert post.call_count == 1


def test_a_cms_403_is_terminal_not_retried(env):
    result, post = _send(_executor(), _RULE_ACTION, {}, [_resp(403)])
    assert result.status == ActionStatus.FAILED
    assert post.call_count == 1


def test_5xx_until_the_budget_runs_out_fails(env):
    result, post = _send(_executor(), _RULE_ACTION, {}, [_resp(503), _resp(502)])
    assert result.status == ActionStatus.FAILED
    assert post.call_count == 2


# ---------------------------------------------------------------------------
# Nothing is sent when the endpoint or the credentials are wrong
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", [
    "",
    "http://yourapi000.execute-api.us-west-2.amazonaws.com/prod",
    "https://cms-internal.example.com/prod",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com.evil.test/prod",
    "https://user:pw@yourapi000.execute-api.us-west-2.amazonaws.com/prod",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com:8443/prod",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod?x=1",
    # Review c2 W1: values the deploy-time grant can't match (no stage, an empty stage,
    # a second path segment, surrounding whitespace the grant would keep).
    "https://yourapi000.execute-api.us-west-2.amazonaws.com",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com//",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/extra",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod ",
    " https://yourapi000.execute-api.us-west-2.amazonaws.com/prod",
    # Review c3 S1/S2: shapes urlsplit normalises or drops (empty query, fragment, userinfo or
    # port; upper case), a doubled slash, and a pasted CDK output line.
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod//",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod?",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod#",
    "https://@yourapi000.execute-api.us-west-2.amazonaws.com/prod/",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com:/prod/",
    "HTTPS://yourapi000.execute-api.us-west-2.amazonaws.com/prod/",
    "https://YOURAPI000.execute-api.us-west-2.amazonaws.com/prod/",
    "cms-staging-ui.FiInternalApiEndpoint = https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/",
])
def test_a_bad_internal_endpoint_sends_nothing(env, monkeypatch, endpoint):
    monkeypatch.setenv("VSA_CMS_FI_INTERNAL_API_ENDPOINT", endpoint)
    calls: list = []
    result, post = _send(_executor(creds_calls=calls), _RULE_ACTION, {}, [_resp(200)])
    assert result.status == ActionStatus.FAILED
    assert post.call_count == 0
    assert calls == []


def test_cms_s_output_with_its_trailing_slash_posts_to_the_route(env, monkeypatch):
    """CMS's FiInternalApiEndpoint is RestApi.url: ``.../prod/``."""
    monkeypatch.setenv("VSA_CMS_FI_INTERNAL_API_ENDPOINT", f"{_INTERNAL_BASE}/")
    result, post = _send(_executor(), _RULE_ACTION, {}, [_resp(200)])
    assert result.status == ActionStatus.EXECUTED
    assert post.call_args.args == (_INTERNAL_URL,)


def test_missing_credentials_send_nothing(env):
    result, post = _send(_executor(creds=ValueError("MissingAwsCredentials")),
                         _RULE_ACTION, {}, [_resp(200)])
    assert result.status == ActionStatus.FAILED
    assert post.call_count == 0


def test_an_incomplete_move_sends_nothing(env):
    action = dict(_RULE_ACTION, parameters=dict(_RULE_ACTION["parameters"], vehicle_vins=[]))
    result, post = _send(_executor(), action, {}, [_resp(200)])
    assert result.status == ActionStatus.FAILED
    assert post.call_count == 0


# ---------------------------------------------------------------------------
# The paths don't cross
# ---------------------------------------------------------------------------

def test_an_operator_approved_move_keeps_the_token_path_and_is_never_signed(env):
    calls: list = []
    with patch("agents.executors.cms.CmsExecutor._get_cms_endpoint", return_value=_CMS_BASE):
        result, post = _send(_executor(creds_calls=calls), _OPERATOR_ACTION,
                             {"token": _OPERATOR_TOKEN}, [_resp(200)])
    assert result.status == ActionStatus.EXECUTED
    (call,) = post.call_args_list
    assert call.args == (f"{_CMS_BASE}/api/v1/fleet-intelligence/moves",)
    assert call.kwargs["headers"] == {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}
    assert calls == []


@pytest.mark.parametrize("decided_by", [
    {"kind": "operator", "id": "op-sub-1"}, {"kind": "user", "id": "u"}, None, "rule",
])
def test_a_non_rule_decider_without_a_token_sends_nothing(env, decided_by):
    action = dict(_RULE_ACTION, decided_by=decided_by)
    calls: list = []
    result, post = _send(_executor(creds_calls=calls), action, {}, [_resp(200)])
    assert result.status == ActionStatus.FAILED
    assert result.last_error_class == "MissingJwt"
    assert post.call_count == 0
    assert calls == []


def test_another_kind_decided_by_a_rule_never_takes_the_signed_path(env):
    action = dict(_RULE_ACTION, action_kind="apply_cost_action",
                  parameters={"vehicle_vin": "MRDVEH00000000021", "cost_action": "driver_coaching",
                              "month": "2026-09", "cause": "efficiency"})
    calls: list = []
    result, post = _send(_executor(creds_calls=calls), action, {}, [_resp(200)])
    assert result.last_error_class == "MissingJwt"
    assert post.call_count == 0
    assert calls == []


def test_registry_dispatch_sends_a_rule_decided_move_signed(env):
    """The auto-approver's own call: registry.dispatch(action, {})."""
    from agents.executors import registry

    with patch.dict(registry.EXECUTORS, {"cms": _executor()}):
        with patch("agents.executors.cms.requests.post", side_effect=[_resp(200)]) as post:
            result = registry.dispatch(_RULE_ACTION, {}, fire_escalation=False)
    assert result.status == ActionStatus.EXECUTED
    assert post.call_args.args == (_INTERNAL_URL,)
    assert post.call_args.kwargs["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")
