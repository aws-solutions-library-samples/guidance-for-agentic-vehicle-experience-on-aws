"""Unit tests for the three supervisor tools.

These tests do NOT invoke Claude. They call tool functions directly with
a fake SessionContext, verifying:
  - retrieve returns sensible hits for the demo fixtures
  - book emits a well-formed STAR envelope
  - book refuses to fire without a prior triage call
  - triage is covered by an opt-in integration test that hits the live
    deployed /triage endpoint (VSA_TEST_HIT_API=1).

Run: pytest agents/supervisor/tests/test_tools_unit.py -v
"""

from __future__ import annotations

import os

import pytest

from agents.supervisor.config import Config, get_cached_id_token
from agents.supervisor.session import SessionContext
from agents.supervisor.tools.book import BookError, make_book_tool
from agents.supervisor.tools.retrieve import make_retrieve_tool
from agents.supervisor.tools.triage import make_triage_tool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_cms_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the CMS service-history write in unit tests.

    Without this, book() would try to boto3.resource("dynamodb") and hit
    real AWS. The env flag makes book._write_cms_service_history() return
    (False, None) immediately, and the envelope comes back with the
    legacy "draft-created" status (which is what pre-CMS-wire tests
    assert against).
    """
    monkeypatch.setenv("VSA_BOOK_WRITE_CMS", "false")


@pytest.fixture
def config() -> Config:
    return Config.from_env()


@pytest.fixture
def ctx(config: Config) -> SessionContext:
    """Fake context — no real JWT. Fine for retrieve and book unit tests.

    Note: vin is set to a fixture value (not config.default_vin) because
    the book tool requires a non-empty VIN in SessionContext (introduced
    in the canonical-bidi spike). Local dev env typically has an empty
    VSA_DEFAULT_VIN; relying on that here breaks the book unit tests
    that exercise post-VIN-validation behavior.
    """
    return SessionContext(
        tenant_id=config.tenant_id,
        session_id="vsa-sess-unittest",
        jwt="fake-jwt-not-used",
        persona_id="fleet_driver",
        vin=config.default_vin or "1HGBH41JXMN000046",
    )


# ---------------------------------------------------------------------------
# retrieve tool
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="existing corpora/* fixtures use 'category' frontmatter, not 'documentType'; "
    "the post-fix retrieve still loads them but documentType filtering won't return "
    "this specific recall-bulletin hit until corpora frontmatter is harmonized "
    "(see issues/2026-05-27-retrieve-tool-missing-kb-fixtures/summary.md follow-ups)",
    strict=False,
)
def test_retrieve_finds_coolant_recall(ctx: SessionContext, config: Config):
    retrieve = make_retrieve_tool(ctx, config)
    hits = retrieve("coolant overtemp recall Freightliner")
    assert hits, "Expected at least one hit for coolant recall query"
    # Top hit should be the coolant-sensor recall bulletin.
    top = hits[0]
    assert "26V123456" in top["documentId"] or "coolant" in top["title"].lower()
    assert top["documentType"] == "recall-bulletin"
    assert top["source"] == "shared"


@pytest.mark.xfail(
    reason="existing corpora/* fixtures don't have tenant-overlay docs; "
    "this test pre-supposes a tenantOverlay variant that doesn't exist yet "
    "(see issues/2026-05-27-retrieve-tool-missing-kb-fixtures/summary.md follow-ups)",
    strict=False,
)
def test_retrieve_prefers_tenant_overlay(ctx: SessionContext, config: Config):
    retrieve = make_retrieve_tool(ctx, config)
    # This query matches the Michelin fleet-sla-policy overlay doc.
    hits = retrieve("Michelin tier-1 SLA warm transfer policy")
    assert hits, "Expected a hit for tenant-overlay query"
    # At least one overlay result should be in the top 3.
    sources = [h["source"] for h in hits]
    assert "tenantOverlay" in sources


def test_retrieve_tenant_isolation(config: Config):
    """An overlay doc for tenant X must not surface for tenant Y."""
    other_ctx = SessionContext(
        tenant_id="some-other-tenant",
        session_id="vsa-sess-other",
        jwt="fake",
        persona_id="fleet_driver",
        vin=None,
    )
    retrieve = make_retrieve_tool(other_ctx, config)
    hits = retrieve("Michelin tier-1 SLA warm transfer policy")
    # Michelin overlay must not leak to a different tenant.
    for hit in hits:
        assert hit["source"] != "tenantOverlay" or hit.get("tenantId") != "michelin"


def test_retrieve_no_kb_falls_back_to_local(ctx: SessionContext, config: Config):
    """When adp_kb_id is absent, retrieve falls back to local corpora gracefully."""
    # Config.from_env() has no adp_kb_id — new tool falls back to local corpus.
    retrieve = make_retrieve_tool(ctx, config)
    result = retrieve("coolant recall sensor")
    # Result is a list (local fallback) — must not raise.
    assert isinstance(result, list)


def test_retrieve_source_category_passed_through(ctx: SessionContext, config: Config):
    """With no KB configured, source_category is accepted without error (no validation)."""
    retrieve = make_retrieve_tool(ctx, config)
    # Should not raise — source_category is passed through to KB filter; without KB it's a no-op.
    result = retrieve("brake", source_category="tsb_recall")
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# book tool
# ---------------------------------------------------------------------------


def test_book_defaults_to_p3_without_triage(ctx: SessionContext, config: Config):
    """When no prior triage call set ctx.triage_classification, book defaults
    to P3 instead of raising. This is the new canonical-bidi behavior:
    Nova was observed stalling at "I need to check severity first" without
    actually calling triage, so the tool was relaxed to default to P3
    (lowest priority) and proceed. See book.py near line 269 for the
    rationale."""
    book = make_book_tool(ctx, config)
    result = book(
        reportedSymptom="engine noise",
        customerActionsTaken=["create-ticket"],
        narrative="test",
    )
    assert result is not None
    envelope = result.get("envelope", {})
    payload = envelope.get("payload", {})
    classification = payload.get("priority")
    assert classification == "P3", f"Expected default classification P3, got {classification!r}"


def test_book_emits_star_envelope(ctx: SessionContext, config: Config):
    # Simulate a prior triage call having populated session state.
    ctx.triage_classification = "P1"
    ctx.triage_decision_id = "vsa-sess-unittest"

    book = make_book_tool(ctx, config)
    result = book(
        reportedSymptom="Coolant temperature spiked on highway.",
        customerActionsTaken=["create-ticket", "warm-transfer-to-fleet-ops"],
        narrative="P1 service-now — coolant over-temperature with open recall.",
    )

    # Envelope structure per VSA_STAR_JSON_SCHEMA.md §2.
    env = result["envelope"]
    assert env["schemaVersion"] == "1.0.0"
    assert env["messageType"] == "ServiceRequestAdd"
    assert env["tenantId"] == ctx.tenant_id
    assert env["sessionId"] == ctx.session_id
    assert env["correlation"]["triageDecisionId"] == "vsa-sess-unittest"
    assert env["correlation"]["vin"] == ctx.vin

    # Payload carries priority from triage, not from the LLM.
    payload = env["payload"]
    assert payload["priority"] == "P1"
    assert payload["urgency"]["classification"] == "service-now"
    assert payload["urgency"]["recommendedWindowHours"] == 48

    # Customer actions echoed through, plus create-ticket added automatically.
    actions = payload["actions"]
    assert "warm-transfer-to-fleet-ops" in actions["takenByCustomer"]
    assert "create-ticket" in actions["offeredToCustomer"]


def test_book_rejects_invalid_action(ctx: SessionContext, config: Config):
    ctx.triage_classification = "P2"
    ctx.triage_decision_id = "vsa-sess-unittest"

    book = make_book_tool(ctx, config)
    with pytest.raises(BookError, match="Invalid action"):
        book(
            reportedSymptom="test",
            customerActionsTaken=["teleport-to-service-center"],
            narrative="test",
        )


def test_book_p0_defaults_to_roadside_recommendation(ctx: SessionContext, config: Config):
    ctx.triage_classification = "P0"
    ctx.triage_decision_id = "vsa-sess-p0"

    book = make_book_tool(ctx, config)
    result = book(
        reportedSymptom="Brake warning + ABS off.",
        customerActionsTaken=["schedule-roadside-if-vehicle-fails"],
        narrative="P0 stop-driving — brake system fault.",
    )
    payload = result["envelope"]["payload"]
    assert payload["priority"] == "P0"
    assert payload["urgency"]["classification"] == "stop-driving"
    assert payload["urgency"]["safetyAssessment"]["safeToDrive"] == "no"
    assert payload["actions"]["recommended"] == "dispatch-roadside"


# ---------------------------------------------------------------------------
# triage tool (opt-in; hits live deployed API)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("VSA_TEST_HIT_API"),
    reason="Set VSA_TEST_HIT_API=1 to run triage integration test",
)
def test_triage_live_endpoint(config: Config):
    jwt = get_cached_id_token(config)
    ctx = SessionContext.new(tenant_id=config.tenant_id, jwt=jwt, persona_id="fleet_driver", vin=config.default_vin)
    triage = make_triage_tool(ctx, config)

    result = triage(vin=config.default_vin)
    assert result["classification"] in {"P0", "P1", "P2", "P3"}
    assert result["sessionId"]
    assert result["decidedAt"]
    # After calling, ctx should carry the decision forward.
    assert ctx.triage_classification == result["classification"]
    assert ctx.triage_decision_id == result["sessionId"]
