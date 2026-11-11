"""
Test suite: Finding + Action JSON Schema fixtures.

Validates that:
  1. All 'valid' fixtures pass schema validation.
  2. The negative scope fixture (two scope fields populated) FAILS validation —
     enforcing the exactly-one-of scope invariant from spec.md § 'Cross-surface
     Action data model — canonical'.
  3. The provenance enum accepts 'mocked' even though Agent 2 has no mocked
     seams (Spec 1b Agent 3 inherits this field — parent PRD § M14).
  4. Contract Addenda D1–D5 are enforced:
     - D1: fleet_id-scoped Finding with per-VIN evidence entries is valid.
     - D2: finding_key required; Finding.audit_trail[] present and valid.
     - D4: Action.status accepts 'dismissed' (terminal, distinct from 'cancelled').
     - D5: Action.status accepts 'failed' (terminal, distinct from 'declined');
           audit_trail transitions to 'declined'/'failed' carry failure_kind;
           transitions to 'failed' carry retry_count.

Spec: specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md
Task: tasks.md Group 1 — 'Freeze Finding + Action data model as JSON Schema fixtures'
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "agents" / "tier2" / "schemas"
FIXTURE_DIR = SCHEMA_DIR / "fixtures"

FINDING_SCHEMA_PATH = SCHEMA_DIR / "finding.schema.json"
ACTION_SCHEMA_PATH = SCHEMA_DIR / "action.schema.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schema fixtures (pytest fixtures)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def finding_schema() -> dict:
    return _load(FINDING_SCHEMA_PATH)


@pytest.fixture(scope="session")
def action_schema() -> dict:
    return _load(ACTION_SCHEMA_PATH)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _validate(schema: dict, instance: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    validator = Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(instance)]


# ---------------------------------------------------------------------------
# Schema file existence
# ---------------------------------------------------------------------------


def test_finding_schema_exists() -> None:
    assert FINDING_SCHEMA_PATH.exists(), f"Missing: {FINDING_SCHEMA_PATH}"


def test_action_schema_exists() -> None:
    assert ACTION_SCHEMA_PATH.exists(), f"Missing: {ACTION_SCHEMA_PATH}"


# ---------------------------------------------------------------------------
# Finding — valid fixtures
# ---------------------------------------------------------------------------


class TestFindingValidFixtures:
    """All 'valid' Finding fixtures must pass schema validation."""

    def test_finding_health_tires_valid(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_finding_health_brakes_mocked_provenance(self, finding_schema: dict) -> None:
        """A fixture with provenance='mocked' must pass — required by Spec 1b Agent 3.

        Agent 2 (Spec 1a) has no mocked seams itself, but the schema must accept
        'mocked' as a valid provenance value so Spec 1b can inherit without schema changes.
        """
        fixture = _load(FIXTURE_DIR / "finding_health_brakes_mocked_provenance.json")
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_finding_fleet_tires_addendum_d1(self, finding_schema: dict) -> None:
        """Addendum D1: fleet_id-scoped Finding with per-VIN evidence entries must be valid.

        Agent 2 fleet_owner persona writes ONE fleet_id-scoped Finding per
        (fleet_id, finding_kind) with per-VIN evidence[] entries.
        """
        fixture = _load(FIXTURE_DIR / "finding_fleet_tires_addendum_d1_valid.json")
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Addendum D1 fleet-scoped fixture failed:\n" + "\n".join(errors)

    def test_mocked_provenance_in_evidence_is_accepted(self, finding_schema: dict) -> None:
        """Mutation-style positive test: 'mocked' provenance must be in the allowed enum.

        This is a load-bearing constraint per the task: the provenance enum MUST
        include 'mocked' even though Agent 2 has no mocked seam.
        """
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        # Swap all evidence provenance to 'mocked'
        mutated = json.loads(json.dumps(fixture))
        for entry in mutated.get("evidence", []):
            entry["provenance"] = "mocked"
        errors = _validate(finding_schema, mutated)
        assert errors == [], (
            "Schema does not accept 'mocked' provenance — "
            "Spec 1b Agent 3 requires it: " + "\n".join(errors)
        )


# ---------------------------------------------------------------------------
# Finding — scope invariant (exactly-one-of) — LOAD-BEARING
# ---------------------------------------------------------------------------


class TestFindingScopeInvariant:
    """The exactly-one-of scope invariant must be enforced by the schema."""

    def test_scope_vin_only_is_valid(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        assert fixture["scope"] == {"vin": "1HGBH41JXMN109186"}, (
            "Fixture must have vin-only scope for this test"
        )
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_scope_owner_id_only_is_valid(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["scope"] = {"owner_id": "owner-usr-4a5b6c7d"}
        errors = _validate(finding_schema, mutated)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_scope_fleet_id_only_is_valid(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["scope"] = {"fleet_id": "fleet-west-001"}
        # Addendum D1: fleet_id-scoped Findings require per-entry vin in evidence
        for entry in mutated.get("evidence", []):
            entry["vin"] = fixture["scope"].get("vin", "1HGBH41JXMN109186")
        errors = _validate(finding_schema, mutated)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_scope_dealership_id_only_is_valid(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["scope"] = {"dealership_id": "dlr-west-coast-001"}
        errors = _validate(finding_schema, mutated)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_scope_district_id_only_is_valid(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["scope"] = {"district_id": "district-pac-nw"}
        errors = _validate(finding_schema, mutated)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_scope_two_fields_fails_validation(self, finding_schema: dict) -> None:
        """LOAD-BEARING: A fixture with two scope fields MUST fail validation.

        This is the exactly-one-of scope invariant from spec.md § 'Cross-surface
        Action data model — canonical': 'Exactly one of the scope fields is
        populated. A Finding without a scope is a schema bug.'

        The negative fixture at fixtures/finding_scope_two_fields_INVALID.json
        has both 'vin' and 'owner_id' populated. The schema must reject it.
        """
        fixture = _load(FIXTURE_DIR / "finding_scope_two_fields_INVALID.json")
        errors = _validate(finding_schema, fixture)
        assert errors != [], (
            "CRITICAL: schema accepted a Finding with two scope fields (vin + owner_id). "
            "The exactly-one-of scope invariant is BROKEN. "
            "This test must fail — the scope constraint must be enforced by the schema."
        )

    def test_scope_vin_plus_owner_id_fails_validation(self, finding_schema: dict) -> None:
        """Inline mutation: vin + owner_id populated together must fail."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["scope"] = {"vin": "1HGBH41JXMN109186", "owner_id": "owner-usr-4a5b6c7d"}
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted two scope fields (vin + owner_id). "
            "The exactly-one-of invariant is broken."
        )

    def test_scope_vin_plus_fleet_id_fails_validation(self, finding_schema: dict) -> None:
        """Inline mutation: vin + fleet_id populated together must fail."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["scope"] = {"vin": "1HGBH41JXMN109186", "fleet_id": "fleet-west-001"}
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted two scope fields (vin + fleet_id). "
            "The exactly-one-of invariant is broken."
        )

    def test_scope_empty_fails_validation(self, finding_schema: dict) -> None:
        """A Finding with zero scope fields populated must also fail.

        'A Finding without a scope is a schema bug' (spec.md).
        """
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["scope"] = {}
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted an empty scope. "
            "The exactly-one-of invariant must require exactly one populated field."
        )


# ---------------------------------------------------------------------------
# Finding — Addendum D2: finding_key required
# ---------------------------------------------------------------------------


class TestFindingKeyAddendumD2:
    """Addendum D2: finding_key is a required field on Finding."""

    def test_finding_key_present(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        assert "finding_key" in fixture, "Fixture must have finding_key"
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_missing_finding_key_fails_validation(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        del mutated["finding_key"]
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted a Finding without finding_key. "
            "finding_key is required per Addendum D2."
        )

    def test_finding_audit_trail_present(self, finding_schema: dict) -> None:
        """Addendum D2: Finding.audit_trail[] is required (may be empty on first write)."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        assert "audit_trail" in fixture, "Fixture must have audit_trail"
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_finding_audit_trail_entry_valid(self, finding_schema: dict) -> None:
        """A Finding with a populated audit_trail (in-place update) must be valid."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["audit_trail"] = [
            {
                "at": "2026-09-25T02:00:00Z",
                "agent_version": "1.0.1",
                "inputs_hash": "049fab714fa0ed1be665a633770cc46f996f4161700a9c27b5c447b3ee72cc36",
                "from_expires_at": "2026-09-25T02:00:00Z",
                "to_expires_at": "2026-10-02T02:00:00Z",
                "note": "In-place update: condition still open, TTL extended"
            }
        ]
        errors = _validate(finding_schema, mutated)
        assert errors == [], (
            "Finding with populated audit_trail entry should be valid (Addendum D2):\n"
            + "\n".join(errors)
        )


# ---------------------------------------------------------------------------
# Action — valid fixtures
# ---------------------------------------------------------------------------


class TestActionValidFixtures:
    """All 'valid' Action fixtures must pass schema validation."""

    def test_action_dms_book_visit_valid(self, action_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "action_dms_book_visit_valid.json")
        errors = _validate(action_schema, fixture)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_action_ios_owner_acknowledge_valid(self, action_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "action_ios_owner_acknowledge_valid.json")
        errors = _validate(action_schema, fixture)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_action_none_target_valid(self, action_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        errors = _validate(action_schema, fixture)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)

    def test_action_dismissed_addendum_d4_valid(self, action_schema: dict) -> None:
        """Addendum D4: 'dismissed' is a first-class terminal status."""
        fixture = _load(FIXTURE_DIR / "action_dismissed_addendum_d4_valid.json")
        errors = _validate(action_schema, fixture)
        assert errors == [], (
            "Addendum D4 dismissed fixture failed:\n" + "\n".join(errors)
        )

    def test_action_declined_4xx_addendum_d5_valid(self, action_schema: dict) -> None:
        """Addendum D5: 'declined' (terminal 4xx refusal) with failure_kind=target_refused."""
        fixture = _load(FIXTURE_DIR / "action_declined_4xx_addendum_d5_valid.json")
        errors = _validate(action_schema, fixture)
        assert errors == [], (
            "Addendum D5 declined-4xx fixture failed:\n" + "\n".join(errors)
        )

    def test_action_failed_5xx_exhausted_addendum_d5_valid(self, action_schema: dict) -> None:
        """Addendum D5: 'failed' (exhausted retries on 5xx) with failure_kind + retry_count."""
        fixture = _load(FIXTURE_DIR / "action_failed_5xx_exhausted_addendum_d5_valid.json")
        errors = _validate(action_schema, fixture)
        assert errors == [], (
            "Addendum D5 failed-5xx-exhausted fixture failed:\n" + "\n".join(errors)
        )


# ---------------------------------------------------------------------------
# Action — status enum completeness (Addenda D4, D5)
# ---------------------------------------------------------------------------


class TestActionStatusEnum:
    """Action.status must include all eight values per Addenda D4 and D5."""

    @pytest.mark.parametrize(
        "status",
        [
            "pending_execution",
            "executed",
            "in_progress",
            "completed",
            "cancelled",
            "declined",
            "dismissed",
            "failed",
        ],
    )
    def test_all_eight_status_values_accepted(
        self, status: str, action_schema: dict
    ) -> None:
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["status"] = status
        # Build a valid audit_trail for this status
        audit_entry: dict = {
            "at": "2026-09-18T12:00:00Z",
            "by": "owner-usr-4a5b6c7d",
            "from_status": "pending_execution",
            "to_status": status,
        }
        # Addendum D5: declined/failed require failure_kind
        if status == "declined":
            audit_entry["failure_kind"] = "target_refused"
        elif status == "failed":
            audit_entry["failure_kind"] = "executor_exhausted_retries"
            audit_entry["retry_count"] = 3
        mutated["audit_trail"] = [audit_entry]
        errors = _validate(action_schema, mutated)
        assert errors == [], (
            f"status={status!r} was rejected — enum must include all 8 values:\n"
            + "\n".join(errors)
        )

    def test_dismissed_is_accepted(self, action_schema: dict) -> None:
        """LOAD-BEARING: 'dismissed' must be in the status enum (Addendum D4)."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["status"] = "dismissed"
        mutated["audit_trail"] = [{
            "at": "2026-09-18T12:00:00Z",
            "by": "owner-usr-4a5b6c7d",
            "from_status": "pending_execution",
            "to_status": "dismissed",
        }]
        errors = _validate(action_schema, mutated)
        assert errors == [], (
            "CRITICAL: 'dismissed' rejected from Action.status. "
            "Addendum D4 requires it as a first-class terminal state. "
            "Missing 'dismissed' / 'failed' is a schema bug per the task Constraints."
        )

    def test_failed_is_accepted(self, action_schema: dict) -> None:
        """LOAD-BEARING: 'failed' must be in the status enum (Addendum D5)."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["status"] = "failed"
        mutated["audit_trail"] = [{
            "at": "2026-09-18T12:00:00Z",
            "by": "system-dms-executor",
            "from_status": "executed",
            "to_status": "failed",
            "failure_kind": "executor_exhausted_retries",
            "retry_count": 3,
        }]
        errors = _validate(action_schema, mutated)
        assert errors == [], (
            "CRITICAL: 'failed' rejected from Action.status. "
            "Addendum D5 requires it (executor exhausted retries). "
            "Missing 'dismissed' / 'failed' is a schema bug per the task Constraints."
        )

    def test_unknown_status_is_rejected(self, action_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["status"] = "totally_unknown"
        errors = _validate(action_schema, mutated)
        assert errors != [], "Schema accepted unknown status — enum must be closed."


# ---------------------------------------------------------------------------
# Action — target_system enum coverage
# ---------------------------------------------------------------------------


class TestActionTargetSystemEnum:
    """Every target_system value must be accepted by the schema."""

    @pytest.mark.parametrize(
        "target_system",
        ["dms", "vehicle-command", "retail", "none", "chat", "ios-owner", "cms"],
    )
    def test_all_target_systems_accepted(
        self, target_system: str, action_schema: dict
    ) -> None:
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["target_system"] = target_system
        errors = _validate(action_schema, mutated)
        assert errors == [], (
            f"target_system={target_system!r} was rejected:\n" + "\n".join(errors)
        )

    def test_unknown_target_system_is_rejected(self, action_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["target_system"] = "unknown-executor"
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "Schema accepted an unknown target_system 'unknown-executor' — enum must be closed."
        )


# ---------------------------------------------------------------------------
# Action — action_kind enum coverage
# ---------------------------------------------------------------------------


class TestActionKindEnum:
    """Every action_kind value must be accepted; unknown kinds must be rejected."""

    @pytest.mark.parametrize(
        "action_kind",
        [
            # Spec 1a values
            "book_combined_visit",
            "schedule_inspection",
            "update_charging_schedule",
            "file_warranty_pre",
            "surface_recall",
            "acknowledge_only",
            # Stage A additions (spec 2026-10-02-fi-action-queue-stage-a D3)
            "book_recall_service",
            "ground_vehicle",
            "move_vehicles",
            "exempt_recall",
            "complete_recall",
            "submit_warranty_claim",
            "apply_cost_action",
            # Retail kinds: contract gap closed; _RETAIL_KINDS gate NOT lifted (D3)
            "lead_capture",
            "configurator_recommend",
        ],
    )
    def test_all_action_kinds_accepted(
        self, action_kind: str, action_schema: dict
    ) -> None:
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["action_kind"] = action_kind
        errors = _validate(action_schema, mutated)
        assert errors == [], (
            f"action_kind={action_kind!r} was rejected:\n" + "\n".join(errors)
        )

    def test_unknown_action_kind_is_rejected(self, action_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["action_kind"] = "fly_to_moon"
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "Schema accepted unknown action_kind 'fly_to_moon' — enum must be closed."
        )


# ---------------------------------------------------------------------------
# Action — mocked provenance in copied evidence
# ---------------------------------------------------------------------------


class TestActionMockedProvenance:
    """Evidence copied from a Finding to an Action must also accept 'mocked' provenance."""

    def test_mocked_provenance_in_action_evidence_accepted(
        self, action_schema: dict
    ) -> None:
        fixture = _load(FIXTURE_DIR / "action_dms_book_visit_valid.json")
        mutated = json.loads(json.dumps(fixture))
        for entry in mutated.get("evidence", []):
            entry["provenance"] = "mocked"
        errors = _validate(action_schema, mutated)
        assert errors == [], (
            "Action schema does not accept 'mocked' provenance in evidence. "
            "Required for Spec 1b Agent 3 inherited actions: " + "\n".join(errors)
        )


# ---------------------------------------------------------------------------
# Finding severity enum
# ---------------------------------------------------------------------------


class TestFindingSeverityEnum:
    @pytest.mark.parametrize(
        "severity", ["p0", "attention", "soon", "routine", "informational"]
    )
    def test_all_severities_accepted(
        self, severity: str, finding_schema: dict
    ) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["severity"] = severity
        errors = _validate(finding_schema, mutated)
        assert errors == [], (
            f"severity={severity!r} rejected:\n" + "\n".join(errors)
        )

    def test_unknown_severity_is_rejected(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["severity"] = "critical"
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            "Schema accepted unknown severity 'critical' — enum must be closed."
        )


# ---------------------------------------------------------------------------
# Finding kind enum (Spec 1a values)
# ---------------------------------------------------------------------------


class TestFindingKindEnum:
    @pytest.mark.parametrize(
        "finding_kind",
        ["health.tires", "health.brakes", "health.battery", "health.diagnostics"],
    )
    def test_spec1a_finding_kinds_accepted(
        self, finding_kind: str, finding_schema: dict
    ) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["finding_kind"] = finding_kind
        errors = _validate(finding_schema, mutated)
        assert errors == [], (
            f"finding_kind={finding_kind!r} rejected:\n" + "\n".join(errors)
        )

    def test_schema_and_openapi_finding_kind_enums_are_equal(
        self, finding_schema: dict
    ) -> None:
        """S1: the finding_kind enum in finding.schema.json must equal the one in OpenAPI.

        Only the byte pin currently guards a dropped value; a later pin re-rotation would
        let that through. This test catches drift without relying on the byte pin.

        Mutation: remove 'health.diagnostics' from either source → this test fails.
        """
        yaml = pytest.importorskip("yaml")
        openapi_path = (
            REPO_ROOT / "docs" / "api" / "avx-findings-actions.openapi.yaml"
        )
        openapi = yaml.safe_load(openapi_path.read_text(encoding="utf-8"))

        # Extract the schema enum.
        schema_enum = set(finding_schema["properties"]["finding_kind"]["enum"])

        # Extract the OpenAPI enum from the inline Finding schema's finding_kind property.
        # The Finding schema is inlined under components.schemas.Finding.
        finding_schema_oa = openapi.get("components", {}).get("schemas", {}).get("Finding", {})
        oa_enum = set(
            finding_schema_oa.get("properties", {})
            .get("finding_kind", {})
            .get("enum", [])
        )

        assert schema_enum, "finding.schema.json finding_kind enum must not be empty"
        assert oa_enum, (
            "OpenAPI Finding.finding_kind enum must not be empty — "
            "check that components.schemas.Finding.properties.finding_kind.enum exists"
        )
        missing_from_oa = schema_enum - oa_enum
        missing_from_schema = oa_enum - schema_enum
        assert not missing_from_oa and not missing_from_schema, (
            "finding_kind enum mismatch between finding.schema.json and OpenAPI:\n"
            f"  In schema but not OpenAPI: {sorted(missing_from_oa)}\n"
            f"  In OpenAPI but not schema: {sorted(missing_from_schema)}\n\n"
            "Mutation catch: removing 'health.diagnostics' from either source fails here."
        )


# ---------------------------------------------------------------------------
# Finding — headline length constraint
# ---------------------------------------------------------------------------


class TestFindingHeadlineLength:
    def test_headline_max_120_chars_accepted(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["headline"] = "A" * 120
        errors = _validate(finding_schema, mutated)
        assert errors == [], "Exactly 120-char headline must be accepted"

    def test_headline_121_chars_rejected(self, finding_schema: dict) -> None:
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["headline"] = "A" * 121
        errors = _validate(finding_schema, mutated)
        assert errors != [], "121-char headline must be rejected (maxLength=120)"


# ---------------------------------------------------------------------------
# Addendum D1 — fleet-scoped Finding negative: fleet_id scope without per-VIN vin
# (Note: the schema accepts fleet_id-scoped Findings with or without per-entry vin.
#  The test that fleet_id-scoped fixture WITH per-VIN vin is valid is in
#  TestFindingValidFixtures.test_finding_fleet_tires_addendum_d1)
# ---------------------------------------------------------------------------


class TestAddendumD1FleetScope:
    """Addendum D1: per-VIN evidence entries in fleet_id-scoped Findings."""

    def test_fleet_scoped_finding_with_per_vin_evidence_valid(
        self, finding_schema: dict
    ) -> None:
        """Agent 2 fleet_owner fixture with per-VIN evidence vin fields must be valid."""
        fixture = _load(FIXTURE_DIR / "finding_fleet_tires_addendum_d1_valid.json")
        assert fixture["scope"] == {"fleet_id": "fleet-meridian-001"}
        # Verify per-entry vin attribution is present
        vins = [e.get("vin") for e in fixture["evidence"]]
        assert len(vins) == 2 and all(v is not None for v in vins), (
            "Fleet-scoped fixture must have per-VIN evidence entries"
        )
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Validation errors:\n" + "\n".join(errors)



# ---------------------------------------------------------------------------
# Verify-criteria negative tests (a), (b), (c)
# ---------------------------------------------------------------------------


class TestVerifyCriteriaNegative:
    """Additional schema-level negative tests required by the task's Verify criteria."""

    def test_a_fleet_scope_without_per_entry_vin_fails(
        self, finding_schema: dict
    ) -> None:
        """Verify criterion (a): fleet_id-scoped Finding with no per-entry vin MUST fail.

        Addendum D1: when scope.fleet_id is set, each evidence entry must carry
        a 'vin' attribution field so the iOS renderer can surface per-vehicle details.
        """
        fixture = _load(FIXTURE_DIR / "finding_fleet_tires_addendum_d1_valid.json")
        assert fixture["scope"] == {"fleet_id": "fleet-meridian-001"}, (
            "Fixture must be fleet-scoped"
        )
        mutated = json.loads(json.dumps(fixture))
        # Strip vin from all evidence entries
        for entry in mutated["evidence"]:
            entry.pop("vin", None)
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted a fleet_id-scoped Finding whose evidence[] "
            "entries have no 'vin' attribution. Addendum D1 requires per-VIN evidence "
            "entries for fleet-scoped Findings."
        )

    def test_b_missing_finding_key_fails(self, finding_schema: dict) -> None:
        """Verify criterion (b): Finding without finding_key must fail (Addendum D2)."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        del mutated["finding_key"]
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted a Finding without finding_key. "
            "finding_key is required per Addendum D2."
        )

    def test_c_action_declined_without_failure_kind_fails(
        self, action_schema: dict
    ) -> None:
        """Verify criterion (c): Action transitioning to 'declined' without failure_kind MUST fail.

        Addendum D5: failure_kind is required on transitions into 'declined' or 'failed'.
        """
        fixture = _load(FIXTURE_DIR / "action_declined_4xx_addendum_d5_valid.json")
        mutated = json.loads(json.dumps(fixture))
        # Remove failure_kind from the terminal transition
        for entry in mutated["audit_trail"]:
            if entry.get("to_status") == "declined":
                entry.pop("failure_kind", None)
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted Action transition to 'declined' without failure_kind. "
            "Addendum D5 requires failure_kind on declined/failed transitions."
        )

    def test_c_action_failed_without_failure_kind_fails(
        self, action_schema: dict
    ) -> None:
        """Verify criterion (c) for 'failed': Action transitioning to 'failed' without failure_kind MUST fail."""
        fixture = _load(FIXTURE_DIR / "action_failed_5xx_exhausted_addendum_d5_valid.json")
        mutated = json.loads(json.dumps(fixture))
        # Remove failure_kind from the terminal transition
        for entry in mutated["audit_trail"]:
            if entry.get("to_status") == "failed":
                entry.pop("failure_kind", None)
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted Action transition to 'failed' without failure_kind. "
            "Addendum D5 requires failure_kind on declined/failed transitions."
        )


# ---------------------------------------------------------------------------
# Contract v2 re-freeze regression guards
#
# Every test in this class FAILED TO EXIST under contract v1, and every
# property it pins validated as ACCEPTED under v1 while all 67 other tests in
# this file passed. That is the point: v1 declared the Addenda D1/D5 fields but
# did not bind them, so the enum values were present and the guarantees were
# not. See test_contract_fixtures_byte_identical.py § "RE-FREEZE" and
# decisions.md § "2026-09-18 — Contract v2 re-freeze".
# ---------------------------------------------------------------------------


class TestAddendumD1FleetEvidenceNonVacuous:
    """
    G1: a fleet-scoped Finding must carry at least one per-VIN evidence entry.

    Under v1 the per-entry `vin` requirement lived under `items`, which JSON
    Schema evaluates vacuously on an empty array — so a fleet-scoped Finding
    with `evidence: []` passed. D1's whole claim is that fleet aggregation
    happens at WRITE time with per-VIN attribution; an empty evidence array is
    precisely the state that claim forbids.
    """

    def test_fleet_scoped_finding_with_empty_evidence_fails(
        self, finding_schema: dict
    ) -> None:
        fixture = _load(FIXTURE_DIR / "finding_fleet_tires_addendum_d1_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["evidence"] = []
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted a scope.fleet_id Finding with evidence: []. "
            "Addendum D1 requires per-VIN evidence attribution on fleet-scoped "
            "Findings; an empty array satisfies `items` vacuously, so minItems: 1 "
            "is what actually enforces D1."
        )

    def test_vin_scoped_finding_with_empty_evidence_still_allowed(
        self, finding_schema: dict
    ) -> None:
        """
        Positive control / scope check: the minItems constraint must apply ONLY to
        fleet-scoped Findings. A vin-scoped Finding may legitimately carry zero
        evidence entries (the absent-data scenario the schema documents).
        """
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["evidence"] = []
        errors = _validate(finding_schema, mutated)
        assert errors == [], (
            "The D1 minItems guard leaked outside fleet scope: a vin-scoped "
            f"Finding with no evidence must remain valid. Errors: {errors}"
        )


class TestAddendumD5FailureKindBinding:
    """
    G2/G3/G4: `failure_kind` must be bound to the status it explains, and a
    `failed` transition must record how many retries were spent.

    D5 exists to separate two operationally different outcomes: 'declined' =
    the dealer refused (terminal 4xx), 'failed' = we could not reach the dealer
    (transient, retries exhausted). Under v1 either status accepted either
    failure_kind, so the audit trail could not actually tell them apart — the
    distinction existed in prose and in the enum, but not in the contract.
    """

    def test_declined_with_transient_failure_kind_fails(
        self, action_schema: dict
    ) -> None:
        """G2: 'declined' must not borrow a transient failure_kind."""
        fixture = _load(FIXTURE_DIR / "action_declined_4xx_addendum_d5_valid.json")
        mutated = json.loads(json.dumps(fixture))
        for entry in mutated["audit_trail"]:
            if entry.get("to_status") == "declined":
                entry["failure_kind"] = "executor_exhausted_retries"
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted to_status='declined' with "
            "failure_kind='executor_exhausted_retries'. Addendum D5 requires "
            "declined => target_refused; without that binding 'dealer said no' "
            "and 'we could not reach the dealer' are indistinguishable."
        )

    def test_failed_with_target_refused_failure_kind_fails(
        self, action_schema: dict
    ) -> None:
        """G3: the inverse mis-binding must also be rejected."""
        fixture = _load(
            FIXTURE_DIR / "action_failed_5xx_exhausted_addendum_d5_valid.json"
        )
        mutated = json.loads(json.dumps(fixture))
        for entry in mutated["audit_trail"]:
            if entry.get("to_status") == "failed":
                entry["failure_kind"] = "target_refused"
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted to_status='failed' with "
            "failure_kind='target_refused'. Addendum D5 constrains failed to "
            "{executor_exhausted_retries, executor_terminal_error}."
        )

    def test_failed_without_retry_count_fails(self, action_schema: dict) -> None:
        """G4: a 'failed' transition must record retry_count."""
        fixture = _load(
            FIXTURE_DIR / "action_failed_5xx_exhausted_addendum_d5_valid.json"
        )
        mutated = json.loads(json.dumps(fixture))
        for entry in mutated["audit_trail"]:
            if entry.get("to_status") == "failed":
                entry.pop("retry_count", None)
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted to_status='failed' without retry_count. "
            "spec.md couples retry_count to the failed transition and Addendum "
            "D5 names test_failed_transition_records_retry_count_in_audit_trail."
        )

    def test_declined_without_retry_count_remains_valid(
        self, action_schema: dict
    ) -> None:
        """
        Interpretation guard (flagged for the user in decisions.md): retry_count
        is required on 'failed' but NOT on 'declined', because a terminal 4xx is
        defined as 'no retry fired'. If the user rules that declined must also
        carry retry_count: 0, this test is the one to invert.
        """
        fixture = _load(FIXTURE_DIR / "action_declined_4xx_addendum_d5_valid.json")
        mutated = json.loads(json.dumps(fixture))
        for entry in mutated["audit_trail"]:
            entry.pop("retry_count", None)
        errors = _validate(action_schema, mutated)
        assert errors == [], (
            "A 'declined' transition without retry_count must remain valid "
            f"(terminal 4xx => no retry fired). Errors: {errors}"
        )

    def test_shipped_terminal_fixtures_still_validate(
        self, action_schema: dict
    ) -> None:
        """
        Positive control: tightening the bindings must not invalidate any shipped
        fixture. If this fails, the v2 constraints are stricter than the contract
        the fixtures encode.
        """
        for name in (
            "action_declined_4xx_addendum_d5_valid.json",
            "action_failed_5xx_exhausted_addendum_d5_valid.json",
            "action_dismissed_addendum_d4_valid.json",
        ):
            errors = _validate(action_schema, _load(FIXTURE_DIR / name))
            assert errors == [], f"{name} no longer validates under v2: {errors}"


class TestApiContractActionScopeSurface:
    """
    G5: the `/actions/*` scope surface must mirror `/findings/*`.

    spec.md § "API contract" defines the actions read surface as "same scope
    surface as findings + dealership + ios-owner". Under v1 only 2 of 6 scope
    routes existed, while the document's own `info.description` asserted the
    three stub equivalents were present — the contract contradicted itself in
    the one artifact Spec 1b and Spec 2 pin against.
    """

    _OPENAPI_PATH = (
        Path(__file__).parent.parent.parent
        / "docs"
        / "api"
        / "avx-findings-actions.openapi.yaml"
    )

    _REQUIRED_ACTION_ROUTES = (
        "/actions/vehicle/{vin}",
        "/actions/owner/{owner_id}",
        "/actions/owner-fleet/{owner_id}",
        "/actions/fleet/{fleet_id}",
        "/actions/dealership/{dealer_id}",
        "/actions/district/{district_id}",
    )

    @pytest.fixture(scope="class")
    def openapi(self) -> dict:
        yaml = pytest.importorskip("yaml")
        return yaml.safe_load(self._OPENAPI_PATH.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("route", _REQUIRED_ACTION_ROUTES)
    def test_action_scope_route_present(self, openapi: dict, route: str) -> None:
        assert route in openapi["paths"], (
            f"Frozen API contract is missing {route}. spec.md § 'API contract' "
            "defines the /actions/* read surface as the same scope surface as "
            "/findings/*; Spec 1b and Spec 2 inherit this document by reference, "
            "so a missing route forces a downstream contract fork."
        )

    def test_findings_and_actions_scope_surfaces_are_symmetric(
        self, openapi: dict
    ) -> None:
        def scopes(prefix: str) -> set[str]:
            # Match only literal scope segments: /<prefix>/<scope>/{param}.
            # Excludes id-addressed routes like /actions/{action_id} and
            # sub-resource routes like /findings/{finding_id}/approve.
            # Pattern uses [^/]+ (not [a-z0-9-]+) so unconventional casing or
            # underscores are matched, not silently skipped (Fix Group 8 W3).
            pattern = re.compile(r"^" + re.escape(prefix) + r"(?P<scope>[^/]+)/\{[^/]+\}$")
            return {
                m.group("scope")
                for m in (pattern.match(p) for p in openapi["paths"])
                if m is not None
            }

        f, a = scopes("/findings/"), scopes("/actions/")
        assert f and a, f"scope extraction found nothing: findings={f} actions={a}"
        assert f == a, (
            "Findings and Actions scope surfaces diverged.\n"
            f"  findings-only: {sorted(f - a)}\n"
            f"  actions-only:  {sorted(a - f)}"
        )

    def test_no_separate_ios_owner_route(self, openapi: dict) -> None:
        """
        ios-owner is deliberately served by GET /actions/owner/{owner_id} rather
        than its own route — the reverse-direction (dealer-proposed) Actions are
        the same owner's Actions. Pinned so nobody 'fixes' the asymmetry by
        adding a duplicate surface.
        """
        assert "/actions/ios-owner/{owner_id}" not in openapi["paths"], (
            "A separate /actions/ios-owner route was added. Spec 1a folds "
            "reverse-direction Actions into GET /actions/owner/{owner_id}."
        )


class TestApiContractStatusEnumParity:
    """
    Fix Group 1 / review Cycle 1 Critical: every status enum in the OpenAPI must
    enumerate the full `Action.status` set from action.schema.json.

    The defect this guards: `components/parameters/StatusFilter` shipped with 6 of
    8 values, omitting `dismissed` (D4) and `failed` (D5) — the two the addenda had
    just added. `Action.status` and both `ActionAuditEntry` status fields were
    correct, so the contract disagreed with itself, and the omission made exactly
    the new terminal states unfilterable through the API. Asserting parity across
    EVERY status enum, rather than spot-checking StatusFilter, is what makes this a
    property test instead of a regression test for one site.
    """

    _OPENAPI_PATH = (
        Path(__file__).parent.parent.parent
        / "docs"
        / "api"
        / "avx-findings-actions.openapi.yaml"
    )

    @staticmethod
    def _status_enum_sites(node: object, path: str = "") -> list[tuple[str, list]]:
        """Every enum in the document that is recognisably an Action.status enum."""
        found: list[tuple[str, list]] = []
        if isinstance(node, dict):
            enum = node.get("enum")
            if isinstance(enum, list) and "pending_execution" in enum:
                found.append((path, enum))
            for key, value in node.items():
                found.extend(
                    TestApiContractStatusEnumParity._status_enum_sites(
                        value, f"{path}/{key}"
                    )
                )
        elif isinstance(node, list):
            for i, value in enumerate(node):
                found.extend(
                    TestApiContractStatusEnumParity._status_enum_sites(
                        value, f"{path}[{i}]"
                    )
                )
        return found

    def test_every_status_enum_matches_action_schema(
        self, action_schema: dict
    ) -> None:
        yaml = pytest.importorskip("yaml")
        openapi = yaml.safe_load(self._OPENAPI_PATH.read_text(encoding="utf-8"))
        canonical = set(action_schema["properties"]["status"]["enum"])

        sites = self._status_enum_sites(openapi)
        assert sites, "found no Action.status enums in the OpenAPI — extractor is broken"

        drift = {
            path: {
                "missing": sorted(canonical - set(enum)),
                "extra": sorted(set(enum) - canonical),
            }
            for path, enum in sites
            if set(enum) != canonical
        }
        assert not drift, (
            "OpenAPI status enums drifted from action.schema.json "
            f"(canonical = {sorted(canonical)}):\n"
            + "\n".join(
                f"  {p}: missing={d['missing']} extra={d['extra']}"
                for p, d in drift.items()
            )
        )

    def test_status_filter_covers_all_eight(self, action_schema: dict) -> None:
        """Named guard for the specific parameter that drifted, for clear failure output."""
        yaml = pytest.importorskip("yaml")
        openapi = yaml.safe_load(self._OPENAPI_PATH.read_text(encoding="utf-8"))
        status_filter = openapi["components"]["parameters"]["StatusFilter"]["schema"]["enum"]
        canonical = action_schema["properties"]["status"]["enum"]
        assert set(status_filter) == set(canonical), (
            "StatusFilter must enumerate every Action.status value, including "
            "'dismissed' (D4) and 'failed' (D5). Missing: "
            f"{sorted(set(canonical) - set(status_filter))}"
        )


class TestApiContractAuthorizationShape:
    """
    Fix Group 2 / security review Cycle 1 Warnings. Both are contract-shape defects
    that would have propagated into Spec 1b and Spec 2 through by-reference
    inheritance, so they are pinned here rather than left to handler-side prose.
    """

    _OPENAPI_PATH = (
        Path(__file__).parent.parent.parent
        / "docs"
        / "api"
        / "avx-findings-actions.openapi.yaml"
    )

    @pytest.fixture(scope="class")
    def openapi(self) -> dict:
        yaml = pytest.importorskip("yaml")
        return yaml.safe_load(self._OPENAPI_PATH.read_text(encoding="utf-8"))

    def test_approve_route_cannot_originate_ios_owner_actions(
        self, openapi: dict, action_schema: dict
    ) -> None:
        """
        W1: `ios-owner` must NOT be an accepted `target_system` on
        POST /findings/{finding_id}/approve.

        ios-owner is the reverse-direction dealer-to-owner proposal channel. If the
        owner-approval route accepts it, any caller who can approve a Finding can
        originate a card on a consumer's iOS feed while bypassing the broker's
        channel policy, consent gating, quiet hours and rate limits. The broker —
        not the approve route — owns the decision to reach a customer.

        The constraint is expressed machine-readably via a oneOf structure (contract
        v2.2 W1). We collect all target_system enums from every branch and verify
        that none of them includes 'ios-owner'.
        """
        schema = openapi["components"]["schemas"]["ApproveFindingRequest"]
        # Support both legacy flat-enum and v2.2 oneOf structures
        all_enums: list[list[str]] = []
        if "properties" in schema and "target_system" in schema["properties"]:
            ts = schema["properties"]["target_system"]
            if "enum" in ts:
                all_enums.append(ts["enum"])
        for branch in schema.get("oneOf", []):
            props = branch.get("properties", {})
            if "target_system" in props and "enum" in props["target_system"]:
                all_enums.append(props["target_system"]["enum"])

        assert all_enums, (
            "ApproveFindingRequest has no target_system enum — cannot verify constraint."
        )
        for enum_values in all_enums:
            assert "ios-owner" not in enum_values, (
                "ApproveFindingRequest.target_system accepts 'ios-owner'. That lets the "
                "owner-approval route originate dealer-to-owner cards, bypassing the "
                "broker's consent and channel policy. Keep it off this route; Spec 2's "
                "POST /actions and the D5 escalation path are where ios-owner Actions "
                "legitimately originate."
            )

    def test_action_target_system_still_carries_all_six(
        self, openapi: dict, action_schema: dict
    ) -> None:
        """
        Positive control for the above: narrowing the *request* enum must not narrow
        the *Action* enum. An Action with target_system 'ios-owner' is legitimate —
        it just cannot be created through the approve route.
        """
        canonical = set(action_schema["properties"]["target_system"]["enum"])
        assert "ios-owner" in canonical, "action.schema.json lost the ios-owner target"
        doc_enum = set(
            openapi["components"]["schemas"]["Action"]["properties"]["target_system"][
                "enum"
            ]
        )
        assert doc_enum == canonical, (
            "Action.target_system in the OpenAPI drifted from action.schema.json: "
            f"missing={sorted(canonical - doc_enum)} extra={sorted(doc_enum - canonical)}"
        )
    # Principals that are permitted in x-scope-predicate.bypass.
    # Wildcard and any-authenticated forms are rejected (Cycle 4 W2).
    _BYPASS_ALLOWLIST: frozenset[str] = frozenset({"platform-admin"})

    # Frozen expected bypass posture per route (Fix Group 8 C1 + W2).
    #
    # Fleet routes: no bypass — `platform-admin` satisfies the Cognito *group*
    # requirement but is still bound by fleet membership (contract v2.2-final W3).
    # Dealership and district routes: `platform-admin` bypasses the membership
    # predicate (the group alone is not scope-binding for those tiers).
    #
    # Changing any entry here is a deliberate, reviewable security decision.
    # The key set must equal the document-derived roster; a seventh route without
    # an entry is a test failure, not a silent pass.
    _EXPECTED_BYPASS: dict[str, frozenset[str]] = {
        "/findings/fleet/{fleet_id}":           frozenset(),
        "/actions/fleet/{fleet_id}":            frozenset(),
        "/approval-rules/fleet/{fleet_id}":     frozenset(),
        "/findings/dealership/{dealer_id}":     frozenset({"platform-admin"}),
        "/actions/dealership/{dealer_id}":      frozenset({"platform-admin"}),
        "/findings/district/{district_id}":     frozenset({"platform-admin"}),
        "/actions/district/{district_id}":      frozenset({"platform-admin"}),
    }

    # Frozen expected mechanism string per route (Fix Group 9 W1).
    #
    # Replaces the ``scope_param in mechanism`` shape heuristic, which was
    # defeated in both directions: 12/12 param-affixed placeholders passed
    # (``"TBD for fleet_id"``, ``"N/A - see ticket for fleet_id"``), and 4/4
    # plausible legitimate mechanisms were rejected (Cycle 6 W1).  A shape check
    # over free text is prose-reading, which this spec forbids.
    #
    # Exact equality means any one-word change — legitimate or not — requires a
    # deliberate, reviewable table edit.  Param-affixed placeholders fail because
    # they are not the exact string.
    _EXPECTED_MECHANISM: dict[str, str] = {
        "/findings/fleet/{fleet_id}":
            "`fleet-operator` or `platform-admin` Cognito group AND fleet membership"
            " for `fleet_id` verified via CMS `_lib/fleet_membership.py`",
        "/actions/fleet/{fleet_id}":
            "`fleet-operator` or `platform-admin` Cognito group AND fleet membership"
            " for `fleet_id` verified via CMS `_lib/fleet_membership.py`",
        "/approval-rules/fleet/{fleet_id}":
            "`fleet-operator` or `platform-admin` Cognito group AND trusted"
            " `custom:fleetIds` (JSON array or CSV) naming `fleet_id`; no membership bypass",
        "/findings/dealership/{dealer_id}":
            "`dealer-admin` Cognito group AND a DMS dealership role scoped to `dealer_id`",
        "/actions/dealership/{dealer_id}":
            "`dealer-admin` Cognito group AND a DMS dealership role scoped to `dealer_id`",
        "/findings/district/{district_id}":
            "`district-manager` Cognito group AND district membership for"
            " `district_id` via the `custom:districtIds` claim",
        "/actions/district/{district_id}":
            "`district-manager` Cognito group AND district membership for"
            " `district_id` via the `custom:districtIds` claim",
    }

    # Self-scoped scope segments that should NOT carry x-scope-predicate: each
    # binds the caller only to their own identity, not to a foreign scope
    # instance.  Keyed on the scope segment string (e.g. "vehicle"), not the
    # full path — a full-path list can be silently grown to exempt fleet routes
    # (Fix Group 8 W3).  Any attempt to add a cross-tenant segment here is
    # caught by the table-key equality in test_bypass_table_matches_contract:
    # exempting a segment removes its routes from the derived roster, causing
    # the roster ↔ table-keys assertion to fail (Fix Group 9 W3).
    _SELF_SCOPED_EXEMPT_SEGMENTS: frozenset[str] = frozenset(
        {"vehicle", "owner", "owner-fleet"}
    )

    @staticmethod
    def _get_scope_predicate(openapi: dict, route: str) -> dict:
        """
        Return the ``x-scope-predicate`` structured object from a route's GET operation.

        This replaces the old ``_extract_predicate`` prose-matcher (Fix Group 6 Critical).
        The guard now reads machine-readable structured data, not English text, so it
        cannot be fooled by rephrasing or by a disclaiming sentence that happens to name
        the parameter.

        Returns an empty dict if the extension is absent (which itself is a defect the
        calling test will surface).
        """
        op = openapi["paths"].get(route, {}).get("get", {})
        return op.get("x-scope-predicate", {})

    @staticmethod
    def _derive_scope_routes(openapi: dict) -> list[tuple[str, str]]:
        """
        Derive the list of cross-tenant scope routes directly from the OpenAPI document.

        A cross-tenant scope route is a route matching
        ``/(findings|actions)/<scope>/{<param>}`` whose path has exactly four
        segments (empty, prefix, scope, param).  Self-scoped routes
        (``vehicle``, ``owner``, ``owner-fleet``) are excluded via
        ``_SELF_SCOPED_EXEMPT_SEGMENTS``.

        The scope segment is matched with ``[^/]+`` (not ``[a-z0-9-]+``) so that
        unconventional spellings like ``Region``, ``sub_fleet``, or ``subFleet``
        are caught by the roster rather than silently ignored (Fix Group 8 W3).

        This replaces the hardcoded six-route roster (Cycle 4 W4) so any
        seventh scope route is covered by construction, not by roster update.
        """
        # Invariant: _SELF_SCOPED_EXEMPT_SEGMENTS must only contain semantically
        # self-scoped segment values.  Any attempt to add a cross-tenant segment
        # is caught independently by the table-key equality in
        # test_bypass_table_matches_contract (Fix Group 9 W3).
        pattern = re.compile(
            r"^/(findings|actions|approval-rules)/([^/]+)/\{([^/]+)\}$"
        )
        routes: list[tuple[str, str]] = []
        for path in openapi.get("paths", {}):
            m = pattern.match(path)
            if m is None:
                continue
            scope_segment = m.group(2)
            if scope_segment in TestApiContractAuthorizationShape._SELF_SCOPED_EXEMPT_SEGMENTS:
                continue
            # m.group(3) is the path parameter name
            routes.append((path, m.group(3)))
        return sorted(routes)

    def _assert_scope_route_has_predicate(
        self, openapi: dict, route: str, scope_param: str
    ) -> None:
        """
        Core assertion: verify route ``route`` declares a valid ``x-scope-predicate``.

        Called by the dynamic roster test, ``test_mechanism_table_matches_contract``,
        and by the negative control.

        Four properties:
        1. Extension present.
        2. ``param`` matches actual path parameter.
        3. ``mechanism`` is a string and equals the exact value in ``_EXPECTED_MECHANISM``
           (Fix Group 9 W1 replaces the ``scope_param in mechanism`` shape heuristic,
           which was defeated in both directions by Cycle 6).
        4. Every principal in ``bypass`` is in the allowlist; no wildcards (W2).
        """
        pred = self._get_scope_predicate(openapi, route)
        assert pred, (
            f"{route} has no x-scope-predicate extension. "
            "Add x-scope-predicate.param / .mechanism / .bypass to the GET operation. "
            "Fix Group 6 Critical."
        )
        assert pred.get("param") == scope_param, (
            f"{route} x-scope-predicate.param is {pred.get('param')!r}, "
            f"expected {scope_param!r}. "
            "The predicate must be bound to the route's actual path parameter."
        )
        # W1 / Fix Group 9 W1 — mechanism must equal the exact expected string.
        # Exact equality replaces the ``scope_param in mechanism`` shape check, which
        # was defeated in both directions (Cycle 6 W1): 12/12 param-affixed placeholders
        # passed; 4/4 plausible legitimate mechanisms were rejected.  A shape check over
        # free text is prose-reading, which this spec forbids (Fix Group 8 generalisation).
        # Any one-word change requires a deliberate, reviewable edit of _EXPECTED_MECHANISM.
        mechanism = pred.get("mechanism", "")
        assert isinstance(mechanism, str) and mechanism == self._EXPECTED_MECHANISM[route], (
            f"{route} x-scope-predicate.mechanism does not match the expected value.\n"
            f"  Got:      {mechanism!r}\n"
            f"  Expected: {self._EXPECTED_MECHANISM[route]!r}\n"
            "Update _EXPECTED_MECHANISM (a deliberate, reviewable change) if the "
            "mechanism legitimately changed. Fix Group 9 W1."
        )
        # W2 — every bypass principal must be in the explicit allowlist.
        bypass = pred.get("bypass", [])
        for principal in bypass:
            assert principal in self._BYPASS_ALLOWLIST, (
                f"{route} x-scope-predicate.bypass contains {principal!r}, which is "
                f"not in the allowlist {sorted(self._BYPASS_ALLOWLIST)!r}. "
                "Wildcards and any-authenticated-principal forms are not allowed. "
                "Fix Group 7 W2."
            )

    def test_scope_route_binds_caller_to_the_specific_scope_instance(
        self, openapi: dict
    ) -> None:
        """
        W2 / Fix Group 6 Critical / Fix Group 7 W4: every cross-tenant scope route
        derived dynamically from the document must carry a valid ``x-scope-predicate``
        extension.

        The roster is now derived from ``openapi["paths"]`` rather than hardcoded,
        so a seventh scope route added to the document is automatically covered (W4).

        Four properties per route are asserted via ``_assert_scope_route_has_predicate``
        (see that method for details).
        """
        routes = self._derive_scope_routes(openapi)
        assert routes, (
            "No cross-tenant scope routes were derived from the document. "
            "Roster derivation is broken or all scope routes were removed."
        )
        for route, scope_param in routes:
            self._assert_scope_route_has_predicate(openapi, route, scope_param)

    def test_every_scope_route_has_predicate(self, openapi: dict) -> None:
        """
        W4 explicit coverage: a seventh scope route added without ``x-scope-predicate``
        must be caught by the dynamic roster, not silently pass.

        This test is a named alias that makes the W4 property legible in test output
        distinct from the main guard.  It calls the same derivation.
        """
        routes = self._derive_scope_routes(openapi)
        missing = [
            route
            for route, _ in routes
            if not self._get_scope_predicate(openapi, route)
        ]
        assert not missing, (
            "The following scope routes have no x-scope-predicate extension:\n"
            + "\n".join(f"  {r}" for r in missing)
            + "\nFix Group 7 W4: roster is now derived from the document, not hardcoded."
        )


    def test_bypass_table_matches_contract(self, openapi: dict) -> None:
        """
        Fix Group 8 C1 + W2 — replace prose-reading consistency guard with a frozen
        test-side expected-bypass table.

        The previous guard (``test_extension_and_prose_bypass_agree``) read the route's
        English prose to infer whether a bypass was granted, which Cycle 5 showed was
        silently defeatable: appending the sentence "`platform-admin` does **not** bypass
        fleet membership" made the guard pass on a re-widened contract because
        ``re.search(r"\\bbypass\\b")`` matches the *word* regardless of whether the
        sentence grants or denies the bypass.

        This test uses a declarative table instead.  Two properties:

        1. **Table ↔ contract**: the contract's ``bypass`` set must equal the table entry
           for that route.  Any drift — adding, removing, or changing a principal — is
           caught immediately without reading prose.

        2. **Roster ↔ table keys**: the table's key set must equal the document-derived
           roster.  A seventh scope route added to the contract without a table entry
           fails here, not silently.

        Together these give the independent failure mode that Cycle 5 W2 required:
        ``test_scope_route_binds_caller_to_the_specific_scope_instance`` asserts that
        every bypass principal is allowlisted; this test asserts **which routes have
        bypass at all and which do not**.  The two assertions are independently
        reachable — removing a bypass value from the table entry while the contract
        still carries it fails only here.

        Changing a table entry is a deliberate, reviewable security decision.
        """
        routes = self._derive_scope_routes(openapi)
        roster = frozenset(route for route, _ in routes)

        # Property 2: table keys must equal the derived roster.
        assert frozenset(self._EXPECTED_BYPASS) == roster, (
            "The _EXPECTED_BYPASS table's key set does not match the document-derived "
            f"scope-route roster.\n"
            f"  Table has but document lacks: {sorted(frozenset(self._EXPECTED_BYPASS) - roster)}\n"
            f"  Document has but table lacks: {sorted(roster - frozenset(self._EXPECTED_BYPASS))}\n"
            "Add a table entry for every new scope route (Fix Group 8 C1)."
        )

        # Property 1: contract bypass must match the table for every route.
        mismatches: list[str] = []
        for route, _ in routes:
            pred = self._get_scope_predicate(openapi, route)
            actual = frozenset(pred.get("bypass", []))
            expected = self._EXPECTED_BYPASS[route]
            if actual != expected:
                mismatches.append(
                    f"  {route}: contract has {sorted(actual)!r}, "
                    f"table expects {sorted(expected)!r}"
                )
        assert not mismatches, (
            "x-scope-predicate.bypass mismatch between the contract and "
            "_EXPECTED_BYPASS table:\n"
            + "\n".join(mismatches)
            + "\nUpdate the table (a security decision) or correct the contract. "
            "Fix Group 8 C1."
        )

    def test_mechanism_table_matches_contract(self, openapi: dict) -> None:
        """
        Fix Group 9 W1 — replace the ``scope_param in mechanism`` shape heuristic with
        a frozen expected-mechanism table.

        Cycle 6 showed the heuristic was defeated in both directions: 12/12
        param-affixed placeholders passed (``"TBD for fleet_id"``,
        ``"N/A - see ticket for fleet_id"``); 4/4 plausible legitimate mechanisms were
        rejected.  A shape check over free text is prose-reading, which this spec forbids.

        This test uses a declarative table instead.  Two properties (mirroring
        ``test_bypass_table_matches_contract``):

        1. **Table ↔ contract**: the contract's mechanism string must equal the table
           entry for that route exactly.  Any one-word change — legitimate or not —
           requires a deliberate, reviewable table edit.
        2. **Roster ↔ table keys**: the table's key set must equal the document-derived
           roster.  A seventh scope route without a table entry fails here, not silently.
        """
        routes = self._derive_scope_routes(openapi)
        roster = frozenset(route for route, _ in routes)

        # Property 2: table keys must equal the derived roster.
        assert frozenset(self._EXPECTED_MECHANISM) == roster, (
            "The _EXPECTED_MECHANISM table's key set does not match the document-derived "
            "scope-route roster.\n"
            f"  Table has but document lacks: "
            f"{sorted(frozenset(self._EXPECTED_MECHANISM) - roster)}\n"
            f"  Document has but table lacks: "
            f"{sorted(roster - frozenset(self._EXPECTED_MECHANISM))}\n"
            "Add a table entry for every new scope route (Fix Group 9 W1)."
        )

        # Property 1: contract mechanism must match the table for every route.
        mismatches: list[str] = []
        for route, _ in routes:
            pred = self._get_scope_predicate(openapi, route)
            actual = pred.get("mechanism", "")
            expected = self._EXPECTED_MECHANISM[route]
            if actual != expected:
                mismatches.append(
                    f"  {route}:\n"
                    f"    got:      {actual!r}\n"
                    f"    expected: {expected!r}"
                )
        assert not mismatches, (
            "x-scope-predicate.mechanism mismatch between the contract and "
            "_EXPECTED_MECHANISM table:\n"
            + "\n".join(mismatches)
            + "\nUpdate the table (a deliberate, reviewable change) or correct the "
            "contract. Fix Group 9 W1."
        )

    @pytest.mark.parametrize(
        "label,patch,match_fragment",
        [
            (
                "extension_deleted",
                lambda doc, route, scope_param: (
                    doc["paths"][route]["get"].pop("x-scope-predicate", None)
                ),
                "no x-scope-predicate",
            ),
            (
                "param_wrong",
                lambda doc, route, scope_param: (
                    doc["paths"][route]["get"]["x-scope-predicate"].__setitem__(
                        "param", "__wrong_param__"
                    )
                ),
                "x-scope-predicate.param",
            ),
            (
                "mechanism_one_word_wrong",
                lambda doc, route, scope_param: (
                    doc["paths"][route]["get"]["x-scope-predicate"].__setitem__(
                        "mechanism", "CHANGED_ONE_WORD"
                    )
                ),
                "x-scope-predicate.mechanism",
            ),
            (
                "mechanism_param_affixed_placeholder",
                lambda doc, route, scope_param: (
                    doc["paths"][route]["get"]["x-scope-predicate"].__setitem__(
                        "mechanism", f"TBD for {scope_param}"
                    )
                ),
                "x-scope-predicate.mechanism",
            ),
            (
                "bypass_wildcard",
                lambda doc, route, scope_param: (
                    doc["paths"][route]["get"]["x-scope-predicate"].__setitem__(
                        "bypass", ["*"]
                    )
                ),
                "x-scope-predicate.bypass",
            ),
        ],
    )
    @pytest.mark.parametrize(
        "route,scope_param",
        [
            ("/actions/fleet/{fleet_id}", "fleet_id"),
            ("/actions/dealership/{dealer_id}", "dealer_id"),
            ("/actions/district/{district_id}", "district_id"),
        ],
    )
    def test_actions_scope_guard_indifferent_to_prose_disclaimer(
        self,
        openapi: dict,
        route: str,
        scope_param: str,
        label: str,
        patch: object,
        match_fragment: str,
    ) -> None:
        """
        Negative control (Fix Group 6 Critical → Fix Group 7 C1 → Fix Group 8 W1+W4):
        the main guard must RAISE for each of the five independently-neuterable
        properties of ``_assert_scope_route_has_predicate``.

        Fix Group 7 C1 introduced one violation (extension deleted).  Cycle 5 W1 showed
        that only the presence assertion (A1) was observable — neutering A2 (param),
        A3 (mechanism), or A4 (bypass allowlist) individually each left 136 passed with
        nothing caught.  This table-driven version constructs one violating document per
        property so each assert has an independent, measured failure mode.

        Five violations:
        1. ``extension_deleted``               — A1 (presence): no ``x-scope-predicate`` at all.
        2. ``param_wrong``                     — A2 (param): param set to a value that cannot match.
        3. ``mechanism_one_word_wrong``        — A3 (mechanism): exact string changed by one word;
           fails because exact equality replaces the ``scope_param in mechanism`` shape heuristic
           (Fix Group 9 W1).
        4. ``mechanism_param_affixed_placeholder`` — A3 (mechanism): ``"TBD for {scope_param}"``
           contains the scope_param substring yet fails, which is the key property that the
           old heuristic lacked — param-affixed placeholders passed before (Cycle 6 W1).
        5. ``bypass_wildcard``                 — A4 (bypass allowlist): ``["*"]`` not in allowlist.

        Mutation-verify each individually: neuter the single assert that this violation
        targets (replacing its condition with ``True``), confirm ``ast.parse`` succeeds
        so a syntax error cannot masquerade as a caught mutation, and confirm the control
        then FAILS (pytest.raises does not catch an AssertionError, so the ``with`` block
        exits cleanly and the parametrised item fails at the context-manager exit).
        """
        import copy

        patched = copy.deepcopy(openapi)
        patch(patched, route, scope_param)  # type: ignore[operator]

        with pytest.raises(AssertionError, match=match_fragment):
            self._assert_scope_route_has_predicate(patched, route, scope_param)

    @pytest.mark.parametrize(
        "label,patch,match_fragment",
        [
            (
                "bypass_roster_drift",
                lambda doc: doc["paths"].__setitem__(
                    "/findings/region/{region_id}",
                    {
                        "get": {
                            "x-scope-predicate": {
                                "param": "region_id",
                                "mechanism": "region check",
                            }
                        }
                    },
                ),
                "_EXPECTED_BYPASS table's key set does not match",
            ),
            (
                "bypass_value_drift",
                lambda doc: doc["paths"]["/findings/fleet/{fleet_id}"]["get"][
                    "x-scope-predicate"
                ].__setitem__("bypass", ["platform-admin"]),
                "x-scope-predicate.bypass mismatch",
            ),
            (
                "mechanism_roster_drift",
                lambda doc: doc["paths"].__setitem__(
                    "/findings/region/{region_id}",
                    {
                        "get": {
                            "x-scope-predicate": {
                                "param": "region_id",
                                "mechanism": "region check",
                            }
                        }
                    },
                ),
                "_EXPECTED_MECHANISM table's key set does not match",
            ),
            (
                "mechanism_value_drift",
                lambda doc: doc["paths"]["/findings/fleet/{fleet_id}"]["get"][
                    "x-scope-predicate"
                ].__setitem__("mechanism", "CHANGED_ONE_WORD"),
                "x-scope-predicate.mechanism mismatch",
            ),
        ],
    )
    def test_table_guards_independently_observable(
        self,
        openapi: dict,
        label: str,
        patch: object,
        match_fragment: str,
    ) -> None:
        """
        Negative control (Fix Group 9 W2): the four assertions in
        ``test_bypass_table_matches_contract`` and
        ``test_mechanism_table_matches_contract`` are each independently observable.

        Cycle 6 W2 showed that neutering ``assert not mismatches`` in either table test
        and re-widening the contract left **146 green** — the sole detector of a
        re-widened bypass or a substituted mechanism had no control.

        This table-driven test follows the same pattern as
        ``test_actions_scope_guard_indifferent_to_prose_disclaimer``: one patched
        document per independently-neuterable assertion, each confirmed to raise.

        Four violations (two per table test):

        1. ``bypass_roster_drift``    — adds a route to the contract not in
           ``_EXPECTED_BYPASS``; catches ``frozenset(_EXPECTED_BYPASS) == roster``.
        2. ``bypass_value_drift``     — widens a fleet bypass from ``frozenset()`` to
           ``{platform-admin}``; catches ``assert not mismatches`` in the bypass test.
        3. ``mechanism_roster_drift`` — same route addition as (1); catches
           ``frozenset(_EXPECTED_MECHANISM) == roster`` independently.
        4. ``mechanism_value_drift``  — changes one word of a mechanism string; catches
           ``assert not mismatches`` in the mechanism test.

        Note: (1) and (3) use the same document patch but target different assertions in
        different test methods — the roster check in each table test is independently
        reachable.
        """
        import copy

        patched = copy.deepcopy(openapi)
        patch(patched)  # type: ignore[operator]

        if label in ("bypass_roster_drift", "bypass_value_drift"):
            with pytest.raises(AssertionError, match=match_fragment):
                self.test_bypass_table_matches_contract(patched)
        else:
            with pytest.raises(AssertionError, match=match_fragment):
                self.test_mechanism_table_matches_contract(patched)



# ---------------------------------------------------------------------------
# Contract v2.2 guards
#
# Changes introduced in contract v2.2 (Fix Group 2 — review C2 + security W1/W2/S1):
#   C2  Action.updated_at required — callers surface staleness per PRD § M5
#   W1  ApproveFindingRequest.target_system oneOf — ios-owner machine-readable constraint
#   W2  district scope routes bind caller to specific scope instance (tested above in
#       TestApiContractAuthorizationShape, no new guard needed for v2.2)
#   S1  Monetary finding_kind values require non-empty disclosure_txt
# ---------------------------------------------------------------------------


class TestActionUpdatedAtC2:
    """
    C2 (review Cycle 1): Action.updated_at is a required RFC 3339 field.

    spec.md line 340 mandates "a computed_at on Findings and an updated_at on Actions
    so the caller can surface staleness (parent PRD § M5)". Group 8's iOS cards render
    staleness from this field, so nullable/optional does not satisfy the requirement.

    The mirror on the Finding side is FindingListResponse.computed_at. This guard
    asserts the symmetric property on Action.
    """

    def test_updated_at_required_in_action_schema(self, action_schema: dict) -> None:
        """C2: updated_at must be in the required array."""
        required = action_schema.get("required", [])
        assert "updated_at" in required, (
            "CRITICAL: Action.updated_at is not in the 'required' array. "
            "spec.md line 340 mandates updated_at on Actions for staleness surfacing "
            "(parent PRD § M5). Group 8 iOS cards depend on this field. "
            "Contract v2.2 change C2."
        )

    def test_updated_at_is_datetime_in_action_schema(self, action_schema: dict) -> None:
        """C2: updated_at must be typed as a date-time string."""
        props = action_schema.get("properties", {})
        assert "updated_at" in props, (
            "Action.updated_at property is absent from the schema properties."
        )
        field = props["updated_at"]
        assert field.get("type") == "string", (
            f"Action.updated_at must have type='string', got {field.get('type')!r}"
        )
        assert field.get("format") == "date-time", (
            f"Action.updated_at must have format='date-time', got {field.get('format')!r}"
        )

    def test_action_without_updated_at_fails_validation(
        self, action_schema: dict
    ) -> None:
        """C2 mutation guard: an Action missing updated_at must fail validation."""
        fixture = _load(FIXTURE_DIR / "action_dms_book_visit_valid.json")
        mutated = json.loads(json.dumps(fixture))
        del mutated["updated_at"]
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "CRITICAL: Action schema accepted an Action without updated_at. "
            "updated_at is required per contract v2.2 C2 — callers surface "
            "staleness from this field (PRD § M5)."
        )

    def test_action_with_updated_at_still_validates(self, action_schema: dict) -> None:
        """C2 positive control: a valid Action with updated_at must still validate."""
        fixture = _load(FIXTURE_DIR / "action_dms_book_visit_valid.json")
        assert "updated_at" in fixture, (
            "Fixture must include updated_at to serve as a positive control."
        )
        errors = _validate(action_schema, fixture)
        assert errors == [], (
            "Action with valid updated_at should pass schema validation:\n"
            + "\n".join(errors)
        )


class TestMonetaryDisclosureS1:
    """
    S1 (security review Cycle 1 Suggestion, promoted to Fix Group 2): monetary
    finding_kind values must carry a non-empty disclosure_txt.

    Parent PRD § M8 and § M14 require verbatim disclosure copy on monetary output.
    The frozen finding_kind enum includes five monetary values (coverage.warranty,
    coverage.recall, energy.cost, loyalty.points, loyalty.expiry). Without an if/then
    guard, a Spec 1b Agent 3 Finding with e.g. finding_kind='energy.cost' and no
    disclosure_txt would validate — a silent monetary-disclosure omission at the
    deterministic seam (~/.kiro/steering/agentic-tiers.md § "What stays deterministic").

    Spec 1a Agent 2 emits no monetary Findings, so these tests exercise the guard via
    synthetic fixtures rather than shipped Agent 2 fixtures.
    """

    MONETARY_KINDS = [
        "coverage.warranty",
        "coverage.recall",
        "energy.cost",
        "loyalty.points",
        "loyalty.expiry",
    ]
    NON_MONETARY_KINDS = [
        "health.tires",
        "health.brakes",
        "health.battery",
        "energy.charging",
        "concierge.appointment",
        "concierge.service",
    ]

    def _make_monetary_fixture(self, finding_kind: str) -> dict:
        """Build a minimal valid Finding with the given monetary finding_kind."""
        base = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        f = json.loads(json.dumps(base))
        f["finding_kind"] = finding_kind
        f["disclosure_txt"] = "This estimate is based on current coverage terms and may change."
        return f

    @pytest.mark.parametrize("finding_kind", MONETARY_KINDS)
    def test_monetary_finding_without_disclosure_txt_fails(
        self, finding_schema: dict, finding_kind: str
    ) -> None:
        """S1 mutation guard: a monetary Finding without disclosure_txt must fail."""
        fixture = self._make_monetary_fixture(finding_kind)
        mutated = json.loads(json.dumps(fixture))
        mutated.pop("disclosure_txt", None)
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            f"CRITICAL: schema accepted finding_kind={finding_kind!r} without "
            "disclosure_txt. Monetary Findings require non-empty disclosure copy "
            "per parent PRD § M8, § M14 and the deterministic-seam rule. "
            "Contract v2.2 security S1."
        )

    @pytest.mark.parametrize("finding_kind", MONETARY_KINDS)
    def test_monetary_finding_with_empty_disclosure_txt_fails(
        self, finding_schema: dict, finding_kind: str
    ) -> None:
        """S1 empty-string guard: disclosure_txt must have minLength:1."""
        fixture = self._make_monetary_fixture(finding_kind)
        mutated = json.loads(json.dumps(fixture))
        mutated["disclosure_txt"] = ""
        errors = _validate(finding_schema, mutated)
        assert errors != [], (
            f"CRITICAL: schema accepted finding_kind={finding_kind!r} with empty "
            "disclosure_txt. minLength:1 is required — an empty string is not "
            "verbatim disclosure copy. Contract v2.2 security S1."
        )

    @pytest.mark.parametrize("finding_kind", MONETARY_KINDS)
    def test_monetary_finding_with_disclosure_txt_validates(
        self, finding_schema: dict, finding_kind: str
    ) -> None:
        """S1 positive control: a monetary Finding WITH non-empty disclosure_txt must validate."""
        fixture = self._make_monetary_fixture(finding_kind)
        errors = _validate(finding_schema, fixture)
        assert errors == [], (
            f"Monetary Finding with finding_kind={finding_kind!r} and non-empty "
            "disclosure_txt should validate. Errors:\n" + "\n".join(errors)
        )

    @pytest.mark.parametrize("finding_kind", NON_MONETARY_KINDS)
    def test_non_monetary_finding_without_disclosure_txt_still_validates(
        self, finding_schema: dict, finding_kind: str
    ) -> None:
        """S1 scope check: the monetary guard must NOT fire for non-monetary finding_kinds.

        Agent 2's health findings (health.tires, health.brakes, health.battery) are
        non-monetary. The if/then constraint must be scoped exactly to the five monetary
        finding_kind values.
        """
        base = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        fixture = json.loads(json.dumps(base))
        fixture["finding_kind"] = finding_kind
        fixture.pop("disclosure_txt", None)
        errors = _validate(finding_schema, fixture)
        assert errors == [], (
            f"The monetary disclosure guard fired for non-monetary finding_kind={finding_kind!r}. "
            "The if/then must be scoped to exactly the five monetary kinds. "
            f"Errors:\n" + "\n".join(errors)
        )


class TestApproveRouteIosOwnerMachineReadableW1:
    """
    W1 (security review Cycle 1 / contract v2.2): the ios-owner constraint on
    ApproveFindingRequest must be expressed machine-readably via a oneOf structure,
    not only in prose.

    "A boundary that exists only in prose will be crossed."

    The oneOf structure makes it unrepresentable to pass ios-owner through this route
    at the schema level — a validator rejects the request before handler code runs.
    """

    _OPENAPI_PATH = (
        Path(__file__).parent.parent.parent
        / "docs"
        / "api"
        / "avx-findings-actions.openapi.yaml"
    )

    @pytest.fixture(scope="class")
    def openapi(self) -> dict:
        yaml = pytest.importorskip("yaml")
        return yaml.safe_load(self._OPENAPI_PATH.read_text(encoding="utf-8"))

    def test_approve_request_has_machine_readable_structure(self, openapi: dict) -> None:
        """W1: ApproveFindingRequest must use a machine-readable constraint structure.

        The constraint may be expressed as a top-level enum (ios-owner excluded from
        target_system), a oneOf with branches, or equivalent — as long as ios-owner is
        structurally unrepresentable on this request schema, not merely forbidden in
        prose.
        """
        schema = openapi["components"]["schemas"]["ApproveFindingRequest"]
        # Collect all target_system enums from wherever they live in the schema
        all_enums: list[list[str]] = []
        if "properties" in schema and "target_system" in schema.get("properties", {}):
            ts = schema["properties"]["target_system"]
            if "enum" in ts:
                all_enums.append(ts["enum"])
        for branch in schema.get("oneOf", []):
            props = branch.get("properties", {})
            if "target_system" in props and "enum" in props["target_system"]:
                all_enums.append(props["target_system"]["enum"])
        assert all_enums, (
            "ApproveFindingRequest has no machine-readable target_system constraint "
            "(no enum found). The ios-owner exclusion must be structurally encoded, "
            "not only described in prose. Contract v2.2 W1."
        )
        for enum_values in all_enums:
            assert "ios-owner" not in enum_values, (
                "ios-owner appears in an ApproveFindingRequest target_system enum. "
                "The machine-readable constraint is broken. Contract v2.2 W1."
            )

    def test_approve_request_uses_oneof_structure(self, openapi: dict) -> None:
        """W1: ApproveFindingRequest must use a oneOf structure (contract v2.2 upgrade).

        A flat enum (pre-v2.2) made ios-owner unrepresentable but did not annotate
        WHY. A oneOf structure makes the distinction machine-readable: each branch
        carries its own description and constraints. Contract v2.2 upgrades this
        from a flat enum to a oneOf so the caller-role boundary is named in the
        schema rather than only in prose.
        """
        schema = openapi["components"]["schemas"]["ApproveFindingRequest"]
        assert "oneOf" in schema, (
            "ApproveFindingRequest must use a oneOf structure per contract v2.2 W1. "
            "The oneOf makes the ios-owner caller-role boundary machine-readable, not "
            "only prose-described. The flat-enum exclusion alone was contract v2.1."
        )
        assert len(schema["oneOf"]) >= 1, (
            "ApproveFindingRequest.oneOf must have at least one branch."
        )

    # ------------------------------------------------------------------
    # Cycle 2 C2 — behavioural property guards (validate documents, not
    # enum lists).  These replace the shape-introspection approach that
    # Mutation M-A defeated while keeping 119 tests green.
    #
    # Design: extract the ApproveFindingRequest schema from the OpenAPI
    # components dict and run Draft202012Validator against real payloads.
    # That means a schema that accepts ios-owner — by any mechanism,
    # whether an added enum value or an unconstrained second oneOf branch —
    # will cause the rejection assertion to fail.  A safe structural
    # refactor (e.g. collapse to a flat 5-value enum) preserves the
    # property and therefore passes.
    #
    # Three mutations are verified:
    #   (a) append a second oneOf branch that does NOT constrain
    #       target_system — the ios-owner rejection test MUST fail.
    #   (b) add "ios-owner" to the existing branch's enum — the rejection
    #       test MUST fail.
    #   (c) collapse oneOf to a flat top-level 5-value enum (safe refactor)
    #       — this test MUST pass, because the property is preserved.
    #       A guard that fails (c) pins style, not the boundary.
    # ------------------------------------------------------------------

    @staticmethod
    def _approve_schema(openapi: dict) -> dict:
        """Return the ApproveFindingRequest component schema."""
        return openapi["components"]["schemas"]["ApproveFindingRequest"]

    @staticmethod
    def _validate_approve(schema: dict, instance: dict) -> list[str]:
        """Validate *instance* against *schema*; return list of error messages."""
        validator = Draft202012Validator(schema)
        return [e.message for e in validator.iter_errors(instance)]

    def test_ios_owner_payload_is_rejected_by_schema(self, openapi: dict) -> None:
        """C2 behavioural guard: a payload with target_system='ios-owner' must fail
        validation against ApproveFindingRequest, regardless of how the constraint is
        expressed (oneOf branch, flat enum, etc.).

        This directly validates the channel-policy property — not the shape of the
        schema that expresses it.  Mutation M-A (an added unconstrained oneOf branch)
        makes ios-owner ACCEPTED while leaving all enum-introspection guards green;
        this test catches it.
        """
        schema = self._approve_schema(openapi)
        ios_owner_payload = {
            "target_system": "ios-owner",
            "action_kind": "book_combined_visit",
            "parameters": {},
        }
        errors = self._validate_approve(schema, ios_owner_payload)
        assert errors != [], (
            "CRITICAL: ApproveFindingRequest accepted target_system='ios-owner'. "
            "This lets the owner-approval route originate dealer-to-owner cards, "
            "bypassing the broker's consent and channel policy. "
            "ios-owner Actions must originate exclusively via Spec 2's POST /actions. "
            "Contract v2.2 W1 / Cycle 2 C2."
        )

    def test_legitimate_target_system_payload_is_accepted_by_schema(
        self, openapi: dict
    ) -> None:
        """C2 positive control: a payload with a legitimate target_system ('dms') must
        pass validation.  Ensures the guard does not reject valid requests.

        Mutation M-A makes dms REJECTED (ios-owner displaces it from the oneOf match);
        this test catches that breakage as well.
        """
        schema = self._approve_schema(openapi)
        dms_payload = {
            "target_system": "dms",
            "action_kind": "book_combined_visit",
            "parameters": {},
        }
        errors = self._validate_approve(schema, dms_payload)
        assert errors == [], (
            "ApproveFindingRequest rejected a legitimate target_system='dms' payload. "
            "The constraint must exclude ONLY ios-owner, not valid executors. "
            f"Errors:\n" + "\n".join(errors)
        )

    @pytest.mark.parametrize(
        "target_system",
        ["vehicle-command", "retail", "none", "chat"],
    )
    def test_all_non_ios_owner_executors_are_accepted(
        self, openapi: dict, target_system: str
    ) -> None:
        """C2 positive control: all five legitimate executors must validate.

        Validates that the guard is not over-broad — a constraint that only admits
        one value while excluding four others would pass the 'dms' test above but
        silently break the remaining legitimate callers.
        """
        schema = self._approve_schema(openapi)
        payload = {
            "target_system": target_system,
            "action_kind": "book_combined_visit",
            "parameters": {},
        }
        errors = self._validate_approve(schema, payload)
        assert errors == [], (
            f"ApproveFindingRequest rejected legitimate target_system={target_system!r}. "
            "The constraint must exclude ONLY ios-owner. "
            f"Errors:\n" + "\n".join(errors)
        )

    def test_unconstrained_value_is_also_rejected(self, openapi: dict) -> None:
        """C2 completeness check: an arbitrary unconstrained value must be rejected.

        If the schema somehow collapsed to 'any string allowed', this test catches it
        before any of the positive controls would notice.
        """
        schema = self._approve_schema(openapi)
        bogus_payload = {
            "target_system": "bogus-executor-xyz",
            "action_kind": "book_combined_visit",
            "parameters": {},
        }
        errors = self._validate_approve(schema, bogus_payload)
        assert errors != [], (
            "ApproveFindingRequest accepted an unconstrained/bogus target_system value. "
            "The schema must constrain target_system to the 5 legitimate executors. "
            "Contract v2.2 W1 / Cycle 2 C2."
        )



# ---------------------------------------------------------------------------
# Contract v2.5 — Stage A additions (spec 2026-10-02-fi-action-queue-stage-a)
# ---------------------------------------------------------------------------


class TestContractV25FindingAdditions:
    """
    Stage A additive additions to the Finding schema (D3):
    - Five new fleet.* finding_kind values
    - Three new agent_id values
    - Optional subject_key, recommended_action, location fields
    - location oneOf constraint (cannot carry both shapes)
    """

    def test_fleet_rebalance_move_fixture_valid(self, finding_schema: dict) -> None:
        """fleet.rebalance.move fixture with subject_key, recommended_action, location (from/to)."""
        fixture = _load(FIXTURE_DIR / "finding_fleet_rebalance_move_v25_valid.json")
        errors = _validate(finding_schema, fixture)
        assert errors == [], "finding_fleet_rebalance_move_v25_valid failed:\n" + "\n".join(errors)

    def test_fleet_gap_fixture_valid(self, finding_schema: dict) -> None:
        """fleet gap finding with location {location_id} is valid."""
        fixture = _load(FIXTURE_DIR / "finding_fleet_gap_v25_valid.json")
        errors = _validate(finding_schema, fixture)
        assert errors == [], "finding_fleet_gap_v25_valid failed:\n" + "\n".join(errors)

    def test_location_both_shapes_invalid(self, finding_schema: dict) -> None:
        """LOAD-BEARING: both location shapes simultaneously MUST fail (oneOf constraint)."""
        fixture = _load(FIXTURE_DIR / "finding_location_both_shapes_INVALID.json")
        errors = _validate(finding_schema, fixture)
        assert errors != [], (
            "CRITICAL: schema accepted a Finding with both location shapes "
            "(location_id AND from/to_location_id). The location field is oneOf — "
            "exactly one shape must be present. D3."
        )

    @pytest.mark.parametrize(
        "finding_kind",
        [
            "fleet.cost.anomaly",
            "fleet.cost.sell_timing",
            "fleet.recall.vehicle",
            "fleet.warranty.claim_candidate",
            "fleet.rebalance.move",
            "fleet.rebalance.gap",
        ],
    )
    def test_fleet_finding_kinds_accepted(
        self, finding_kind: str, finding_schema: dict
    ) -> None:
        """All five new fleet.* finding_kind values must be accepted."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["finding_kind"] = finding_kind
        errors = _validate(finding_schema, mutated)
        assert errors == [], (
            f"finding_kind={finding_kind!r} rejected — Stage A D3:\n" + "\n".join(errors)
        )

    @pytest.mark.parametrize(
        "agent_id",
        ["agent-fi-cost", "agent-fi-recall-warranty", "agent-fi-rebalancing"],
    )
    def test_fi_agent_ids_accepted(self, agent_id: str, finding_schema: dict) -> None:
        """The three new FI agent IDs must be accepted."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["agent_id"] = agent_id
        errors = _validate(finding_schema, mutated)
        assert errors == [], (
            f"agent_id={agent_id!r} rejected — Stage A D3:\n" + "\n".join(errors)
        )

    def test_subject_key_optional(self, finding_schema: dict) -> None:
        """subject_key is optional — a Finding without it must still validate."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        assert "subject_key" not in fixture, "Baseline fixture must not have subject_key"
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Fixture without subject_key failed:\n" + "\n".join(errors)

    def test_subject_key_when_present(self, finding_schema: dict) -> None:
        """subject_key, when present, is a string."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["subject_key"] = "loc-downtown-01__loc-airport-02__2026-10-07"
        errors = _validate(finding_schema, mutated)
        assert errors == [], "Finding with subject_key failed:\n" + "\n".join(errors)

    def test_recommended_action_optional(self, finding_schema: dict) -> None:
        """recommended_action is optional — existing Findings without it still validate."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        assert "recommended_action" not in fixture
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Fixture without recommended_action failed:\n" + "\n".join(errors)

    def test_recommended_action_shape(self, finding_schema: dict) -> None:
        """A valid recommended_action object (all required fields) must be accepted."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["recommended_action"] = {
            "action_kind": "acknowledge_only",
            "target_system": "none",
            "parameters": {},
        }
        errors = _validate(finding_schema, mutated)
        assert errors == [], "Finding with minimal recommended_action failed:\n" + "\n".join(errors)

    def test_recommended_action_optional_value_fields(self, finding_schema: dict) -> None:
        """estimated_value_usd and estimated_value_basis are both optional."""
        fixture = _load(FIXTURE_DIR / "finding_fleet_rebalance_move_v25_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["recommended_action"].pop("estimated_value_usd", None)
        mutated["recommended_action"].pop("estimated_value_basis", None)
        errors = _validate(finding_schema, mutated)
        assert errors == [], "recommended_action without value fields failed:\n" + "\n".join(errors)

    def test_location_single_shape(self, finding_schema: dict) -> None:
        """{location_id} shape is valid."""
        fixture = _load(FIXTURE_DIR / "finding_fleet_gap_v25_valid.json")
        assert fixture["location"] == {"location_id": "loc-airport-02"}
        errors = _validate(finding_schema, fixture)
        assert errors == [], "location {location_id} shape rejected:\n" + "\n".join(errors)

    def test_location_move_shape(self, finding_schema: dict) -> None:
        """{from_location_id, to_location_id} shape is valid."""
        fixture = _load(FIXTURE_DIR / "finding_fleet_rebalance_move_v25_valid.json")
        assert "from_location_id" in fixture["location"]
        assert "to_location_id" in fixture["location"]
        errors = _validate(finding_schema, fixture)
        assert errors == [], "location {from/to} shape rejected:\n" + "\n".join(errors)

    def test_location_optional(self, finding_schema: dict) -> None:
        """location is optional — existing Findings without it must still validate."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        assert "location" not in fixture
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Fixture without location failed:\n" + "\n".join(errors)

    def test_existing_fixtures_still_valid(self, finding_schema: dict) -> None:
        """Additive-only guard: all pre-existing fixtures must still pass."""
        for fname in [
            "finding_health_tires_valid.json",
            "finding_fleet_tires_addendum_d1_valid.json",
            "finding_health_brakes_mocked_provenance.json",
        ]:
            errors = _validate(finding_schema, _load(FIXTURE_DIR / fname))
            assert errors == [], f"{fname} regressed after v2.5 changes:\n" + "\n".join(errors)


