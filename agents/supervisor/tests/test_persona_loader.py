"""Tests for agents.supervisor.persona_loader (Group 3.A).

All 6 tests that were previously skip-marked are now implemented.
Uses unittest.mock — no new mocking framework.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from agents.supervisor.persona_definitions import PERSONA_PRODUCT_BINDINGS
from agents.supervisor.persona_loader import (
    PersonaLoaderConfig,
    PersonaSnapshot,
    _athena_query,
    _normalize_row,
    _validate_id,
    _validate_stage,
    load_persona,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> PersonaLoaderConfig:
    defaults = dict(
        memory_id="mem-test-001",
        adp_account_id="123456789012",
        adp_region="us-east-1",
        adp_stage="staging",
        athena_workgroup="cvx-staging-analytics",
        athena_results_bucket="cvx-staging-athena-results-123456789012-us-east-1",
        ttl_minutes=15,
        memory_region="us-west-2",
    )
    defaults.update(overrides)
    return PersonaLoaderConfig(**defaults)


def _make_list_memory_response(records: list) -> dict:
    return {"memoryRecords": records}


def _make_athena_start() -> dict:
    return {"QueryExecutionId": "qe-test-001"}


def _make_athena_status(state: str = "SUCCEEDED") -> dict:
    return {"QueryExecution": {"Status": {"State": state}}}


def _make_athena_results(headers: list[str], row: list[str]) -> dict:
    return {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": h} for h in headers]},
                {"Data": [{"VarCharValue": v} for v in row]},
            ]
        }
    }


def _make_batch_create_response() -> dict:
    return {"createdRecords": [{"requestIdentifier": "req-001", "memoryRecordId": "mr-001"}]}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config() -> PersonaLoaderConfig:
    return _make_config()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_load_persona_returns_snapshot_for_known_persona(config):
    """load_persona returns a PersonaSnapshot with data_sources populated."""
    # fleet_driver has: vehicle_identity, service_records, charging_sessions,
    # ota_campaigns, vehicle_telemetry_aggregated (pyspark-gated), energy_usage (gated).
    # We stub Memory as empty (no cache), Athena as succeeding for non-gated products,
    # and Glue as EntityNotFoundException for gated products.

    headers = ["vin", "make", "model", "year"]
    row = ["1FMCU0J91KUB12345", "Ford", "F-150", "2022"]

    memory_mock = MagicMock()
    memory_mock.list_memory_records.return_value = _make_list_memory_response([])  # no cache

    control_mock = MagicMock()
    memory_mock.batch_create_memory_records.return_value = _make_batch_create_response()

    athena_mock = MagicMock()
    athena_mock.start_query_execution.return_value = _make_athena_start()
    athena_mock.get_query_execution.return_value = _make_athena_status("SUCCEEDED")
    athena_mock.get_query_results.return_value = _make_athena_results(headers, row)

    glue_mock = MagicMock()
    # EntityNotFoundException means PySpark tables don't exist yet
    glue_mock.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
    glue_mock.get_table.side_effect = glue_mock.exceptions.EntityNotFoundException("not found")

    def _boto3_client(service, region_name=None):
        if service == "bedrock-agentcore":
            return memory_mock
        if service == "bedrock-agentcore-control":
            return control_mock
        if service == "athena":
            return athena_mock
        if service == "glue":
            return glue_mock
        return MagicMock()

    with patch("agents.supervisor.persona_loader.boto3.client", side_effect=_boto3_client):
        snapshot = asyncio.run(load_persona(
            "fleet_driver",
            vehicle_id="1FMCU0J91KUB12345",
            customer_id=None,
            config=config,
        ))

    assert isinstance(snapshot, PersonaSnapshot)
    assert snapshot.persona_id == "fleet_driver"
    assert isinstance(snapshot.data_sources, dict)
    # Non-gated, vehicle-id-bearing products should be loaded
    assert "vehicle_identity" in snapshot.data_sources
    assert snapshot.data_sources["vehicle_identity"]["vin"] == "1FMCU0J91KUB12345"
    assert snapshot.fetched_at > 0


def test_load_persona_skips_athena_for_cached_products(config):
    """When Memory has a fresh record for a product, Athena is NOT called for it."""
    memory_mock = MagicMock()
    # vehicle_identity is cached (fresh record returned)
    memory_mock.list_memory_records.return_value = _make_list_memory_response(
        [{"memoryRecordId": "mr-cached", "content": {"text": '{"vin":"VIN1","found":true}'}}]
    )

    athena_mock = MagicMock()
    athena_mock.start_query_execution.return_value = _make_athena_start()
    athena_mock.get_query_execution.return_value = _make_athena_status("SUCCEEDED")
    athena_mock.get_query_results.return_value = _make_athena_results(["vin"], ["VIN1"])

    control_mock = MagicMock()
    memory_mock.batch_create_memory_records.return_value = _make_batch_create_response()

    glue_mock = MagicMock()
    glue_mock.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
    glue_mock.get_table.side_effect = glue_mock.exceptions.EntityNotFoundException("not found")

    def _boto3_client(service, region_name=None):
        if service == "bedrock-agentcore":
            return memory_mock
        if service == "bedrock-agentcore-control":
            return control_mock
        if service == "athena":
            return athena_mock
        if service == "glue":
            return glue_mock
        return MagicMock()

    # Use retail_advisor — small product set (vehicle_identity, customer_360, customer_interactions)
    # Provide both vehicle_id and customer_id so all 3 products can attempt queries
    with patch("agents.supervisor.persona_loader.boto3.client", side_effect=_boto3_client):
        snapshot = asyncio.run(load_persona(
            "retail_advisor",
            vehicle_id="VIN1",
            customer_id="CUST1",
            config=config,
        ))

    # All products got a cache hit → Athena start_query_execution never called
    athena_mock.start_query_execution.assert_not_called()
    assert isinstance(snapshot, PersonaSnapshot)


def test_load_persona_handles_missing_pyspark_tables(config):
    """PySpark-gated products are silently skipped when Glue table is absent."""
    memory_mock = MagicMock()
    memory_mock.list_memory_records.return_value = _make_list_memory_response([])

    athena_mock = MagicMock()
    athena_mock.start_query_execution.return_value = _make_athena_start()
    athena_mock.get_query_execution.return_value = _make_athena_status("SUCCEEDED")
    athena_mock.get_query_results.return_value = _make_athena_results(["vin"], ["VIN1"])

    control_mock = MagicMock()
    memory_mock.batch_create_memory_records.return_value = _make_batch_create_response()

    glue_mock = MagicMock()
    EntityNotFound = type("EntityNotFoundException", (Exception,), {})
    glue_mock.exceptions.EntityNotFoundException = EntityNotFound
    glue_mock.get_table.side_effect = EntityNotFound("table not found")

    def _boto3_client(service, region_name=None):
        if service == "bedrock-agentcore":
            return memory_mock
        if service == "bedrock-agentcore-control":
            return control_mock
        if service == "athena":
            return athena_mock
        if service == "glue":
            return glue_mock
        return MagicMock()

    # fleet_driver has both pyspark-gated products
    with patch("agents.supervisor.persona_loader.boto3.client", side_effect=_boto3_client):
        snapshot = asyncio.run(load_persona(
            "fleet_driver",
            vehicle_id="VIN1",
            customer_id=None,
            config=config,
        ))

    # PySpark-gated products must NOT be in data_sources
    assert "vehicle_telemetry_aggregated" not in snapshot.data_sources
    assert "energy_usage" not in snapshot.data_sources
    # Does not raise
    assert isinstance(snapshot, PersonaSnapshot)


def test_load_persona_writes_metadata_per_product(config):
    """BatchCreateMemoryRecords is called with correct metadata for each loaded product."""
    memory_mock = MagicMock()
    memory_mock.list_memory_records.return_value = _make_list_memory_response([])

    athena_mock = MagicMock()
    athena_mock.start_query_execution.return_value = _make_athena_start()
    athena_mock.get_query_execution.return_value = _make_athena_status("SUCCEEDED")
    athena_mock.get_query_results.return_value = _make_athena_results(
        ["vin", "make"], ["VIN-OWNER-1", "Tesla"]
    )

    control_mock = MagicMock()
    memory_mock.batch_create_memory_records.return_value = _make_batch_create_response()

    glue_mock = MagicMock()
    EntityNotFound = type("EntityNotFoundException", (Exception,), {})
    glue_mock.exceptions.EntityNotFoundException = EntityNotFound
    glue_mock.get_table.side_effect = EntityNotFound("not found")

    def _boto3_client(service, region_name=None):
        if service == "bedrock-agentcore":
            return memory_mock
        if service == "bedrock-agentcore-control":
            return control_mock
        if service == "athena":
            return athena_mock
        if service == "glue":
            return glue_mock
        return MagicMock()

    # vehicle_owner: vehicle_identity, service_records, charging_sessions, customer_360, ota_campaigns, telemetry(gated)
    with patch("agents.supervisor.persona_loader.boto3.client", side_effect=_boto3_client):
        asyncio.run(load_persona(
            "vehicle_owner",
            vehicle_id="VIN-OWNER-1",
            customer_id="CUST-OWNER-1",
            config=config,
        ))

    # Each non-gated product should produce a batch_create call
    assert memory_mock.batch_create_memory_records.call_count >= 1

    # Inspect a call's metadata fields
    call_args = memory_mock.batch_create_memory_records.call_args_list[0]
    record = call_args.kwargs["records"][0]
    meta = record["metadata"]

    assert "data_source" in meta
    assert "last_updated" in meta
    assert "numberValue" in meta["last_updated"]  # must be NUMBER type
    assert "vehicle_id" in meta
    assert meta["vehicle_id"]["stringValue"] == "VIN-OWNER-1"
    assert meta["customer_id"]["stringValue"] == "CUST-OWNER-1"
    assert meta["tenant_id"]["stringValue"] == "oem"  # vehicle_owner → oem


def test_load_persona_failure_is_non_fatal(config):
    """Athena/Memory errors produce WARNING logs and return snapshot without that product."""
    memory_mock = MagicMock()
    memory_mock.list_memory_records.return_value = _make_list_memory_response([])

    athena_mock = MagicMock()
    # All Athena queries fail
    athena_mock.start_query_execution.side_effect = RuntimeError("Athena unavailable")

    control_mock = MagicMock()
    memory_mock.batch_create_memory_records.return_value = _make_batch_create_response()

    glue_mock = MagicMock()
    EntityNotFound = type("EntityNotFoundException", (Exception,), {})
    glue_mock.exceptions.EntityNotFoundException = EntityNotFound
    glue_mock.get_table.side_effect = EntityNotFound("not found")

    def _boto3_client(service, region_name=None):
        if service == "bedrock-agentcore":
            return memory_mock
        if service == "bedrock-agentcore-control":
            return control_mock
        if service == "athena":
            return athena_mock
        if service == "glue":
            return glue_mock
        return MagicMock()

    with patch("agents.supervisor.persona_loader.boto3.client", side_effect=_boto3_client):
        # Must NOT raise
        snapshot = asyncio.run(load_persona(
            "technician",
            vehicle_id="VIN-TECH-1",
            customer_id=None,
            config=config,
        ))

    assert isinstance(snapshot, PersonaSnapshot)
    # data_sources is empty because all Athena calls failed
    assert snapshot.data_sources == {}


def test_persona_product_bindings_cover_all_personas():
    """PERSONA_PRODUCT_BINDINGS has an entry for each persona slug.

    fleet_manager is a deliberate exception: it is a fleet-aggregate, non-VIN
    persona (spec 2026-08-21-cvx-fleet-manager-persona § Design), so it carries
    NO per-VIN product bindings — its data comes from CMS fleet endpoints via
    tools, not from ADP per-VIN data products. It is therefore excluded from the
    "every persona has >=1 product / vehicle_identity is universal" invariant.
    """
    # Personas whose data is per-VIN ADP products (the universal-anchor invariant applies).
    vin_scoped = {
        "fleet_driver",
        "consumer",
        "retail_advisor",
        "vehicle_owner",
        "service_advisor",
        "technician",
    }
    # Non-VIN personas with an intentionally empty product binding.
    # fleet_manager: fleet-aggregate, uses CMS REST endpoints (spec 2026-08-21).
    # DMS personas: all Tier 1 dealer-facing, no LTM hydration at v1
    # (spec 2026-08-26-cvx-dms-personas-extension).
    non_vin = {
        "fleet_manager",
        "warranty_agent",
        "dm_copilot",
        "comms_triage",
        "parts_intel",
        "compliance_agent",
    }
    expected = vin_scoped | non_vin
    assert set(PERSONA_PRODUCT_BINDINGS.keys()) == expected
    # fleet_manager (and any future non-VIN persona) carries an empty binding.
    for slug in non_vin:
        assert PERSONA_PRODUCT_BINDINGS[slug] == [], (
            f"{slug} is a non-VIN persona and must have an empty product binding"
        )
    # Every VIN-scoped persona has at least one product …
    for slug in vin_scoped:
        assert len(PERSONA_PRODUCT_BINDINGS[slug]) >= 1, f"{slug} has no product bindings"
    # … and vehicle_identity is bound to every VIN-scoped persona (universal anchor).
    for slug in vin_scoped:
        assert "vehicle_identity" in PERSONA_PRODUCT_BINDINGS[slug], (
            f"{slug} is missing vehicle_identity binding"
        )


# ---------------------------------------------------------------------------
# DMS persona structural tests (spec 2026-08-26-cvx-dms-personas-extension T6.3)
# ---------------------------------------------------------------------------
# Verifies that every DMS persona slug is properly wired in the supervisor
# registries AND is reachable via persona_map.py.
#
# These tests are "pure structural" — no network, no Claude, no AWS.  They
# import persona_map from the lambda root (same pattern as
# test_fleet_manager_persona.py) to confirm the end-to-end routing chain:
#
#     Cognito group → persona_map.infer_persona → persona_definitions registries
#
# If a persona is in the registries but missing from persona_map, it cannot be
# reached from the CMS web chat route.  If it is in persona_map but missing
# from the registries, supervisor dispatch crashes with a KeyError inside a
# conversational turn.  Both ends of the chain must be wired.
# ---------------------------------------------------------------------------

_DMS_PERSONA_SLUGS = [
    "warranty_agent",
    "dm_copilot",
    "comms_triage",
    "parts_intel",
    "compliance_agent",
]

# Cognito groups that should route to each DMS persona.
_DMS_GROUP_TO_SLUG = {
    "dealer-admin":      "service_advisor",    # broadest dealer view → service_advisor
    "service-advisor":   "service_advisor",    # DMS group variant of the CMS role
    "district-manager":  "dm_copilot",
    "bdc-rep":           "comms_triage",
    "parts-manager":     "parts_intel",
    "f-and-i-manager":   "service_advisor",    # customer-data access → service_advisor
    "dms-viewer":        "consumer",           # narrowest privilege → consumer
}


def test_dms_personas_in_persona_product_bindings():
    """All five DMS personas appear in PERSONA_PRODUCT_BINDINGS with [] bindings."""
    for slug in _DMS_PERSONA_SLUGS:
        assert slug in PERSONA_PRODUCT_BINDINGS, (
            f"DMS persona {slug!r} is missing from PERSONA_PRODUCT_BINDINGS"
        )
        assert PERSONA_PRODUCT_BINDINGS[slug] == [], (
            f"DMS persona {slug!r} must have [] binding at v1 "
            f"(no per-VIN LTM hydration); got {PERSONA_PRODUCT_BINDINGS[slug]!r}"
        )


def test_dms_personas_reachable_via_persona_map():
    """Each DMS Cognito group resolves to the expected CVX persona slug.

    Confirms the persona_map.py entries added by T6.3 are wired correctly.
    Without this, personas registered in all four supervisor registries are
    unreachable from the CMS web chat route (spec § Reachability).
    """
    import os
    import sys
    _lambda_root = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "lambdas", "api-assistant-chat"
    )
    if _lambda_root not in sys.path:
        sys.path.insert(0, _lambda_root)
    from persona_map import infer_persona

    for group, expected_slug in _DMS_GROUP_TO_SLUG.items():
        result = infer_persona({"cognito:groups": group})
        assert result == expected_slug, (
            f"Cognito group {group!r}: expected persona {expected_slug!r}, "
            f"got {result!r}.  Check persona_map._GROUP_MAP."
        )


def test_dms_personas_not_reachable_claimlessly():
    """Claimless callers must never land on a DMS persona.

    The DMS group mappings are in _GROUP_MAP (step 5 of infer_persona).  Step 5
    only fires when `groups` is non-empty, and the final fallback at step 6
    returns DEFAULT_PERSONA (fleet_driver) or _ANONYMOUS_PERSONA (consumer) —
    neither of which is a DMS persona.  This test verifies the invariant holds
    after the T6.3 additions.
    """
    import os
    import sys
    _lambda_root = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "lambdas", "api-assistant-chat"
    )
    if _lambda_root not in sys.path:
        sys.path.insert(0, _lambda_root)
    from persona_map import infer_persona

    dms_slugs = set(_DMS_PERSONA_SLUGS)
    for claims in [
        {},
        {"cognito:groups": ""},
        {"cognito:groups": None},
        {"custom:role": "dealer-admin"},     # a forged role that names a DMS group
        {"custom:role": "district-manager"}, # same
    ]:
        result = infer_persona(claims)
        assert result not in dms_slugs, (
            f"Claims {claims!r} returned DMS persona {result!r}. "
            "Claimless/anonymous callers must never land on a DMS persona."
        )


# ---------------------------------------------------------------------------
# Group 6.D — golden fixture normalization test
# ---------------------------------------------------------------------------

def test_normalization_matches_golden():
    """Each golden fixture row passes _normalize_row without exceptions.

    Asserts:
    - No exceptions raised
    - Result has found=True
    - Result has data_source injected
    """
    import json
    from pathlib import Path

    fixture_dir = (
        Path(__file__).parent / "fixtures" / "persona_loader_golden"
    )
    products = [
        "vehicle_identity",
        "service_records",
        "charging_sessions",
        "customer_360",
        "customer_interactions",
        "ota_campaigns",
    ]
    for product in products:
        fixture_path = fixture_dir / f"{product}.json"
        assert fixture_path.exists(), f"Missing fixture: {fixture_path}"
        rows = json.loads(fixture_path.read_text())
        assert isinstance(rows, list) and len(rows) >= 1, (
            f"{product}: fixture must have at least 1 row"
        )
        for row in rows:
            result = _normalize_row(product, row)
            assert result["found"] is True, f"{product}: found != True"
            assert result["data_source"] == product, (
                f"{product}: data_source not injected correctly"
            )
            # Original row keys are preserved
            for k in row:
                assert k in result, f"{product}: key '{k}' lost after normalization"


# ---------------------------------------------------------------------------
# R15 — Athena SQL injection guard
#
# These tests cover the guard described in the persona_loader module docstring
# (R15) and in issues/2026-08-03-cvx-persona-loader-sql-guard/report.md.
#
# Why this matters: `vehicle_id` and `customer_id` reach `_athena_query` from
# session context (bidi_app.py passes ctx.vin / ctx.customer_id), and Athena's
# StartQueryExecution accepts no bind parameters, so the allowlist regex is the
# ONLY barrier between request-path input and interpolated SQL. Before
# 2026-08-03 that barrier had no test coverage at all.
#
# The `test_no_product_branch_emits_sql_for_*` tests are deliberately
# enumeration-driven rather than per-product: they parse the product branches
# out of the source so that ADDING a new product branch without a validation
# call fails the suite instead of silently shipping an unguarded query.
#
# NOTE: the payload list below contains SQL-shaped strings purely as inert test
# fixtures. They are only ever passed to `_validate_id` / `_athena_query`, both
# of which are pure string functions — no database connection exists in this
# test module.
# ---------------------------------------------------------------------------

# Payloads that must never survive validation. Each targets a different escape:
# quote-breakout, statement-chaining, comment-truncation, whitespace/newline
# smuggling, and identifier-quoting.
_INJECTION_PAYLOADS = [
    "' OR '1'='1",
    "'; DELETE FROM vehicle_identity; --",
    "' UNION SELECT * FROM adp_prod_customer_360.customer_360 --",
    "VIN123' --",
    "VIN123'/*",
    'VIN123" OR "1"="1',
    "VIN123; SELECT 1",
    "VIN123 OR 1=1",
    "VIN123\nOR 1=1",
    "VIN123\tOR 1=1",
    "VIN123\\' OR 1=1",
    "VIN123%27",
    "`vehicle_identity`",
    "a" * 65,          # over the 64-char bound
    "",                 # empty
    "   ",              # whitespace only
]

_VALID_IDS = [
    "1HGBH41JXMN109186",   # realistic VIN
    "VEH-0025",
    "DRV-0055",
    "cust_000123",
    "a",
    "A" * 64,              # exactly at the bound
]


def _products_requiring_vehicle_id() -> list[str]:
    return ["vehicle_identity", "service_records", "charging_sessions", "ota_campaigns"]


def _products_requiring_customer_id() -> list[str]:
    return ["customer_360", "customer_interactions"]


def _all_athena_products() -> list[str]:
    """Product branches present in `_athena_query`, parsed from the source.

    Parsing rather than hardcoding means a newly-added branch shows up here
    automatically and must satisfy the guard tests below.
    """
    import inspect
    import re as _re

    from agents.supervisor import persona_loader as _pl

    src = inspect.getsource(_pl._athena_query)
    return _re.findall(r'if product == "([a-z0-9_]+)"', src)


class TestValidateId:
    """Unit coverage for the `_validate_id` allowlist."""

    @pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
    def test_rejects_injection_payloads(self, payload):
        assert _validate_id(payload, "vehicle_id") is False, (
            f"_validate_id accepted a dangerous value: {payload!r}"
        )

    @pytest.mark.parametrize("value", _VALID_IDS)
    def test_accepts_legitimate_ids(self, value):
        assert _validate_id(value, "vehicle_id") is True, (
            f"_validate_id rejected a legitimate id: {value!r}"
        )

    def test_rejects_none(self):
        assert _validate_id(None, "vehicle_id") is False


class TestValidateStage:
    """Unit coverage for the `_validate_stage` allowlist (added 2026-08-03)."""

    @pytest.mark.parametrize("value", ["staging", "prod", "dev", "test2", "a", "A" * 32])
    def test_accepts_legitimate_stages(self, value):
        assert _validate_stage(value) is True, f"rejected legitimate stage {value!r}"

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "",
            "   ",
            "staging; SELECT 1",
            "staging'",
            "staging.other",
            "staging-1",          # hyphen invalid in an unquoted identifier
            "staging vehicle",
            "staging\nprod",
            "a" * 33,             # over the 32-char bound
        ],
    )
    def test_rejects_unsafe_stages(self, payload):
        assert _validate_stage(payload) is False, (
            f"_validate_stage accepted a dangerous value: {payload!r}"
        )


class TestAthenaQueryGuard:
    """`_athena_query` must fail closed — return None, never unguarded SQL."""

    def test_source_parsing_found_expected_branches(self):
        """Guards the enumeration helper itself against silent drift."""
        products = _all_athena_products()
        assert len(products) == 6, f"expected 6 product branches, parsed {products}"
        assert set(products) == set(
            _products_requiring_vehicle_id() + _products_requiring_customer_id()
        ), f"branch list drifted: {products}"

    @pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
    def test_no_product_branch_emits_sql_for_malicious_vehicle_id(self, payload):
        for product in _products_requiring_vehicle_id():
            sql = _athena_query(product, "staging", payload, None)
            assert sql is None, (
                f"{product}: built SQL from a malicious vehicle_id {payload!r} -> {sql!r}"
            )

    @pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
    def test_no_product_branch_emits_sql_for_malicious_customer_id(self, payload):
        for product in _products_requiring_customer_id():
            sql = _athena_query(product, "staging", None, payload)
            assert sql is None, (
                f"{product}: built SQL from a malicious customer_id {payload!r} -> {sql!r}"
            )

    @pytest.mark.parametrize("payload", ["staging; SELECT 1", "staging'", "", None])
    def test_no_product_branch_emits_sql_for_malicious_stage(self, payload):
        for product in _all_athena_products():
            sql = _athena_query(product, payload, "VEH-0025", "cust_000123")
            assert sql is None, (
                f"{product}: built SQL from a malicious adp_stage {payload!r} -> {sql!r}"
            )

    def test_every_branch_requires_an_id(self):
        """No product may return SQL when both ids are absent."""
        for product in _all_athena_products():
            assert _athena_query(product, "staging", None, None) is None, (
                f"{product}: returned SQL with no ids supplied"
            )

    def test_unknown_product_returns_none(self):
        assert _athena_query("not_a_product", "staging", "VEH-0025", "cust_1") is None

    def test_valid_inputs_still_build_sql(self):
        """The guard must not be so strict that legitimate queries stop working."""
        for product in _products_requiring_vehicle_id():
            sql = _athena_query(product, "staging", "1HGBH41JXMN109186", None)
            assert sql is not None, f"{product}: valid vehicle_id produced no SQL"
            assert "1HGBH41JXMN109186" in sql
            assert "adp_staging_" in sql, f"{product}: stage not interpolated"
        for product in _products_requiring_customer_id():
            sql = _athena_query(product, "staging", None, "cust_000123")
            assert sql is not None, f"{product}: valid customer_id produced no SQL"
            assert "cust_000123" in sql
            assert "adp_staging_" in sql, f"{product}: stage not interpolated"

    def test_interpolated_id_stays_inside_a_quoted_literal(self):
        """Sanity: the id lands in a single-quoted literal and adds no quotes."""
        sql = _athena_query("vehicle_identity", "staging", "1HGBH41JXMN109186", None)
        assert "'1HGBH41JXMN109186'" in sql
        # An allowlisted id can never introduce an unbalanced quote.
        assert sql.count("'") % 2 == 0, f"unbalanced quotes in built SQL: {sql!r}"
