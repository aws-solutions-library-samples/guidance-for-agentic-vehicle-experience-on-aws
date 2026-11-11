"""11.1a RED tests — DMS executor: simulated decider goes signed, no bearer (D10 D10e).

D10 Decision 4: simulated book_fleet_service, book_recall_service and submit_warranty_claim
go to the internal routes (IAM SigV4), with no Authorization: Bearer header even when claims
carry a token.  Operator decisions keep the existing bearer-token path.

Internal routes:
  POST {VSA_DMS_INTERNAL_API_ENDPOINT}/api/dms/internal/fleet/repair-orders   (fleet/recall)
  POST {VSA_DMS_INTERNAL_API_ENDPOINT}/api/dms/internal/fleet/warranty-claims (warranty)

VSA_DMS_INTERNAL_API_ENDPOINT is validated like the CMS internal endpoint: https, a single
execute-api host, region extracted from the host, nothing else.
"""
from __future__ import annotations

from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

from agents.executors.base import ActionStatus, RetryPolicy
from agents.executors.dms import DmsExecutor
from agents.executors.simulated_operator import DECIDER_ID, DECIDER_KIND, DEMO_FLEET_IDS

_DEMO_FLEET = next(iter(sorted(DEMO_FLEET_IDS)))
_INTERNAL_BASE = "https://dmsinternal0.execute-api.us-west-2.amazonaws.com/staging"
_INTERNAL_RO_URL = f"{_INTERNAL_BASE}/api/dms/internal/fleet/repair-orders"
_INTERNAL_WC_URL = f"{_INTERNAL_BASE}/api/dms/internal/fleet/warranty-claims"
_DMS_BASE = "https://dms-staging.example.com"
_PUBLIC_RO_URL = f"{_DMS_BASE}/api/dms/fleet/repair-orders"
_PUBLIC_WC_URL = f"{_DMS_BASE}/api/dms/fleet/warranty-claims"
_TOKEN = "op-bearer-token-dms-tests"

_Creds = namedtuple("_Creds", "access_key secret_key token")
_CREDS = _Creds("TESTEXAMPLEACCESSKEY", "secret/DMS+TEST+KEY", "session-dms-EXAMPLE")


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
        retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
        credentials_fn=_creds_fn,
    )


@pytest.fixture(autouse=True)
def envs(monkeypatch):
    monkeypatch.setenv("VSA_DMS_INTERNAL_API_ENDPOINT", _INTERNAL_BASE)
    monkeypatch.setenv("VSA_DMS_API_ENDPOINT", _DMS_BASE)


def _sim_decider():
    return {"kind": DECIDER_KIND, "id": DECIDER_ID}


def _op_decider():
    return {"kind": "operator", "id": "op-sub-0001"}


def _book_fleet_action(decider=None) -> dict:
    return {
        "action_id": "AVX-ACT-d2000000-0000-4000-8000-000000000001",
        "finding_id": "FIND-d2000000-0000-4000-8000-000000000001",
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "fleet_id": _DEMO_FLEET,
        "decided_by": decider or _sim_decider(),
        "parameters": {
            "vehicle_vin": "MRDVEH00000000031",
            "dealer_id": "DLR-001",
            "needs": [{"labor_op": "03100", "need_id": "NID-001"}],
        },
    }


def _book_recall_action(decider=None) -> dict:
    return {
        "action_id": "AVX-ACT-d3000000-0000-4000-8000-000000000001",
        "finding_id": "FIND-d3000000-0000-4000-8000-000000000001",
        "action_kind": "book_recall_service",
        "target_system": "dms",
        "fleet_id": _DEMO_FLEET,
        "decided_by": decider or _sim_decider(),
        "parameters": {
            "vehicle_vin": "MRDVEH00000000031",
            "dealer_id": "DLR-001",
            "recall_id": "MRD-2026-001",
        },
    }


def _warranty_action(decider=None) -> dict:
    return {
        "action_id": "AVX-ACT-d4000000-0000-4000-8000-000000000001",
        "finding_id": "FIND-d4000000-0000-4000-8000-000000000001",
        "action_kind": "submit_warranty_claim",
        "target_system": "dms",
        "fleet_id": _DEMO_FLEET,
        "decided_by": decider or _sim_decider(),
        "parameters": {
            "vehicle_vin": "MRDVEH00000000031",
            "ro_id": "DMS-RO-9999",
            "line_no": "1",
            "dealer_id": "DLR-001",
            "labor_op": "03100",
        },
    }


def _send(action, claims, responses, creds=_CREDS):
    ex = _executor(creds)
    with patch("agents.executors.dms.requests.post", side_effect=list(responses)) as post:
        result = ex.dispatch(action, claims)
    return result, post


# ---------------------------------------------------------------------------
# book_fleet_service — simulated decider → signed internal path
# ---------------------------------------------------------------------------

