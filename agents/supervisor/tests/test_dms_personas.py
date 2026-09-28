"""Structural and unit tests for CVX DMS persona extension.

Spec: `.kiro/specs/2026-08-26-cvx-dms-personas-extension/spec.md`

Coverage:
  A. Persona registry wiring — all 5 DMS slugs in the 4 required dicts.
  B. Tool factory shape — each active tool returns the correct keys.
  C. Verbatim warranty verdict constraint — validate_claim narrates VERBATIM
     using the real DMS schema (verdict_status / verdict_reason /
     matched_rule_id, verified against
     ~/guidance-for-dealer-management-system-on-aws/source/handlers/warranty_claims.py).
  D. visit_brief Tier 2 contract — surfaces computed_at, returns explicit
     phrase when artifact absent, never recomputes.
  E. Stub tools — parts_intel and compliance_agent return the full deferral
     shape (available:False + reason with spec citation + persona key).
  F. No LLM inside tools — factories produce callables, not agent turns.
  G. Existing 7 personas still present — no regressions.
  H. Cert-gate coverage — below-Gold callers get the deterministic response;
     Gold/Platinum callers reach the DDB read.
  I. Prompt hygiene — active prompts contain the required placeholders and
     do not invite customer push (the R2c pull-only boundary at the prompt
     surface).
  J. PERSONA_TOOL_FACTORIES pruning — parts_intel and compliance_agent are
     bound to ONLY their stub factory; dm_copilot does not have triage.

All tests are pure structural / unit (no AWS calls, no Claude, no network).
The certification-tier cache is patched to return Gold by default so the
active-tool tests exercise the AI path; a dedicated group asserts the below-
Gold path.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9.sig"


def _make_ctx(persona_id: str = "warranty_agent",
              dealer_id: str = "DLR-0042",
              dealer_ids: list[str] | None = None,
              district_ids: list[str] | None = None) -> SimpleNamespace:
    """Fixture context.  ``dealer_ids`` / ``district_ids`` default to a
    one-entry list containing the caller's own dealer / a canonical district
    so happy-path scope checks pass; parametrised scope-check tests supply
    empty lists explicitly.
    """
    if dealer_ids is None:
        dealer_ids = [dealer_id] if dealer_id else []
    if district_ids is None:
        district_ids = ["DST-WEST-01"]
    return SimpleNamespace(
        jwt=_FAKE_JWT,
        session_id="vsa-sess-dmstest01",
        tenant_id="fleet",
        vin=None,
        persona_id=persona_id,
        dealer_id=dealer_id,
        dealer_ids=dealer_ids,
        district_ids=district_ids,
    )


def _make_config() -> SimpleNamespace:
    """Minimal Config-compatible stub (no real AWS needed)."""
    return SimpleNamespace(
        cms_api_base="https://cms.example.com",
        api_base="",
        adp_kb_id="",
        adp_region="us-east-1",
        adp_account_id="",
    )


def _mock_ddb_table(item: dict | None):
    """Return a mock DynamoDB Table serving the given item via get_item OR query.

    Both shapes are populated so this helper serves readers using either access
    pattern.  ``visit_brief`` reads via Query (newest-first, Limit=1) because
    ``dms-{stage}-dm-visit-briefs`` has a composite key and a point GetItem on
    the partition key alone raises ValidationException — see CVX
    ``issues/2026-09-19-dms-copilot-getitem-key-schema-mismatch/``.  The other
    tools still use get_item.
    """
    mock_resp = {"Item": item} if item is not None else {}
    mock_tbl = MagicMock()
    mock_tbl.get_item.return_value = mock_resp
    mock_tbl.query.return_value = {"Items": [item] if item is not None else []}
    return mock_tbl


@pytest.fixture
def _gold_tier(monkeypatch):
    """Patch the cert gate to Gold so active-tool tests reach DDB."""
    from agents.supervisor._lib import dms_cert_cache
    monkeypatch.setattr(
        dms_cert_cache,
        "check_dealer_certification",
        lambda dealer_id: ("Gold", "2026-08-29T00:00:00Z"),
    )
    # Also patch the copies imported into the tool modules.
    for module_name in (
        "agents.supervisor.tools.dms_warranty",
        "agents.supervisor.tools.dms_copilot",
        "agents.supervisor.tools.dms_comms",
    ):
        import importlib
        mod = importlib.import_module(module_name)
        monkeypatch.setattr(
            mod, "check_dealer_certification",
            lambda dealer_id: ("Gold", "2026-08-29T00:00:00Z"),
        )
    yield


# ---------------------------------------------------------------------------
# A. Persona registry wiring
# ---------------------------------------------------------------------------

_DMS_SLUGS = [
    "warranty_agent",
    "dm_copilot",
    "comms_triage",
    "parts_intel",
    "compliance_agent",
]

_V1_ACTIVE_SLUGS = ["warranty_agent", "dm_copilot", "comms_triage", "parts_intel"]
_V11_STUB_SLUGS = ["compliance_agent"]

_ALL_SLUGS = [
    "fleet_driver",
    "consumer",
    "retail_advisor",
    "vehicle_owner",
    "service_advisor",
    "technician",
    "fleet_manager",
] + _DMS_SLUGS


def test_all_12_slugs_in_persona_product_bindings():
    from agents.supervisor.persona_definitions import PERSONA_PRODUCT_BINDINGS
    for slug in _ALL_SLUGS:
        assert slug in PERSONA_PRODUCT_BINDINGS, (
            f"PERSONA_PRODUCT_BINDINGS missing '{slug}'"
        )


def test_all_12_slugs_in_persona_tenant_defaults():
    from agents.supervisor.persona_definitions import PERSONA_TENANT_DEFAULTS
    for slug in _ALL_SLUGS:
        assert slug in PERSONA_TENANT_DEFAULTS, (
            f"PERSONA_TENANT_DEFAULTS missing '{slug}'"
        )


def test_all_12_slugs_in_persona_prompts():
    from agents.supervisor.persona_definitions import PERSONA_PROMPTS
    for slug in _ALL_SLUGS:
        assert slug in PERSONA_PROMPTS, (
            f"PERSONA_PROMPTS missing '{slug}'"
        )


def test_all_12_slugs_in_persona_tool_factories():
    from agents.supervisor.tools import PERSONA_TOOL_FACTORIES
    for slug in _ALL_SLUGS:
        assert slug in PERSONA_TOOL_FACTORIES, (
            f"PERSONA_TOOL_FACTORIES missing '{slug}'"
        )


def test_dms_product_bindings_are_empty_lists():
    """All 5 DMS personas have empty product bindings at v1 (no LTM hydration)."""
    from agents.supervisor.persona_definitions import PERSONA_PRODUCT_BINDINGS
    for slug in _DMS_SLUGS:
        assert PERSONA_PRODUCT_BINDINGS[slug] == [], (
            f"Expected PERSONA_PRODUCT_BINDINGS['{slug}'] == [] "
            f"but got {PERSONA_PRODUCT_BINDINGS[slug]!r}"
        )


def test_dms_tenant_defaults_are_fleet():
    from agents.supervisor.persona_definitions import PERSONA_TENANT_DEFAULTS
    for slug in _DMS_SLUGS:
        assert PERSONA_TENANT_DEFAULTS[slug] == "fleet", (
            f"Expected PERSONA_TENANT_DEFAULTS['{slug}'] == 'fleet' "
            f"but got {PERSONA_TENANT_DEFAULTS[slug]!r}"
        )


def test_dms_prompts_contain_required_placeholders():
    from agents.supervisor.persona_definitions import PERSONA_PROMPTS
    for slug in _DMS_SLUGS:
        prompt = PERSONA_PROMPTS[slug]
        for placeholder in ("{tenant_display}", "{vin}", "{persona_id}"):
            assert placeholder in prompt, (
                f"PERSONA_PROMPTS['{slug}'] is missing placeholder {placeholder!r}"
            )


def test_dms_prompts_within_length_budget():
    from agents.supervisor.persona_definitions import PERSONA_PROMPTS
    for slug in _DMS_SLUGS:
        length = len(PERSONA_PROMPTS[slug])
        assert length <= 2500, (
            f"PERSONA_PROMPTS['{slug}'] is {length} chars, exceeds 2500-char budget"
        )


def test_existing_7_personas_untouched():
    """R7 regression control — none of the seven pre-existing personas were
    dropped when the five DMS personas were added.  Count matters: the DMS
    spec said six, but fleet_manager was added 2026-08-21."""
    from agents.supervisor.persona_definitions import PERSONA_PROMPTS
    existing = [
        "fleet_driver", "consumer", "retail_advisor", "vehicle_owner",
        "service_advisor", "technician", "fleet_manager",
    ]
    for slug in existing:
        assert slug in PERSONA_PROMPTS, (
            f"PERSONA_PROMPTS is missing pre-existing slug '{slug}' — regression"
        )


# ---------------------------------------------------------------------------
# B/C. warranty_agent — real DMS schema (verdict_status / verdict_reason / matched_rule_id)
# ---------------------------------------------------------------------------


def test_warranty_validate_returns_verbatim_verdict(_gold_tier):
    """validate_claim returns verdict_status, verdict_reason, matched_rule_id
    exactly as stored by DMS's rules engine.  Field names match the DMS-side
    writer at
    ~/guidance-for-dealer-management-system-on-aws/source/handlers/warranty_claims.py:438-450."""
    from agents.supervisor.tools.dms_warranty import make_warranty_validate_tool

    ctx = _make_ctx("warranty_agent")
    config = _make_config()
    item = {
        "claim_id": "WC-2026-001",
        "dealer_id": "DLR-0042",   # in-scope for _make_ctx's default caller
        "status": "Validated",  # DMS claim lifecycle — NOT the verdict
        "verdict_status": "denied",
        "verdict_reason": "labor_op_mismatch: RO labor op 999-99 not covered under powertrain warranty",
        "matched_rule_id": "R-LO-004",
        "updated_at": "2026-08-26T02:00:00Z",
    }

    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_warranty_validate_tool(ctx, config)
        result = tool_fn("WC-2026-001")

    assert result["available"] is True
    assert result["verdict_status"] == "denied"
    assert result["verdict_reason"] == item["verdict_reason"]  # byte-for-byte
    assert result["matched_rule_id"] == "R-LO-004"
    # Claim lifecycle state — separate from verdict.
    assert result["claim_state"] == "Validated"


def test_warranty_validate_approved_claim(_gold_tier):
    from agents.supervisor.tools.dms_warranty import make_warranty_validate_tool

    ctx = _make_ctx("warranty_agent")
    config = _make_config()
    item = {
        "claim_id": "WC-2026-002",
        "dealer_id": "DLR-0042",   # in-scope
        "status": "Validated",
        "verdict_status": "approved",
        "verdict_reason": "",  # per DMS Verdict shape — empty when approved
        "matched_rule_id": "R-COV-BASE",
        "amount": "450.00",
        "updated_at": "2026-08-26T02:05:00Z",
    }

    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_warranty_validate_tool(ctx, config)
        result = tool_fn("WC-2026-002")

    assert result["available"] is True
    assert result["verdict_status"] == "approved"
    assert result["amount"] == "450.00"


def test_warranty_validate_claim_not_found(_gold_tier):
    """Claim not found returns the SAME shape as out-of-scope so an
    authenticated caller cannot enumerate claim IDs by comparing responses.
    See security-review.md Cycle 2 Suggestion 1."""
    from agents.supervisor.tools.dms_warranty import make_warranty_validate_tool

    ctx = _make_ctx("warranty_agent")
    config = _make_config()

    mock_tbl = _mock_ddb_table(None)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_warranty_validate_tool(ctx, config)
        result = tool_fn("WC-MISSING")

    assert result["available"] is False
    assert "no access" in result["reason"].lower()


def test_warranty_suggest_returns_hints(_gold_tier):
    from agents.supervisor.tools.dms_warranty import make_warranty_suggest_tool

    ctx = _make_ctx("warranty_agent")
    config = _make_config()
    item = {
        "claim_id": "WC-2026-001",
        "dealer_id": "DLR-0042",   # in-scope
        "correction_hints": [
            "Submit OEM part number, not aftermarket equivalent",
            "Include labour time sheet signed by technician",
        ],
        "matched_rule_id": "R-LO-004",
        "updated_at": "2026-08-26T02:00:00Z",
    }

    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_warranty_suggest_tool(ctx, config)
        result = tool_fn("WC-2026-001")

    assert result["available"] is True
    assert isinstance(result["hints"], list)
    assert len(result["hints"]) == 2
    assert result["matched_rule_id"] == "R-LO-004"


def test_warranty_denied_verdict_not_softened(_gold_tier):
    """The tool MUST NOT transform the verdict.  This test protects the
    deterministic-seam boundary from a code change that runs the verdict
    through an LLM to "improve readability" — the seam is verbatim."""
    from agents.supervisor.tools.dms_warranty import make_warranty_validate_tool

    ctx = _make_ctx("warranty_agent")
    config = _make_config()
    raw_reason = (
        "mileage_exceeded: VIN not within powertrain warranty window — "
        "odometer 152,400 km exceeds 150,000 km limit"
    )
    item = {
        "claim_id": "WC-2026-003",
        "dealer_id": "DLR-0042",   # in-scope
        "status": "Validated",
        "verdict_status": "denied",
        "verdict_reason": raw_reason,
        "matched_rule_id": "R-MILE-006",
        "updated_at": "2026-08-26T03:00:00Z",
    }

    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_warranty_validate_tool(ctx, config)
        result = tool_fn("WC-2026-003")

    assert result["verdict_status"] == "denied"
    assert result["verdict_reason"] == raw_reason


# ---------------------------------------------------------------------------
# D. visit_brief Tier 2 contract
# ---------------------------------------------------------------------------


def test_visit_brief_surfaces_computed_at(_gold_tier):
    from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

    ctx = _make_ctx("dm_copilot")
    config = _make_config()
    item = {
        "dealer_id": "DLR-0042",
        "computed_at": "2026-08-26T02:00:00Z",
        "agent_version": "v1.0",
        "confidence": 0.87,
        "inputs_hash": "sha256:abc123",
        "evidence": [{"source": "athena://adp_dealer_domain", "rows_scanned": 1200}],
        "priority_items": ["CSI recovery plan for Q3", "Safety recall follow-up"],
        "open_concerns": ["Customer complaint: long wait times"],
        "recommendations": ["Conduct technician capacity review"],
    }

    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_visit_brief_tool(ctx, config)
        result = tool_fn("DLR-0042")

    assert result["available"] is True
    assert result["computed_at"] == "2026-08-26T02:00:00Z", (
        "visit_brief must surface computed_at — it is required for staleness narration"
    )
    # Full Tier-2 artifact contract fields per ~/.kiro/steering/agentic-tiers.md.
    for field in ("agent_version", "confidence", "inputs_hash", "evidence"):
        assert field in result, f"visit_brief must surface Tier-2 field {field!r}"
    assert isinstance(result["priority_items"], list)


def test_visit_brief_no_artifact_returns_explicit_phrase(_gold_tier):
    """Spec R4: no artifact → 'no brief computed yet', never fabricate.

    The requested dealer is IN scope (matches the caller's dealer_ids) but
    has no computed brief — so the tool clears both gates and lands on the
    honest deferral.  A missing dealer that's OUT of scope would return
    ``no_access_response`` instead; that's exercised in
    ``test_scope_check_dealer_out_of_scope``.
    """
    from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

    ctx = _make_ctx("dm_copilot", dealer_id="DLR-IN-SCOPE-42",
                    dealer_ids=["DLR-IN-SCOPE-42"])
    config = _make_config()

    mock_tbl = _mock_ddb_table(None)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_visit_brief_tool(ctx, config)
        result = tool_fn("DLR-IN-SCOPE-42")

    assert result["available"] is False
    assert result["reason"] == "no brief computed yet", (
        f"Expected exact phrase 'no brief computed yet' but got {result['reason']!r}"
    )


def test_visit_brief_does_not_call_llm(_gold_tier):
    """visit_brief returns a plain dict — no Agent, no invoke."""
    from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

    ctx = _make_ctx("dm_copilot")
    config = _make_config()
    item = {
        "dealer_id": "DLR-0042", "computed_at": "2026-08-26T02:00:00Z",
        "priority_items": [], "open_concerns": [], "recommendations": []
    }

    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_visit_brief_tool(ctx, config)
        result = tool_fn("DLR-0042")

    assert isinstance(result, dict), (
        "visit_brief must return a plain dict — not an Agent result object"
    )


def test_portfolio_summary_happy_path(_gold_tier):
    from agents.supervisor.tools.dms_copilot import make_portfolio_summary_tool

    ctx = _make_ctx("dm_copilot")
    config = _make_config()
    item = {
        "district_id": "DST-WEST-01",
        "computed_at": "2026-08-26T02:00:00Z",
        "dealer_count": 8,
        "top_performers": ["DLR-0001", "DLR-0005"],
        "needs_attention": ["DLR-0003"],
        "aggregate": {"avg_csi": 87.2, "warranty_approval_rate": 0.91},
    }

    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_portfolio_summary_tool(ctx, config)
        result = tool_fn("DST-WEST-01")

    assert result["available"] is True
    assert result["dealer_count"] == 8


def test_action_plan_happy_path(_gold_tier):
    from agents.supervisor.tools.dms_copilot import make_action_plan_tool

    ctx = _make_ctx("dm_copilot")
    config = _make_config()
    item = {
        "dealer_id": "DLR-0042",
        "updated_at": "2026-08-25T15:00:00Z",
        "actions": [{
            "action_id": "ACT-001",
            "title": "Hire 2 additional service technicians",
            "owner": "dealer_principal",
            "due_date": "2026-09-30",
            "status": "open",
            "priority": "high",
        }],
    }

    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_action_plan_tool(ctx, config)
        result = tool_fn("DLR-0042")

    assert result["available"] is True
    assert result["actions"][0]["status"] == "open"


# ---------------------------------------------------------------------------
# E. Stubs — full deferral shape (available:False + reason + persona)
# ---------------------------------------------------------------------------


def test_parts_intel_stub_returns_deferred():
    from agents.supervisor.tools.dms_stubs import make_parts_intel_stub_tool

    tool_fn = make_parts_intel_stub_tool(_make_ctx("parts_intel"), _make_config())
    inner = getattr(tool_fn, "func", tool_fn)
    result = inner()

    assert result["available"] is False
    assert "v1.1 deferred" in result["reason"]
    assert "2026-08-26-cvx-dms-personas-extension" in result["reason"]
    assert result["persona"] == "parts_intel"


def test_compliance_agent_stub_returns_deferred():
    from agents.supervisor.tools.dms_stubs import make_compliance_agent_stub_tool

    tool_fn = make_compliance_agent_stub_tool(_make_ctx("compliance_agent"), _make_config())
    inner = getattr(tool_fn, "func", tool_fn)
    result = inner()

    assert result["available"] is False
    assert "v1.1 deferred" in result["reason"]
    assert "2026-08-26-cvx-dms-personas-extension" in result["reason"]
    assert result["persona"] == "compliance_agent"


def test_stubs_do_not_raise_on_dispatch():
    """Every stub persona has ≥ 1 factory so dispatch can't KeyError."""
    from agents.supervisor.tools import PERSONA_TOOL_FACTORIES
    for slug in _V11_STUB_SLUGS:
        factories = PERSONA_TOOL_FACTORIES.get(slug)
        assert factories, (
            f"PERSONA_TOOL_FACTORIES['{slug}'] is empty — dispatch would KeyError"
        )


# ---------------------------------------------------------------------------
# J. Pruning per decisions.md 2026-09-03 (c)
# ---------------------------------------------------------------------------


def test_v11_stub_personas_have_only_stub_tools():
    """compliance_agent binds to EXACTLY its stub factory —
    no escalate, no retrieve, no partial-activation gap.

    AMENDED by spec 2026-09-10-dms-parts-intel (T3.2): parts_intel moved
    from v1.1-stub to v1-active; only compliance_agent remains as a stub persona.
    """
    from agents.supervisor.tools import (
        PERSONA_TOOL_FACTORIES,
        make_compliance_agent_stub_tool,
    )
    expected = {
        "compliance_agent": make_compliance_agent_stub_tool,
    }
    for slug, only_factory in expected.items():
        factories = PERSONA_TOOL_FACTORIES[slug]
        assert len(factories) == 1, (
            f"{slug} should have exactly one factory (the stub) "
            f"but has {len(factories)}: {[f.__name__ for f in factories]}"
        )
        assert factories[0] is only_factory, (
            f"{slug} bound to {factories[0].__name__}, expected {only_factory.__name__}"
        )


def test_dm_copilot_does_not_have_triage_tool():
    """decisions.md 2026-09-03 (c): dm_copilot loses make_triage_tool — a
    district manager triaging a vehicle concern is the service_advisor's job."""
    from agents.supervisor.tools import PERSONA_TOOL_FACTORIES, make_triage_tool
    assert make_triage_tool not in PERSONA_TOOL_FACTORIES["dm_copilot"], (
        "make_triage_tool must not be in dm_copilot's factories — see "
        "decisions.md 2026-09-03 (c)"
    )


def test_v1_active_personas_have_real_tool_factories():
    """Active-v1 personas carry their DMS tool factories AND retrieve/escalate;
    they are NOT bound to the stub factories."""
    from agents.supervisor.tools import (
        PERSONA_TOOL_FACTORIES,
        make_parts_intel_stub_tool,
        make_compliance_agent_stub_tool,
    )
    stub_ids = {id(make_parts_intel_stub_tool), id(make_compliance_agent_stub_tool)}
    for slug in _V1_ACTIVE_SLUGS:
        factories = PERSONA_TOOL_FACTORIES[slug]
        assert factories, f"{slug} has no factories"
        assert not (set(map(id, factories)) & stub_ids), (
            f"active v1 persona {slug} bound to a stub factory — quiet-activation bug"
        )


# ---------------------------------------------------------------------------
# F. comms_triage — classify_bulletin shape
# ---------------------------------------------------------------------------


def test_classify_bulletin_happy_path(_gold_tier):
    from agents.supervisor.tools.dms_comms import make_classify_bulletin_tool

    ctx = _make_ctx("comms_triage")
    config = _make_config()
    item = {
        "bulletin_id": "BUL-2026-001",
        "category": "recall",
        "urgency": "immediate",
        "audience": ["service_advisor", "technician"],
        "summary": "Brake actuator recall — all 2024 model year vehicles",
        "action_required": True,
        "computed_at": "2026-08-26T04:00:00Z",
    }

    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_classify_bulletin_tool(ctx, config)
        result = tool_fn("BUL-2026-001")

    assert result["available"] is True
    assert result["category"] == "recall"
    assert result["urgency"] == "immediate"


def test_classify_bulletin_not_found(_gold_tier):
    """Enumeration-safe: not-found and no-access share the same shape.
    See security-review.md Cycle 2 Suggestion 1."""
    from agents.supervisor.tools.dms_comms import make_classify_bulletin_tool

    ctx = _make_ctx("comms_triage")
    config = _make_config()
    mock_tbl = _mock_ddb_table(None)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_classify_bulletin_tool(ctx, config)
        result = tool_fn("BUL-MISSING")

    assert result["available"] is False
    assert "no access" in result["reason"].lower()


def test_classify_bulletin_not_yet_classified(_gold_tier):
    """An item without a Tier-2 classification returns the honest 'not yet
    classified' phrase — the tool MUST NOT synthesise a client-side
    classification, which would silently move Tier 2 work to Tier 1."""
    from agents.supervisor.tools.dms_comms import make_classify_bulletin_tool

    ctx = _make_ctx("comms_triage")
    config = _make_config()
    # Record exists but the classifier hasn't run.
    item = {"bulletin_id": "BUL-NEW-9", "arrived_at": "2026-08-30T00:00:00Z"}
    mock_tbl = _mock_ddb_table(item)
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_tbl
        tool_fn = make_classify_bulletin_tool(ctx, config)
        result = tool_fn("BUL-NEW-9")

    assert result["available"] is False
    assert result["reason"] == "bulletin not yet classified"


# ---------------------------------------------------------------------------
# H. Certification-tier gate — below Gold gets the deterministic response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["Bronze", "Silver", "UNKNOWN"])
def test_below_gold_warranty_returns_deterministic(tier, monkeypatch):
    """Below-Gold callers to validate_claim never reach DDB — they get the
    fixed deterministic response.  DDB mocks assert un-called."""
    from agents.supervisor.tools import dms_warranty
    monkeypatch.setattr(
        dms_warranty, "check_dealer_certification",
        lambda dealer_id: (tier, "2026-08-29T00:00:00Z"),
    )

    with patch("boto3.resource") as mock_resource:
        tool_fn = dms_warranty.make_warranty_validate_tool(
            _make_ctx("warranty_agent"), _make_config()
        )
        result = tool_fn("WC-2026-001")

    assert result["available"] is False
    assert "Gold or Platinum" in result["reason"]
    assert result["tier"] == tier
    mock_resource.assert_not_called()


@pytest.mark.parametrize("tier", ["Bronze", "Silver", "UNKNOWN"])
def test_below_gold_visit_brief_returns_deterministic(tier, monkeypatch):
    from agents.supervisor.tools import dms_copilot
    monkeypatch.setattr(
        dms_copilot, "check_dealer_certification",
        lambda dealer_id: (tier, None),
    )

    with patch("boto3.resource") as mock_resource:
        tool_fn = dms_copilot.make_visit_brief_tool(
            _make_ctx("dm_copilot"), _make_config()
        )
        result = tool_fn("DLR-0042")

    assert result["available"] is False
    assert result["tier"] == tier
    mock_resource.assert_not_called()


@pytest.mark.parametrize("tier", ["Bronze", "Silver", "UNKNOWN"])
def test_below_gold_classify_bulletin_returns_deterministic(tier, monkeypatch):
    from agents.supervisor.tools import dms_comms
    monkeypatch.setattr(
        dms_comms, "check_dealer_certification",
        lambda dealer_id: (tier, None),
    )

    with patch("boto3.resource") as mock_resource:
        tool_fn = dms_comms.make_classify_bulletin_tool(
            _make_ctx("comms_triage"), _make_config()
        )
        result = tool_fn("BUL-2026-001")

    assert result["available"] is False
    assert result["tier"] == tier
    mock_resource.assert_not_called()


# ---------------------------------------------------------------------------
# I. Prompt hygiene — no customer-push language, R2c enforcement at prompt surface
# ---------------------------------------------------------------------------


def test_prompt_does_not_imply_customer_push():
    """For each active DMS prompt, no phrase invites the LLM to reach a
    customer directly.  The R2c pull-only boundary can only be honored if
    the prompt doesn't invite crossing it."""
    from agents.supervisor.persona_definitions import PERSONA_PROMPTS
    forbidden = (
        "send a text",
        "text the customer",
        "email the customer",
        "call the customer",
        "outbound call",
        "reach out to the customer",
    )
    for slug in _V1_ACTIVE_SLUGS:
        body = PERSONA_PROMPTS[slug].lower()
        for needle in forbidden:
            assert needle not in body, (
                f"{slug} prompt contains {needle!r} — R2c pull-only violation"
            )


# ---------------------------------------------------------------------------
# G. No-LLM contract — factories produce callables, return plain dicts
# ---------------------------------------------------------------------------


def test_all_dms_tool_factories_return_callables():
    from agents.supervisor.tools.dms_warranty import (
        make_warranty_validate_tool, make_warranty_suggest_tool)
    from agents.supervisor.tools.dms_copilot import (
        make_visit_brief_tool, make_portfolio_summary_tool, make_action_plan_tool)
    from agents.supervisor.tools.dms_comms import make_classify_bulletin_tool
    from agents.supervisor.tools.dms_stubs import (
        make_parts_intel_stub_tool, make_compliance_agent_stub_tool)

    ctx = _make_ctx()
    config = _make_config()

    factories = [
        make_warranty_validate_tool, make_warranty_suggest_tool,
        make_visit_brief_tool, make_portfolio_summary_tool, make_action_plan_tool,
        make_classify_bulletin_tool,
        make_parts_intel_stub_tool, make_compliance_agent_stub_tool,
    ]
    for factory in factories:
        result = factory(ctx, config)
        assert callable(result), (
            f"{factory.__name__} did not return a callable — got {type(result)}"
        )




# ---------------------------------------------------------------------------
# K. Caller-to-target authorization — spec § Constraints ("agent does not
# widen scope").  Added 2026-09-04 in Fix Group 2 to address
# security-review.md Cycle 1 Warning 2.
# ---------------------------------------------------------------------------


class TestCallerToTargetScope:
    """Each of the six DMS tools MUST refuse to disclose data for a target
    that is not in the caller's Cognito claim set.  All six tests here
    parametrise across active-v1 tools and assert:
      * response shape is ``{"available": False, "reason": "you have no access to that dealer"}``
      * DDB is not read (for the four tools that can gate pre-read)
      * for warranty tools that must read to discover the item's dealer_id,
        DDB IS read but the return is still no-access-shaped (not a leak).
    """

    def _out_of_scope_ctx(self, persona_id: str) -> SimpleNamespace:
        """Session context whose dealer_ids and district_ids do NOT match
        the target the tool will be called with."""
        return SimpleNamespace(
            jwt=_FAKE_JWT,
            session_id="vsa-sess-oos",
            tenant_id="fleet",
            vin=None,
            persona_id=persona_id,
            dealer_id="DLR-CALLER-1",
            dealer_ids=["DLR-CALLER-1"],       # NOT the target
            district_ids=["DST-CALLER"],       # NOT the target
        )

    def test_visit_brief_dealer_out_of_scope_refuses_pre_read(self, _gold_tier):
        """visit_brief refuses out-of-scope targets BEFORE reading DDB."""
        from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

        with patch("boto3.resource") as mock_resource:
            tool_fn = make_visit_brief_tool(
                self._out_of_scope_ctx("dm_copilot"), _make_config()
            )
            result = tool_fn("DLR-B")  # not in dealer_ids

        assert result["available"] is False
        assert "no access" in result["reason"].lower()
        mock_resource.assert_not_called()

    def test_action_plan_dealer_out_of_scope_refuses_pre_read(self, _gold_tier):
        from agents.supervisor.tools.dms_copilot import make_action_plan_tool

        with patch("boto3.resource") as mock_resource:
            tool_fn = make_action_plan_tool(
                self._out_of_scope_ctx("dm_copilot"), _make_config()
            )
            result = tool_fn("DLR-B")

        assert result["available"] is False
        assert "no access" in result["reason"].lower()
        mock_resource.assert_not_called()

    def test_portfolio_summary_district_out_of_scope_refuses_pre_read(self, _gold_tier):
        """portfolio_summary refuses out-of-scope districts pre-read."""
        from agents.supervisor.tools.dms_copilot import make_portfolio_summary_tool

        with patch("boto3.resource") as mock_resource:
            tool_fn = make_portfolio_summary_tool(
                self._out_of_scope_ctx("dm_copilot"), _make_config()
            )
            result = tool_fn("DST-B")  # not in district_ids

        assert result["available"] is False
        assert "no access" in result["reason"].lower()
        mock_resource.assert_not_called()

    def test_warranty_validate_claim_dealer_id_out_of_scope_refuses_post_read(
        self, _gold_tier
    ):
        """warranty.validate_claim must read the claim to see its dealer_id,
        then refuse if the claim's dealer isn't in the caller's scope.
        The DDB call happens; the response shape is still no-access."""
        from agents.supervisor.tools.dms_warranty import make_warranty_validate_tool

        item = {
            "claim_id": "WC-OOS-1",
            "dealer_id": "DLR-B",          # OUT of caller's scope
            "verdict_status": "denied",
            "verdict_reason": "labor_op_mismatch",
            "matched_rule_id": "R-LO-004",
        }
        mock_tbl = _mock_ddb_table(item)
        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_tbl
            tool_fn = make_warranty_validate_tool(
                self._out_of_scope_ctx("warranty_agent"), _make_config()
            )
            result = tool_fn("WC-OOS-1")

        assert result["available"] is False
        assert "no access" in result["reason"].lower()
        # The verdict fields MUST NOT appear — the whole point is no leak.
        assert "verdict_status" not in result
        assert "verdict_reason" not in result

    def test_warranty_suggest_dealer_id_out_of_scope_refuses_post_read(
        self, _gold_tier
    ):
        from agents.supervisor.tools.dms_warranty import make_warranty_suggest_tool

        item = {
            "claim_id": "WC-OOS-2",
            "dealer_id": "DLR-B",
            "correction_hints": ["hint-1", "hint-2"],
            "matched_rule_id": "R-LO-004",
        }
        mock_tbl = _mock_ddb_table(item)
        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_tbl
            tool_fn = make_warranty_suggest_tool(
                self._out_of_scope_ctx("warranty_agent"), _make_config()
            )
            result = tool_fn("WC-OOS-2")

        assert result["available"] is False
        assert "no access" in result["reason"].lower()
        assert "hints" not in result

    def test_classify_bulletin_dealer_id_out_of_scope_refuses_post_read(
        self, _gold_tier
    ):
        """A bulletin addressed to another dealer is out-of-scope."""
        from agents.supervisor.tools.dms_comms import make_classify_bulletin_tool

        item = {
            "bulletin_id": "BUL-OOS-1",
            "dealer_id": "DLR-B",           # addressed to a different dealer
            "category": "service-campaign",
            "urgency": "informational",
            "audience": ["service_advisor"],
            "summary": "internal — should not leak",
            "action_required": False,
        }
        mock_tbl = _mock_ddb_table(item)
        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_tbl
            tool_fn = make_classify_bulletin_tool(
                self._out_of_scope_ctx("comms_triage"), _make_config()
            )
            result = tool_fn("BUL-OOS-1")

        assert result["available"] is False
        assert "no access" in result["reason"].lower()
        assert "summary" not in result

    def test_classify_bulletin_broadcast_reaches_authenticated_dealer(
        self, _gold_tier
    ):
        """A broadcast bulletin (empty dealer_id) reaches any authenticated
        caller with a non-empty dealer_ids claim set — recall / campaign
        distribution has to work."""
        from agents.supervisor.tools.dms_comms import make_classify_bulletin_tool

        item = {
            "bulletin_id": "BUL-BROADCAST-1",
            "dealer_id": "",                # broadcast — no specific dealer
            "category": "recall",
            "urgency": "immediate",
            "audience": ["service_advisor", "technician"],
            "summary": "safety recall, all model years",
            "action_required": True,
        }
        mock_tbl = _mock_ddb_table(item)
        # Caller HAS a dealer id, just not the (empty) one on the bulletin.
        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_tbl
            tool_fn = make_classify_bulletin_tool(
                self._out_of_scope_ctx("comms_triage"), _make_config()
            )
            result = tool_fn("BUL-BROADCAST-1")

        assert result["available"] is True
        assert result["category"] == "recall"

    def test_dealer_ids_empty_refuses_everything(self, _gold_tier):
        """The current SessionContext-empty reality: no dealer claims → every
        DMS tool refuses.  This is the fail-closed default that hardens the
        session shape before Cognito plumbing lands."""
        from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

        ctx = SimpleNamespace(
            jwt=_FAKE_JWT, session_id="vsa-sess-empty",
            tenant_id="fleet", vin=None, persona_id="dm_copilot",
            dealer_id="DLR-CALLER", dealer_ids=[], district_ids=[],
        )
        with patch("boto3.resource") as mock_resource:
            tool_fn = make_visit_brief_tool(ctx, _make_config())
            result = tool_fn("DLR-ANY")

        assert result["available"] is False
        assert "no access" in result["reason"].lower()
        mock_resource.assert_not_called()


# ---------------------------------------------------------------------------
# L. Generic DDB error response — no exception string leakage
# (security-review.md Cycle 1 Suggestion 2)
# ---------------------------------------------------------------------------


class TestNoExceptionStringLeakage:
    """When the DDB read raises, the tool response MUST NOT embed the raw
    boto3 exception message — those can carry table ARNs (account + region
    + resource) and IAM policy names.  Log the diagnostic detail; return a
    generic message that the LLM can safely narrate."""

    def _mk_client_error(self):
        return ClientError(
            error_response={
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "Requested resource not found: "
                               "arn:aws:dynamodb:us-west-2:999999999999:table/dms-secret-name",
                }
            },
            operation_name="GetItem",
        )

    def test_warranty_ddb_error_message_generic(self, _gold_tier):
        from agents.supervisor.tools.dms_warranty import make_warranty_validate_tool

        mock_tbl = MagicMock()
        mock_tbl.get_item.side_effect = self._mk_client_error()
        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_tbl
            tool_fn = make_warranty_validate_tool(_make_ctx("warranty_agent"), _make_config())
            result = tool_fn("WC-2026-001")

        assert result["available"] is False
        # NO ARN, no account number, no boto3 exception text.
        assert "arn:aws:" not in result["reason"]
        assert "999999999999" not in result["reason"]
        assert result["reason"] == "DMS table temporarily unreachable"

    def test_visit_brief_ddb_error_message_generic(self, _gold_tier):
        from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

        mock_tbl = MagicMock()
        # visit_brief reads via Query, not GetItem — the brief tables have
        # composite keys. See CVX
        # issues/2026-09-19-dms-copilot-getitem-key-schema-mismatch/.
        mock_tbl.query.side_effect = self._mk_client_error()
        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_tbl
            tool_fn = make_visit_brief_tool(_make_ctx("dm_copilot"), _make_config())
            result = tool_fn("DLR-0042")

        assert result["available"] is False
        assert "arn:aws:" not in result["reason"]
        assert result["reason"] == "DMS table temporarily unreachable"

    def test_classify_bulletin_ddb_error_message_generic(self, _gold_tier):
        from agents.supervisor.tools.dms_comms import make_classify_bulletin_tool

        mock_tbl = MagicMock()
        mock_tbl.get_item.side_effect = self._mk_client_error()
        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_tbl
            tool_fn = make_classify_bulletin_tool(_make_ctx("comms_triage"), _make_config())
            result = tool_fn("BUL-2026-001")

        assert result["available"] is False
        assert "arn:aws:" not in result["reason"]
        assert result["reason"] == "DMS table temporarily unreachable"