class TestContractV25ActionAdditions:
    """
    Stage A additive additions to the Action schema (D3):
    - target_system += cms
    - Nine new action_kind values (seven FI + two retail kinds)
    - Optional decided_by and decision fields
    """

    def test_action_v25_decided_by_fixture_valid(self, action_schema: dict) -> None:
        """Action fixture with decided_by and decision must validate."""
        fixture = _load(FIXTURE_DIR / "action_v25_decided_by_valid.json")
        errors = _validate(action_schema, fixture)
        assert errors == [], "action_v25_decided_by_valid failed:\n" + "\n".join(errors)

    def test_cms_target_system_accepted(self, action_schema: dict) -> None:
        """target_system='cms' must be accepted (Stage A CmsExecutor placeholder)."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["target_system"] = "cms"
        errors = _validate(action_schema, mutated)
        assert errors == [], "target_system='cms' rejected — Stage A D3:\n" + "\n".join(errors)

    @pytest.mark.parametrize(
        "action_kind",
        [
            "book_recall_service",
            "ground_vehicle",
            "move_vehicles",
            "exempt_recall",
            "complete_recall",
            "submit_warranty_claim",
            "apply_cost_action",
            "lead_capture",
            "configurator_recommend",
        ],
    )
    def test_new_action_kinds_accepted(
        self, action_kind: str, action_schema: dict
    ) -> None:
        """All nine new action_kind values must be accepted by the schema."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["action_kind"] = action_kind
        errors = _validate(action_schema, mutated)
        assert errors == [], (
            f"action_kind={action_kind!r} rejected — Stage A D3:\n" + "\n".join(errors)
        )

    def test_decided_by_optional(self, action_schema: dict) -> None:
        """decided_by is optional — pre-Stage-A Actions without it still validate."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        assert "decided_by" not in fixture
        errors = _validate(action_schema, fixture)
        assert errors == [], "Fixture without decided_by failed:\n" + "\n".join(errors)

    def test_decided_by_operator_kind(self, action_schema: dict) -> None:
        """decided_by {kind: 'operator', id: <sub>} must be accepted."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["decided_by"] = {"kind": "operator", "id": "usr-12345678-abcd-ef01-2345-6789abcdef01"}
        errors = _validate(action_schema, mutated)
        assert errors == [], "decided_by operator kind rejected:\n" + "\n".join(errors)

    def test_decided_by_rule_kind(self, action_schema: dict) -> None:
        """decided_by {kind: 'rule', id: <fleet_id>/<rule_id>} must be accepted."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["decided_by"] = {"kind": "rule", "id": "FLEET-MRD-WEST/rule-ack-only-001"}
        errors = _validate(action_schema, mutated)
        assert errors == [], "decided_by rule kind rejected:\n" + "\n".join(errors)

    def test_decided_by_unknown_kind_rejected(self, action_schema: dict) -> None:
        """LOAD-BEARING: decided_by.kind must be 'operator' or 'rule' only."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["decided_by"] = {"kind": "system", "id": "some-id"}
        errors = _validate(action_schema, mutated)
        assert errors != [], "Schema accepted unknown decided_by.kind 'system' — enum must be closed."

    def test_decision_optional(self, action_schema: dict) -> None:
        """decision is optional — pre-Stage-A Actions without it still validate."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        assert "decision" not in fixture
        errors = _validate(action_schema, fixture)
        assert errors == [], "Fixture without decision failed:\n" + "\n".join(errors)

    @pytest.mark.parametrize(
        "decision",
        ["approved", "overridden", "auto_approved", "declined"],
    )
    def test_all_decision_values_accepted(
        self, decision: str, action_schema: dict
    ) -> None:
        """All four decision enum values must be accepted."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["decision"] = decision
        errors = _validate(action_schema, mutated)
        assert errors == [], f"decision={decision!r} rejected:\n" + "\n".join(errors)

    def test_unknown_decision_rejected(self, action_schema: dict) -> None:
        """Unknown decision values must be rejected."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["decision"] = "pending"
        errors = _validate(action_schema, mutated)
        assert errors != [], "Schema accepted unknown decision value 'pending'."

    def test_guardrail_on_non_declined_entry_fails(self, action_schema: dict) -> None:
        """LOAD-BEARING: guardrail is only valid on a 'declined' transition (allOf Stage A D5).

        schema allOf[2] says: if 'guardrail' is present, then to_status must be 'declined'
        and failure_kind must be 'target_refused'. An entry that carries guardrail on any
        other transition (e.g. 'executed' or 'completed') must fail validation.
        """
        fixture = _load(FIXTURE_DIR / "action_dms_book_visit_valid.json")
        mutated = json.loads(json.dumps(fixture))
        # Inject guardrail on the 'executed' transition — not a declined transition
        for entry in mutated["audit_trail"]:
            entry["guardrail"] = "G1"
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted guardrail on a non-declined audit entry. "
            "allOf Stage A D5 requires to_status='declined' when guardrail is set. "
            "A guardrail block is defined as 'declined' with failure_kind=target_refused."
        )

    def test_decided_by_empty_id_fails(self, action_schema: dict) -> None:
        """LOAD-BEARING: decided_by.id must have minLength:1 — empty string must be refused."""
        fixture = _load(FIXTURE_DIR / "action_none_target_valid.json")
        mutated = json.loads(json.dumps(fixture))
        mutated["decided_by"] = {"kind": "operator", "id": ""}
        errors = _validate(action_schema, mutated)
        assert errors != [], (
            "CRITICAL: schema accepted decided_by.id='' (empty string). "
            "decided_by.id must have minLength:1 — the Cognito token sub is never empty. "
            "Stage A D3."
        )

    def test_existing_fixtures_still_valid(self, action_schema: dict) -> None:
        """Additive-only guard: all pre-existing action fixtures must still pass."""
        for fname in [
            "action_dms_book_visit_valid.json",
            "action_ios_owner_acknowledge_valid.json",
            "action_none_target_valid.json",
            "action_dismissed_addendum_d4_valid.json",
            "action_declined_4xx_addendum_d5_valid.json",
            "action_failed_5xx_exhausted_addendum_d5_valid.json",
        ]:
            errors = _validate(action_schema, _load(FIXTURE_DIR / fname))
            assert errors == [], f"{fname} regressed after v2.5 changes:\n" + "\n".join(errors)


class TestContractV25OpenAPISync:
    """
    Stage A: the two action_kind enum sites in the OpenAPI must contain all
    15 values (6 original + 9 new). The Finding schema's action_kind and
    target_system sync is tested by the existing parity guards.
    """

    _OPENAPI_PATH = (
        Path(__file__).parent.parent.parent
        / "docs"
        / "api"
        / "avx-findings-actions.openapi.yaml"
    )

    @pytest.fixture(scope="class")
    def openapi(self) -> dict:
        yaml = pytest.importorskip("yaml")
        return yaml.safe_load(self._OPENAPI_PATH.read_text(encoding="utf-8"))

    _EXPECTED_ACTION_KINDS: frozenset = frozenset({
        "book_combined_visit",
        "schedule_inspection",
        "update_charging_schedule",
        "file_warranty_pre",
        "surface_recall",
        "acknowledge_only",
        "book_recall_service",
        "ground_vehicle",
        "move_vehicles",
        "exempt_recall",
        "complete_recall",
        "submit_warranty_claim",
        "apply_cost_action",
        "lead_capture",
        "configurator_recommend",
    })

    def test_action_schema_action_kind_enum(
        self, openapi: dict, action_schema: dict
    ) -> None:
        """Action schema action_kind enum in OpenAPI must equal action.schema.json."""
        oa_enum = set(
            openapi["components"]["schemas"]["Action"]["properties"]["action_kind"]["enum"]
        )
        schema_enum = set(action_schema["properties"]["action_kind"]["enum"])
        assert oa_enum == schema_enum, (
            f"Action.action_kind enum mismatch between OpenAPI and action.schema.json:\n"
            f"  OpenAPI only: {sorted(oa_enum - schema_enum)}\n"
            f"  Schema only:  {sorted(schema_enum - oa_enum)}"
        )

    def test_approve_request_action_kind_enum(self, openapi: dict, action_schema: dict) -> None:
        """ApproveRequest action_kind enum in OpenAPI must equal action.schema.json."""
        branches = openapi["components"]["schemas"]["ApproveFindingRequest"].get("oneOf", [])
        assert branches, "ApproveFindingRequest must have a oneOf structure"
        oa_kinds: set[str] = set()
        for branch in branches:
            branch_enum = branch.get("properties", {}).get("action_kind", {}).get("enum", [])
            oa_kinds.update(branch_enum)
        schema_enum = set(action_schema["properties"]["action_kind"]["enum"])
        assert oa_kinds == schema_enum, (
            f"ApproveRequest action_kind mismatch vs action.schema.json:\n"
            f"  OpenAPI only: {sorted(oa_kinds - schema_enum)}\n"
            f"  Schema only:  {sorted(schema_enum - oa_kinds)}"
        )

    def test_finding_kind_enum_openapi_equals_schema(
        self, openapi: dict, finding_schema: dict
    ) -> None:
        """Finding.finding_kind in OpenAPI must equal finding.schema.json (includes fleet.*)."""
        oa_finding = openapi["components"]["schemas"]["Finding"]
        oa_enum = set(oa_finding["properties"]["finding_kind"]["enum"])
        schema_enum = set(finding_schema["properties"]["finding_kind"]["enum"])
        assert oa_enum == schema_enum, (
            f"finding_kind enum mismatch between OpenAPI and finding.schema.json:\n"
            f"  OpenAPI only: {sorted(oa_enum - schema_enum)}\n"
            f"  Schema only:  {sorted(schema_enum - oa_enum)}"
        )

    def test_retail_kinds_in_schema_not_lifted_from_gate(self) -> None:
        """
        LOAD-BEARING: retail kinds are in the schema enum (closing the contract gap)
        but the _RETAIL_KINDS runtime gate is not lifted (D3). This test pins that
        the schema change is additive, not a gate removal — the gate is in handler.py
        and this test must not touch it.

        Mutation: remove lead_capture from action.schema.json → this test fails.
        """
        schema_path = (
            Path(__file__).parent.parent.parent
            / "agents" / "tier2" / "schemas" / "action.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema_kinds = set(schema["properties"]["action_kind"]["enum"])
        assert "lead_capture" in schema_kinds, (
            "lead_capture missing from action.schema.json enum. "
            "Stage A D3 adds it to close the _RETAIL_KINDS contract gap."
        )
        assert "configurator_recommend" in schema_kinds, (
            "configurator_recommend missing from action.schema.json enum. "
            "Stage A D3 adds it to close the _RETAIL_KINDS contract gap."
        )


# ---------------------------------------------------------------------------
# Stage A Fix Group 2 (review Cycle 2, W1/W3): the approval-rules write routes carry the
# same scope predicate as their GET. The roster guard above derives only GET,
# single-parameter routes, so these two would otherwise be checked by prose alone.
# ---------------------------------------------------------------------------

def test_approval_rule_write_routes_carry_the_get_scope_predicate() -> None:
    import yaml  # noqa: PLC0415

    doc = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docs" / "api"
         / "avx-findings-actions.openapi.yaml").read_text()
    )
    get_pred = doc["paths"]["/approval-rules/fleet/{fleet_id}"]["get"]["x-scope-predicate"]
    item = doc["paths"]["/approval-rules/fleet/{fleet_id}/{rule_id}"]
    for method in ("put", "delete"):
        pred = item[method].get("x-scope-predicate")
        assert pred == get_pred, (
            f"{method.upper()} /approval-rules/fleet/{{fleet_id}}/{{rule_id}} must carry the "
            f"same x-scope-predicate as the GET route; got {pred!r}"
        )
        assert "bypass" not in pred, "approval rules have no membership bypass (contract § 6)"


# ---------------------------------------------------------------------------
# Stage A Fix Group 4 (spec D14): failure_reason and error_status on audit entries.
# ---------------------------------------------------------------------------

def _refused_action(entry_extra: dict) -> dict:
    fixture = _load(FIXTURE_DIR / "action_v25_decided_by_valid.json")
    entry = {
        "at": "2026-10-02T12:00:01Z", "by": "executor-dms",
        "from_status": "pending_execution", "to_status": "declined",
        "failure_kind": "target_refused",
    }
    entry.update(entry_extra)
    return {**fixture, "status": "declined", "audit_trail": [entry]}


def test_failure_reason_and_error_status_validate(action_schema: dict) -> None:
    action = _refused_action({"failure_reason": "DMS refused the request (HTTP 422).",
                              "error_status": 422})
    assert _validate(action_schema, action) == []


_R = "DMS refused the request (HTTP 422)."


@pytest.mark.parametrize("extra", [
    {"failure_reason": ""},
    {"failure_reason": "x" * 301},
    {"error_status": 99, "failure_reason": _R},
    {"error_status": 600, "failure_reason": _R},
    {"error_status": "409", "failure_reason": _R},
], ids=["empty-reason", "long-reason", "status-low", "status-high", "status-string"])
def test_bad_failure_fields_fail_validation(action_schema: dict, extra: dict) -> None:
    """Each case carries a valid reason where it needs one, so it fails on the bound it
    names rather than on the error_status-requires-failure_reason rule."""
    assert _validate(action_schema, _refused_action(extra)) != []


# ---------------------------------------------------------------------------
# Stage A Fix Group 5 (review FG4-contract Warnings 4, Suggestions 1 and 5): the
# vocabulary is a schema pattern, the fields are restricted to the transitions they
# describe, null is invalid, and the OpenAPI copy agrees with the schema.
# ---------------------------------------------------------------------------

def _entry_action(entry: dict, status: str) -> dict:
    fixture = _load(FIXTURE_DIR / "action_v25_decided_by_valid.json")
    base = {"at": "2026-10-02T12:00:01Z", "by": "executor-dms",
            "from_status": "pending_execution"}
    return {**fixture, "status": status, "audit_trail": [{**base, **entry}]}


#: Every § 10a sentence shape, copied literally from the contract (never generated).
_VOCABULARY = [
    "Blocked: exempting a safety recall needs a fleet operator.",
    "Blocked: grounding a vehicle needs a fleet operator.",
    "Blocked: sending a warranty claim needs a fleet operator.",
    "Blocked: completing a recall needs a fleet operator.",
    "Blocked: completing a recall needs a service record.",
    "Blocked: the service record could not be checked.",
    "Blocked: the service record could not be found.",
    "Blocked: the service record is for a different vehicle.",
    "Blocked: the service record is not complete (status Closed).",
    "Blocked: the service record is not complete (status AwaitingParts).",
    "Blocked: the service record is not complete (status unknown).",
    "AVX could not complete this action.",
    "CMS cannot carry out this action yet.",
    "DMS refused the request (HTTP 422).",
    "The vehicle refused the request (HTTP 409).",
    "The owner's app refused the request.",
    "DMS was unavailable (HTTP 503) after 3 attempts.",
    "The retail system was unavailable (HTTP 429) after 1 attempt.",
    "DMS did not respond after 1 attempt.",
    "The vehicle did not respond after 12 attempts.",
    "AVX ran out of time before it could contact the retail system.",
    "DMS did not accept the request (HTTP 400).",
    "AVX could not complete the request to the vehicle.",
]

_NOT_VOCABULARY = [
    "DMS did not respond after 3 attempts. (HTTPSConnectionPool(host='internal-dms.corp'))",
    "Bearer eyJ.secret refused the request.",
    "Blocked: the service record is not complete (status JaneSmith).",
    "Blocked: the service record is not complete (status Secret123).",
    "DMS did not respond after 1 attempts.",
    "DMS did not respond after 0 attempts.",
    "DMS was unavailable (HTTP 503) after 1 attempts.",
    "DMS was unavailable (HTTP 503) after 0 attempts.",
    "AVX could not complete the request to AVX.",
    "dealer_id is not a valid DMS rooftop",
    "DMS refused the request (HTTP 4090).",
    "the vehicle refused the request.",
]


@pytest.mark.parametrize("reason", _VOCABULARY)
def test_every_vocabulary_sentence_validates(action_schema: dict, reason: str) -> None:
    action = _entry_action({"to_status": "declined", "failure_kind": "target_refused",
                            "failure_reason": reason}, "declined")
    assert _validate(action_schema, action) == []


@pytest.mark.parametrize("reason", _NOT_VOCABULARY)
def test_text_outside_the_vocabulary_fails_validation(action_schema: dict, reason: str) -> None:
    action = _entry_action({"to_status": "declined", "failure_kind": "target_refused",
                            "failure_reason": reason}, "declined")
    assert _validate(action_schema, action) != []


@pytest.mark.parametrize("entry,status", [
    ({"to_status": "completed", "failure_reason": "DMS refused the request."}, "completed"),
    ({"to_status": "in_progress", "error_status": 503,
      "failure_reason": "DMS refused the request (HTTP 503)."}, "in_progress"),
    ({"to_status": "declined", "failure_kind": "target_refused", "guardrail": "G2"},
     "declined"),
    ({"to_status": "declined", "failure_kind": "target_refused", "guardrail": "G2",
      "failure_reason": "Blocked: grounding a vehicle needs a fleet operator.",
      "error_status": 403}, "declined"),
    ({"to_status": "failed", "failure_kind": "executor_terminal_error", "retry_count": 0,
      "error_status": 400}, "failed"),
    ({"to_status": "failed", "failure_kind": "executor_terminal_error", "retry_count": 0,
      "failure_reason": "AVX could not complete the request to DMS.", "error_status": None},
     "failed"),
    ({"to_status": "declined", "failure_kind": "target_refused", "failure_reason": None},
     "declined"),
    ({"to_status": "declined", "failure_kind": "target_refused", "guardrail": None},
     "declined"),
], ids=["reason-on-completed", "status-on-in-progress", "guardrail-without-reason",
        "guardrail-with-status", "status-without-reason", "null-status", "null-reason",
        "null-guardrail"])
def test_failure_fields_outside_their_transition_fail(action_schema: dict, entry: dict,
                                                      status: str) -> None:
    assert _validate(action_schema, _entry_action(entry, status)) != []


@pytest.mark.parametrize("entry,status", [
    ({"to_status": "failed", "failure_kind": "executor_terminal_error", "retry_count": 0,
      "failure_reason": "DMS did not accept the request (HTTP 400).", "error_status": 400},
     "failed"),
    ({"to_status": "failed", "failure_kind": "executor_exhausted_retries", "retry_count": 3,
      "failure_reason": "DMS did not respond after 3 attempts."}, "failed"),
    ({"to_status": "declined", "failure_kind": "target_refused", "guardrail": "G4",
      "failure_reason": "Blocked: the service record could not be checked."}, "declined"),
    ({"to_status": "declined", "failure_kind": "target_refused"}, "declined"),
], ids=["terminal-with-status", "exhausted-no-status", "guardrail-block", "pre-v25-refusal"])
def test_failure_fields_on_their_transition_validate(action_schema: dict, entry: dict,
                                                     status: str) -> None:
    assert _validate(action_schema, _entry_action(entry, status)) == []


def test_openapi_audit_entry_agrees_with_the_schema(action_schema: dict) -> None:
    """Suggestion 5: nothing else compares the two copies. Same property set; the v2.5
    fields agree on type, bounds, enum and pattern, and neither copy admits null."""
    import yaml  # noqa: PLC0415

    doc = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docs" / "api"
         / "avx-findings-actions.openapi.yaml").read_text()
    )
    oa = doc["components"]["schemas"]["ActionAuditEntry"]["properties"]
    js = action_schema["$defs"]["audit_entry"]["properties"]
    assert set(oa) == set(js)
    for name in ("failure_reason", "error_status", "guardrail"):
        assert "nullable" not in oa[name], f"{name}: omitted, never null (contract § 10a)"
        for key in ("type", "minLength", "maxLength", "minimum", "maximum", "enum", "pattern"):
            assert oa[name].get(key) == js[name].get(key), (name, key)




# ---------------------------------------------------------------------------
# Stage A security review Cycle 1 W3 (and W1): every write route carries a frozen,
# machine-readable scope predicate, including the order of checks. The read-route
# roster above derives only GET routes ending in a path parameter, so approve,
# decline, PATCH and dismiss were outside it and their mechanism was prose only.
# ---------------------------------------------------------------------------

_RULES_MECHANISM = (
    "`fleet-operator` or `platform-admin` Cognito group AND trusted"
    " `custom:fleetIds` (JSON array or CSV) naming `fleet_id`; no membership bypass"
)

#: Changing an entry is a deliberate, reviewable security decision.
_EXPECTED_WRITE_PREDICATES: dict[str, dict] = {
    "POST /findings/{finding_id}/approve": {
        "param": "finding_id", "bypass": ["platform-admin"],
        "mechanism": (
            "`_check_approve_scope` on the loaded Finding (`platform-admin`; `service-advisor`"
            " group; `fleet-operator` with trusted `custom:fleetIds` naming the Finding's fleet;"
            " owner or driver standing per the identity contract); FI queue items also need"
            " operator standing (`platform-admin`, or `fleet-operator` with trusted"
            " `custom:fleetIds` naming the Finding's fleet); every 403 is decided before the"
            " decision is read (200 repeat, 409 `already_decided`)"),
    },
    "POST /findings/{finding_id}/decline": {
        "param": "finding_id", "bypass": ["platform-admin"],
        "mechanism": (
            "`_check_approve_scope` on the loaded Finding, then operator standing"
            " (`platform-admin`, or `fleet-operator` with trusted `custom:fleetIds` naming the"
            " Finding's fleet); every 403 is decided before the decision is read (200 repeat,"
            " 409 `already_decided`)"),
    },
    "PATCH /actions/{action_id}": {
        "param": "action_id", "bypass": ["platform-admin"],
        "mechanism": (
            "the Action's `created_by` caller; `service-advisor` group; `fleet-operator` with"
            " trusted `custom:fleetIds` naming the Action's `fleet_id`; every 403 is decided"
            " before `fi_decision_immutable` (409)"),
    },
    "POST /actions/{action_id}/dismiss": {
        "param": "action_id", "bypass": ["platform-admin"],
        "mechanism": ("the Action's `created_by` caller; every 403 is decided before"
                      " `fi_decision_immutable` (409)"),
    },
    "PUT /approval-rules/fleet/{fleet_id}/{rule_id}": {
        "param": "fleet_id", "bypass": [], "mechanism": _RULES_MECHANISM},
    "DELETE /approval-rules/fleet/{fleet_id}/{rule_id}": {
        "param": "fleet_id", "bypass": [], "mechanism": _RULES_MECHANISM},
}


def test_write_routes_carry_a_frozen_scope_predicate() -> None:
    import yaml  # noqa: PLC0415

    doc = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docs" / "api"
         / "avx-findings-actions.openapi.yaml").read_text()
    )
    derived = {}
    for path, item in doc["paths"].items():
        if not path.startswith(("/findings/", "/actions/", "/approval-rules/")):
            continue
        for method in ("post", "put", "patch", "delete"):
            if method in item:
                derived[f"{method.upper()} {path}"] = item[method].get("x-scope-predicate")
    # A new write route without a table entry fails here, not silently.
    assert set(derived) == set(_EXPECTED_WRITE_PREDICATES), (
        f"document-only: {sorted(set(derived) - set(_EXPECTED_WRITE_PREDICATES))}; "
        f"table-only: {sorted(set(_EXPECTED_WRITE_PREDICATES) - set(derived))}")
    for route, expected in _EXPECTED_WRITE_PREDICATES.items():
        pred = derived[route]
        assert pred, f"{route} has no x-scope-predicate"
        got = {"param": pred.get("param"), "bypass": pred.get("bypass", []),
               "mechanism": pred.get("mechanism")}
        assert got == expected, f"{route}:\n  got      {got!r}\n  expected {expected!r}"
        assert set(got["bypass"]) <= {"platform-admin"}, route
        param = expected["param"]
        assert "{" + param + "}" in route.split(" ", 1)[1], (route, param)



# ---------------------------------------------------------------------------
# v2.7 additions — contract v2.7 (spec 2026-10-05-fi-fleet-health-stage-d, task 3.5)
#
# 1. Backward-compatibility: v2.6-era Finding and Action still validate unchanged.
# 2. New enum values admit valid instances.
# 3. New guards enforce their properties.
# ---------------------------------------------------------------------------


class TestV27BackwardCompatibility:
    """
    All v2.6-era fixtures still validate under v2.7.

    Regression guard: a schema change that breaks a pre-existing enum value
    (e.g. removes a v2.5 agent_id) would fail these tests, not silently pass.
    """

    def test_v26_era_finding_fleet_gap_still_validates(self, finding_schema: dict) -> None:
        """A v2.5 fleet.rebalance.gap Finding (agent-fi-rebalancing) still validates."""
        fixture = _load(FIXTURE_DIR / "finding_fleet_gap_v25_valid.json")
        errors = _validate(finding_schema, fixture)
        assert errors == [], "v2.6-era fleet gap finding no longer validates:\n" + "\n".join(errors)

    def test_v26_era_action_decided_by_still_validates(self, action_schema: dict) -> None:
        """A v2.5 Action with decided_by.kind='operator' still validates."""
        fixture = _load(FIXTURE_DIR / "action_v25_decided_by_valid.json")
        errors = _validate(action_schema, fixture)
        assert errors == [], "v2.6-era action still validates:\n" + "\n".join(errors)

    def test_v26_era_action_declined_still_validates(self, action_schema: dict) -> None:
        """A v2.5 declined Action (G1 guardrail) still validates."""
        fixture = _load(FIXTURE_DIR / "action_declined_4xx_addendum_d5_valid.json")
        errors = _validate(action_schema, fixture)
        assert errors == [], "v2.6-era declined action no longer validates:\n" + "\n".join(errors)

    def test_v26_era_finding_health_tires_still_validates(self, finding_schema: dict) -> None:
        """A v2.2 health.tires Finding (agent-2-vehicle-health) still validates."""
        fixture = _load(FIXTURE_DIR / "finding_health_tires_valid.json")
        errors = _validate(finding_schema, fixture)
        assert errors == [], "Pre-v2.7 health.tires finding no longer validates:\n" + "\n".join(errors)


class TestV27NewFindingValues:
    """
    New v2.7 enum values and fields accept valid instances.
    """

    def _base_finding(self, *, kind: str, agent_id: str, scope: dict, severity: str = "attention") -> dict:
        return {
            "finding_id": "FIND-a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "finding_key": "e3d71785c0a854b79f71bd239e2d15b63fa7ce38c8421cc1ead9529f4669bab0",
            "agent_id": agent_id,
            "agent_version": "1.0.0",
            "scope": scope,
            "severity": severity,
            "computed_at": "2026-10-08T05:40:00Z",
            "expires_at": "2026-10-15T05:40:00Z",
            "confidence": "high",
            "finding_kind": kind,
            "headline": "Test headline",
            "detail": "Test detail",
            "evidence": [],
            "inputs_hash": "049fab714fa0ed1be665a633770cc46f996f4161700a9c27b5c447b3ee72cc36",
            "tokens_used": 0,
            "cost_usd": 0.0,
            "audit_trail": [],
        }

    def test_agent_fi_fleet_health_validates(self, finding_schema: dict) -> None:
        """agent-fi-fleet-health is a valid agent_id (v2.7 addition)."""
        finding = self._base_finding(
            kind="fleet.health.service",
            agent_id="agent-fi-fleet-health",
            scope={"vin": "1MRDVEH0000000001"},
        )
        errors = _validate(finding_schema, finding)
        assert errors == [], f"agent-fi-fleet-health should be valid:\n" + "\n".join(errors)

    def test_agent_fi_quality_validates(self, finding_schema: dict) -> None:
        """agent-fi-quality is a valid agent_id (v2.7 addition)."""
        finding = self._base_finding(
            kind="quality.investigation",
            agent_id="agent-fi-quality",
            scope={"population_id": "meridian-motors"},
        )
        errors = _validate(finding_schema, finding)
        assert errors == [], f"agent-fi-quality should be valid:\n" + "\n".join(errors)

    def test_fleet_health_service_kind_validates(self, finding_schema: dict) -> None:
        """fleet.health.service is a valid finding_kind (v2.7 addition)."""
        finding = self._base_finding(
            kind="fleet.health.service",
            agent_id="agent-fi-fleet-health",
            scope={"vin": "1MRDVEH0000000001"},
        )
        errors = _validate(finding_schema, finding)
        assert errors == [], f"fleet.health.service kind should be valid:\n" + "\n".join(errors)

    def test_quality_investigation_kind_validates(self, finding_schema: dict) -> None:
        """quality.investigation is a valid finding_kind (v2.7 addition)."""
        finding = self._base_finding(
            kind="quality.investigation",
            agent_id="agent-fi-quality",
            scope={"population_id": "meridian-motors"},
        )
        errors = _validate(finding_schema, finding)
        assert errors == [], f"quality.investigation kind should be valid:\n" + "\n".join(errors)

    def test_quality_issue_kind_validates(self, finding_schema: dict) -> None:
        """quality.issue is a valid finding_kind (v2.7 addition)."""
        finding = self._base_finding(
            kind="quality.issue",
            agent_id="agent-fi-quality",
            scope={"population_id": "meridian-motors"},
        )
        errors = _validate(finding_schema, finding)
        assert errors == [], f"quality.issue kind should be valid:\n" + "\n".join(errors)

    def test_population_id_scope_validates(self, finding_schema: dict) -> None:
        """A Finding scoped by population_id alone is valid (v2.7 addition)."""
        finding = self._base_finding(
            kind="quality.investigation",
            agent_id="agent-fi-quality",
            scope={"population_id": "meridian-motors"},
        )
        errors = _validate(finding_schema, finding)
        assert errors == [], f"population_id scope should be valid:\n" + "\n".join(errors)

    def test_fleet_health_service_p0_with_disclosure_txt_validates(self, finding_schema: dict) -> None:
        """fleet.health.service at p0 WITH disclosure_txt validates (guard satisfied)."""
        finding = self._base_finding(
            kind="fleet.health.service",
            agent_id="agent-fi-fleet-health",
            scope={"vin": "1MRDVEH0000000001"},
            severity="p0",
        )
        finding["disclosure_txt"] = "Stop driving immediately — fault code P0001 requires immediate attention."
        errors = _validate(finding_schema, finding)
        assert errors == [], f"p0 fleet.health.service with disclosure_txt should validate:\n" + "\n".join(errors)


class TestV27NewFindingGuards:
    """
    v2.7 guards reject invalid instances.
    """

    def _base_finding(self, *, kind: str, agent_id: str, scope: dict, severity: str = "attention") -> dict:
        return {
            "finding_id": "FIND-a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "finding_key": "e3d71785c0a854b79f71bd239e2d15b63fa7ce38c8421cc1ead9529f4669bab0",
            "agent_id": agent_id,
            "agent_version": "1.0.0",
            "scope": scope,
            "severity": severity,
            "computed_at": "2026-10-08T05:40:00Z",
            "expires_at": "2026-10-15T05:40:00Z",
            "confidence": "high",
            "finding_kind": kind,
            "headline": "Test headline",
            "detail": "Test detail",
            "evidence": [],
            "inputs_hash": "049fab714fa0ed1be665a633770cc46f996f4161700a9c27b5c447b3ee72cc36",
            "tokens_used": 0,
            "cost_usd": 0.0,
            "audit_trail": [],
        }

    def test_fleet_health_service_p0_without_disclosure_txt_fails(self, finding_schema: dict) -> None:
        """
        Stage D D9: fleet.health.service at p0 WITHOUT disclosure_txt must FAIL.

        Mutation guard: this test directly checks the allOf guard added in v2.7.
        If the guard were absent (mutation: remove the fleet.health.service allOf branch),
        this test would PASS the schema validation — proving the guard is present
        and functional.
        """
        finding = self._base_finding(
            kind="fleet.health.service",
            agent_id="agent-fi-fleet-health",
            scope={"vin": "1MRDVEH0000000001"},
            severity="p0",
        )
        # No disclosure_txt — must fail
        errors = _validate(finding_schema, finding)
        assert errors, (
            "fleet.health.service at p0 without disclosure_txt should FAIL validation "
            "but validated successfully. The Stage D D9 disclosure_txt guard is missing."
        )

    def test_fleet_health_service_attention_without_disclosure_txt_validates(self, finding_schema: dict) -> None:
        """
        The disclosure_txt guard fires ONLY at p0. Non-p0 severities do not require it.
        """
        finding = self._base_finding(
            kind="fleet.health.service",
            agent_id="agent-fi-fleet-health",
            scope={"vin": "1MRDVEH0000000001"},
            severity="attention",
        )
        errors = _validate(finding_schema, finding)
        assert errors == [], (
            f"fleet.health.service at 'attention' should validate without disclosure_txt:\n"
            + "\n".join(errors)
        )

    def test_population_id_with_other_scope_field_fails(self, finding_schema: dict) -> None:
        """population_id cannot coexist with another scope field (oneOf).

        C1 fix (G3F1.5): parametrized over all five other scope fields.
        The test uses a non-fleet scope pairing so it doesn't trip the fleet-scoped
        evidence minItems rule — the test must fail for the oneOf reason, not another.

        Mutation: remove population_id: {not: {}} from any of the 5 existing branches →
        that branch stops forbidding population_id → {that_field, population_id} validates
        → assertion fails.
        """
        _VALID_EVIDENCE = [
            {
                "kind": "quality.root_cause",
                "source": "agent-fi-quality",
                "source_ref": "src-001",
                "value": {"candidate_id": "c1"},
                "computed_at": "2026-10-08T00:00:00Z",
                "provenance": "live",
            }
        ]
        other_fields = [
            ("vin", "1MRDVEH0000000001"),
            ("owner_id", "owner-001"),
            ("fleet_id", "FLEET-MRD-001"),
            ("dealership_id", "DLRSHP-001"),
            ("district_id", "DIST-001"),
        ]
        for field_name, field_value in other_fields:
            scope = {"population_id": "meridian-motors", field_name: field_value}
            finding = self._base_finding(
                kind="quality.investigation",
                agent_id="agent-fi-quality",
                scope=scope,
            )
            # Provide non-empty evidence for fleet_id scoped findings to avoid the
            # evidence minItems rule masking the oneOf error.
            if field_name == "fleet_id":
                finding["evidence"] = _VALID_EVIDENCE
            errors = _validate(finding_schema, finding)
            assert errors, (
                f"population_id + {field_name} in scope should fail the oneOf but validated. "
                f"C1 mutation guard: removing population_id: {{not: {{}}}} from the "
                f"{field_name} branch → this pairing would validate → FAILS."
            )
            # The error must be about oneOf / scope conflict, not just evidence.
            # A weak test that passes because of an unrelated rule doesn't guard C1.
            error_text = " ".join(errors).lower()
            assert "oneof" in error_text or "population_id" in error_text or "scope" in error_text, (
                f"population_id + {field_name} scope failure must be a oneOf/scope error, "
                f"not an unrelated rule. Got: {errors!r}"
            )

    def test_unknown_agent_id_fails(self, finding_schema: dict) -> None:
        """An agent_id not in the enum fails validation."""
        finding = self._base_finding(
            kind="health.tires",
            agent_id="agent-unknown-new",
            scope={"vin": "1HGBH41JXMN109186"},
        )
        errors = _validate(finding_schema, finding)
        assert errors, (
            "Unknown agent_id 'agent-unknown-new' should fail enum validation."
        )


class TestV27NewActionValues:
    """
    New v2.7 Action enum values accept valid instances.
    """

    def _base_action(self, *, target_system: str, action_kind: str,
                     decided_by_kind: str = "operator") -> dict:
        return {
            "action_id": "AVX-ACT-f0a1b2c3-d4e5-6789-f0ab-cdef01234567",
            "finding_id": "FIND-d7e8f9a0-b1c2-3456-def0-123456789abc",
            "created_by": "owner-a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "created_at": "2026-10-08T10:00:00Z",
            "updated_at": "2026-10-08T10:00:00Z",
            "target_system": target_system,
            "status": "pending_execution",
            "action_kind": action_kind,
            "parameters": {},
            "evidence": [],
            "audit_trail": [],
            "decided_by": {
                "kind": decided_by_kind,
                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            },
        }

    def test_adp_target_system_validates(self, action_schema: dict) -> None:
        """target_system='adp' is valid (v2.7 addition)."""
        action = self._base_action(
            target_system="adp",
            action_kind="record_engineering_decision",
            decided_by_kind="engineer",
        )
        errors = _validate(action_schema, action)
        assert errors == [], f"adp target_system should be valid:\n" + "\n".join(errors)

    def test_book_fleet_service_kind_validates(self, action_schema: dict) -> None:
        """action_kind='book_fleet_service' is valid (v2.7 addition)."""
        action = self._base_action(
            target_system="dms",
            action_kind="book_fleet_service",
        )
        errors = _validate(action_schema, action)
        assert errors == [], f"book_fleet_service should be valid:\n" + "\n".join(errors)

    def test_record_engineering_decision_kind_validates(self, action_schema: dict) -> None:
        """action_kind='record_engineering_decision' is valid (v2.7 addition)."""
        action = self._base_action(
            target_system="adp",
            action_kind="record_engineering_decision",
            decided_by_kind="engineer",
        )
        errors = _validate(action_schema, action)
        assert errors == [], f"record_engineering_decision should be valid:\n" + "\n".join(errors)

    def test_issue_recall_kind_validates(self, action_schema: dict) -> None:
        """action_kind='issue_recall' is valid (v2.7 addition)."""
        action = self._base_action(
            target_system="adp",
            action_kind="issue_recall",
            decided_by_kind="engineer",
        )
        errors = _validate(action_schema, action)
        assert errors == [], f"issue_recall should be valid:\n" + "\n".join(errors)

    def test_issue_software_campaign_kind_validates(self, action_schema: dict) -> None:
        """action_kind='issue_software_campaign' is valid (v2.7 addition)."""
        action = self._base_action(
            target_system="adp",
            action_kind="issue_software_campaign",
            decided_by_kind="engineer",
        )
        errors = _validate(action_schema, action)
        assert errors == [], f"issue_software_campaign should be valid:\n" + "\n".join(errors)

    def test_engineer_decided_by_kind_validates(self, action_schema: dict) -> None:
        """decided_by.kind='engineer' is valid (v2.7 addition)."""
        action = self._base_action(
            target_system="adp",
            action_kind="record_engineering_decision",
            decided_by_kind="engineer",
        )
        errors = _validate(action_schema, action)
        assert errors == [], f"engineer decided_by.kind should be valid:\n" + "\n".join(errors)

    def test_g5_guardrail_validates(self, action_schema: dict) -> None:
        """guardrail='G5' in an audit entry is valid (v2.7 addition)."""
        action = self._base_action(
            target_system="adp",
            action_kind="record_engineering_decision",
        )
        action["status"] = "declined"
        action["audit_trail"] = [
            {
                "at": "2026-10-08T10:00:00Z",
                "by": "registry.dispatch()",
                "from_status": "pending_execution",
                "to_status": "declined",
                "failure_kind": "target_refused",
                "guardrail": "G5",
                "failure_reason": "Blocked: engineering decisions need a product engineer.",
            }
        ]
        errors = _validate(action_schema, action)
        assert errors == [], f"G5 guardrail should be valid:\n" + "\n".join(errors)

    def test_g5_failure_reason_sentence_validates(self, action_schema: dict) -> None:
        """
        The G5 failure_reason sentence is admitted by the pattern (v2.7 addition).

        Mutation guard: if the G5 sentence were absent from the pattern (mutation:
        remove it from the alternation), this test would FAIL — proving the sentence
        is present and the pattern is wired correctly.
        """
        action = self._base_action(
            target_system="adp",
            action_kind="record_engineering_decision",
        )
        action["status"] = "declined"
        action["audit_trail"] = [
            {
                "at": "2026-10-08T10:00:00Z",
                "by": "registry.dispatch()",
                "from_status": "pending_execution",
                "to_status": "declined",
                "failure_kind": "target_refused",
                "guardrail": "G5",
                "failure_reason": "Blocked: engineering decisions need a product engineer.",
            }
        ]
        errors = _validate(action_schema, action)
        assert errors == [], (
            "G5 failure_reason sentence should be admitted by the pattern:\n"
            + "\n".join(errors)
        )

    def test_adp_refused_sentence_validates(self, action_schema: dict) -> None:
        """
        ADP target-name in failure_reason is admitted by the pattern (v2.7 addition).

        The proposal adds ADP to all target-name groups in the failure_reason pattern.
        Without it, 'ADP refused the request (HTTP 422).' would fail pattern validation.
        """
        action = self._base_action(
            target_system="adp",
            action_kind="record_engineering_decision",
            decided_by_kind="engineer",
        )
        action["status"] = "declined"
        action["audit_trail"] = [
            {
                "at": "2026-10-08T10:00:00Z",
                "by": "registry.dispatch()",
                "from_status": "pending_execution",
                "to_status": "declined",
                "failure_kind": "target_refused",
                "failure_reason": "ADP refused the request (HTTP 422).",
                "error_status": 422,
            }
        ]
        errors = _validate(action_schema, action)
        assert errors == [], (
            "'ADP refused the request (HTTP 422).' should be admitted by the pattern:\n"
            + "\n".join(errors)
        )


def test_openapi_decided_by_kinds_match_the_action_schema() -> None:
    """The OpenAPI Action's ``decided_by.kind`` enum equals ``action.schema.json``'s.

    11.1 (spec 2026-11-02-fi-ground-book-return D10) added ``simulated_fleet_operator`` to the
    JSON Schema and left the OpenAPI, which has ``additionalProperties: false``, rejecting
    every stand-in Action (docs review g11 W5, 2026-11-08). Order-insensitive, exact set.
    """
    yaml = pytest.importorskip("yaml")
    openapi = yaml.safe_load(
        (REPO_ROOT / "docs" / "api" / "avx-findings-actions.openapi.yaml").read_text(encoding="utf-8")
    )
    schema = json.loads(
        (REPO_ROOT / "agents" / "tier2" / "schemas" / "action.schema.json").read_text(encoding="utf-8")
    )
    api_kinds = openapi["components"]["schemas"]["Action"]["properties"]["decided_by"]["properties"]["kind"]["enum"]
    schema_kinds = schema["properties"]["decided_by"]["properties"]["kind"]["enum"]
    assert len(api_kinds) == len(set(api_kinds))
    assert set(api_kinds) == set(schema_kinds)

