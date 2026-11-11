"""Wiring tests for ``run_health_agent``'s warranty-verdict fetch-and-pass.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md` § Fix Group 21 task 2.
Design record: `decisions.md` § "2026-09-23 — Fix Group 21 tasks 1–2".

## What this file is really testing

Agent 2's post-service-visit narration path shipped complete, tested and deployed in
``df5fb43`` and was never called: 18 non-test call sites of ``assess_vehicle_health``, none
passing ``warranty_verdict_phrase``. Roughly 1,700 lines of narration path and guard sat dead,
and Fix Group 19 went on to correct the *vocabulary* of something nothing reached.

**No test failed when that happened.** Every test of the narration path passed
``warranty_verdict_phrase`` itself, so all of them proved the path works *when called* and
none proved anything about whether it is called. That is the gap this file closes, and it is
why :func:`test_denied_verdict_is_narrated_verbatim` is the load-bearing test here rather
than one more narration case.

These tests drive the **real** :func:`agents.health.dms_warranty_source.fetch_verdict`
through a fake DynamoDB client, instead of monkeypatching
:func:`agents.health.agent.fetch_warranty_verdict_phrase`. Stubbing the seam under test would
reproduce the original defect: a green suite that never exercises the wiring.

## Two properties, pulling in opposite directions

1. A verdict that exists **must** reach the narration verbatim.
2. A verdict that cannot be fetched must **not** cost the owner their health assessment.

Property 2 is the reason every failure degrades to ``None``, and it is load-bearing right now:
the Tier 2 role has no grant on the DMS tables and the CDK injects no table-name env vars
(`decisions.md` F4), so in the current deployment *every* call takes the degradation path.
A regression that turned a warranty-lookup failure into a lost assessment would therefore
break every vehicle, not an edge case.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.health import agent as agent_mod
from agents.health.agent import assess_vehicle_health, run_health_agent
from agents.health.dms_warranty_source import (
    CLAIMS_RO_INDEX,
    CLAIMS_TABLE_ENV_VAR,
    REPAIR_ORDERS_TABLE_ENV_VAR,
    REPAIR_ORDERS_VIN_INDEX,
)
from agents.health.dms_verdicts import (
    DMS_VERDICT_APPROVED,
    DMS_VERDICT_DENIED,
    DMS_VERDICT_ENUM,
)

CLAIMS_TABLE = "dms-staging-warranty-claims"
REPAIR_ORDERS_TABLE = "dms-staging-repair-orders"

VIN = "MRDN0298222340245"
RO_ID = "RO-PORTLAND-202510-0172"

#: A battery row, the shape ``adp_battery_source`` emits. Chosen because it is the component
#: ``run_health_agent`` falls back to, so it exercises the most code paths.
BATTERY_ROW = {"health_pct": 92.0, "age_days": 400, "vin": VIN}


def _ago(days: float) -> str:
    """An ISO-8601 UTC timestamp *days* in the past, in DMS's output format.

    Relative, not hardcoded. This file drives the **real** ``fetch_verdict``, so the recency
    bound added for review Cycle 1 Warning 3 applies to its fixtures — and the original
    hardcoded ``2026-09-01T00:00:00Z`` was 22.7 days old against a 30-day window, i.e. it
    would have silently crossed the bound on 2026-10-01 and taken **all 13 mutation-D
    control tests** red with it. Mutation D is the control for the wiring this whole group
    exists to create, so the group's load-bearing guard had an 8-day fuse on it.

    The sibling ``test_dms_warranty_source.py`` already had this helper for exactly this
    reason; the lesson was applied in one of the two files the bound governs. Caught in
    review Cycle 2.
    """
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class FakeDdb:
    """DynamoDB client double serving one repair order and one claim."""

    def __init__(self, *, verdict_status: str | None, reason: str = "Out of coverage"):
        self._verdict_status = verdict_status
        self._reason = reason
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["IndexName"] == REPAIR_ORDERS_VIN_INDEX:
            return {"Items": [{"ro_id": {"S": RO_ID}, "vehicle_vin": {"S": VIN}}]}
        if kwargs["IndexName"] == CLAIMS_RO_INDEX:
            claim = {
                "claim_id": {"S": "WC-PORTLAND-0007"},
                "ro_id": {"S": RO_ID},
                "status": {"S": "Validated"},
                "updated_at": {"S": _ago(1)},
            }
            if self._verdict_status is not None:
                claim["verdict_status"] = {"S": self._verdict_status}
                claim["verdict_reason"] = {"S": self._reason}
            return {"Items": [claim]}
        raise AssertionError(f"unexpected index {kwargs['IndexName']!r}")


class ExplodingDdb:
    """Client double that fails the way a denied cross-account read fails."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        raise self._exc