# ---------------------------------------------------------------------------
# M. Table-name drift regression (security-review.md Cycle 1 Suggestion 1)
# ---------------------------------------------------------------------------


def test_dms_copilot_uses_authoritative_table_names():
    """Portfolio surface reads dms-{stage}-dealer-performance (the DMS-side
    authoritative name — verified against dms_storage_stack.py:170), NOT
    the invented 'dm-portfolio'."""
    from agents.supervisor.tools import dms_copilot as m
    assert "dealer-performance" in m._PORTFOLIO_TABLE_TPL, (
        f"portfolio_summary reads {m._PORTFOLIO_TABLE_TPL!r} — DMS storage "
        "creates 'dms-{stage}-dealer-performance', not 'dm-portfolio'."
    )


def test_dms_comms_uses_authoritative_table_names():
    """Bulletin surface reads dms-{stage}-oem-communications (per
    dms_storage_stack.py:178), NOT the invented 'comms-bulletins'."""
    from agents.supervisor.tools import dms_comms as m
    assert "oem-communications" in m._BULLETINS_TABLE_TPL, (
        f"classify_bulletin reads {m._BULLETINS_TABLE_TPL!r} — DMS storage "
        "creates 'dms-{stage}-oem-communications', not 'comms-bulletins'."
    )




