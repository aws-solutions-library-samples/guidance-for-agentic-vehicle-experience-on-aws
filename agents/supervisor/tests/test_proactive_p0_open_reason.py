"""openReason reaches the session through the real ``_establish_session`` (review cycle 1 C2).

Spec 2026-11-05-avx-proactive-p0-alert § V D2: a session opened from a P0 push tap
(``openReason == "p0_push"``) asks the classifier about the baseline at once. The
deployed AgentCore path always carries Tenant-Id and Vin as signed custom headers,
so ``_establish_session`` never reads ``session.start``; the reason must arrive as
the ``OpenReason`` custom header. These tests run the real function with those
headers present (``receive_json`` raises if it is called), the same way
``test_bidi_app_avx_preseed.py`` does for AvxFindingId.

Synchronous ``asyncio.run`` on purpose: pytest-asyncio is not installed here.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
from unittest.mock import AsyncMock

import pytest
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor.bidi_app import (
    KNOWN_OPEN_REASONS,
    OPEN_REASON_QUERY_PARAMS,
    _establish_session,
)

BASE_HEADERS = {
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.abc",
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
}
REPO = pathlib.Path(__file__).resolve().parents[3]


def _establish(headers: dict, first_message: dict | None = None):
    ctx = RequestContext(session_id="sid", request_headers=headers, request=None)
    ws = AsyncMock()
    if first_message is None:
        ws.receive_json = AsyncMock(
            side_effect=AssertionError("receive_json should not be called")
        )
    else:
        ws.receive_json = AsyncMock(return_value=first_message)
    return asyncio.run(_establish_session(ws, ctx, "sid"))


@pytest.mark.parametrize(
    "header",
    [
        # What iOS sends (VSABidiClient.openReasonHeaderName) and AgentCore may
        # deliver in any of these casings.
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-OpenReason",
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Open-Reason",
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Open_Reason",
    ],
)
def test_open_reason_header_reaches_ctx_with_tenant_and_vin_headers(header):
    session = _establish({**BASE_HEADERS, header: "p0_push"})
    assert session.open_reason == "p0_push"


def test_absent_header_leaves_open_reason_none():
    assert _establish(dict(BASE_HEADERS)).open_reason is None


def test_unknown_open_reason_is_treated_as_absent():
    session = _establish(
        {**BASE_HEADERS, "X-Amzn-Bedrock-AgentCore-Runtime-Custom-OpenReason": "anything"}
    )
    assert session.open_reason is None


def test_known_open_reasons_are_exactly_p0_push():
    assert KNOWN_OPEN_REASONS == frozenset({"p0_push"})


def test_session_start_fallback_still_reads_open_reason():
    """The local backend has no custom headers; session.start keeps working."""
    session = _establish(
        {"X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.abc"},
        first_message={
            "type": "session.start",
            "tenantId": "meridian-fleet",
            "vin": "1FMCU0F70MUA12345",
            "openReason": "p0_push",
        },
    )
    assert session.open_reason == "p0_push"


def test_declared_casings_are_lowercase_and_distinct():
    assert len(OPEN_REASON_QUERY_PARAMS) == 3
    assert len(set(OPEN_REASON_QUERY_PARAMS)) == 3
    for key in OPEN_REASON_QUERY_PARAMS:
        assert key == key.lower()
        assert key.startswith("x-amzn-bedrock-agentcore-runtime-custom-")


def test_allowlist_wire_name_is_the_header_ios_sends():
    """The deploy-time allowlist entry must be the spelling iOS sends, or AgentCore
    drops the header with no error and a P0-push session takes the normal baseline."""
    script = REPO / "scripts" / "verify_agentcore_header_allowlist.py"
    spec = importlib.util.spec_from_file_location("_verify_allowlist_p0", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_verify_allowlist_p0"] = mod
    spec.loader.exec_module(mod)
    assert mod._WIRE_NAMES["OPEN_REASON_QUERY_PARAMS"] == "OpenReason"
    assert "OPEN_REASON_QUERY_PARAMS" not in mod._KNOWN_GAPS
