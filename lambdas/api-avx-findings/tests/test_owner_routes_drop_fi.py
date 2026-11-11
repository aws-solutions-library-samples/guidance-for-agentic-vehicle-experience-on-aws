"""The owner list routes drop Fleet Intelligence items (issue 2026-10-07).

Since the Tier 2 writer projects the assigned driver's ``sub`` as ``owner_id``
(``issues/2026-10-07-avx-finding-owner-is-purchaser-not-driver/``), a driver's
``GET /findings/owner/{sub}`` and ``/findings/owner-fleet/{sub}`` reach the VIN Findings on
their vehicle, and FI cost Findings carry the same projection. FI items are
operator-only (spec D13 / FI contract § 12), as on ``/findings/me``.

Each case has a control: the non-FI Finding on the same page is returned.
Mutation: delete either ``items = _drop_fi_findings(items)`` line in handler.py.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h  # noqa: E402

DRIVER_SUB = "1111aaaa-2222-4333-8444-5555bbbb5555"
ROUTES = {"owner": "/findings/owner/{owner_id}", "owner-fleet": "/findings/owner-fleet/{owner_id}"}


def _f(fid: str, **extra) -> dict:
    return {"finding_id": fid, "agent_id": "agent-2-vehicle-health", "scope": {"vin": "VIN-A"},
            "vin": "VIN-A", "owner_id": DRIVER_SUB, "expires_at": "2099-01-01T00:00:00Z",
            "headline": fid, **extra}


PAGE = [
    _f("F-health"),
    # Expired, non-FI: kept. The owner routes have always returned expired Findings
    # (see _drop_fi_findings); this pins that the FI drop did not widen into an
    # expiry filter (review cycle 2 mutation X3).
    _f("F-expired", expires_at="2000-01-01T00:00:00Z"),
    _f("F-fi-agent", agent_id="agent-fi-cost"),
    _f("F-fi-recommendation", recommended_action={"action_kind": "acknowledge_only"}),
]


def _read(route: str) -> tuple[int, list[str], str | None]:
    event = {
        "httpMethod": "GET",
        "resource": ROUTES[route],
        "pathParameters": {"owner_id": DRIVER_SUB},
        "queryStringParameters": {},
        "requestContext": {"authorizer": {"claims": {"sub": DRIVER_SUB,
                                                     "cognito:groups": "fleet-driver"}}},
        "body": None,
    }
    with patch.object(h, "_list_findings_by_owner", return_value=([dict(f) for f in PAGE], "tok")):
        resp = h.handler(event, None)
    body = json.loads(resp["body"])
    return resp["statusCode"], sorted(f["finding_id"] for f in body.get("findings", [])), body.get("next_token")


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_the_driver_gets_their_vehicle_findings_but_no_fi_item(route):
    status, ids, token = _read(route)
    assert status == 200
    assert ids == ["F-expired", "F-health"], "non-FI Findings returned (expired too), both FI shapes dropped"
    assert token == "tok", "a short page keeps its cursor"


def test_drop_fi_findings_uses_the_shared_predicate():
    assert [f["finding_id"] for f in h._drop_fi_findings(PAGE)] == ["F-health", "F-expired"]
    assert all(h._is_fi_finding(f) for f in PAGE[2:])