# ---------------------------------------------------------------------------
# N. Enumeration-safety — post-read tools cannot distinguish "not found"
# from "no access" by response shape (security-review Cycle 2 Suggestion 1)
# ---------------------------------------------------------------------------


class TestEnumerationSafetyPostRead:
    """The three post-read tools (validate_claim, suggest_correction,
    classify_bulletin) MUST return the SAME response shape whether the
    target does not exist OR the caller is not authorized for it —
    otherwise a Gold-tier caller can enumerate target IDs by comparing
    reason strings even though no content leaks."""

    def _oos_ctx(self, persona_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            jwt=_FAKE_JWT, session_id="vsa-sess-enum",
            tenant_id="fleet", vin=None, persona_id=persona_id,
            dealer_id="DLR-CALLER", dealer_ids=["DLR-CALLER"],
            district_ids=["DST-CALLER"],
        )

    def test_warranty_not_found_shape_equals_no_access_shape(self, _gold_tier):
        from agents.supervisor.tools.dms_warranty import make_warranty_validate_tool

        # not-found path
        with patch("boto3.resource") as r_nf:
            r_nf.return_value.Table.return_value = _mock_ddb_table(None)
            tool_fn = make_warranty_validate_tool(
                self._oos_ctx("warranty_agent"), _make_config()
            )
            not_found = tool_fn("WC-DOES-NOT-EXIST")

        # no-access path (item exists but dealer_id is out of scope)
        with patch("boto3.resource") as r_oos:
            item = {"claim_id": "WC-EXISTS-1", "dealer_id": "DLR-B",
                    "verdict_status": "denied", "verdict_reason": "hidden"}
            r_oos.return_value.Table.return_value = _mock_ddb_table(item)
            tool_fn = make_warranty_validate_tool(
                self._oos_ctx("warranty_agent"), _make_config()
            )
            no_access = tool_fn("WC-EXISTS-1")

        # Reason strings identical — enumeration is impossible.
        assert not_found["reason"] == no_access["reason"], (
            f"validate_claim: not-found ({not_found['reason']!r}) vs "
            f"no-access ({no_access['reason']!r}) reason strings differ — "
            "an authenticated caller can enumerate claim IDs"
        )
        assert not_found["available"] is False
        assert no_access["available"] is False

    def test_suggest_correction_not_found_shape_equals_no_access_shape(self, _gold_tier):
        from agents.supervisor.tools.dms_warranty import make_warranty_suggest_tool

        with patch("boto3.resource") as r_nf:
            r_nf.return_value.Table.return_value = _mock_ddb_table(None)
            tool_fn = make_warranty_suggest_tool(
                self._oos_ctx("warranty_agent"), _make_config()
            )
            not_found = tool_fn("WC-DOES-NOT-EXIST")

        with patch("boto3.resource") as r_oos:
            item = {"claim_id": "WC-EXISTS-2", "dealer_id": "DLR-B",
                    "correction_hints": ["hidden-1"]}
            r_oos.return_value.Table.return_value = _mock_ddb_table(item)
            tool_fn = make_warranty_suggest_tool(
                self._oos_ctx("warranty_agent"), _make_config()
            )
            no_access = tool_fn("WC-EXISTS-2")

        assert not_found["reason"] == no_access["reason"], (
            "suggest_correction: enumeration primitive via reason-string drift"
        )

    def test_classify_bulletin_not_found_shape_equals_no_access_shape(self, _gold_tier):
        from agents.supervisor.tools.dms_comms import make_classify_bulletin_tool

        with patch("boto3.resource") as r_nf:
            r_nf.return_value.Table.return_value = _mock_ddb_table(None)
            tool_fn = make_classify_bulletin_tool(
                self._oos_ctx("comms_triage"), _make_config()
            )
            not_found = tool_fn("BUL-DOES-NOT-EXIST")

        with patch("boto3.resource") as r_oos:
            item = {"bulletin_id": "BUL-EXISTS-1", "dealer_id": "DLR-B",
                    "category": "hidden", "summary": "hidden"}
            r_oos.return_value.Table.return_value = _mock_ddb_table(item)
            tool_fn = make_classify_bulletin_tool(
                self._oos_ctx("comms_triage"), _make_config()
            )
            no_access = tool_fn("BUL-EXISTS-1")

        assert not_found["reason"] == no_access["reason"], (
            "classify_bulletin: enumeration primitive via reason-string drift"
        )


