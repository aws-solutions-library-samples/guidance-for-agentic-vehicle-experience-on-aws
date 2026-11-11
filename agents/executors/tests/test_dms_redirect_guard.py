"""W5: allow_redirects=False and 3xx → UnexpectedRedirect for DMS signed POSTs.

Both ``_signed_ro_post`` (used by book_fleet_service and book_recall_service with a
simulated decider) and ``_dispatch_warranty_claim_signed`` must:
  - pass ``allow_redirects=False`` to requests.post
  - map any 3xx to a terminal error with last_error_class="UnexpectedRedirect"

R1 mutant (redirects followed again): removing ``allow_redirects=False`` causes
``requests`` to follow the redirect, the Location URL is unregistered, and
``responses`` raises a ConnectionError → the test gets ``exhausted`` rather than
``terminal_error(UnexpectedRedirect)``.

Additionally, one signed request verifies that:
  - ``Authorization`` starts with ``AWS4-HMAC-SHA256`` (SigV4, not Bearer)
  - the request body equals the bytes that were signed
"""
from __future__ import annotations

import json
from collections import namedtuple

import pytest
import responses as rsps  # type: ignore[import]

from agents.executors.base import ActionStatus, FailureKind, RetryPolicy
from agents.executors.dms import DmsExecutor, _fleet_service_body

_INTERNAL_BASE = "https://dmsinternal0.execute-api.us-west-2.amazonaws.com/staging"
_INTERNAL_RO_URL = f"{_INTERNAL_BASE}/api/dms/internal/fleet/repair-orders"
_INTERNAL_WC_URL = f"{_INTERNAL_BASE}/api/dms/internal/fleet/warranty-claims"

_Creds = namedtuple("_Creds", "access_key secret_key token")
_CREDS = _Creds("TESTEXAMPLEACCESSKEYID", "secret/REDIRECT+TEST+KEY", "session-redirect-EXAMPLE")

_FAST_POLICY = RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0)


def _executor() -> DmsExecutor:
    def _creds_fn():
        return _CREDS

    return DmsExecutor(
        sleep_fn=lambda _: None,
        retry_policy=_FAST_POLICY,
        credentials_fn=_creds_fn,
    )


@pytest.fixture(autouse=True)
def envs(monkeypatch):
    monkeypatch.setenv("VSA_DMS_INTERNAL_API_ENDPOINT", _INTERNAL_BASE)
    monkeypatch.setenv("VSA_DMS_API_ENDPOINT", "https://dms-staging.example.com")


def _fleet_service_action() -> dict:
    return {
        "action_id": "AVX-ACT-aa000000-0000-4000-8000-000000000001",
        "finding_id": "FIND-aa000000-0000-4000-8000-000000000001",
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "fleet_id": "fleet-redirect-test",
        "decided_by": {"kind": "simulated_fleet_operator", "id": "simulated-fleet-operator"},
        "parameters": {
            "vehicle_vin": "REDIR000000000001",
            "dealer_id": "DLR-REDIR-001",
            "needs": [{"labor_op": "09000", "need_id": "NID-REDIR-001"}],
        },
    }


def _warranty_claim_action() -> dict:
    return {
        "action_id": "AVX-ACT-bb000000-0000-4000-8000-000000000002",
        "finding_id": "FIND-bb000000-0000-4000-8000-000000000002",
        "action_kind": "submit_warranty_claim",
        "target_system": "dms",
        "fleet_id": "fleet-redirect-test",
        "decided_by": {"kind": "simulated_fleet_operator", "id": "simulated-fleet-operator"},
        "parameters": {
            "vehicle_vin": "REDIR000000000002",
            "dealer_id": "DLR-REDIR-002",
            "labor_op": "WARRANTY-01",
            "line_no": "1",
            "ro_id": "RO-REDIR-002",
        },
    }


# ---------------------------------------------------------------------------
# _signed_ro_post (book_fleet_service simulated path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("redirect_code", [301, 302, 307, 308])
@rsps.activate
def test_signed_ro_post_redirect_is_terminal_no_follow(redirect_code: int) -> None:
    """A 3xx on a signed repair-orders POST is terminal UnexpectedRedirect, no retry.

    R1 mutant: removing allow_redirects=False causes requests to follow the Location
    URL, which is not registered; responses raises ConnectionError → exhausted, not
    terminal_error(UnexpectedRedirect).
    """
    rsps.add(
        rsps.POST, _INTERNAL_RO_URL,
        status=redirect_code,
        headers={"Location": "https://dms-new.test/api/dms/internal/fleet/repair-orders"},
    )

    result = _executor().dispatch(_fleet_service_action(), {})

    assert len(rsps.calls) == 1, (
        f"Expected exactly 1 POST for {redirect_code}; got {len(rsps.calls)}"
    )
    assert result.status == ActionStatus.FAILED, (
        f"Expected FAILED for 3xx redirect, got {result.status!r}"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, (
        f"Expected EXECUTOR_TERMINAL_ERROR, got {result.failure_kind!r}"
    )
    assert result.last_error_class == "UnexpectedRedirect", (
        f"Expected UnexpectedRedirect, got {result.last_error_class!r}"
    )
    assert result.last_error_status == redirect_code


@rsps.activate
def test_signed_ro_post_uses_sigv4_and_posts_signed_bytes() -> None:
    """Authorization starts with AWS4-HMAC-SHA256; the posted body equals the signed bytes.

    Guards that the code doesn't change the payload between signing and sending.
    Uses a 302 (terminal) so the request completes and headers are inspectable.
    """
    rsps.add(
        rsps.POST, _INTERNAL_RO_URL,
        status=302,
        headers={"Location": "https://dms-new.test/"},
    )
    action = _fleet_service_action()

    _executor().dispatch(action, {})

    req = rsps.calls[0].request
    auth = req.headers.get("Authorization", "")
    assert auth.startswith("AWS4-HMAC-SHA256"), (
        f"Expected SigV4 Authorization, got: {auth[:60]!r}"
    )
    # The signed bytes are json.dumps(body, separators=(',', ':')).encode('utf-8').
    expected_payload = json.dumps(_fleet_service_body(action), separators=(",", ":")).encode()
    assert req.body == expected_payload, (
        "Posted bytes differ from the bytes that were signed"
    )


# ---------------------------------------------------------------------------
# _dispatch_warranty_claim_signed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("redirect_code", [301, 302, 307, 308])
@rsps.activate
def test_warranty_claim_signed_redirect_is_terminal_no_follow(redirect_code: int) -> None:
    """A 3xx on a signed warranty-claims POST is terminal UnexpectedRedirect, no retry."""
    rsps.add(
        rsps.POST, _INTERNAL_WC_URL,
        status=redirect_code,
        headers={"Location": "https://dms-new.test/api/dms/internal/fleet/warranty-claims"},
    )

    result = _executor().dispatch(_warranty_claim_action(), {})

    assert len(rsps.calls) == 1, (
        f"Expected exactly 1 POST for {redirect_code}; got {len(rsps.calls)}"
    )
    assert result.status == ActionStatus.FAILED, (
        f"Expected FAILED for 3xx redirect on warranty-claim, got {result.status!r}"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, (
        f"Expected EXECUTOR_TERMINAL_ERROR, got {result.failure_kind!r}"
    )
    assert result.last_error_class == "UnexpectedRedirect", (
        f"Expected UnexpectedRedirect, got {result.last_error_class!r}"
    )
    assert result.last_error_status == redirect_code
