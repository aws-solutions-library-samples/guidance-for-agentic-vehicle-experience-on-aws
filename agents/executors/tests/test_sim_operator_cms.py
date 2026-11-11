"""11.1a RED tests — CMS executor: simulated ground_vehicle goes signed, never with a bearer.

D10 Decision 4 / D10e: the executor routes decided_by.kind == "simulated_fleet_operator"
to the IAM-signed internal groundings route BEFORE the token guard.  Even when claims carry
a token, no Authorization header is sent (M4/M5).  Operator decisions keep the existing
bearer-token path.  Redirects are never followed on either path.
"""
from __future__ import annotations

import os
from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

from agents.executors.base import ActionStatus, RetryPolicy
from agents.executors.cms import CmsExecutor
from agents.executors.simulated_operator import DECIDER_ID, DECIDER_KIND, DEMO_FLEET_IDS

_DEMO_FLEET = next(iter(sorted(DEMO_FLEET_IDS)))
_INTERNAL_BASE = "https://xyzinternal0.execute-api.us-west-2.amazonaws.com/staging"
_INTERNAL_GROUNDINGS_URL = f"{_INTERNAL_BASE}/api/v1/fleet-intelligence/internal/groundings"
_CMS_BASE = "https://cms-staging.example.com"
_PUBLIC_GROUNDINGS_URL = f"{_CMS_BASE}/api/v1/fleet-intelligence/groundings"
_TOKEN = "op-bearer-token-for-testing"

_Creds = namedtuple("_Creds", "access_key secret_key token")
_CREDS = _Creds("TESTEXAMPLEACCESSKEY", "secret/TEST+KEY+VALUE", "session-tok-EXAMPLE")

_SIM_GROUND = {
    "action_id": "AVX-ACT-d1000000-0000-4000-8000-000000000001",
    "finding_id": "FIND-d1000000-0000-4000-8000-000000000001",
    "action_kind": "ground_vehicle",
    "target_system": "cms",
    "fleet_id": _DEMO_FLEET,
    "decided_by": {"kind": DECIDER_KIND, "id": DECIDER_ID},
    "parameters": {"vehicle_vin": "MRDVEH00000000031", "fault_codes": ["P0100"]},
}

_OP_GROUND = {
    **_SIM_GROUND,
    "decided_by": {"kind": "operator", "id": "op-sub-0001"},
}


def _resp(status: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    return r


def _executor(creds=_CREDS) -> CmsExecutor:
    calls: list = []

    def _creds():
        calls.append(1)
        if isinstance(creds, Exception):
            raise creds
        return creds

    executor = CmsExecutor(
        sleep_fn=lambda _: None,
        credentials_fn=_creds,
        retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
    )
    executor._creds_call_count = calls
    return executor


@pytest.fixture(autouse=True)
def envs(monkeypatch):
    monkeypatch.setenv("VSA_CMS_FI_INTERNAL_API_ENDPOINT", _INTERNAL_BASE)
    monkeypatch.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)


def _dispatch(action, claims, responses):
    ex = _executor()
    with patch("agents.executors.cms.requests.post", side_effect=list(responses)) as post:
        result = ex.dispatch(action, claims)
    return result, post


# ---------------------------------------------------------------------------
# Simulated decider → internal signed route
# ---------------------------------------------------------------------------

class TestSimulatedGroundVehicleSignedRoute:
    """Simulated ground_vehicle goes to the internal route, signed, no bearer."""

    def test_posts_to_internal_groundings_url(self):
        """RED: executor does not yet have the signed path for ground_vehicle."""
        _result, post = _dispatch(_SIM_GROUND, {}, [_resp(200)])
        assert post.called, "executor must POST to the internal route"
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _INTERNAL_GROUNDINGS_URL, (
            f"expected {_INTERNAL_GROUNDINGS_URL!r}, got {url!r}"
        )

    def test_no_authorization_bearer_header(self):
        """M5: the signed path must never send a bearer token."""
        _result, post = _dispatch(_SIM_GROUND, {}, [_resp(200)])
        headers = (
            post.call_args[1].get("headers") or
            (post.call_args[0][1] if len(post.call_args[0]) > 1 else {})
        )
        auth = (headers or {}).get("Authorization", "")
        assert not auth.startswith("Bearer "), (
            f"signed path must not carry a bearer token; got Authorization={auth!r}"
        )

    def test_no_bearer_even_when_claims_carry_token(self):
        """M4: the signed path bypasses the token guard; claims token is never forwarded."""
        claims_with_token = {"token": _TOKEN, "sub": "op-sub-for-claims"}
        _result, post = _dispatch(_SIM_GROUND, claims_with_token, [_resp(200)])
        headers = post.call_args[1].get("headers") or {}
        auth = headers.get("Authorization", "")
        assert not auth.startswith("Bearer "), (
            f"a bearer token from claims must not appear on the signed path; "
            f"got Authorization={auth!r}"
        )

    def test_2xx_returns_executed(self):
        result, _post = _dispatch(_SIM_GROUND, {}, [_resp(200)])
        assert result.status == ActionStatus.EXECUTED

    def test_redirect_on_signed_path_is_terminal_error(self):
        """Redirects on a POST that carries signed credentials are never followed."""
        result, _post = _dispatch(_SIM_GROUND, {}, [_resp(302)])
        assert result.status == ActionStatus.FAILED
        assert result.last_error_class == "UnexpectedRedirect"


# ---------------------------------------------------------------------------
# Operator decider → public bearer-token path (regression guard)
# ---------------------------------------------------------------------------

class TestOperatorGroundVehicleTokenPath:
    """Operator decisions keep the existing bearer-token path."""

    def test_posts_to_public_groundings_url(self):
        _result, post = _dispatch(_OP_GROUND, {"token": _TOKEN}, [_resp(200)])
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _PUBLIC_GROUNDINGS_URL, (
            f"operator ground_vehicle must use the public route; got {url!r}"
        )

    def test_bearer_header_is_present(self):
        _result, post = _dispatch(_OP_GROUND, {"token": _TOKEN}, [_resp(200)])
        headers = post.call_args[1].get("headers") or {}
        assert headers.get("Authorization") == f"Bearer {_TOKEN}", (
            "operator path must send the bearer token"
        )

    def test_missing_token_is_terminal_error(self):
        """No token and no simulated decider → MissingJwt (existing behaviour)."""
        result, _post = _dispatch(_OP_GROUND, {}, [])
        assert result.status == ActionStatus.FAILED
        assert result.last_error_class == "MissingJwt"
