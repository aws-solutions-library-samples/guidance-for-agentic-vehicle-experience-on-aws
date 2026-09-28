"""Shared pytest fixtures for the VSA eval pipeline.

Provides mock clients for all external services and a tool_factory_kit
that wires every supervisor tool factory with the appropriate mocks.

All fixtures are function-scoped so each test gets a fresh instance with
no shared mutable state.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evals.runner.mocks import (
    MockADPClient,
    MockCMSClient,
    MockConnectClient,
    MockKBClient,
    MockSessionContext,
)


# ---------------------------------------------------------------------------
# CLI options
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    """Register --staging: explicit opt-in for live-staging smoke tests.

    Live per-persona smoke tests (evals/runner/tier3_e2e.py::test_persona_smoke_*)
    connect to the deployed AgentCore runtime and require BOTH --staging AND a
    RUNTIME_ID env var. Without both they skip, so the default suite and PR CI
    never attempt a network call. Spec: 2026-06-19-cvx-tier3-persona-smoke-harness.
    """
    parser.addoption(
        "--staging",
        action="store_true",
        default=False,
        help="Run live-staging smoke tests against the deployed AgentCore runtime "
        "(also requires RUNTIME_ID env var).",
    )


@pytest.fixture(scope="session")
def staging_enabled(request) -> bool:
    """True when --staging was passed on the pytest command line."""
    return bool(request.config.getoption("--staging"))


# ---------------------------------------------------------------------------
# Basic mock fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_cms():
    """Fresh MockCMSClient for each test.

    Mocks: lambdas/shared/cms_client_real.py::RealCmsClient
    """
    yield MockCMSClient()


@pytest.fixture
def mock_adp():
    """Fresh MockADPClient for each test.

    Mocks: agents/supervisor/tools/{vehicle_identity,parts_lookup,service_network}.py
    """
    yield MockADPClient()


@pytest.fixture
def mock_kb():
    """Fresh MockKBClient for each test.

    Mocks: agents/supervisor/tools/knowledge.py (_query_bedrock_kb)
    """
    yield MockKBClient()


@pytest.fixture
def mock_connect():
    """Fresh MockConnectClient for each test.

    Mocks: agents/supervisor/tools/escalate.py (call_escalate_endpoint)
    """
    yield MockConnectClient()


@pytest.fixture
def mock_session_ctx():
    """Fresh SessionContext populated with fixture values for each test.

    Uses the real SessionContext from agents.supervisor.session.
    Fixture: tenantId='meridian-fleet', vehicleId='VEH-0025',
             driverId='drv-samantha-carter', VIN='1FTRX18W6XKB12345'.
    """
    yield MockSessionContext()


# ---------------------------------------------------------------------------
# tool_factory_kit
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_factory_kit(mock_session_ctx, mock_adp, mock_kb, mock_connect):
    """Dict mapping tool name → instantiated tool function, wired with mocks.

    Each tool factory from agents.supervisor.tools.* is called with the
    appropriate mock dependencies. Tools that call DynamoDB or HTTP are
    patched so no real AWS calls are made.

    Returns:
        dict with keys: 'triage', 'book', 'find_service_center',
        'parts_lookup', 'vehicle_identity', 'knowledge', 'escalate',
        'diy_repair_advisor', 'retrieve'
    """
    from agents.supervisor.config import Config
    from agents.supervisor.tools import (
        make_book_tool,
        make_diy_repair_advisor_tool,
        make_escalate_tool,
        make_knowledge_tool,
        make_parts_lookup_tool,
        make_retrieve_tool,
        make_service_network_tool,
        make_triage_tool,
        make_vehicle_identity_tool,
    )

    # Minimal Config — api_base is empty; tools that call HTTP are patched.
    _ROOT = Path(__file__).resolve().parent.parent
    config = Config(
        api_base="http://mock-api.local",
        tenant_id="meridian-fleet",
        cognito_pool_id="",
        cognito_client_id="",
        demo_username="demo@meridian-fleet.example",
        demo_password="",
        default_vin="1FTRX18W6XKB12345",
        default_vehicle_id="VEH-0025",
        default_driver_id="drv-samantha-carter",
        kb_fixtures_dir=_ROOT / "corpora",
        aws_region="us-east-1",
        nova_sonic_region="us-east-1",
        supervisor_model_id="us.anthropic.claude-sonnet-4-6",
        supervisor_max_tokens=4096,
    )

    # --- vehicle_identity: patch DynamoDB Table.get_item ---
    _vin_item = mock_adp.decode_vin("1FTRX18W6XKB12345")
    _mock_vi_table = MagicMock()
    _mock_vi_table.get_item.return_value = {"Item": _vin_item}

    # --- parts_lookup: patch DynamoDB Table.scan ---
    def _parts_scan(**kwargs):
        # Return all fixture parts; the tool filters by category.
        all_parts = []
        for cat_parts in [
            mock_adp.lookup_parts("brakes")["parts"],
            mock_adp.lookup_parts("tires")["parts"],
            mock_adp.lookup_parts("engine")["parts"],
        ]:
            for p in cat_parts:
                # Add compatibleVehicles so the tool's make/model filter works.
                p.setdefault("compatibleVehicles", [{"make": "Ford", "model": "F-150"}])
                all_parts.append(p)
        return {"Items": all_parts}

    _mock_parts_table = MagicMock()
    _mock_parts_table.scan.side_effect = _parts_scan

    # --- service_network: patch DynamoDB Table.scan ---
    def _sn_scan(**kwargs):
        centers = mock_adp.find_service_centers("brakes")["centers"]
        return {"Items": centers}

    _mock_sn_table = MagicMock()
    _mock_sn_table.scan.side_effect = _sn_scan

    # --- knowledge: patch boto3 bedrock-agent-runtime client ---
    def _kb_retrieve(**kwargs):
        query = kwargs.get("retrievalQuery", {}).get("text", "")
        max_r = (
            kwargs.get("retrievalConfiguration", {})
            .get("vectorSearchConfiguration", {})
            .get("numberOfResults", 3)
        )
        result = mock_kb.retrieve(query, max_r)
        # Convert to boto3 response shape
        return {
            "retrievalResults": [
                {
                    "content": {"text": p["text"]},
                    "location": {"s3Location": {"uri": p["source"]}},
                    "score": p["score"],
                }
                for p in result["passages"]
            ]
        }

    _mock_kb_client = MagicMock()
    _mock_kb_client.retrieve.side_effect = _kb_retrieve

    # --- triage: patch requests.post ---
    import json as _json

    def _triage_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "classification": "P2",
            "sessionId": mock_session_ctx.session_id,
            "decidedAt": "2026-05-25T10:00:00.000Z",
            "latencyMs": 42,
            "triageDecisionId": "mock-triage-decision-001",
        }
        return resp

    # --- escalate: patch requests.post ---
    def _escalate_post(url, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = mock_connect.start_chat_contact(
            severity=json.get("severity", "P2") if json else "P2",
            vin=json.get("vin", "") if json else "",
            vehicle_id=json.get("vehicleId", "") if json else "",
            reason=json.get("reason", "") if json else "",
            session_id=json.get("voiceSessionId", "") if json else "",
        )
        return resp

    # --- book: patch DynamoDB (book.py writes to CMS service-history table) ---
    _mock_book_ddb = MagicMock()
    _mock_book_ddb.Table.return_value.put_item.return_value = {}
    _mock_book_ddb.Table.return_value.get_item.return_value = {"Item": {
        "vehicleId": "VEH-0025",
        "make": "Ford",
        "model": "F-150",
        "year": 2022,
        "odometer": 45000,
    }}

    # --- requests.post: single routed patch ---
    # NOTE: agents.supervisor.tools.triage.requests and ...escalate.requests
    # reference the SAME `requests` module object. Patching
    # `agents.supervisor.tools.triage.requests.post` and
    # `agents.supervisor.tools.escalate.requests.post` separately is a no-op:
    # the second patch wins because both target the same attribute on the
    # shared module. Instead, we patch ONCE at the canonical `requests.post`
    # attribute and dispatch by URL. Fix Cycle 3 — F2b.
    def _routed_post(url, json=None, headers=None, timeout=None):
        if "/triage" in url:
            return _triage_post(url, json=json, headers=headers, timeout=timeout)
        if "/escalate" in url:
            return _escalate_post(url, json=json, headers=headers, timeout=timeout)
        raise RuntimeError(f"Unmocked HTTP POST to {url}")

    with (
        patch(
            "agents.supervisor.tools.vehicle_identity.boto3.resource",
            return_value=MagicMock(Table=MagicMock(return_value=_mock_vi_table)),
        ),
        patch(
            "agents.supervisor.tools.parts_lookup.boto3.resource",
            return_value=MagicMock(Table=MagicMock(return_value=_mock_parts_table)),
        ),
        patch(
            "agents.supervisor.tools.service_network.boto3.resource",
            return_value=MagicMock(Table=MagicMock(return_value=_mock_sn_table)),
        ),
        patch(
            "agents.supervisor.tools.knowledge.boto3.client",
            return_value=_mock_kb_client,
        ),
        patch("requests.post", side_effect=_routed_post),
        patch("agents.supervisor.tools.book.boto3.resource", return_value=_mock_book_ddb),
    ):
        kit = {
            "triage": make_triage_tool(mock_session_ctx, config),
            "book": make_book_tool(mock_session_ctx, config),
            "find_service_center": make_service_network_tool(mock_session_ctx),
            "parts_lookup": make_parts_lookup_tool(),
            "vehicle_identity": make_vehicle_identity_tool(mock_session_ctx, config),
            "knowledge": make_knowledge_tool(),
            "escalate": make_escalate_tool(mock_session_ctx, config),
            "diy_repair_advisor": make_diy_repair_advisor_tool(mock_session_ctx),
            "retrieve": make_retrieve_tool(mock_session_ctx, config),
        }
        yield kit
