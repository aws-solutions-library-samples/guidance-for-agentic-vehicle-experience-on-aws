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
from typing import Any

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
    # This query matches the Meridian Fleet-sla-policy overlay doc.
    hits = retrieve("Meridian Fleet tier-1 SLA warm transfer policy")
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
    hits = retrieve("Meridian Fleet tier-1 SLA warm transfer policy")
    # Meridian Fleet overlay must not leak to a different tenant.
    for hit in hits:
        assert hit["source"] != "tenantOverlay" or hit.get("tenantId") != "meridian-fleet"


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


def test_book_fails_closed_without_triage(ctx: SessionContext, config: Config):
    """book MUST refuse when no triage call set ctx.triage_classification.

    INVERTED 2026-09-23. This test previously asserted the OPPOSITE — that
    book silently defaulted to P3 — and was named
    `test_book_defaults_to_p3_without_triage`. It was codifying a fail-OPEN
    on the platform's only deterministic P0–P3 seam, so the defect was
    test-locked: any attempt to fix the fail-open would have turned this
    test red and looked like a regression.

    The fail-open fired in staging on 2026-09-23 (session
    session_1790101469102_mhpvti4vs-x): `triage` returned 401, the LLM
    narrated an invented P0 pull-over instruction, and this default quietly
    supplied "P3" so the booking succeeded — surfacing a real ticket
    (VSA-2026-09-23-9736) whose priority contradicted the safety narrative
    the driver had just been given, with safeToDrive="yes" and a 14-day SLA.

    The original rationale (Nova Sonic stalling at "I need to check the
    severity first..." without calling `triage`) may still be a real voice
    path bug — but the fix for that belongs in the voice-side triage
    invocation, not in a silent default here.

    See issues/2026-09-23-triage-401-llm-improvises-classification/ and
    ~/.kiro/steering/agentic-tiers.md § "What stays deterministic, and why".
    """
    assert ctx.triage_classification is None, (
        "fixture precondition: no triage has run on this context"
    )
    book = make_book_tool(ctx, config)
    with pytest.raises(BookError, match="without a prior triage"):
        book(
            reportedSymptom="engine noise",
            customerActionsTaken=["create-ticket"],
            narrative="test",
        )


def test_book_does_not_mutate_classification_when_refusing(
    ctx: SessionContext, config: Config
):
    """The refusal must leave the context clean.

    If book set a phantom classification on its way out, a later call in the
    same session would find the field populated and proceed — so the fix
    would hold for exactly one call and then fail open anyway.
    """
    book = make_book_tool(ctx, config)
    with pytest.raises(BookError):
        book(
            reportedSymptom="engine noise",
            customerActionsTaken=["create-ticket"],
            narrative="test",
        )
    assert ctx.triage_classification is None, (
        "book must not leave a phantom classification behind when it refuses"
    )
    assert ctx.triage_decision_id is None, (
        "book must not fabricate a triage_decision_id from the session id — "
        "that made the STAR envelope claim correlation with a triage "
        "decision that never happened"
    )


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


# ---------------------------------------------------------------------------
# F17.2: booking idempotency + priority override (2026-09-28)
# ---------------------------------------------------------------------------
#
# `issues/2026-09-28-p0-double-booking-auto-book-plus-model-book/`: a single P0
# session was generating two service-history rows because both the
# auto-triage P0 auto-book and Nova Sonic's own `book` tool call reached
# `call_book_endpoint`, and each minted a fresh `request_number`
# (`uuid4().hex[:4]`) plus a fresh `serviceId` (`uuid4().hex[:8]`).
#
# `issues/2026-09-27-driver-confirmed-p0-books-at-classifier-grade/`: the
# driver-confirmed P0 path books at whatever `ctx.triage_classification`
# holds — which is the classifier's P2/P3 grade, not the confirmed P0.
# `priority=` overrides that.
#
# These tests exercise `call_book_endpoint` directly (not `make_book_tool`)
# because the priority override is server-only — the @tool wrapper does not
# expose it, so Nova cannot set its own priority.

from agents.supervisor.tools.book import call_book_endpoint as _call_book_endpoint


