"""G8-AVX-2 tests — DMS executor: rule-decided book_fleet_service → signed path (D7).

A rule-decided ``book_fleet_service`` goes to the internal repair-orders route
(SigV4-signed, no bearer header) even when claims carry a token.

Mutants tested:
  E1: rule path sends the bearer   → test_no_bearer_even_with_token_in_claims catches it
  E2: rule-decided book_recall_service takes the signed path
      → test_rule_book_recall_stays_bearer catches it
"""
from __future__ import annotations

from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

from agents.executors.base import ActionStatus, RetryPolicy
from agents.executors.dms import DmsExecutor

# ---------------------------------------------------------------------------
# Constants shared with test_sim_operator_dms.py
# ---------------------------------------------------------------------------

_INTERNAL_BASE = "https://dmsinternal0.execute-api.us-west-2.amazonaws.com/staging"
_INTERNAL_RO_URL = f"{_INTERNAL_BASE}/api/dms/internal/fleet/repair-orders"
_DMS_BASE = "https://dms-staging.example.com"
_PUBLIC_RO_URL = f"{_DMS_BASE}/api/dms/fleet/repair-orders"
_TOKEN = "op-bearer-token-rebook-tests"

_Creds = namedtuple("_Creds", "access_key secret_key token")
_CREDS = _Creds("TESTEXAMPLEACCESSKEYID", "secret/REBOOK+TEST+KEY", "session-rebook-EXAMPLE")

# A valid rule-decider shape: fleet_id/rule_id (registry._RULE_DECIDER_ID_RE).
_RULE_DECIDER = {"kind": "rule", "id": "fleet-001/rule-rebook-svc-1"}
# A malformed rule id — _is_valid_rule_decider returns False for it.
_BAD_RULE_DECIDER = {"kind": "rule", "id": "/bad-slash-first"}
_OP_DECIDER = {"kind": "operator", "id": "op-sub-rebook-001"}


def _resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    if body is not None:
        r.json.return_value = body
    return r


def _executor(creds=_CREDS) -> DmsExecutor:
    def _creds_fn():
        if isinstance(creds, Exception):
            raise creds
        return creds
    return DmsExecutor(
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
        credentials_fn=_creds_fn,
    )


@pytest.fixture(autouse=True)
def envs(monkeypatch):
    monkeypatch.setenv("VSA_DMS_INTERNAL_API_ENDPOINT", _INTERNAL_BASE)
    monkeypatch.setenv("VSA_DMS_API_ENDPOINT", _DMS_BASE)


def _book_fleet_action(decider=None) -> dict:
    return {
        "action_id": "AVX-ACT-e1000000-0000-4000-8000-000000000001",
        "finding_id": "FIND-e1000000-0000-4000-8000-000000000001",
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "fleet_id": "fleet-001",
        "decided_by": decider if decider is not None else _RULE_DECIDER,
        "parameters": {
            "vehicle_vin": "RBTST000000000001",
            "dealer_id": "DLR-001",
            "needs": [{"labor_op": "03100", "need_id": "NID-001"}],
        },
    }


def _book_recall_action(decider=None) -> dict:
    return {
        "action_id": "AVX-ACT-e2000000-0000-4000-8000-000000000001",
        "finding_id": "FIND-e2000000-0000-4000-8000-000000000001",
        "action_kind": "book_recall_service",
        "target_system": "dms",
        "fleet_id": "fleet-001",
        "decided_by": decider if decider is not None else _RULE_DECIDER,
        "parameters": {
            "vehicle_vin": "RBTST000000000001",
            "dealer_id": "DLR-001",
            "recall_id": "RCL-2026-001",
        },
    }


def _send(action, claims, responses, creds=_CREDS):
    ex = _executor(creds)
    with patch("agents.executors.dms.requests.post", side_effect=list(responses)) as post:
        result = ex.dispatch(action, claims)
    return result, post


# ---------------------------------------------------------------------------
# Rule-decided book_fleet_service → signed internal repair-orders path (E1)
# ---------------------------------------------------------------------------

