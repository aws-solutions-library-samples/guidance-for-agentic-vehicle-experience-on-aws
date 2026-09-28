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
        ["dms", "vehicle-command", "retail", "none", "chat", "ios-owner"],
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
            "book_combined_visit",
            "schedule_inspection",
            "update_charging_schedule",
            "file_warranty_pre",
            "surface_recall",
            "acknowledge_only",
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
            r"^/(findings|actions)/([^/]+)/\{([^/]+)\}$"
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