# ---------------------------------------------------------------------------
# H. AVX Spec 1a Group 6 — persona-prompt content pins (mutation-verified)
#
# Added 2026-09-19 (AVX Spec 1a Group 6 T6.1/T6.3). Pre-existing coverage
# tested the tool-boundary data pass-through but not the prompts' verbatim /
# staleness clauses that make the LLM narration seam safe. Softening these
# clauses passed 55/55 tests before these pins were added — the failure
# mode ~/.kiro/steering/testing.md § "Mutation testing at the green
# boundary" rule 5 exists to catch. Each pin below is exact-literal;
# any weakening of the wording breaks the property.
# ---------------------------------------------------------------------------


def test_warranty_agent_prompt_pins_verbatim_denied_clause():
    """T6.1 safety/legal-critical guard.

    WARRANTY_AGENT_PROMPT MUST instruct the LLM to preserve the 'denied'
    verdict verbatim. A softened prompt (paraphrase 'denied' to any hedged
    variant) invalidates the deterministic-seam guarantee at the
    tool -> LLM narration boundary — see
    ~/.kiro/steering/agentic-tiers.md § "Determinism at the seam".
    """
    from agents.supervisor.persona_definitions import WARRANTY_AGENT_PROMPT
    assert "never soften 'denied'" in WARRANTY_AGENT_PROMPT, (
        "WARRANTY_AGENT_PROMPT lost its 'never soften denied' clause — "
        "warranty verdicts could be softened at the LLM narration seam"
    )
    assert "VERBATIM" in WARRANTY_AGENT_PROMPT, (
        "WARRANTY_AGENT_PROMPT lost its VERBATIM directive"
    )