@pytest.fixture
def _tables(monkeypatch):
    monkeypatch.setenv(CLAIMS_TABLE_ENV_VAR, CLAIMS_TABLE)
    monkeypatch.setenv(REPAIR_ORDERS_TABLE_ENV_VAR, REPAIR_ORDERS_TABLE)


def _wire(monkeypatch, client):
    """Point the agent's cached DDB client at *client*, leaving fetch_verdict real."""
    monkeypatch.setattr(agent_mod, "_dms_ddb_client", lambda: client)
    # Defeat the module-level cache so one test's client cannot leak into the next.
    monkeypatch.setattr(agent_mod, "_dms_ddb_singleton", None)


# ---------------------------------------------------------------------------
# Property 1: an existing verdict reaches the narration verbatim
# ---------------------------------------------------------------------------


def test_denied_verdict_is_narrated_verbatim(monkeypatch, _tables):
    """MUTATION TARGET (task 2, the load-bearing mutation): dropping the
    ``warranty_verdict_phrase=`` argument from ``run_health_agent``'s
    ``assess_vehicle_health`` calls must fail this test.

    This is the assertion whose absence let the entire narration path sit unwired through
    two sessions and a vocabulary fix."""
    _wire(monkeypatch, FakeDdb(verdict_status=DMS_VERDICT_DENIED))

    result = run_health_agent(VIN, [BATTERY_ROW])

    assert DMS_VERDICT_DENIED in result["finding"], (
        "the DMS verdict did not reach the narration — run_health_agent is not forwarding "
        "warranty_verdict_phrase"
    )


def test_verdict_is_narrated_verbatim_not_softened(monkeypatch, _tables):
    """'denied' must appear as DMS spells it. Softening is the boundary violation."""
    _wire(monkeypatch, FakeDdb(verdict_status=DMS_VERDICT_DENIED))

    finding = run_health_agent(VIN, [BATTERY_ROW])["finding"]

    assert DMS_VERDICT_DENIED in finding
    for softening in ("declined", "may require review", "under review", "Denied"):
        # Anti-vacuity, matching the two sibling tests exempted in
        # `_TOKEN_BACKSTOP_EXEMPT_SITES`: assert the token really is outside the vocabulary
        # before asserting its absence, so this cannot silently invert if a softening ever
        # enters the enum.
        assert softening not in DMS_VERDICT_ENUM, (
            f"{softening!r} is now a real DMS verdict — this test asserts its ABSENCE from "
            "the narration and would now be asserting the wrong thing"
        )
        assert softening not in finding, f"verdict softened to {softening!r}"


def test_approved_verdict_also_reaches_narration(monkeypatch, _tables):
    """Not only denials — the wiring must be verdict-agnostic."""
    _wire(monkeypatch, FakeDdb(verdict_status=DMS_VERDICT_APPROVED))
    assert DMS_VERDICT_APPROVED in run_health_agent(VIN, [BATTERY_ROW])["finding"]


