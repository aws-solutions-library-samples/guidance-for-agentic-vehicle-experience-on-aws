"""Tests for the five CVX Acquire tool factories.

Spec: connected-mobility-guidance-on-aws/.kiro/specs/
      2026-08-04-cvx-oem-discover-order-meridian/ § Group 2 (merged from predecessor 2026-07-21)

Covers:
  - configurator_recommend  (≥3 tests)
  - variant_compare         (≥3 tests) — renamed from configurator_compare per
                              spec 2026-08-04-cvx-oem-discover-order-meridian (umbrella PRD Q3)
  - delivery_status         (≥3 tests — critical: tenant isolation)
  - manufacturing_explain   (≥3 tests)
  - resolve_or_create_owner_identity (≥3 tests)

All DDB and bedrock-agent-runtime calls are mocked so no AWS credentials
or live infrastructure are required.

Run: pytest agents/supervisor/tests/test_acquire_tools.py -v
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_config(
    catalog_table: str = "vsa-staging-acquire-catalog",
    orders_table: str = "vsa-staging-acquire-orders",
    kb_id: str = "test-kb-id",
    region: str = "us-east-1",
    kb_source_category: str = "vehicle_manufacturing",
) -> SimpleNamespace:
    """Minimal config stub matching the Config dataclass fields the tools read."""
    return SimpleNamespace(
        acquire_catalog_table=catalog_table,
        acquire_orders_table=orders_table,
        adp_kb_id=kb_id,
        adp_region=region,
        acquire_kb_source_category=kb_source_category,
    )


def _make_ctx(tenant_id: str = "genericmoto-demo") -> SimpleNamespace:
    """Minimal session context stub."""
    return SimpleNamespace(
        tenant_id=tenant_id,
        session_id="test-session-001",
        persona_id="consumer",
    )


def _make_model_item(
    tenant_id: str,
    category_id: str,
    model_id: str,
    display_name: str,
    base_price: Decimal,
    tagline: str = "",
    specs: dict | None = None,
) -> dict:
    return {
        "tenantId": tenant_id,
        "itemKey": f"MODEL#{category_id}#{model_id}",
        "categoryKey": f"CAT#{category_id}",
        "displayName": display_name,
        "tagline": tagline,
        "basePrice": base_price,
        "specs": specs or {},
    }


def _make_variant_item(
    tenant_id: str,
    category_id: str,
    model_id: str,
    variant_id: str,
    display_name: str,
    price_delta: Decimal,
) -> dict:
    return {
        "tenantId": tenant_id,
        "itemKey": f"VAR#{category_id}#{model_id}#{variant_id}",
        "displayName": display_name,
        "priceDelta": price_delta,
    }


def _make_order_item(
    order_id: str,
    tenant_id: str,
    stage: str,
    next_transition_at: str | None = "2026-07-25T00:00:00Z",
    plant_id: str = "plant-a",
    dealer_ref: str = "dealer-01",
    stage_history: list | None = None,
) -> dict:
    item = {
        "orderId": order_id,
        "tenantId": tenant_id,
        "stage": stage,
        "plantId": plant_id,
        "dealerRef": dealer_ref,
        "stageHistory": stage_history or [{"stage": stage, "enteredAt": "2026-07-21T15:01:00Z"}],
    }
    if next_transition_at is not None:
        item["nextTransitionAt"] = next_transition_at
    return item


# ---------------------------------------------------------------------------
# configurator_recommend
# ---------------------------------------------------------------------------


class TestConfiguratorRecommend:
    """Tests for make_configurator_recommend_tool."""

    def _make_tool(self, config=None, ctx=None):
        from agents.supervisor.tools.configurator_recommend import make_configurator_recommend_tool
        return make_configurator_recommend_tool(ctx or _make_ctx(), config or _make_config())

    def test_happy_path_returns_up_to_3_recommendations(self):
        """Happy path: catalog has models; tool returns ≤3 ranked results."""
        ctx = _make_ctx()
        tenant = ctx.tenant_id
        config = _make_config()

        model_items = [
            _make_model_item(tenant, "commuter", "meridian-300", "Meridian 300",
                             Decimal("4599"), "The everyday commuter.", {"power_hp": 24}),
            _make_model_item(tenant, "commuter", "meridian-500", "Meridian 500",
                             Decimal("6299"), "Urban agility.", {"power_hp": 35}),
            _make_model_item(tenant, "adventure", "solstice-adv", "Solstice ADV",
                             Decimal("7999"), "Adventure touring.", {"power_hp": 48}),
        ]

        mock_table = MagicMock()
        # First query = by-category GSI (returns category items)
        mock_table.query.side_effect = [
            {"Items": [{"itemKey": "CAT#commuter"}, {"itemKey": "CAT#adventure"}]},
            # Second query = MODEL# scan
            {"Items": model_items},
        ]

        with patch("agents.supervisor.tools.configurator_recommend._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(useCase="city commuting")

        assert "recommendations" in result
        recs = result["recommendations"]
        assert len(recs) <= 3
        assert all("modelId" in r for r in recs)
        assert all("displayName" in r for r in recs)
        assert all("basePrice" in r for r in recs)
        assert all("matchReasoning" in r for r in recs)

    def test_budget_filter_excludes_over_budget_models(self):
        """Models priced above budgetMax are excluded from recommendations."""
        ctx = _make_ctx()
        tenant = ctx.tenant_id
        config = _make_config()

        model_items = [
            _make_model_item(tenant, "commuter", "budget-model", "Budget Bike",
                             Decimal("3000"), "Affordable commuter."),
            _make_model_item(tenant, "sport", "premium-model", "Premium Sport",
                             Decimal("12000"), "High performance."),
        ]

        mock_table = MagicMock()
        mock_table.query.side_effect = [
            {"Items": [{"itemKey": "CAT#commuter"}, {"itemKey": "CAT#sport"}]},
            {"Items": model_items},
        ]

        with patch("agents.supervisor.tools.configurator_recommend._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(useCase="daily ride", budgetMax=5000)

        recs = result["recommendations"]
        prices = [r["basePrice"] for r in recs]
        assert all(p <= 5000 for p in prices), f"Over-budget model leaked into results: {prices}"

    def test_catalog_table_not_configured_returns_unavailable(self):
        """When acquire_catalog_table is empty, returns {available: false}."""
        ctx = _make_ctx()
        config = _make_config(catalog_table="")
        tool_fn = self._make_tool(config, ctx)
        result = tool_fn(useCase="commuting")

        assert result.get("available") is False
        assert "reason" in result

    def test_priority_hint_electric_filters_to_electric_category(self):
        """priorityHint='electric' only returns models from electric category."""
        ctx = _make_ctx()
        tenant = ctx.tenant_id
        config = _make_config()

        model_items = [
            _make_model_item(tenant, "electric", "solstice-e", "Solstice-e",
                             Decimal("5999"), "Zero emissions."),
            _make_model_item(tenant, "commuter", "meridian-300", "Meridian 300",
                             Decimal("4599"), "Petrol commuter."),
        ]

        mock_table = MagicMock()
        mock_table.query.side_effect = [
            {"Items": [{"itemKey": "CAT#electric"}, {"itemKey": "CAT#commuter"}]},
            {"Items": model_items},
        ]

        with patch("agents.supervisor.tools.configurator_recommend._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(useCase="electric vehicle", priorityHint="electric")

        recs = result["recommendations"]
        model_ids = [r["modelId"] for r in recs]
        assert "meridian-300" not in model_ids, "Non-electric model leaked through electric filter"
        assert "solstice-e" in model_ids


# ---------------------------------------------------------------------------
# variant_compare  (renamed from configurator_compare per spec 2026-08-04-cvx-oem-discover-order-meridian)
# ---------------------------------------------------------------------------


class TestVariantCompare:
    """Tests for make_variant_compare_tool."""

    def _make_tool(self, config=None, ctx=None):
        from agents.supervisor.tools.variant_compare import make_variant_compare_tool
        return make_variant_compare_tool(ctx or _make_ctx(), config or _make_config())

    def test_happy_path_returns_comparison_for_two_variants(self):
        """Happy path: valid modelId + two variantIds → comparison list + summary."""
        ctx = _make_ctx()
        tenant = ctx.tenant_id
        config = _make_config()

        model_a = _make_model_item(tenant, "commuter", "meridian-300", "Meridian 300",
                                   Decimal("4599"), specs={"power_hp": 24})
        variant_std = _make_variant_item(tenant, "commuter", "meridian-300", "standard",
                                         "Standard", Decimal("0"))
        variant_racing = _make_variant_item(tenant, "commuter", "meridian-300", "racing",
                                            "Racing", Decimal("500"))

        mock_table = MagicMock()

        def query_side_effect(**kwargs):
            expr_vals = kwargs.get("ExpressionAttributeValues", {})
            # Fixed code: MODEL# prefix key-condition (no :mid); Python narrows
            # to exact modelId.
            if expr_vals.get(":model_prefix") == "MODEL#":
                return {"Items": [model_a]}
            if ":var_prefix" in expr_vals:
                return {"Items": [variant_std, variant_racing]}
            return {"Items": []}

        mock_table.query.side_effect = query_side_effect

        with patch("agents.supervisor.tools.variant_compare._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(modelId="meridian-300", variantIds=["standard", "racing"])

        assert "comparison" in result
        assert "summary" in result
        assert result.get("modelId") == "meridian-300"
        comparison = result["comparison"]
        assert len(comparison) == 2
        found_variants = {e["variantId"] for e in comparison if e.get("found")}
        assert "standard" in found_variants
        assert "racing" in found_variants

    def test_invalid_variant_ids_count_returns_error(self):
        """Passing fewer than 2 or more than 3 variantIds returns error."""
        ctx = _make_ctx()
        config = _make_config()
        tool_fn = self._make_tool(config, ctx)

        result_too_few = tool_fn(modelId="meridian-300", variantIds=["only-one"])
        assert result_too_few.get("available") is False
        assert "2" in str(result_too_few.get("reason", ""))

        result_too_many = tool_fn(modelId="meridian-300", variantIds=["a", "b", "c", "d"])
        assert result_too_many.get("available") is False

    def test_catalog_table_not_configured_returns_unavailable(self):
        """When acquire_catalog_table is empty, returns {available: false}."""
        ctx = _make_ctx()
        config = _make_config(catalog_table="")
        tool_fn = self._make_tool(config, ctx)
        result = tool_fn(modelId="meridian-300", variantIds=["standard", "racing"])

        assert result.get("available") is False
        assert "reason" in result

    def test_unknown_variant_id_is_marked_not_found(self):
        """If a variant doesn't exist in DDB, its entry has found=False."""
        ctx = _make_ctx()
        tenant = ctx.tenant_id
        config = _make_config()

        model_item = _make_model_item(tenant, "commuter", "meridian-300", "Meridian 300",
                                      Decimal("4599"))

        mock_table = MagicMock()

        def query_side_effect(**kwargs):
            expr_vals = kwargs.get("ExpressionAttributeValues", {})
            if ":model_prefix" in expr_vals and expr_vals.get(":model_prefix") == "MODEL#":
                return {"Items": [model_item]}
            # Return empty for variants
            return {"Items": []}

        mock_table.query.side_effect = query_side_effect

        with patch("agents.supervisor.tools.variant_compare._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(modelId="meridian-300", variantIds=["nonexistent-a", "nonexistent-b"])

        not_found = [e for e in result["comparison"] if not e.get("found", True)]
        assert len(not_found) == 2


# ---------------------------------------------------------------------------
# delivery_status
# ---------------------------------------------------------------------------


class TestDeliveryStatus:
    """Tests for make_delivery_status_tool — including critical tenant isolation."""

    def _make_tool(self, config=None, ctx=None):
        from agents.supervisor.tools.delivery_status import make_delivery_status_tool
        return make_delivery_status_tool(ctx or _make_ctx(), config or _make_config())

    def test_happy_path_returns_order_state(self):
        """Happy path: valid orderId for correct tenant → full order state."""
        ctx = _make_ctx(tenant_id="genericmoto-demo")
        config = _make_config()
        order = _make_order_item(
            "ORD-20260721-abc123",
            tenant_id="genericmoto-demo",
            stage="subAssembly",
            next_transition_at="2026-07-25T00:00:00Z",
            plant_id="meridian-plant-a",
            dealer_ref="dealer-koramangala-01",
        )

        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": order}

        with patch("agents.supervisor.tools.delivery_status._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(orderId="ORD-20260721-abc123")

        assert result["found"] is True
        assert result["orderId"] == "ORD-20260721-abc123"
        assert result["stage"] == "subAssembly"
        assert result["plantId"] == "meridian-plant-a"
        assert result["dealerRef"] == "dealer-koramangala-01"
        assert result["hasEta"] is True
        assert result["nextTransitionAt"] == "2026-07-25T00:00:00Z"

    def test_order_not_found_returns_found_false(self):
        """When DDB returns no Item, tool returns {found: false}."""
        ctx = _make_ctx()
        config = _make_config()

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no "Item" key

        with patch("agents.supervisor.tools.delivery_status._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(orderId="ORD-DOES-NOT-EXIST")

        assert result["found"] is False
        assert "reason" in result

    def test_tenant_isolation_blocks_cross_tenant_order(self):
        """CRITICAL: order belonging to a different tenant returns {found: false}.

        An order item whose tenantId != ctx.tenant_id MUST NOT be exposed.
        This is the primary tenant-data-isolation guard for the delivery
        status tool.
        """
        # The session belongs to tenant A
        ctx = _make_ctx(tenant_id="tenant-A")
        config = _make_config()

        # But the DDB item belongs to tenant B
        cross_tenant_order = _make_order_item(
            "ORD-CROSS-TENANT-001",
            tenant_id="tenant-B",
            stage="paint",
        )

        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": cross_tenant_order}

        with patch("agents.supervisor.tools.delivery_status._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(orderId="ORD-CROSS-TENANT-001")

        # Must NOT return the item — tenant mismatch must block the response
        assert result["found"] is False, (
            "CRITICAL: cross-tenant order was exposed. "
            "Tenant isolation check failed."
        )
        assert result.get("reason") == "tenant mismatch"
        # Verify none of the sensitive fields leaked through
        assert "stage" not in result
        assert "plantId" not in result
        assert "dealerRef" not in result

    def test_orders_table_not_configured_returns_unavailable(self):
        """When acquire_orders_table is empty, returns {available: false}."""
        ctx = _make_ctx()
        config = _make_config(orders_table="")
        tool_fn = self._make_tool(config, ctx)
        result = tool_fn(orderId="ORD-ANYTHING")

        assert result.get("available") is False
        assert "reason" in result

    def test_terminal_stage_has_no_eta(self):
        """readyForDelivery stage omits nextTransitionAt → hasEta=False."""
        ctx = _make_ctx(tenant_id="genericmoto-demo")
        config = _make_config()
        order = _make_order_item(
            "ORD-TERMINAL",
            tenant_id="genericmoto-demo",
            stage="readyForDelivery",
            next_transition_at=None,  # terminal stage omits this field
        )

        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": order}

        with patch("agents.supervisor.tools.delivery_status._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(orderId="ORD-TERMINAL")

        assert result["found"] is True
        assert result["hasEta"] is False
        assert result.get("nextTransitionAt") is None


# ---------------------------------------------------------------------------
# manufacturing_explain
# ---------------------------------------------------------------------------


class TestManufacturingExplain:
    """Tests for make_manufacturing_explain_tool."""

    def _make_tool(self, config=None, ctx=None):
        from agents.supervisor.tools.manufacturing_explain import make_manufacturing_explain_tool
        return make_manufacturing_explain_tool(ctx or _make_ctx(), config or _make_config())

    def test_happy_path_returns_passages(self):
        """Happy path: KB returns results for a valid stage."""
        ctx = _make_ctx()
        config = _make_config(kb_id="test-kb-id")

        kb_response = {
            "retrievalResults": [
                {
                    "content": {"text": "Sub-assembly is the stage where the engine and frame are joined."},
                    "location": {"s3Location": {"uri": "s3://kb/sub-assembly.md"}},
                    "score": 0.85,
                }
            ]
        }

        with patch("agents.supervisor.tools.manufacturing_explain.boto3") as mock_boto3:
            mock_boto3.client.return_value.retrieve.return_value = kb_response
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(stage="subAssembly")

        assert "stage" in result
        assert result["stage"] == "subAssembly"
        assert "passages" in result
        assert len(result["passages"]) >= 1
        assert "answer" in result
        assert "sub-assembly" in result["answer"].lower() or "engine" in result["answer"].lower()

    def test_kb_not_configured_returns_unavailable(self):
        """When adp_kb_id is empty, returns {available: false}."""
        ctx = _make_ctx()
        config = _make_config(kb_id="")
        tool_fn = self._make_tool(config, ctx)
        result = tool_fn(stage="paint")

        assert result.get("available") is False
        assert "reason" in result

    def test_kb_error_returns_graceful_degradation(self):
        """When KB Retrieve raises an exception, tool returns graceful response."""
        from botocore.exceptions import ClientError

        ctx = _make_ctx()
        config = _make_config(kb_id="test-kb-id")

        with patch("agents.supervisor.tools.manufacturing_explain.boto3") as mock_boto3:
            mock_boto3.client.return_value.retrieve.side_effect = ClientError(
                {"Error": {"Code": "ServiceUnavailableException", "Message": "KB down"}},
                "Retrieve",
            )
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(stage="qualityControl")

        # Must not raise; must return a usable response structure
        assert "stage" in result
        assert result["stage"] == "qualityControl"
        assert "answer" in result

    def test_source_category_filter_applied_with_gate_on(self, monkeypatch):
        """With VSA_KB_SOURCE_CATEGORY_FILTER=true, the filter is applied."""
        monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")
        ctx = _make_ctx()
        config = _make_config(kb_id="test-kb-id", kb_source_category="vehicle_manufacturing")

        kb_response = {
            "retrievalResults": [
                {
                    "content": {"text": "Paint stage applies enamel coat."},
                    "location": {"s3Location": {"uri": "s3://kb/paint.md"}},
                    "score": 0.9,
                }
            ]
        }

        with patch("agents.supervisor.tools.manufacturing_explain.boto3") as mock_boto3:
            mock_boto3.client.return_value.retrieve.return_value = kb_response
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(stage="paint")

        # With gate ON, the filter should be present in the retrieve call
        call_kwargs = mock_boto3.client.return_value.retrieve.call_args[1]
        vsc = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
        assert "filter" in vsc
        flt = vsc["filter"]
        assert flt == {"equals": {"key": "source_category", "value": "vehicle_manufacturing"}}


# ---------------------------------------------------------------------------
# resolve_or_create_owner_identity
# ---------------------------------------------------------------------------


class TestResolveOrCreateOwnerIdentity:
    """Tests for make_resolve_or_create_owner_identity_tool."""

    def _make_tool(self, config=None, ctx=None):
        import agents.supervisor.tools.resolve_or_create_owner_identity as mod
        # Reset the module-level _seen set before each test to ensure
        # isNewIdentity is deterministic across tests
        mod._seen.clear()
        from agents.supervisor.tools.resolve_or_create_owner_identity import make_resolve_or_create_owner_identity_tool
        return make_resolve_or_create_owner_identity_tool(ctx or _make_ctx(), config or _make_config())

    def test_happy_path_returns_owner_sub_and_fields(self):
        """Happy path: new email returns ownerSub, isNewIdentity=True, and contact fields."""
        ctx = _make_ctx(tenant_id="genericmoto-demo")
        tool_fn = self._make_tool(ctx=ctx)

        result = tool_fn(
            email="buyer@example.com",
            firstName="Alice",
            lastName="Buyer",
            phone="+15555550100",
        )

        assert "ownerSub" in result
        assert len(result["ownerSub"]) == 36  # UUID format
        assert result["isNewIdentity"] is True
        assert result["email"] == "buyer@example.com"
        assert result["firstName"] == "Alice"
        assert result["lastName"] == "Buyer"

    def test_same_email_returns_false_is_new_identity_on_second_call(self):
        """Same email in same process → isNewIdentity=False on second call."""
        ctx = _make_ctx(tenant_id="genericmoto-demo")
        tool_fn = self._make_tool(ctx=ctx)

        first = tool_fn(email="repeat@example.com", firstName="Bob", lastName="Smith")
        second = tool_fn(email="repeat@example.com", firstName="Bob", lastName="Smith")

        assert first["isNewIdentity"] is True
        assert second["isNewIdentity"] is False
        # Sub must be the same for the same identity
        assert first["ownerSub"] == second["ownerSub"]

    def test_deterministic_sub_for_same_tenant_and_email(self):
        """Same tenant + email always produces the same ownerSub (UUID v5)."""
        ctx_a = _make_ctx(tenant_id="genericmoto-demo")
        ctx_b = _make_ctx(tenant_id="genericmoto-demo")

        import agents.supervisor.tools.resolve_or_create_owner_identity as mod
        mod._seen.clear()
        from agents.supervisor.tools.resolve_or_create_owner_identity import make_resolve_or_create_owner_identity_tool

        tool_a = make_resolve_or_create_owner_identity_tool(ctx_a, _make_config())
        mod._seen.clear()
        tool_b = make_resolve_or_create_owner_identity_tool(ctx_b, _make_config())

        result_a = tool_a(email="stable@example.com", firstName="Carol", lastName="Chen")
        mod._seen.clear()
        result_b = tool_b(email="stable@example.com", firstName="Carol", lastName="Chen")

        assert result_a["ownerSub"] == result_b["ownerSub"]

    def test_email_normalized_to_lowercase(self):
        """Email is normalized to lower-case before sub generation."""
        ctx = _make_ctx()
        tool_fn = self._make_tool(ctx=ctx)

        result_upper = tool_fn(email="UPPER@EXAMPLE.COM", firstName="Dave", lastName="Li")
        import agents.supervisor.tools.resolve_or_create_owner_identity as mod
        mod._seen.clear()
        from agents.supervisor.tools.resolve_or_create_owner_identity import make_resolve_or_create_owner_identity_tool
        tool_fn2 = make_resolve_or_create_owner_identity_tool(ctx, _make_config())
        result_lower = tool_fn2(email="upper@example.com", firstName="Dave", lastName="Li")

        assert result_upper["ownerSub"] == result_lower["ownerSub"]
        assert result_upper["email"] == "upper@example.com"

    def test_different_tenants_produce_different_subs_for_same_email(self):
        """Same email under different tenants → different ownerSubs (tenant isolation)."""
        import agents.supervisor.tools.resolve_or_create_owner_identity as mod

        mod._seen.clear()
        from agents.supervisor.tools.resolve_or_create_owner_identity import make_resolve_or_create_owner_identity_tool

        ctx_tenant_a = _make_ctx(tenant_id="tenant-A")
        ctx_tenant_b = _make_ctx(tenant_id="tenant-B")
        config = _make_config()

        tool_a = make_resolve_or_create_owner_identity_tool(ctx_tenant_a, config)
        tool_b = make_resolve_or_create_owner_identity_tool(ctx_tenant_b, config)

        result_a = tool_a(email="shared@example.com", firstName="Eve", lastName="Zhang")
        mod._seen.clear()
        result_b = tool_b(email="shared@example.com", firstName="Eve", lastName="Zhang")

        # Different tenants → different subs for the same email address
        assert result_a["ownerSub"] != result_b["ownerSub"]

    def test_resolves_from_lead_id(self):
        """When leadId is supplied, email/phone are hydrated from the lead record."""
        import agents.supervisor.tools.resolve_or_create_owner_identity as mod
        mod._seen.clear()
        from agents.supervisor.tools.resolve_or_create_owner_identity import make_resolve_or_create_owner_identity_tool

        ctx = _make_ctx(tenant_id="genericmoto-demo")
        config = _make_config()
        # Add acquire_leads_table to the config stub so the tool will attempt a DDB read.
        config_with_leads = type(config)(
            **{**vars(config), "acquire_leads_table": "vsa-staging-acquire-leads"}
        ) if hasattr(config, "__dict__") else None
        # Use SimpleNamespace with extra field
        from types import SimpleNamespace
        config_with_leads = SimpleNamespace(
            acquire_catalog_table=config.acquire_catalog_table,
            acquire_orders_table=config.acquire_orders_table,
            adp_kb_id=config.adp_kb_id,
            adp_region=config.adp_region,
            acquire_kb_source_category=config.acquire_kb_source_category,
            acquire_leads_table="vsa-staging-acquire-leads",
        )

        lead_item = {
            "leadId": "LEAD-20260728-abcd1234",
            "tenantId": "genericmoto-demo",
            "contactInfo": {
                "emailNormalized": "discovered@example.com",
                "phoneE164": "+15555550199",
                "contactHash": "abc123",
            },
        }

        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": lead_item}

        with patch(
            "agents.supervisor.tools.resolve_or_create_owner_identity._get_ddb"
        ) as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = make_resolve_or_create_owner_identity_tool(ctx, config_with_leads)
            result = tool_fn(
                email="caller@example.com",  # will be overridden by lead record
                firstName="Frank",
                lastName="Hydrated",
                leadId="LEAD-20260728-abcd1234",
            )

        # The email should be hydrated from the lead record, not the caller-supplied value
        assert result["email"] == "discovered@example.com"
        # ownerSub should be stable (deterministic on the hydrated email)
        assert "ownerSub" in result
        assert len(result["ownerSub"]) == 36
        # get_item was called on the leads table with the correct key
        mock_table.get_item.assert_called_once_with(Key={"leadId": "LEAD-20260728-abcd1234"})


# ---------------------------------------------------------------------------
# Regression: no primary-key attribute in FilterExpression
# ---------------------------------------------------------------------------


class TestNoPrimaryKeyInFilterExpression:
    """Regression tests: itemKey (sort key) must never appear in FilterExpression.

    DynamoDB raises ValidationException if a primary-key attribute appears in
    FilterExpression.  These tests assert that every DDB query call made by the
    Acquire-phase tools omits itemKey (and tenantId) from FilterExpression.
    """

    @staticmethod
    def _pk_in_filter(call_kwargs: dict) -> bool:
        """Return True if FilterExpression references itemKey or tenantId."""
        fe = call_kwargs.get("FilterExpression", "")
        return "itemKey" in str(fe) or "tenantId" in str(fe)

    @staticmethod
    def _all_query_calls(mock_table) -> list[dict]:
        return [c[1] for c in mock_table.query.call_args_list]

    # --- configurator_recommend ---

    def test_configurator_recommend_no_pk_in_filter(self):
        """configurator_recommend must not put itemKey in FilterExpression."""
        from agents.supervisor.tools.configurator_recommend import make_configurator_recommend_tool

        ctx = _make_ctx()
        config = _make_config()
        model_items = [
            _make_model_item(ctx.tenant_id, "commuter", "meridian-300", "Meridian 300",
                             Decimal("4599")),
        ]
        mock_table = MagicMock()
        mock_table.query.side_effect = [
            {"Items": [{"itemKey": "CAT#commuter"}]},  # GSI call
            {"Items": model_items},                     # MODEL# key-condition call
        ]

        with patch("agents.supervisor.tools.configurator_recommend._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = make_configurator_recommend_tool(ctx, config)
            tool_fn(useCase="city commuting")

        for call_kw in self._all_query_calls(mock_table):
            assert not self._pk_in_filter(call_kw), (
                f"itemKey or tenantId found in FilterExpression: {call_kw}"
            )

    # --- variant_compare ---

    def test_variant_compare_no_pk_in_filter(self):
        """variant_compare must not put itemKey in FilterExpression."""
        from agents.supervisor.tools.variant_compare import make_variant_compare_tool

        ctx = _make_ctx()
        config = _make_config()
        model_item = _make_model_item(ctx.tenant_id, "commuter", "meridian-300",
                                      "Meridian 300", Decimal("4599"))
        var_std = _make_variant_item(ctx.tenant_id, "commuter", "meridian-300",
                                     "standard", "Standard", Decimal("0"))
        var_racing = _make_variant_item(ctx.tenant_id, "commuter", "meridian-300",
                                        "racing", "Racing", Decimal("500"))
        mock_table = MagicMock()

        def query_side_effect(**kwargs):
            expr_vals = kwargs.get("ExpressionAttributeValues", {})
            if expr_vals.get(":model_prefix") == "MODEL#":
                return {"Items": [model_item]}
            if ":var_prefix" in expr_vals:
                return {"Items": [var_std, var_racing]}
            return {"Items": []}

        mock_table.query.side_effect = query_side_effect

        with patch("agents.supervisor.tools.variant_compare._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = make_variant_compare_tool(ctx, config)
            tool_fn(modelId="meridian-300", variantIds=["standard", "racing"])

        for call_kw in self._all_query_calls(mock_table):
            assert not self._pk_in_filter(call_kw), (
                f"itemKey or tenantId found in FilterExpression: {call_kw}"
            )
