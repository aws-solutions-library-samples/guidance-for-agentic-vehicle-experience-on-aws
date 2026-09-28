"""Tests for KB-backed reference tool factories (Group 4.A).

Happy-path tests use mocked bedrock-agent-runtime:Retrieve responses.
KB-unavailable fallback tests stay skipped (Group 6.A fixtures cover them).

See `.kiro/specs/2026-06-15-cvx-on-foundation-v1/tasks.md` § Group 4.A.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _fake_config(kb_id: str = "test-kb-id", region: str = "us-east-1") -> SimpleNamespace:
    """Minimal config stub for KB tool factories."""
    return SimpleNamespace(
        adp_kb_id=kb_id,
        adp_region=region,
        kb_fixtures_dir=None,  # not used in KB path
    )


def _fake_session(persona_id: str = "service_advisor") -> SimpleNamespace:
    """Minimal session stub."""
    return SimpleNamespace(
        persona_id=persona_id,
        vin="1FMCU0J91KUB12345",
        tenant_id="fleet",
        customer_id="cust-123",
    )


def _retrieve_response(*texts: str) -> dict:
    """Build a minimal bedrock-agent-runtime:Retrieve response."""
    return {
        "retrievalResults": [
            {
                "content": {"text": t},
                "location": {"s3Location": {"uri": f"s3://bucket/doc-{i}.md"}},
                "score": 0.9 - i * 0.1,
            }
            for i, t in enumerate(texts)
        ]
    }


# ---------------------------------------------------------------------------
# retrieve (cross-account KB)
# ---------------------------------------------------------------------------


def test_retrieve_happy_path(monkeypatch):
    """retrieve tool returns KB results when Retrieve succeeds.

    The source_category filter is gated OFF by default (tools/kb_filter.py);
    enable it here so the filter-construction path is exercised, matching the
    knowledge/parts/diy filter-on tests. Gate-OFF default behavior is covered by
    test_retrieve_default_no_source_category_filter.
    """
    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")
    from agents.supervisor.tools.retrieve import make_retrieve_tool

    session = _fake_session("service_advisor")
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        "TSB 2024-BR-0847: Brake squeal on light application. Root cause: glazed pads."
    )

    with patch("agents.supervisor.tools.retrieve.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client

        retrieve_fn = make_retrieve_tool(session, config)
        result = retrieve_fn(query="brake squeal TSB", source_category="tsb_recall")

    assert isinstance(result, list)
    assert len(result) >= 1
    assert "snippet" in result[0] or "available" not in result[0]
    # Confirm Retrieve was called with correct KB ID and filter
    call_kwargs = mock_boto3.client.return_value.retrieve.call_args[1]
    assert call_kwargs["knowledgeBaseId"] == "test-kb-id"
    flt = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]
    assert flt == {"equals": {"key": "source_category", "value": "tsb_recall"}}


def test_retrieve_default_no_source_category_filter():
    """Interim (gate OFF by default): retrieve sends NO source_category filter and
    still grounds on the KB. Guards issues/2026-06-19-cvx-staging-kb-grounding-
    local-fallback (retrieve.py was missed by the 2026-06-18 metadata-mismatch
    gate, silently forcing local fallback on the un-tagged ADP KB).
    """
    from agents.supervisor.tools.retrieve import make_retrieve_tool

    session = _fake_session("service_advisor")
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        "TSB 2024-BR-0847: Brake squeal on light application."
    )

    with patch("agents.supervisor.tools.retrieve.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        retrieve_fn = make_retrieve_tool(session, config)
        result = retrieve_fn(query="brake squeal TSB", source_category="tsb_recall")

    assert isinstance(result, list) and len(result) >= 1
    assert result[0]["source"].startswith("s3://")  # live KB hit, not local fallback
    call_kwargs = mock_boto3.client.return_value.retrieve.call_args[1]
    vsc = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert "filter" not in vsc, "gate OFF must omit the source_category filter"


def test_retrieve_kb_unavailable_falls_back_to_local():
    """When KB Retrieve raises ClientError, retrieve falls back to local corpora (non-reference personas)."""
    from botocore.exceptions import ClientError
    from agents.supervisor.tools.retrieve import make_retrieve_tool

    session = _fake_session("consumer")  # non-reference persona → local fallback
    config = _fake_config(kb_id="test-kb-id")

    mock_client = MagicMock()
    mock_client.retrieve.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailableException", "Message": "KB unavailable"}},
        "Retrieve",
    )

    with patch("agents.supervisor.tools.retrieve.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        # Also patch CloudWatch metric emission to avoid network calls
        with patch("agents.supervisor.tools.retrieve.boto3.client"):
            retrieve_fn = make_retrieve_tool(session, config)
            result = retrieve_fn(query="tire pressure check")

    # Non-reference persona: fallback returns local results or empty list (not an error dict)
    assert isinstance(result, list)


def test_retrieve_records_citations_on_session_context():
    """Each retrieve() call writes its hits to ``ctx.last_kb_citations`` so
    ``agentcore_app.py`` can surface them in the runtime response. Guards the
    PO UAT 2026-06-22 finding #1 (grounding indistinguishable from model
    knowledge because citations were not surfaced).
    """
    from agents.supervisor.tools.retrieve import make_retrieve_tool

    # Real SessionContext (not SimpleNamespace) — verifies the dataclass field.
    from agents.supervisor.session import SessionContext
    session = SessionContext(
        tenant_id="meridian-fleet",
        session_id="vsa-sess-test",
        jwt="t.t.t",
        persona_id="fleet_driver",
        vin="1FMCU0F70MUA12345",
    )
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        "DTC P0420 — catalyst efficiency below threshold.",
        "TSB 2024-EM-0123: Catalyst monitor delay.",
    )

    assert session.last_kb_citations is None, "field starts unset"

    with patch("agents.supervisor.tools.retrieve.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        retrieve_fn = make_retrieve_tool(session, config)
        retrieve_fn(query="DTC P0420")

    assert isinstance(session.last_kb_citations, list)
    assert len(session.last_kb_citations) == 2
    sources = [c["source"] for c in session.last_kb_citations]
    assert all(s.startswith("s3://") for s in sources)
    # Score is coerced to float (the wire payload may carry str or int)
    assert isinstance(session.last_kb_citations[0]["score"], float)
    # bool subclass of int must NOT slip through as a score (matches tier3 _coerce_score)
    assert all(not isinstance(c["score"], bool) for c in session.last_kb_citations)


def test_retrieve_records_citations_replaces_prior_call():
    """A second retrieve() call replaces the citations list (last-call-wins).

    Without this, a multi-turn session would accumulate stale citations from
    earlier prompts and mis-attribute them to the current answer.
    """
    from agents.supervisor.tools.retrieve import make_retrieve_tool
    from agents.supervisor.session import SessionContext

    session = SessionContext(
        tenant_id="meridian-fleet",
        session_id="vsa-sess-test",
        jwt="t.t.t",
        persona_id="fleet_driver",
        vin="1FMCU0F70MUA12345",
    )
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.side_effect = [
        _retrieve_response("First call doc"),
        _retrieve_response("Second call doc A", "Second call doc B"),
    ]

    with patch("agents.supervisor.tools.retrieve.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        retrieve_fn = make_retrieve_tool(session, config)
        retrieve_fn(query="first")
        assert len(session.last_kb_citations) == 1
        retrieve_fn(query="second")

    # Second call replaced the first — last-call-wins, not append.
    assert len(session.last_kb_citations) == 2
    assert all("Second" in c.get("source", "") or "doc-" in c.get("source", "")
               for c in session.last_kb_citations)


# ---------------------------------------------------------------------------
# parts_lookup (KB-backed)
# ---------------------------------------------------------------------------


def test_parts_lookup_happy_path(monkeypatch):
    """lookup_parts returns part entries when Retrieve succeeds (filter gate ON)."""
    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")
    from agents.supervisor.tools.parts_lookup import make_parts_lookup_tool

    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        '{"partNumber": "BRK-4721", "name": "Front Brake Pad Set", "supersession": "BRK-4700"}'
    )

    with patch("agents.supervisor.tools.parts_lookup.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client

        parts_fn = make_parts_lookup_tool(config=config)
        result = parts_fn(category="brakes", vehicle_make="Ford")

    assert result["found"] is True
    assert len(result["parts"]) >= 1
    part = result["parts"][0]
    assert "partNumber" in part
    assert "name" in part
    assert "supersession" in part
    # Confirm filter was parts_catalog
    call_kwargs = mock_boto3.client.return_value.retrieve.call_args[1]
    flt = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]
    assert flt == {"equals": {"key": "source_category", "value": "parts_catalog"}}


def test_parts_lookup_kb_unavailable_falls_back_to_local():
    """When KB Retrieve raises ClientError, parts_lookup returns empty parts list gracefully."""
    from botocore.exceptions import ClientError
    from agents.supervisor.tools.parts_lookup import make_parts_lookup_tool

    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailableException", "Message": "KB unavailable"}},
        "Retrieve",
    )

    with patch("agents.supervisor.tools.parts_lookup.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        parts_fn = make_parts_lookup_tool(config=config)
        result = parts_fn(category="brakes", vehicle_make="Ford")

    assert result["found"] is False
    assert isinstance(result["parts"], list)
    assert "reason" in result


# ---------------------------------------------------------------------------
# knowledge (KB-backed, persona-routed)
# ---------------------------------------------------------------------------


def test_knowledge_happy_path(monkeypatch):
    """lookup_knowledge returns KB passages routed by persona filter (filter gate ON)."""
    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")
    from agents.supervisor.tools.knowledge import make_knowledge_tool

    session = _fake_session("technician")
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        "DTC P0420: Catalyst efficiency below threshold. Check O2 sensors and catalytic converter."
    )

    with patch("agents.supervisor.tools.knowledge.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client

        knowledge_fn = make_knowledge_tool(session=session, config=config)
        result = knowledge_fn(query="DTC P0420 diagnosis")

    assert result.get("found") is True
    assert len(result["passages"]) >= 1
    assert "P0420" in result["passages"][0]["text"]
    # Confirm per-persona filter applied: technician gets dtc_guide, parts_catalog, service_policy
    call_kwargs = mock_boto3.client.return_value.retrieve.call_args[1]
    flt = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]
    # technician has 3 categories → "in" filter
    assert flt.get("in", {}).get("key") == "source_category"
    assert "dtc_guide" in flt["in"]["value"]


def test_knowledge_default_no_source_category_filter():
    """Interim (gate OFF by default): no source_category filter is sent, and the
    tool still grounds on the KB. Guards the 2026-06-18 metadata-mismatch fix.
    """
    from agents.supervisor.tools.knowledge import make_knowledge_tool

    session = _fake_session("technician")
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        "DTC P0420: Catalyst efficiency below threshold."
    )

    with patch("agents.supervisor.tools.knowledge.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        knowledge_fn = make_knowledge_tool(session=session, config=config)
        result = knowledge_fn(query="DTC P0420 diagnosis")

    assert result.get("found") is True
    call_kwargs = mock_boto3.client.return_value.retrieve.call_args[1]
    vsc = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert "filter" not in vsc, "gate OFF must omit the source_category filter"


def test_knowledge_kb_unavailable_falls_back_to_local():
    """When KB Retrieve raises ClientError, knowledge falls back to local corpora."""
    from botocore.exceptions import ClientError
    from agents.supervisor.tools.knowledge import make_knowledge_tool

    session = _fake_session("service_advisor")
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailableException", "Message": "KB unavailable"}},
        "Retrieve",
    )

    with patch("agents.supervisor.tools.knowledge.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        knowledge_fn = make_knowledge_tool(session=session, config=config)
        result = knowledge_fn(query="brake pad replacement guide")

    # Fallback: returns a dict with found key (local corpus result or empty)
    assert isinstance(result, dict)
    assert "found" in result or "available" in result


# ---------------------------------------------------------------------------
# diy_repair_advisor (KB-augmented)
# ---------------------------------------------------------------------------


def test_diy_repair_advisor_happy_path(monkeypatch):
    """diy_repair_advisor returns rule-based corpus PLUS KB dtc_guide snippets (filter gate ON)."""
    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")
    from agents.supervisor.tools.diy_repair_advisor import make_diy_repair_advisor_tool

    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        "P0420: Catalytic converter efficiency below threshold. Check for exhaust leaks first."
    )

    with patch("agents.supervisor.tools.diy_repair_advisor.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client

        diy_fn = make_diy_repair_advisor_tool(config=config)
        result = diy_fn(query="catalyst efficiency warning light", dtc_code="P0420")

    # Rule-based path: reference_material from owner-manuals corpus
    assert "reference_material" in result
    # KB augmentation: kb_dtc_snippets appended
    assert "kb_dtc_snippets" in result
    assert len(result["kb_dtc_snippets"]) >= 1
    assert "P0420" in result["kb_dtc_snippets"][0]["text"]
    # Confirm dtc_guide filter
    call_kwargs = mock_boto3.client.return_value.retrieve.call_args[1]
    flt = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]
    assert flt == {"equals": {"key": "source_category", "value": "dtc_guide"}}


def test_diy_repair_advisor_kb_unavailable_falls_back_to_local():
    """When KB Retrieve raises ClientError, diy_repair_advisor returns rule-based result only."""
    from botocore.exceptions import ClientError
    from agents.supervisor.tools.diy_repair_advisor import make_diy_repair_advisor_tool

    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailableException", "Message": "KB unavailable"}},
        "Retrieve",
    )

    with patch("agents.supervisor.tools.diy_repair_advisor.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        diy_fn = make_diy_repair_advisor_tool(config=config)
        result = diy_fn(query="check engine light", dtc_code="P0420")

    # Rule-based path still works; kb_dtc_snippets absent or empty
    assert "reference_material" in result
    assert result.get("kb_dtc_snippets", []) == []
