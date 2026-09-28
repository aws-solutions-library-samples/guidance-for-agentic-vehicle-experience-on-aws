"""The two persona seeds must agree on tenant ids.

Two scripts write tenant identity and they are not the same script:

* ``scripts/seed-personas.py`` writes the tenant CONFIG rows into
  ``vsa-{stage}-tenant-config`` — the rows the app looks up.
* ``scripts/seed-persona-users.py`` writes the Cognito USERS, whose
  ``custom:tenantId`` is the key used to perform that lookup.

If the two disagree, nothing raises. ``MainTabView.loadTenantConfig(tenantId:)``
matches by exact string; a miss leaves ``tenantConfig`` nil and
``LayoutSegment.from(nil)`` falls through to ``.fleet``, so the app mislabels tabs
and applies the wrong theming with no error anywhere.

That is exactly what happened on 2026-08-21: commit 389ab7c renamed the OEM tenant
``ford`` -> ``meridian`` in the config seeder, the live DynamoDB row, and the iOS
DemoPersona — but missed the user seeder, leaving it writing a ``custom:tenantId``
with no matching row. The existing guard
(``scripts/tests/test_seed_personas_tenant_ids.py``) could not catch it because it
only ever inspected ONE of the two scripts.

Worse, ``custom:tenantId`` is immutable in the pool, so a wrongly-seeded user cannot
be patched — it has to be deleted and recreated. A silent drift here costs a user
rebuild, not an attribute update.

This test compares the two sets directly. It is the only place that does.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONFIG_SEED = REPO / "scripts" / "seed-personas.py"
USER_SEED = REPO / "scripts" / "seed-persona-users.py"


@pytest.fixture(scope="module")
def config_tenant_ids() -> set[str]:
    """Tenant ids the CONFIG seeder would write.

    Imported rather than parsed, so the assertion tracks the real value including any
    computed ones. `seed-personas.py` validates STAGE and parses `sys.argv` at import
    time on purpose — a bare run must not be able to seed prod — so both are
    neutralised here.
    """
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(sys, "argv", ["seed-personas.py"])
        mp.setenv("STAGE", "staging")
        mp.setenv("AWS_REGION", "us-west-2")
        spec = importlib.util.spec_from_file_location("seed_personas_x", CONFIG_SEED)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return {c["tenantId"] for c in mod.ALL_CONFIGS}
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def user_tenant_ids() -> set[str]:
    """Tenant ids the USER seeder would write.

    Read by regex rather than import: `seed-persona-users.py` reaches for AWS clients
    and stage config at import time, and this test must not need credentials to state
    a fact about the source. The pattern is anchored to the persona-level key, not
    `custom:tenantId` inside a nested payload, so it cannot drift into matching
    something else.
    """
    src = USER_SEED.read_text(encoding="utf-8")
    ids = set()
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(r'^"tenantId":\s*"([a-z0-9-]+)",?$', stripped)
        if m:
            ids.add(m.group(1))
    return ids


def test_both_seeders_are_readable(config_tenant_ids, user_tenant_ids):
    """A comparison of two empty sets passes and proves nothing."""
    assert config_tenant_ids, "config seeder yielded no tenant ids"
    assert user_tenant_ids, "user seeder yielded no tenant ids — has the key shape changed?"


def test_user_seed_tenant_ids_all_have_a_config_row(config_tenant_ids, user_tenant_ids):
    """Every tenantId written onto a user must have a config row to look up.

    This is the direction that breaks the app: a user carrying an unknown tenantId
    gets a silent lookup miss and the wrong layout.
    """
    orphans = sorted(user_tenant_ids - config_tenant_ids)
    assert not orphans, (
        f"seed-persona-users.py writes tenantId(s) with no row in seed-personas.py: "
        f"{orphans}. The app looks these up by exact match and fails SILENTLY — "
        f"LayoutSegment falls back to .fleet. Note custom:tenantId is immutable in "
        f"Cognito, so a user already seeded with one of these must be deleted and "
        f"recreated, not patched."
    )


def test_the_sets_are_identical(config_tenant_ids, user_tenant_ids):
    """Both directions, so a config row for a tenant with no persona is also caught.

    A config row nobody uses is a smaller problem than an orphaned user, but it is
    still drift, and it is usually the first half of a half-finished rename.
    """
    assert config_tenant_ids == user_tenant_ids, (
        f"tenant ids differ between the two seeds.\n"
        f"  only in seed-personas.py     : {sorted(config_tenant_ids - user_tenant_ids)}\n"
        f"  only in seed-persona-users.py: {sorted(user_tenant_ids - config_tenant_ids)}"
    )


def test_retired_ford_tenant_is_gone_from_both(config_tenant_ids, user_tenant_ids):
    """`ford` was the last real-company tenant key in the live path.

    Pinned by name because the rename is the kind of thing that gets reverted by a
    merge, and the failure it causes is invisible at runtime.
    """
    assert "ford" not in config_tenant_ids
    assert "ford" not in user_tenant_ids
    assert "meridian" in config_tenant_ids
    assert "meridian" in user_tenant_ids