def test_narration_degraded_does_not_survive_the_runner_adapter(monkeypatch, _tables):
    """The ``narration_degraded`` flag is set by the narration path and **dropped here**.

    An earlier version of this test claimed the opposite. Its docstring asserted it "proves
    the guard's output is not silently dropped by ``_extract_single_component``", while its
    body only re-checked a text prefix already covered above — and the property it named is
    false. Recorded rather than quietly rewritten because it is the exact defect shape this
    spec keeps producing: an assertion whose name promises more than its body checks.

    The real mechanism, verified end to end:

    1. ``assess_vehicle_health`` sets ``narration_degraded=True`` on the component result
       when the verbatim guard has to rewrite the finding (``agent.py`` step 2).
    2. :func:`_extract_single_component` returns a **six-key whitelist** — ``finding``,
       ``severity_hint``, ``confidence``, ``evidence``, ``_tokens_used``, ``_cost_usd`` —
       so the flag does not reach the runner.
    3. Even if it did, ``runner._build_artifact`` selects named keys from the agent dict via
       ``.get()``; it does not splat. And ``finding.schema.json`` is
       ``additionalProperties: false``, so surfacing the flag on the artifact is a frozen-
       contract change, not a passthrough.

    A reader therefore needs three coordinated changes plus a UI consumer, which is outside
    this group — the spec states the frozen contract is untouched by it. Tracked as the
    follow-on in `decisions.md`; it is the sharper form of FG19 security Suggestion S2, which
    said "no reader": the accurate statement is *no reachable reader*, and per FG19's own
    recorded constraint the rewrite path fires on **100% of narrated verdicts** while
    ``_llm_caller`` is ``None``, so the signal is dropped every time rather than rarely.

    This test pins the current behaviour so the gap cannot close silently: when a reader is
    wired, this test fails and must be updated deliberately.
    """
    _wire(monkeypatch, FakeDdb(verdict_status=DMS_VERDICT_DENIED))

    result = run_health_agent(VIN, [BATTERY_ROW])

    # The guard did fire — the verdict was prepended to the finding.
    assert result["finding"].startswith(DMS_VERDICT_DENIED)
    # And the flag it set did not survive the adapter.
    assert "narration_degraded" not in result, (
        "narration_degraded now survives run_health_agent — if that is intentional, this "
        "test and the decisions.md follow-on both need updating, and the artifact/schema "
        "side must be checked because finding.schema.json is additionalProperties: false"
    )
    # ``headline`` is the one the coverage-verdict guard checked (spec
    # 2026-09-25-avx-own-vehicle-findings T4.3); the runner writes it as-is.
    assert set(result) == {
        "finding", "severity_hint", "confidence", "evidence",
        "_tokens_used", "_cost_usd", "headline",
    }


@pytest.mark.parametrize(
    "rows,component",
    [
        ({"component": "battery", "data": [BATTERY_ROW]}, "battery"),
        ({"component": "tires", "data": [{"replacement_flagged": False, "wear_band": "ok"}]}, "tires"),
        ({"component": "brakes", "data": {"outcome": "resolved", "service_date": "2026-01-01"}}, "brakes"),
        ({"component": "nonsense", "data": []}, "battery"),
    ],
)
def test_every_multi_component_branch_forwards_the_verdict(monkeypatch, _tables, rows, component):
    """All four branches of the multi-component convention, including the unknown-component
    fallback. A branch that forgets the argument is the defect this group exists to fix, so
    each is pinned rather than trusting one representative."""
    _wire(monkeypatch, FakeDdb(verdict_status=DMS_VERDICT_DENIED))
    assert DMS_VERDICT_DENIED in run_health_agent(VIN, rows)["finding"]


@pytest.mark.parametrize(
    "rows",
    [
        [BATTERY_ROW],
        [{"replacement_flagged": True, "wear_band": "replace"}],
        [{"outcome": "parts_pending", "service_date": "2026-01-01"}],
        [{"totally": "unrecognised"}],
        [],
    ],
)
def test_every_single_component_branch_forwards_the_verdict(monkeypatch, _tables, rows):
    """All five shape-detection branches, including the empty-rows and unknown-shape paths."""
    _wire(monkeypatch, FakeDdb(verdict_status=DMS_VERDICT_DENIED))
    assert DMS_VERDICT_DENIED in run_health_agent(VIN, rows)["finding"]


def test_verdict_is_fetched_once_per_call_not_once_per_component(monkeypatch, _tables):
    """A per-vehicle fact fetched per component would be two cross-account reads for one
    answer. Two queries total = one two-hop lookup."""
    ddb = FakeDdb(verdict_status=DMS_VERDICT_DENIED)
    _wire(monkeypatch, ddb)

    run_health_agent(VIN, [BATTERY_ROW])

    assert len(ddb.calls) == 2, f"expected 2 queries (one two-hop fetch), got {len(ddb.calls)}"


# ---------------------------------------------------------------------------
# Property 2: absence and failure must not cost the health assessment
# ---------------------------------------------------------------------------


def _baseline(rows) -> dict:
    """The pre-Fix-Group-21 output: assess with no verdict, extracted as the runner does."""
    return agent_mod._extract_single_component(
        assess_vehicle_health(VIN, battery_rows=rows), "battery"
    )


def test_no_claim_produces_byte_identical_output(monkeypatch, _tables):
    """A vehicle with no warranty verdict must be assessed exactly as before this group."""
    _wire(monkeypatch, FakeDdb(verdict_status=None))

    assert run_health_agent(VIN, [BATTERY_ROW]) == _baseline([BATTERY_ROW])