def test_dm_copilot_prompt_pins_computed_at_disclosure():
    """T6.2 deterministic-seam guard (Tier 2 artifact staleness).

    DM_COPILOT_PROMPT MUST require the LLM to surface `computed_at` on every
    citation of a score/forecast/recommendation. Dropping this clause hides
    Tier 2 artifact staleness from the district manager — the exact
    correctness bug ~/.kiro/steering/agentic-tiers.md § "The Tier 2
    artifact contract" ("staleness is surfaced, not hidden") exists to
    prevent.
    """
    from agents.supervisor.persona_definitions import DM_COPILOT_PROMPT
    assert "ALWAYS include the `computed_at` timestamp" in DM_COPILOT_PROMPT, (
        "DM_COPILOT_PROMPT lost its computed_at disclosure clause — "
        "Tier 2 staleness would be hidden from the district manager"
    )


def test_comms_triage_prompt_pins_no_invented_actions_clause():
    """T6.3 classifier-verbatim guard.

    COMMS_TRIAGE_PROMPT MUST forbid the LLM from inventing action items
    beyond what the deterministic classifier returned. Softening this
    clause turns the persona from a narrator of a deterministic seam into
    an LLM that decides categories — the boundary
    ~/.kiro/steering/agentic-tiers.md § "The data platform predicts, the
    agent reasons" exists to preserve.
    """
    from agents.supervisor.persona_definitions import COMMS_TRIAGE_PROMPT
    assert "Do NOT invent action items" in COMMS_TRIAGE_PROMPT, (
        "COMMS_TRIAGE_PROMPT lost its 'do not invent action items' clause — "
        "the LLM could now expand action items beyond the classifier's output"
    )
