"""The seeder must not reset configured Connect coordinates to PLACEHOLDER.

`seed-personas.py` runs on every `make deploy-staging` and `_put` replaces the
whole tenant row. Before this, a row an operator had configured was silently
reset to `PLACEHOLDER` by the next deploy: escalation would work, then stop,
with nothing in the deploy output naming the cause. That is the regression these
tests exist to make impossible.

Context: escalation failed with `Resource not found of Type: INSTANCE` for as
long as the feature existed, because the seeded `instanceArn` was the literal
`arn:aws:connect:us-east-1:YOUR_ACCOUNT_ID:instance/PLACEHOLDER` and the Lambda
parsed instance id `PLACEHOLDER`. See
issues/2026-09-22-escalation-connect-config-is-placeholder/.

Run:
    pytest scripts/tests/test_seed_personas_connect_preserve.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# Real-shaped values. Deliberately NOT the live ids — this file is version
# controlled and the underlying Connect resources are tenant-named.
REAL_INSTANCE = "arn:aws:connect:us-east-1:000000000000:instance/11111111-2222-3333-4444-555555555555"
REAL_FLOW = "66666666-7777-8888-9999-aaaaaaaaaaaa"
REAL_QUEUE = f"{REAL_INSTANCE}/queue/bbbbbbbb-cccc-dddd-eeee-ffffffffffff"

PLACEHOLDER_INSTANCE = "arn:aws:connect:us-east-1:YOUR_ACCOUNT_ID:instance/PLACEHOLDER"


def _load():
    """Import seed-personas.py, which is hyphenated and needs STAGE/REGION set.

    `sys.argv` is neutralised across the import: the script rejects unrecognised
    arguments at module scope, so pytest's own argv would abort the import with
    SystemExit before a single test ran.
    """
    os.environ.setdefault("STAGE", "staging")
    os.environ.setdefault("AWS_REGION", "us-west-2")
    path = _REPO / "scripts" / "seed-personas.py"
    spec = importlib.util.spec_from_file_location("seed_personas_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    saved_argv = sys.argv
    sys.argv = [str(path)]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved_argv
    return mod


mod = _load()


class FakeTable:
    """Minimal DynamoDB Table double: records puts, replays one stored item."""

    def __init__(self, existing: dict | None = None):
        self._existing = existing
        self.puts: list[dict] = []

    def get_item(self, Key):  # noqa: N803 — boto3 kwarg casing
        if self._existing is None:
            return {}
        return {"Item": self._existing}

    def put_item(self, Item):  # noqa: N803
        self.puts.append(Item)


def _config(instance=PLACEHOLDER_INSTANCE, flow="PLACEHOLDER", queue=PLACEHOLDER_INSTANCE + "/queue/tier2-ops"):
    """A tenant config shaped like the seeder's, only the fields under test."""
    return {
        "tenantId": "meridian",
        "version": "1.0.0",
        "segment": "oem",
        "channels": {"connect": {"instanceArn": instance, "contactFlowId": flow}},
        "escalation": {"tier2QueueArn": queue},
        "booking": {"warmTransfer": {"queueArn": queue}},
    }


def _stored(instance=REAL_INSTANCE, flow=REAL_FLOW, queue=REAL_QUEUE):
    return {
        "tenantId": "meridian",
        "version": "1.0.0",
        "channels": {"connect": {"instanceArn": instance, "contactFlowId": flow}},
        "escalation": {"tier2QueueArn": queue},
        "booking": {"warmTransfer": {"queueArn": queue}},
    }


# ---------------------------------------------------------------------------
# _is_placeholder — the predicate everything else rests on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        PLACEHOLDER_INSTANCE,
        "PLACEHOLDER",
        "arn:aws:connect:us-east-1:YOUR_ACCOUNT_ID:instance/PLACEHOLDER/queue/tier2-ops",
        # A HALF-filled ARN: real instance id, account still a placeholder. This
        # case is why `YOUR_ACCOUNT_ID` is a marker in its own right — every
        # placeholder the seeder ships happens to contain "PLACEHOLDER" too, so
        # without this the marker could be deleted with the suite still green
        # (mutation S5).
        f"arn:aws:connect:us-east-1:YOUR_ACCOUNT_ID:instance/{REAL_FLOW}",
        "",
        "   ",
        None,
        12345,
    ],
)
def test_placeholder_values_are_detected(value):
    assert mod._is_placeholder(value) is True, value


@pytest.mark.parametrize("value", [REAL_INSTANCE, REAL_FLOW, REAL_QUEUE])
def test_real_values_are_not_placeholders(value):
    assert mod._is_placeholder(value) is False, value


# ---------------------------------------------------------------------------
# THE load-bearing property: no SSM must not mean "reset to placeholder"
# ---------------------------------------------------------------------------

