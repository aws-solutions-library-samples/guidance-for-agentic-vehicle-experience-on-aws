"""Every tenant config in seed-personas.py must satisfy the published schema.

WHY THIS EXISTS
---------------
`seed-personas.py` seeded only 2 of its 5 tenants for roughly six weeks
(2026-06-18 → 2026-08-02) and nothing noticed. Two underscored `tenantId`
values — `vehicle_owner` and `service_advisor` — violate the schema's
`^[a-z0-9-]{3,32}$` pattern. The loop validated and wrote one tenant at a time,
so the first violation raised mid-loop: `ford` and `enterprise` were already
committed, and `vehicle_owner`, `service_advisor`, and `technician` were never
written. `technician` is itself schema-valid and failed purely because of its
position in the tuple.

Root cause was namespace confusion: `vehicle_owner` and `service_advisor` are
legitimate **persona_id** values, where snake_case is the convention (see
`agents/supervisor/tools/__init__.py`, `evals/baselines/tier3.json`). They were
reused verbatim as **tenantId**, which is a different namespace with a stricter
kebab-case contract.

See issues/2026-08-02-seed-personas-underscore-tenantid-truncates-seed/.

These tests are cheap, offline, and would have caught it on the day it shipped.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "seed-personas.py"
SCHEMA_PATH = REPO_ROOT / "schemas" / "vsa-tenant-config.schema.json"


@pytest.fixture(scope="module")
def seed_module():
    spec = importlib.util.spec_from_file_location("seed_personas", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


class TestSeedPersonasTenantIds:
    def test_all_configs_validate_against_the_schema(self, seed_module, schema):
        """The regression itself: every config must validate, not just the first two."""
        failures = []
        for cfg in seed_module.ALL_CONFIGS:
            try:
                jsonschema.validate(cfg, schema)
            except jsonschema.ValidationError as exc:
                failures.append(f"{cfg.get('tenantId')!r}: {exc.message}")
        assert not failures, (
            "seed-personas.py would abort mid-loop and leave a partially-seeded "
            "table:\n  " + "\n  ".join(failures)
        )

    def test_validate_all_reports_every_offender_not_just_the_first(self, seed_module):
        """A half-applied seed is worse than a refused one. `validate_all` must
        surface all failures so an operator fixes them in one pass."""
        bad = [
            {**seed_module.FORD, "tenantId": "bad_one"},
            {**seed_module.ENTERPRISE, "tenantId": "bad_two"},
        ]
        errors = seed_module.validate_all(bad)
        assert len(errors) == 2, (
            f"validate_all must report both offenders, got {len(errors)}: {errors}"
        )
        assert any("bad_one" in e for e in errors)
        assert any("bad_two" in e for e in errors)

    def test_tenant_ids_are_kebab_case(self, seed_module, schema):
        pattern = re.compile(schema["properties"]["tenantId"]["pattern"])
        for cfg in seed_module.ALL_CONFIGS:
            tid = cfg["tenantId"]
            assert pattern.match(tid), (
                f"tenantId {tid!r} violates {pattern.pattern} — kebab-case only. "
                f"A persona_id is not a tenantId; do not reuse one verbatim."
            )

    def test_no_underscores_in_any_tenant_id(self, seed_module):
        """Stated separately from the pattern test because the underscore is the
        specific mistake that caused the outage, and a future schema loosening
        should not silently re-permit it here."""
        offenders = [c["tenantId"] for c in seed_module.ALL_CONFIGS if "_" in c["tenantId"]]
        assert not offenders, (
            f"Underscored tenantIds reintroduced: {offenders}. These are persona_id "
            f"values, not tenant ids — see the issue for why this aborted the seed."
        )

    def test_tenant_ids_are_unique(self, seed_module):
        ids = [c["tenantId"] for c in seed_module.ALL_CONFIGS]
        assert len(ids) == len(set(ids)), f"duplicate tenantIds: {ids}"

    def test_all_five_personas_still_present(self, seed_module):
        """Guard against 'fixing' the seed by deleting the failing tenants."""
        assert len(seed_module.ALL_CONFIGS) == 5, (
            f"Expected 5 persona tenant configs, found "
            f"{len(seed_module.ALL_CONFIGS)}. Dropping a tenant is not a valid fix "
            f"for a schema violation."
        )
        assert {c["tenantId"] for c in seed_module.ALL_CONFIGS} == {
            "ford", "enterprise", "vehicle-owner", "service-advisor", "technician",
        }
