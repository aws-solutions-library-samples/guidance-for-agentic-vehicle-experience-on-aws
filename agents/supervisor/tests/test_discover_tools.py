"""Tests for the four CVX Discover tool factories.

Spec: connected-mobility-guidance-on-aws/.kiro/specs/
      2026-08-04-cvx-oem-discover-order-meridian/ § Group 2 (merged from predecessor 2026-07-28)

Covers:
  - catalog_query     (≥4 tests: happy path, missing input, tenant scope, unavailable)
  - offers_lookup     (≥4 tests: happy path, validity window, guardrails id, unavailable)
  - catalog_compare   (≥4 tests: happy path, invalid count, not found, unavailable)
  - lead_capture      (≥6 tests: happy path, no-consent block, idempotency,
                        missing contact, unavailable, guardrails id on offers)

All DDB calls are mocked. No AWS credentials or live infrastructure required.

Run: pytest agents/supervisor/tests/test_discover_tools.py -v
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
    leads_table: str = "vsa-staging-acquire-leads",
    region: str = "us-east-1",
    guardrails_id: str = "discover-offers-guardrails-v1",
) -> SimpleNamespace:
    return SimpleNamespace(
        acquire_catalog_table=catalog_table,
        acquire_leads_table=leads_table,
        acquire_orders_table="vsa-staging-acquire-orders",
        adp_kb_id="test-kb-id",
        adp_region=region,
        discover_guardrails_identifier=guardrails_id,
    )


def _make_ctx(tenant_id: str = "genericmoto-demo") -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=tenant_id,
        session_id="test-session-discover-001",
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


def _make_offer_item(
    tenant_id: str,
    model_id: str,
    offer_id: str,
    display_name: str,
    body: str,
    deposit_amount: Decimal,
    disclosure_txt: str,
    valid_from: str = "2026-01-01T00:00:00Z",
    valid_until: str = "2026-12-31T23:59:59Z",
    regions: list[str] | None = None,
) -> dict:
    item = {
        "tenantId": tenant_id,
        "itemKey": f"OFFER#{model_id}#{offer_id}",
        "displayName": display_name,
        "body": body,
        "depositAmount": deposit_amount,
        "validFrom": valid_from,
        "validUntil": valid_until,
        "disclosure_txt": disclosure_txt,
    }
    if regions is not None:
        item["regions"] = regions
    return item


# ---------------------------------------------------------------------------
# catalog_query
# ---------------------------------------------------------------------------


class TestCatalogQuery:
    """Tests for make_catalog_query_tool."""

    def _make_tool(self, config=None, ctx=None):
        from agents.supervisor.tools.catalog_query import make_catalog_query_tool
        return make_catalog_query_tool(ctx or _make_ctx(), config or _make_config())

    def test_happy_path_returns_up_to_3_models(self):
        """Happy path: catalog has models; returns ≤3 ranked results."""
        ctx = _make_ctx()
        tenant = ctx.tenant_id
        config = _make_config()

        model_items = [
            _make_model_item(tenant, "commuter", "meridian-300", "Meridian 300",
                             Decimal("4599"), "The everyday commuter.", {"use": "city"}),
            _make_model_item(tenant, "adventure", "solstice-adv", "Solstice ADV",
                             Decimal("7999"), "Adventure touring."),
            _make_model_item(tenant, "electric", "solstice-e", "Solstice E",
                             Decimal("5999"), "Zero emissions city ride."),
        ]

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": model_items}

        with patch("agents.supervisor.tools.catalog_query._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(criteria="city commuting")

        assert result.get("available") is True
        models = result.get("models", [])
        assert len(models) <= 3
        assert all("modelId" in m for m in models)
        assert all("displayName" in m for m in models)
        assert all("matchReasoning" in m for m in models)

    def test_budget_filter_excludes_over_budget_models(self):
        """Models priced above budgetMax are excluded."""
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
        mock_table.query.return_value = {"Items": model_items}

        with patch("agents.supervisor.tools.catalog_query._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(criteria="daily ride", budgetMax=5000)

        models = result.get("models", [])
        prices = [m["basePrice"] for m in models]
        assert all(p <= 5000 for p in prices), f"Over-budget model leaked: {prices}"

    def test_catalog_table_not_configured_returns_unavailable(self):
        """When acquire_catalog_table is empty, returns {available: false}."""
        config = _make_config(catalog_table="")
        tool_fn = self._make_tool(config)
        result = tool_fn(criteria="anything")

        assert result.get("available") is False
        assert "reason" in result

    def test_tenant_scope_only_returns_own_tenant_models(self):
        """Only models matching ctx.tenant_id are returned (DDB query is tenant-scoped)."""
        ctx = _make_ctx(tenant_id="tenant-A")
        config = _make_config()

        # Only items belonging to tenant-A
        model_items = [
            _make_model_item("tenant-A", "commuter", "model-a", "Model A",
                             Decimal("4000"), "Commuter A."),
        ]

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": model_items}

        with patch("agents.supervisor.tools.catalog_query._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(criteria="commuting")

        # Verify DDB was queried with the correct tenantId
        call_kwargs = mock_table.query.call_args[1]
        expr_vals = call_kwargs.get("ExpressionAttributeValues", {})
        assert expr_vals.get(":tid") == "tenant-A"


# ---------------------------------------------------------------------------
# offers_lookup
# ---------------------------------------------------------------------------


class TestOffersLookup:
    """Tests for make_offers_lookup_tool."""

    def _make_tool(self, config=None, ctx=None):
        from agents.supervisor.tools.offers_lookup import make_offers_lookup_tool
        return make_offers_lookup_tool(ctx or _make_ctx(), config or _make_config())

    def test_happy_path_returns_active_offers_with_disclosure(self):
        """Happy path: active offer returned with disclosure_txt field."""
        ctx = _make_ctx()
        tenant = ctx.tenant_id
        config = _make_config()

        offer = _make_offer_item(
            tenant, "meridian-300", "promo-jan",
            "January Launch Offer",
            "Save 500 on your booking deposit.",
            Decimal("500"),
            "Offer valid until 31 Dec 2026. T&Cs apply. Subject to availability.",
        )

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": [offer]}

        with patch("agents.supervisor.tools.offers_lookup._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(modelId="meridian-300")

        assert result.get("available") is True
        assert result.get("modelId") == "meridian-300"
        offers = result.get("offers", [])
        assert len(offers) == 1
        assert "disclosure_txt" in offers[0]
        assert "T&Cs apply" in offers[0]["disclosure_txt"]

    def test_expired_offer_is_excluded(self):
        """An offer whose validUntil is in the past is filtered out."""
        ctx = _make_ctx()
        tenant = ctx.tenant_id
        config = _make_config()

        expired_offer = _make_offer_item(
            tenant, "meridian-300", "old-promo",
            "Old Promo",
            "Expired deal.",
            Decimal("200"),
            "This offer has expired.",
            valid_from="2020-01-01T00:00:00Z",
            valid_until="2020-12-31T23:59:59Z",
        )

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": [expired_offer]}

        with patch("agents.supervisor.tools.offers_lookup._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(modelId="meridian-300")

        assert result.get("available") is True
        # Expired offer should not appear
        assert result.get("offers", []) == []

    def test_guardrails_identifier_threaded_in_response(self):
        """guardrailsIdentifier from config is returned on every response."""
        ctx = _make_ctx()
        config = _make_config(guardrails_id="discover-offers-guardrails-v1")

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}

        with patch("agents.supervisor.tools.offers_lookup._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(modelId="any-model")

        assert result.get("guardrailsIdentifier") == "discover-offers-guardrails-v1"

    def test_catalog_table_not_configured_returns_unavailable(self):
        """When acquire_catalog_table is empty, returns {available: false}."""
        config = _make_config(catalog_table="")
        tool_fn = self._make_tool(config)
        result = tool_fn(modelId="meridian-300")

        assert result.get("available") is False
        assert "reason" in result


# ---------------------------------------------------------------------------
# catalog_compare
# ---------------------------------------------------------------------------


class TestCatalogCompare:
    """Tests for make_catalog_compare_tool."""

    def _make_tool(self, config=None, ctx=None):
        from agents.supervisor.tools.catalog_compare import make_catalog_compare_tool
        return make_catalog_compare_tool(ctx or _make_ctx(), config or _make_config())

    def test_happy_path_returns_comparison_for_two_models(self):
        """Happy path: two valid modelIds → comparison list + summary."""
        ctx = _make_ctx()
        tenant = ctx.tenant_id
        config = _make_config()

        model_a = _make_model_item(tenant, "commuter", "meridian-300", "Meridian 300",
                                   Decimal("4599"), specs={"power_hp": 24})
        model_b = _make_model_item(tenant, "electric", "solstice-e", "Solstice E",
                                   Decimal("5999"), specs={"range_km": 120})
        variant_a = _make_variant_item(tenant, "commuter", "meridian-300", "standard",
                                       "Standard", Decimal("0"))

        mock_table = MagicMock()

        def query_side_effect(**kwargs):
            expr_vals = kwargs.get("ExpressionAttributeValues", {})
            # Fixed code: MODEL# prefix key-condition returns all MODEL# items;
            # Python then narrows to the specific modelId.  Both models are
            # returned so both meridian-300 and solstice-e are found.
            if expr_vals.get(":model_prefix") == "MODEL#":
                return {"Items": [model_a, model_b]}
            if ":var_prefix" in expr_vals:
                return {"Items": [variant_a]}
            return {"Items": []}

        mock_table.query.side_effect = query_side_effect

        with patch("agents.supervisor.tools.catalog_compare._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(modelIds=["meridian-300", "solstice-e"],
                             criteria="daily commuting")

        assert result.get("available") is True
        assert "comparison" in result
        assert "summary" in result
        found = {e["modelId"] for e in result["comparison"] if e.get("found")}
        assert "meridian-300" in found
        assert "solstice-e" in found

    def test_invalid_model_ids_count_returns_error(self):
        """Passing fewer than 2 or more than 3 modelIds returns error."""
        tool_fn = self._make_tool()

        result_too_few = tool_fn(modelIds=["only-one"])
        assert result_too_few.get("available") is False
        assert "2" in str(result_too_few.get("reason", ""))

        result_too_many = tool_fn(modelIds=["a", "b", "c", "d"])
        assert result_too_many.get("available") is False

    def test_unknown_model_id_is_marked_not_found(self):
        """If a model doesn't exist in DDB, its entry has found=False."""
        config = _make_config()
        ctx = _make_ctx()

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}

        with patch("agents.supervisor.tools.catalog_compare._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(modelIds=["nonexistent-a", "nonexistent-b"])

        not_found = [e for e in result["comparison"] if not e.get("found", True)]
        assert len(not_found) == 2

    def test_catalog_table_not_configured_returns_unavailable(self):
        """When acquire_catalog_table is empty, returns {available: false}."""
        config = _make_config(catalog_table="")
        tool_fn = self._make_tool(config)
        result = tool_fn(modelIds=["model-a", "model-b"])

        assert result.get("available") is False
        assert "reason" in result


# ---------------------------------------------------------------------------
# lead_capture
# ---------------------------------------------------------------------------


class TestLeadCapture:
    """Tests for make_lead_capture_tool."""

    def _make_tool(self, config=None, ctx=None):
        from agents.supervisor.tools.lead_capture import make_lead_capture_tool
        return make_lead_capture_tool(ctx or _make_ctx(), config or _make_config())

    def test_happy_path_writes_lead_returns_lead_id(self):
        """Happy path: valid contact + consent → DDB write + leadId returned."""
        ctx = _make_ctx()
        config = _make_config()

        mock_table = MagicMock()
        # GSI query returns no existing lead (new write)
        mock_table.query.return_value = {"Items": []}
        mock_table.put_item.return_value = {}

        with patch("agents.supervisor.tools.lead_capture._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(
                contact_info={"email": "buyer@example.com"},
                consent_flags={"contact_consent": True},
                saved_config={"preferred_category": "commuter", "preferred_model_id": "meridian-300"},
            )

        assert result.get("available") is True
        assert "leadId" in result
        assert result["leadId"].startswith("LEAD-")
        assert "wroteAt" in result
        assert result.get("idempotent") is False
        mock_table.put_item.assert_called_once()

    def test_lead_capture_blocked_without_consent(self):
        """lead_capture MUST NOT write when contact_consent is False or missing."""
        ctx = _make_ctx()
        config = _make_config()

        mock_table = MagicMock()

        with patch("agents.supervisor.tools.lead_capture._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)

            # False consent
            result_false = tool_fn(
                contact_info={"email": "buyer@example.com"},
                consent_flags={"contact_consent": False},
            )
            assert result_false.get("available") is False
            assert "consent" in result_false.get("reason", "").lower()

            # Missing consent_flags entirely
            result_none = tool_fn(
                contact_info={"email": "buyer@example.com"},
                consent_flags={},
            )
            assert result_none.get("available") is False

        # DDB must not have been written in either case
        mock_table.put_item.assert_not_called()

    def test_lead_capture_rejects_truthy_non_true_consent(self):
        """Consent must be EXACTLY True, not merely truthy.

        Security review 2026-07-29 W1. The gate was `if not contact_consent`,
        which refused falsy values but accepted any truthy non-True value.
        That matters because the value originates from an LLM tool call whose
        arguments are influenced by user text, and because the STAR envelope
        written to DynamoDB hard-codes `consent.contactConsent: True` — so a
        lead captured on a truthy-ish value would be recorded as if consent
        had been explicit.

        The string "false" is the case that should worry anyone: it is truthy
        in Python and reads as consent-denied to a human.
        """
        ctx = _make_ctx()
        config = _make_config()

        truthy_non_true = [
            "false",   # truthy string that a human reads as denial
            "true",    # right meaning, wrong type
            1,         # int, not bool
            [False],   # non-empty container
            {"a": 1},  # dict
            -1,
            0.5,
            "0",
        ]

        mock_table = MagicMock()
        with patch("agents.supervisor.tools.lead_capture._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)

            for value in truthy_non_true:
                result = tool_fn(
                    contact_info={"email": "buyer@example.com"},
                    consent_flags={"contact_consent": value},
                )
                assert result.get("available") is False, (
                    f"consent value {value!r} ({type(value).__name__}) was "
                    f"ACCEPTED — must require exactly True"
                )
                assert "consent" in result.get("reason", "").lower()

        # Not a single write may have occurred across any of those values.
        mock_table.put_item.assert_not_called()

    def test_idempotent_second_call_returns_existing_lead_id(self):
        """Second call with same email+tenant returns existing leadId, no new write."""
        ctx = _make_ctx()
        config = _make_config()

        existing_lead_id = "LEAD-20260728-EXISTING"

        mock_table = MagicMock()
        # GSI query returns an existing lead
        mock_table.query.return_value = {"Items": [{"leadId": existing_lead_id}]}
        mock_table.update_item.return_value = {}

        with patch("agents.supervisor.tools.lead_capture._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = self._make_tool(config, ctx)
            result = tool_fn(
                contact_info={"email": "repeat@example.com"},
                consent_flags={"contact_consent": True},
            )

        assert result.get("available") is True
        assert result.get("leadId") == existing_lead_id
        assert result.get("idempotent") is True
        # put_item must NOT have been called — no duplicate row
        mock_table.put_item.assert_not_called()
        # update_item should have been called to update associatedSessions
        mock_table.update_item.assert_called_once()

    def test_missing_contact_info_returns_error(self):
        """Empty email AND phone returns validation error."""
        config = _make_config()
        tool_fn = self._make_tool(config)

        result = tool_fn(
            contact_info={},
            consent_flags={"contact_consent": True},
        )

        assert result.get("available") is False
        assert "email" in result.get("reason", "").lower() or "phone" in result.get("reason", "").lower()

    def test_leads_table_not_configured_returns_unavailable(self):
        """When acquire_leads_table is empty, returns {available: false}."""
        config = _make_config(leads_table="")
        tool_fn = self._make_tool(config)

        result = tool_fn(
            contact_info={"email": "buyer@example.com"},
            consent_flags={"contact_consent": True},
        )

        assert result.get("available") is False
        assert "reason" in result

    def test_contact_hash_is_deterministic_for_same_tenant_and_email(self):
        """SHA256 contactHash is stable: same tenant + email → same hash."""
        from agents.supervisor.tools.lead_capture import _compute_contact_hash, _normalise_email

        email = "User@Example.COM"
        tenant = "genericmoto-demo"
        email_norm = _normalise_email(email)

        hash1 = _compute_contact_hash(tenant, email_norm, "")
        hash2 = _compute_contact_hash(tenant, email_norm, "")
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex = 64 chars

    def test_different_tenants_produce_different_contact_hashes(self):
        """Same email under different tenants produces different contactHash."""
        from agents.supervisor.tools.lead_capture import _compute_contact_hash, _normalise_email

        email_norm = _normalise_email("shared@example.com")
        hash_a = _compute_contact_hash("tenant-A", email_norm, "")
        hash_b = _compute_contact_hash("tenant-B", email_norm, "")
        assert hash_a != hash_b


# ---------------------------------------------------------------------------
# Regression: no primary-key attribute in FilterExpression
# ---------------------------------------------------------------------------


class TestNoPrimaryKeyInFilterExpression:
    """Regression tests: itemKey (sort key) must never appear in FilterExpression.

    DynamoDB raises ValidationException if a primary-key attribute appears in
    FilterExpression.  These tests assert that every DDB query call made by the
    Discover-phase tools omits itemKey (and tenantId) from FilterExpression.
    """

    # --- helpers ---

    @staticmethod
    def _pk_in_filter(call_kwargs: dict) -> bool:
        """Return True if FilterExpression references itemKey or tenantId."""
        fe = call_kwargs.get("FilterExpression", "")
        return "itemKey" in str(fe) or "tenantId" in str(fe)

    @staticmethod
    def _all_query_calls(mock_table) -> list[dict]:
        """Return the kwargs dict for every table.query() call."""
        return [c[1] for c in mock_table.query.call_args_list]

    # --- catalog_query ---

    def test_catalog_query_no_pk_in_filter(self):
        """catalog_query must not put itemKey in FilterExpression."""
        from agents.supervisor.tools.catalog_query import make_catalog_query_tool

        ctx = _make_ctx()
        config = _make_config()
        model_items = [
            _make_model_item(ctx.tenant_id, "commuter", "model-x", "Model X",
                             Decimal("4000")),
        ]
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": model_items}

        with patch("agents.supervisor.tools.catalog_query._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = make_catalog_query_tool(ctx, config)
            tool_fn(criteria="city commuting")

        for call_kw in self._all_query_calls(mock_table):
            assert not self._pk_in_filter(call_kw), (
                f"itemKey or tenantId found in FilterExpression: {call_kw}"
            )

    # --- catalog_compare ---

    def test_catalog_compare_no_pk_in_filter(self):
        """catalog_compare must not put itemKey in FilterExpression."""
        from agents.supervisor.tools.catalog_compare import make_catalog_compare_tool

        ctx = _make_ctx()
        config = _make_config()
        model_a = _make_model_item(ctx.tenant_id, "commuter", "meridian-300",
                                   "Meridian 300", Decimal("4599"))
        model_b = _make_model_item(ctx.tenant_id, "electric", "solstice-e",
                                   "Solstice E", Decimal("5999"))
        mock_table = MagicMock()

        def query_side_effect(**kwargs):
            expr_vals = kwargs.get("ExpressionAttributeValues", {})
            if expr_vals.get(":model_prefix") == "MODEL#":
                return {"Items": [model_a, model_b]}
            if ":var_prefix" in expr_vals:
                return {"Items": []}
            return {"Items": []}

        mock_table.query.side_effect = query_side_effect

        with patch("agents.supervisor.tools.catalog_compare._get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            tool_fn = make_catalog_compare_tool(ctx, config)
            tool_fn(modelIds=["meridian-300", "solstice-e"])

        for call_kw in self._all_query_calls(mock_table):
            assert not self._pk_in_filter(call_kw), (
                f"itemKey or tenantId found in FilterExpression: {call_kw}"
            )