def test_fetch_exception_degrades_to_none_and_output_is_unchanged(monkeypatch, _tables):
    """MUTATION TARGET (task 2): making a fetch exception propagate instead of degrading to
    ``None`` must fail this test.

    A warranty lookup is additive evidence. Losing a battery/tire/brake assessment because a
    cross-account DynamoDB read failed would be a strictly worse outcome than not narrating
    the verdict."""
    _wire(monkeypatch, ExplodingDdb(RuntimeError("AccessDeniedException: not authorized")))

    assert run_health_agent(VIN, [BATTERY_ROW]) == _baseline([BATTERY_ROW])


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("AccessDeniedException"),
        ValueError("malformed response"),
        KeyError("Items"),
        TimeoutError("read timed out"),
        OSError("endpoint unreachable"),
    ],
)
def test_any_exception_class_degrades_rather_than_propagating(monkeypatch, _tables, exc):
    """The broad except is deliberate — the list of what a cross-account DDB read can raise
    is long, and any omission turns a warranty failure into a lost assessment."""
    _wire(monkeypatch, ExplodingDdb(exc))
    assert run_health_agent(VIN, [BATTERY_ROW]) == _baseline([BATTERY_ROW])


def test_missing_table_env_vars_degrade_to_none(monkeypatch):
    """This is the CURRENT deployed state, not a hypothetical: the CDK injects no table-name
    env vars and the Tier 2 role has no grant (`decisions.md` F4/F5). Every production call
    takes this path today, so it must be the *safe* path."""
    monkeypatch.delenv(CLAIMS_TABLE_ENV_VAR, raising=False)
    monkeypatch.delenv(REPAIR_ORDERS_TABLE_ENV_VAR, raising=False)
    _wire(monkeypatch, FakeDdb(verdict_status=DMS_VERDICT_DENIED))

    assert run_health_agent(VIN, [BATTERY_ROW]) == _baseline([BATTERY_ROW])


def test_out_of_enum_verdict_degrades_rather_than_narrating_it(monkeypatch, _tables):
    """The source raises on an unrecognised verdict; the runner adapter must absorb that.
    Narrating 'declined' verbatim is the Fix Group 19 defect; dropping the whole assessment
    over one corrupt row is the Property-2 defect. Neither is acceptable — degrade."""
    _wire(monkeypatch, FakeDdb(verdict_status="some_unknown_verdict"))

    assert run_health_agent(VIN, [BATTERY_ROW]) == _baseline([BATTERY_ROW])


def test_boto3_absent_degrades_to_none(monkeypatch, _tables):
    """``_dms_ddb_client`` imports boto3 lazily. An ImportError must not break the agent in
    an environment without boto3 on the path."""

    def _raise():
        raise ImportError("No module named 'boto3'")

    monkeypatch.setattr(agent_mod, "_dms_ddb_client", _raise)
    monkeypatch.setattr(agent_mod, "_dms_ddb_singleton", None)

    assert run_health_agent(VIN, [BATTERY_ROW]) == _baseline([BATTERY_ROW])


def test_degradation_still_returns_the_runner_budget_fields(monkeypatch, _tables):
    """The runner reads ``_tokens_used`` / ``_cost_usd`` for its budget cap; a degraded
    warranty fetch must not omit them."""
    _wire(monkeypatch, ExplodingDdb(RuntimeError("boom")))

    result = run_health_agent(VIN, [BATTERY_ROW])

    assert result["_tokens_used"] == 0
    assert result["_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# The fetch helper in isolation
# ---------------------------------------------------------------------------


def test_fetch_helper_returns_the_verdict_status_string(monkeypatch, _tables):
    """The helper returns the bare ``verdict_status`` value, which is what the narration
    guard compares verbatim — not the whole verdict dict."""
    _wire(monkeypatch, FakeDdb(verdict_status=DMS_VERDICT_DENIED))
    assert agent_mod.fetch_warranty_verdict_phrase(VIN) == DMS_VERDICT_DENIED


def test_fetch_helper_returns_none_when_no_verdict(monkeypatch, _tables):
    _wire(monkeypatch, FakeDdb(verdict_status=None))
    assert agent_mod.fetch_warranty_verdict_phrase(VIN) is None


def test_fetch_helper_never_raises(monkeypatch, _tables):
    _wire(monkeypatch, ExplodingDdb(RuntimeError("boom")))
    assert agent_mod.fetch_warranty_verdict_phrase(VIN) is None


def test_dms_region_is_us_west_2_not_us_east_1():
    """DMS `decisions.md` 2026-08-28 records us-east-1 as the wrong default; the Tier 1
    ``dms_warranty`` tool pins us-west-2 and this must match it."""
    assert agent_mod._DMS_REGION == "us-west-2"