class TestRuleBookFleetServiceSigned:
    """Rule-decided book_fleet_service posts to the internal route, no bearer (G8-AVX-2 D7)."""

    def test_posts_to_internal_ro_url(self):
        """A rule-decided booking goes to the signed internal repair-orders URL."""
        _result, post = _send(_book_fleet_action(), {}, [_resp(201, {"ro_id": "DMS-RO-10"})])
        assert post.called
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _INTERNAL_RO_URL, f"expected {_INTERNAL_RO_URL!r}, got {url!r}"

    def test_no_authorization_bearer(self):
        """Rule path never sends a bearer header (E1 mutant guard)."""
        _result, post = _send(_book_fleet_action(), {}, [_resp(201, {"ro_id": "DMS-RO-10"})])
        headers = post.call_args[1].get("headers") or {}
        auth = headers.get("Authorization", "")
        assert not auth.startswith("Bearer "), f"bearer must not appear; got {auth!r}"

    def test_no_bearer_even_with_token_in_claims(self):
        """Token in claims must not appear on the signed path (E1 mutant guard — direct check)."""
        _result, post = _send(_book_fleet_action(), {"token": _TOKEN},
                              [_resp(201, {"ro_id": "DMS-RO-10"})])
        headers = post.call_args[1].get("headers") or {}
        auth = headers.get("Authorization", "")
        assert not auth.startswith("Bearer "), (
            f"claims token must not appear on signed path; got {auth!r}"
        )

    def test_2xx_returns_in_progress(self):
        result, _ = _send(_book_fleet_action(), {}, [_resp(201, {"ro_id": "DMS-RO-10"})])
        assert result.status == ActionStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Operator-decided book_fleet_service keeps bearer path (regression)
# ---------------------------------------------------------------------------

class TestOperatorBookFleetServiceBearer:
    """Operator-decided booking still uses the bearer path (operator unchanged)."""

    def test_operator_uses_public_url(self):
        _result, post = _send(_book_fleet_action(_OP_DECIDER), {"token": _TOKEN},
                              [_resp(201, {"ro_id": "DMS-RO-11"})])
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _PUBLIC_RO_URL, f"expected {_PUBLIC_RO_URL!r}, got {url!r}"

    def test_operator_sends_bearer(self):
        _result, post = _send(_book_fleet_action(_OP_DECIDER), {"token": _TOKEN},
                              [_resp(201, {"ro_id": "DMS-RO-11"})])
        headers = post.call_args[1].get("headers") or {}
        assert headers.get("Authorization") == f"Bearer {_TOKEN}"


# ---------------------------------------------------------------------------
# Malformed rule id does not take the signed path (E1 variant)
# ---------------------------------------------------------------------------

class TestMalformedRuleDeciderStaysBearer:
    """A decided_by whose _is_valid_rule_decider returns False does not take the signed path."""

    def test_bad_rule_id_uses_bearer_path(self):
        """Malformed rule id falls through to the bearer path (missing token → terminal error,
        but we confirm it did not post to the signed internal route)."""
        result, post = _send(_book_fleet_action(_BAD_RULE_DECIDER), {}, [])
        # No token in claims → terminal error (MissingJwt), nothing posted.
        assert result.status == ActionStatus.FAILED
        assert not post.called, (
            "a malformed rule id must not reach the internal signed route"
        )

    def test_bad_rule_id_with_token_uses_public_url(self):
        """With a token, malformed rule id goes to the public (bearer) URL, not the signed one."""
        _result, post = _send(_book_fleet_action(_BAD_RULE_DECIDER), {"token": _TOKEN},
                              [_resp(201, {"ro_id": "DMS-RO-12"})])
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _PUBLIC_RO_URL, (
            f"malformed rule id must use public URL; got {url!r}"
        )


# ---------------------------------------------------------------------------
# Rule-decided book_recall_service does NOT take the signed path (E2 mutant guard)
# ---------------------------------------------------------------------------

class TestRuleBookRecallServiceStaysBearer:
    """A rule-decided book_recall_service must NOT take the signed path (spec D7: never)."""

    def test_rule_book_recall_stays_bearer(self):
        """Rule-decided book_recall_service with token → public repair-orders URL (E2 guard)."""
        _result, post = _send(_book_recall_action(_RULE_DECIDER), {"token": _TOKEN},
                              [_resp(201, {"ro_id": "DMS-RO-13"})])
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _PUBLIC_RO_URL, (
            f"rule-decided recall must use public URL, not internal; got {url!r}"
        )

    def test_rule_book_recall_sends_bearer(self):
        _result, post = _send(_book_recall_action(_RULE_DECIDER), {"token": _TOKEN},
                              [_resp(201, {"ro_id": "DMS-RO-13"})])
        headers = post.call_args[1].get("headers") or {}
        assert headers.get("Authorization") == f"Bearer {_TOKEN}"