class TestBookIdempotencyPerSessionAndPriority:
    def _ctx_with_triage(self, config: Config) -> SessionContext:
        c = SessionContext(
            tenant_id=config.tenant_id,
            session_id="vsa-sess-idempotency",
            jwt="fake-jwt",
            persona_id="fleet_driver",
            vin="1HGBH41JXMN000046",
        )
        c.triage_classification = "P0"
        c.triage_decision_id = "trd-idempotency-p0"
        return c

    def test_two_calls_same_priority_return_the_same_envelope(
        self, config: Config
    ) -> None:
        """The core idempotency property.

        Mutation: remove the `cached_by_priority.get(effective_priority)`
        short-circuit at the top of ``call_book_endpoint`` — this test
        FAILS because ``requestNumber`` is drawn from a fresh
        ``uuid4().hex[:4]`` on every call.
        """
        ctx = self._ctx_with_triage(config)
        first = _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="fire in engine bay",
            customer_actions_taken=["create-ticket"],
            narrative="P0 auto-escalation for fire.",
        )
        second = _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="different phrasing from Nova",
            customer_actions_taken=["create-ticket"],
            narrative="Nova's paraphrase of the same fault.",
        )
        assert first["requestNumber"] == second["requestNumber"], (
            "call_book_endpoint must be idempotent per (session, priority) "
            "so the auto-triage auto-book and Nova's own `book` call do "
            "not each mint a request_number "
            "(issues/2026-09-28-p0-double-booking-auto-book-plus-model-book/)"
        )
        assert first["messageId"] == second["messageId"]
        # Full envelope equality: cache returns the first call byte-identically.
        assert first == second, (
            "the cached replay must return the first envelope unchanged, "
            "including its serviceId in cmsServiceHistoryKey — otherwise a "
            "consumer that keys on serviceId sees two rows"
        )

    def test_the_second_call_did_not_write_a_second_service_history_row(
        self, config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DDB write is the actual defect surface (two rows in
        cms-*-storage-service-history). Verify the write function is
        invoked exactly once across two calls.

        Mutation: remove the idempotency check — this test FAILS because
        `_write_cms_service_history` runs twice.
        """
        from agents.supervisor.tools import book as book_mod

        calls: list[tuple[Any, ...]] = []

        def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return (False, None)  # CMS write disabled → draft-created

        monkeypatch.setattr(book_mod, "_write_cms_service_history", _spy)
        ctx = self._ctx_with_triage(config)
        _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="fire",
            customer_actions_taken=["create-ticket"],
            narrative="first",
        )
        _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="fire (Nova replay)",
            customer_actions_taken=["create-ticket"],
            narrative="second",
        )
        assert len(calls) == 1, (
            f"_write_cms_service_history must be called exactly once for "
            f"two calls at the same priority; got {len(calls)}"
        )

    def test_the_cache_key_scopes_by_priority(self, config: Config) -> None:
        """A legitimate escalation from a lower priority to P0 must not be
        blocked by the earlier lower-priority booking.

        Mutation: key the cache on `session_id` alone (drop priority from
        the key) — this test FAILS because the P0 call returns the P2
        envelope.
        """
        ctx = self._ctx_with_triage(config)
        # First, an initial booking at P2 (a legitimate lower-priority book).
        ctx.triage_classification = "P2"
        p2 = _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="rough idle at cold start",
            customer_actions_taken=["create-ticket"],
            narrative="P2 book.",
        )
        # Then an escalated P0 — separate priority → separate cache entry.
        ctx.triage_classification = "P0"
        p0 = _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="engine on fire",
            customer_actions_taken=["create-ticket"],
            narrative="P0 book.",
        )
        assert p2["requestNumber"] != p0["requestNumber"], (
            "cache must scope by priority; a P0 following a P2 in the same "
            "session must produce a new request_number"
        )
        assert (
            p2["envelope"]["payload"]["priority"] == "P2"
        ), p2["envelope"]["payload"]["priority"]
        assert (
            p0["envelope"]["payload"]["priority"] == "P0"
        ), p0["envelope"]["payload"]["priority"]

    def test_different_sessions_do_not_share_the_cache(
        self, config: Config
    ) -> None:
        """Anti-vacuity: two SessionContexts with distinct session_ids each
        get their own booking. The cache lives on ``ctx.booked_by_priority``,
        so a fresh ctx starts empty.
        """
        ctx_a = self._ctx_with_triage(config)
        ctx_a.session_id = "vsa-sess-A"
        ctx_b = self._ctx_with_triage(config)
        ctx_b.session_id = "vsa-sess-B"

        a = _call_book_endpoint(
            ctx_a, config, ctx_a.vin,
            reported_symptom="fire",
            customer_actions_taken=["create-ticket"],
            narrative="A",
        )
        b = _call_book_endpoint(
            ctx_b, config, ctx_b.vin,
            reported_symptom="fire",
            customer_actions_taken=["create-ticket"],
            narrative="B",
        )
        assert a["requestNumber"] != b["requestNumber"]

    def test_the_first_call_populates_the_cache_on_ctx(
        self, config: Config
    ) -> None:
        """Mutation: remove the ``cached_by_priority[effective_priority] =
        result`` assignment after the write — the cache never fills, and
        every subsequent call still mints a new request_number.
        """
        ctx = self._ctx_with_triage(config)
        assert ctx.booked_by_priority == {}
        result = _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="fire",
            customer_actions_taken=["create-ticket"],
            narrative="first",
        )
        assert "P0" in ctx.booked_by_priority
        assert ctx.booked_by_priority["P0"] == result


class TestBookPriorityOverride:
    def _ctx_at_p3(self, config: Config) -> SessionContext:
        """The setup for the driver-confirmed P0 path: classifier graded P3,
        so ``ctx.triage_classification`` is P3 even though the driver has
        now confirmed a P0 emergency.
        """
        c = SessionContext(
            tenant_id=config.tenant_id,
            session_id="vsa-sess-override",
            jwt="fake-jwt",
            persona_id="fleet_driver",
            vin="1HGBH41JXMN000046",
        )
        c.triage_classification = "P3"
        c.triage_decision_id = "trd-classifier-p3"
        return c

    def test_priority_override_wins_over_ctx_triage_classification(
        self, config: Config
    ) -> None:
        """The STAR payload and the row's ``triagePriority`` reflect the
        override, not the classifier's grade on ctx.

        This is the property `issues/2026-09-27-driver-confirmed-p0-books-
        at-classifier-grade/` needs: a driver-confirmed P0 with a
        classifier verdict of P3 must not book at P3.

        Mutation: change the internal reference from ``effective_priority``
        to ``ctx.triage_classification`` in ``_build_payload`` or
        ``_write_cms_service_history`` — this test FAILS.
        """
        ctx = self._ctx_at_p3(config)
        result = _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="my truck is on fire",
            customer_actions_taken=["create-ticket"],
            narrative="P0 driver-confirmed fire.",
            priority="P0",
        )
        payload = result["envelope"]["payload"]
        assert payload["priority"] == "P0", (
            "override must take precedence over ctx.triage_classification; "
            "otherwise a driver-confirmed P0 books at the classifier's "
            "lower grade (issues/2026-09-27-driver-confirmed-p0-books-at-"
            "classifier-grade/)"
        )
        # And the urgency / SLA reflect P0, not P3.
        assert payload["urgency"]["classification"] == "stop-driving"
        assert payload["urgency"]["safetyAssessment"]["safeToDrive"] == "no"

    def test_override_shares_the_idempotency_cache_at_the_override_priority(
        self, config: Config
    ) -> None:
        """A subsequent call with ``priority="P0"`` on the same ctx returns
        the first override's envelope; a call with no override (falls back
        to ``ctx.triage_classification="P3"``) is a separate cache entry.

        This is the property that makes the driver-confirmed P0 path
        compose cleanly with Fix 2: after the confirmed P0 books, Nova
        calling `book` (with no priority override) — which falls back to
        ``ctx.triage_classification="P3"`` — is a different cache slot and
        would mint a fresh P3 row. In practice the injection wording after
        the confirmed P0 also tells Nova not to call `book`, and the model
        typically respects "for this fault" scoping. This test pins the
        composition: the guard's cache scope is (session, effective_priority),
        so callers that want to defend against Nova's ambient P3 book must
        also pass an override or the confirmation-path injection.
        """
        ctx = self._ctx_at_p3(config)
        first = _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="fire",
            customer_actions_taken=["create-ticket"],
            narrative="confirmed P0",
            priority="P0",
        )
        # A second call at P0 replays the first.
        second = _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="fire",
            customer_actions_taken=["create-ticket"],
            narrative="Nova retry",
            priority="P0",
        )
        assert first["requestNumber"] == second["requestNumber"]
        # But a call at the ctx priority (P3, no override) is a different slot.
        p3_call = _call_book_endpoint(
            ctx, config, ctx.vin,
            reported_symptom="unrelated P3 concern",
            customer_actions_taken=["create-ticket"],
            narrative="separate P3",
        )
        assert p3_call["requestNumber"] != first["requestNumber"]
        assert p3_call["envelope"]["payload"]["priority"] == "P3"

    def test_override_still_requires_prior_triage(
        self, config: Config
    ) -> None:
        """The precondition-guard on `ctx.triage_classification is None` is
        still the enforcement point — passing ``priority="P0"`` does not
        bypass the "no triage, no booking" rule. This is the same guard
        `test_book_fails_closed_without_triage` above defends for the @tool
        path.

        Mutation: guard on ``effective_priority is None`` instead of
        ``ctx.triage_classification is None`` — this test FAILS because
        the guard would accept the call solely on the override.
        """
        ctx = SessionContext(
            tenant_id=config.tenant_id,
            session_id="vsa-sess-no-triage",
            jwt="fake-jwt",
            persona_id="fleet_driver",
            vin="1HGBH41JXMN000046",
        )
        # No triage_classification, no triage_decision_id on ctx.
        with pytest.raises(BookError, match="without a prior triage"):
            _call_book_endpoint(
                ctx, config, ctx.vin,
                reported_symptom="fire",
                customer_actions_taken=["create-ticket"],
                narrative="override without triage",
                priority="P0",
            )

    def test_tool_wrapper_does_not_expose_priority(
        self, config: Config
    ) -> None:
        """The @tool ``book`` (what Nova calls) must not let the model set
        priority. Mutation: expose ``priority`` on the @tool's schema — this
        test FAILS because inspecting the function signature would find
        the new field.
        """
        import inspect
        ctx = SessionContext(
            tenant_id=config.tenant_id, session_id="s", jwt="j",
            persona_id="fleet_driver", vin="1HGBH41JXMN000046",
        )
        book = make_book_tool(ctx, config)
        params = inspect.signature(book).parameters
        assert "priority" not in params, (
            "the @tool wrapper must not expose `priority` to Nova — the "
            "override is a server-only escape hatch"
        )
        assert set(params) == {
            "reportedSymptom", "customerActionsTaken", "narrative",
        }