def test_existing_real_values_are_preserved_when_ssm_is_absent():
    """The exact deploy-resets-escalation regression."""
    cfg = _config()
    table = FakeTable(existing=_stored())

    mod._apply_connect_config(cfg, table, {})

    assert cfg["channels"]["connect"]["instanceArn"] == REAL_INSTANCE
    assert cfg["channels"]["connect"]["contactFlowId"] == REAL_FLOW
    assert cfg["escalation"]["tier2QueueArn"] == REAL_QUEUE
    assert cfg["booking"]["warmTransfer"]["queueArn"] == REAL_QUEUE


def test_put_does_not_write_a_placeholder_over_a_configured_row():
    """End-to-end through `_put`, which is what the deploy actually calls.

    Uses the module's real MERIDIAN config rather than the minimal fixture,
    because `_put` validates against the full tenant schema.
    """
    import copy

    table = FakeTable(existing=_stored())

    mod._put(copy.deepcopy(mod.MERIDIAN), table, {})

    written = table.puts[0]
    assert written["channels"]["connect"]["instanceArn"] == REAL_INSTANCE
    for marker in mod._PLACEHOLDER_MARKERS:
        assert marker not in written["channels"]["connect"]["instanceArn"]
        assert marker not in written["escalation"]["tier2QueueArn"]


def test_ssm_overrides_win_over_both_placeholder_and_stored():
    """SSM is the source of truth when present — including over a stale row."""
    cfg = _config()
    stale = "arn:aws:connect:us-east-1:000000000000:instance/99999999-9999-9999-9999-999999999999"
    table = FakeTable(existing=_stored(instance=stale))

    mod._apply_connect_config(
        cfg, table,
        {"instanceArn": REAL_INSTANCE, "contactFlowId": REAL_FLOW, "tier2QueueArn": REAL_QUEUE},
    )

    assert cfg["channels"]["connect"]["instanceArn"] == REAL_INSTANCE
    # warmTransfer is the Lambda's documented fallback (handler.py:149) and must
    # also be filled, or a live fallback path stays broken.
    assert cfg["booking"]["warmTransfer"]["queueArn"] == REAL_QUEUE


def test_fresh_account_still_seeds_placeholders():
    """Nothing stored and no SSM: placeholders are correct, not a failure."""
    cfg = _config()
    mod._apply_connect_config(cfg, FakeTable(existing=None), {})
    assert mod._is_placeholder(cfg["channels"]["connect"]["instanceArn"])


def test_a_stored_placeholder_never_shadows_anything():
    """Preserve must not carry a placeholder forward as though it were config.

    Asserts the config keeps its OWN value, not merely that the result is still
    *a* placeholder — swapping one placeholder for a different one satisfies a
    bare `_is_placeholder` check, so that weaker assertion could not see the
    guard being dropped (mutation S8). The stored placeholder is therefore made
    textually distinct from the seeded one.
    """
    distinct_stored = "arn:aws:connect:us-east-1:YOUR_ACCOUNT_ID:instance/PLACEHOLDER-FROM-THE-STORED-ROW"
    assert distinct_stored != PLACEHOLDER_INSTANCE

    cfg = _config()
    table = FakeTable(existing=_stored(instance=distinct_stored, flow="PLACEHOLDER-STORED"))

    mod._apply_connect_config(cfg, table, {})

    assert cfg["channels"]["connect"]["instanceArn"] == PLACEHOLDER_INSTANCE
    assert cfg["channels"]["connect"]["contactFlowId"] == "PLACEHOLDER"
    assert mod._is_placeholder(cfg["channels"]["connect"]["instanceArn"])


def test_ddb_read_failure_does_not_abort_the_seed():
    """A seeder that cannot read must still write the other ~40 fields."""

    class Exploding(FakeTable):
        def get_item(self, Key):  # noqa: N803
            raise RuntimeError("AccessDenied")

    cfg = _config()
    mod._apply_connect_config(cfg, Exploding(), {})  # must not raise
    assert cfg["tenantId"] == "meridian"


def test_partial_ssm_is_refused_rather_than_written_beside_placeholders(monkeypatch):
    """One real value beside two placeholders reads as configured and is not."""

    class PartialSSM:
        def get_parameter(self, Name):  # noqa: N803
            if Name.endswith("connect-instance-arn"):
                return {"Parameter": {"Value": REAL_INSTANCE}}
            raise RuntimeError("ParameterNotFound")

    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: PartialSSM())
    assert mod._resolve_connect_from_ssm("staging", "us-west-2") == {}


def test_ssm_holding_a_placeholder_is_ignored(monkeypatch):
    """A parameter that exists but was never filled in is not configuration."""

    class PlaceholderSSM:
        def get_parameter(self, Name):  # noqa: N803
            return {"Parameter": {"Value": PLACEHOLDER_INSTANCE}}

    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: PlaceholderSSM())
    assert mod._resolve_connect_from_ssm("staging", "us-west-2") == {}


def test_seeder_carries_no_brand_name():
    """The Connect resources are tenant-named; their names must not enter git."""
    src = (_REPO / "scripts" / "seed-personas.py").read_text().lower()
    assert "michelin" not in src