class TestSimBookFleetServiceSignedPath:
    """Simulated book_fleet_service goes to the internal repair-orders route, signed."""

    def test_posts_to_internal_ro_url(self):
        """RED: DmsExecutor does not yet have the signed path for book_fleet_service."""
        _result, post = _send(_book_fleet_action(), {}, [_resp(201, {"ro_id": "DMS-RO-1"})])
        assert post.called
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _INTERNAL_RO_URL, f"expected {_INTERNAL_RO_URL!r}, got {url!r}"

    def test_no_authorization_bearer(self):
        """M5: no bearer header on the signed path."""
        _result, post = _send(_book_fleet_action(), {}, [_resp(201, {"ro_id": "DMS-RO-1"})])
        headers = post.call_args[1].get("headers") or {}
        auth = headers.get("Authorization", "")
        assert not auth.startswith("Bearer "), f"bearer must not appear; got {auth!r}"

    def test_no_bearer_even_with_token_in_claims(self):
        """M4: token from claims must not appear on the signed path."""
        _result, post = _send(_book_fleet_action(), {"token": _TOKEN},
                              [_resp(201, {"ro_id": "DMS-RO-1"})])
        headers = post.call_args[1].get("headers") or {}
        auth = headers.get("Authorization", "")
        assert not auth.startswith("Bearer "), f"claims token must not appear; got {auth!r}"

    def test_2xx_returns_in_progress(self):
        result, _ = _send(_book_fleet_action(), {}, [_resp(201, {"ro_id": "DMS-RO-1"})])
        assert result.status == ActionStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# book_recall_service — simulated decider → signed internal path
# ---------------------------------------------------------------------------

class TestSimBookRecallServiceSignedPath:
    def test_posts_to_internal_ro_url(self):
        _result, post = _send(_book_recall_action(), {}, [_resp(201, {"ro_id": "DMS-RO-2"})])
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _INTERNAL_RO_URL

    def test_no_bearer_header(self):
        _result, post = _send(_book_recall_action(), {}, [_resp(201, {"ro_id": "DMS-RO-2"})])
        headers = post.call_args[1].get("headers") or {}
        assert not headers.get("Authorization", "").startswith("Bearer ")


# ---------------------------------------------------------------------------
# submit_warranty_claim — simulated decider → signed internal path
# ---------------------------------------------------------------------------

class TestSimWarrantyClaimSignedPath:
    def test_posts_to_internal_wc_url(self):
        _result, post = _send(_warranty_action(), {}, [_resp(201)])
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _INTERNAL_WC_URL, f"expected {_INTERNAL_WC_URL!r}, got {url!r}"

    def test_no_bearer_header(self):
        _result, post = _send(_warranty_action(), {}, [_resp(201)])
        headers = post.call_args[1].get("headers") or {}
        assert not headers.get("Authorization", "").startswith("Bearer ")

    def test_2xx_returns_executed(self):
        result, _ = _send(_warranty_action(), {}, [_resp(201)])
        assert result.status == ActionStatus.EXECUTED


# ---------------------------------------------------------------------------
# Operator decisions unchanged (regression)
# ---------------------------------------------------------------------------

class TestOperatorDeciderUnchanged:
    def test_book_fleet_service_op_uses_public_url_and_bearer(self):
        _result, post = _send(_book_fleet_action(_op_decider()), {"token": _TOKEN},
                              [_resp(201, {"ro_id": "DMS-RO-3"})])
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _PUBLIC_RO_URL
        headers = post.call_args[1].get("headers") or {}
        assert headers.get("Authorization") == f"Bearer {_TOKEN}"

    def test_warranty_claim_op_uses_public_url_and_bearer(self):
        _result, post = _send(_warranty_action(_op_decider()), {"token": _TOKEN}, [_resp(201)])
        url = post.call_args[0][0] if post.call_args[0] else post.call_args[1].get("url")
        assert url == _PUBLIC_WC_URL
        headers = post.call_args[1].get("headers") or {}
        assert headers.get("Authorization") == f"Bearer {_TOKEN}"


# ---------------------------------------------------------------------------
# DMS internal endpoint validation
# ---------------------------------------------------------------------------

class TestDmsInternalEndpointValidation:
    """VSA_DMS_INTERNAL_API_ENDPOINT must be an execute-api HTTPS URL."""

    @pytest.mark.parametrize("value", [
        "",                                         # empty
        "http://dms.example.com/staging",           # not https
        "https://dms.example.com/staging",          # not execute-api
        "https://user:pass@xyz.execute-api.us-west-2.amazonaws.com/staging",  # credentials
    ], ids=["empty", "not-https", "not-execute-api", "credentials"])
    def test_invalid_endpoint_is_terminal_error(self, value, monkeypatch):
        """Misconfigured endpoint → terminal error, nothing sent."""
        monkeypatch.setenv("VSA_DMS_INTERNAL_API_ENDPOINT", value)
        result, post = _send(_book_fleet_action(), {}, [])
        assert result.status == ActionStatus.FAILED
        assert not post.called, "nothing must be sent on endpoint validation failure"
