"""
Unit tests for metric emission in agents/tier2/observability.py — Group 4.

Spec § D5: three CloudWatch metrics (VinsSelected, ArtifactsWritten,
DuplicatesSkipped) emitted in one PutMetricData call per run end, namespace
CVX/Tier2, dimensions stage + component.

Critical invariants:
  1. All datums are sent in a single call with correct metric names.
  2. Dimensions carry ``stage`` and ``component`` — no VIN, no other dimension.
  3. Namespace is exactly ``CVX/Tier2``.
  4. A raising CloudWatch client does NOT propagate — the exception is swallowed.
  5. A None CloudWatch client is a no-op (no call made, no error).
  6. Values round-trip correctly (int → float in the datum, correct count).
  7. The runner wires emit_metrics and swallows a raising client end-to-end.

All tests use stub/double clients — no network access.
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import MagicMock, call

import pytest

from agents.tier2.observability import emit_metrics


# ---------------------------------------------------------------------------
# Helpers / constants
# ---------------------------------------------------------------------------

_STAGE = "staging"
_COMPONENT = "tires"


class _CapturingCWClient:
    """A stub CloudWatch client that records every put_metric_data call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _RaisingCWClient:
    """A stub CloudWatch client whose put_metric_data always raises."""

    def put_metric_data(self, **kwargs: Any) -> None:
        raise RuntimeError("simulated CloudWatch service error")


# ---------------------------------------------------------------------------
# Basic call shape
# ---------------------------------------------------------------------------


def test_emit_metrics_calls_put_metric_data_once():
    """Exactly one PutMetricData call per emit_metrics invocation."""
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=10, artifacts_written=5, duplicates_skipped=5)
    assert len(cw.calls) == 1, "emit_metrics must make exactly one PutMetricData call"


def test_emit_metrics_uses_cvx_tier2_namespace():
    """Namespace must be exactly 'CVX/Tier2'."""
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=1, artifacts_written=1, duplicates_skipped=0)
    assert cw.calls[0]["Namespace"] == "CVX/Tier2"


def test_emit_metrics_sends_all_datums():
    """The single call carries exactly five MetricData entries (updated Fix Group 21 W2).

    Was 4. OwnerIntegrityDegraded was added when review cycle 2 found the counter
    had a run-summary field and an observer method but no CloudWatch datum — and
    that this very assertion was enshrining the absence as correct.
    """
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=10, artifacts_written=7, duplicates_skipped=3)
    metric_data = cw.calls[0]["MetricData"]
    assert len(metric_data) == 5, (
        f"Expected 5 datums (VinsSelected, ArtifactsWritten, DuplicatesSkipped, "
        f"AvxWriteFailures, OwnerIntegrityDegraded), got {len(metric_data)}: "
        f"{[d['MetricName'] for d in metric_data]}"
    )


def test_emit_metrics_datum_names():
    """The five datums have exactly the names specified in spec § D5 + T17.4 W1 + Fix Group 21 W2."""
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=10, artifacts_written=7, duplicates_skipped=3)
    names = {d["MetricName"] for d in cw.calls[0]["MetricData"]}
    assert names == {"VinsSelected", "ArtifactsWritten", "DuplicatesSkipped",
                     "AvxWriteFailures", "OwnerIntegrityDegraded"}


def test_emit_metrics_vins_selected_value():
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=42, artifacts_written=0, duplicates_skipped=0)
    datum = next(d for d in cw.calls[0]["MetricData"] if d["MetricName"] == "VinsSelected")
    assert datum["Value"] == 42.0


def test_emit_metrics_artifacts_written_value():
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=0, artifacts_written=17, duplicates_skipped=0)
    datum = next(d for d in cw.calls[0]["MetricData"] if d["MetricName"] == "ArtifactsWritten")
    assert datum["Value"] == 17.0


def test_emit_metrics_duplicates_skipped_value():
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=0, artifacts_written=0, duplicates_skipped=99)
    datum = next(d for d in cw.calls[0]["MetricData"] if d["MetricName"] == "DuplicatesSkipped")
    assert datum["Value"] == 99.0


def test_emit_metrics_unit_is_count():
    """All datums must use Unit='Count'."""
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=1, artifacts_written=1, duplicates_skipped=1)
    for datum in cw.calls[0]["MetricData"]:
        assert datum["Unit"] == "Count", (
            f"Datum {datum['MetricName']} has Unit={datum['Unit']!r}, expected 'Count'"
        )


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------


def test_emit_metrics_dimensions_contain_stage():
    """Each datum must have a 'stage' dimension matching the passed value."""
    cw = _CapturingCWClient()
    emit_metrics(cw, "prod", "battery",
                 vins_selected=5, artifacts_written=5, duplicates_skipped=0)
    for datum in cw.calls[0]["MetricData"]:
        dim_map = {d["Name"]: d["Value"] for d in datum["Dimensions"]}
        assert "stage" in dim_map, f"Datum {datum['MetricName']} missing 'stage' dimension"
        assert dim_map["stage"] == "prod"


def test_emit_metrics_dimensions_contain_component():
    """Each datum must have a 'component' dimension matching the passed value."""
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, "brakes",
                 vins_selected=2000, artifacts_written=1999, duplicates_skipped=1)
    for datum in cw.calls[0]["MetricData"]:
        dim_map = {d["Name"]: d["Value"] for d in datum["Dimensions"]}
        assert "component" in dim_map, f"Datum {datum['MetricName']} missing 'component' dimension"
        assert dim_map["component"] == "brakes"


def test_emit_metrics_dimensions_count_is_two():
    """Each datum carries exactly 2 dimensions: stage and component."""
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=1, artifacts_written=1, duplicates_skipped=0)
    for datum in cw.calls[0]["MetricData"]:
        assert len(datum["Dimensions"]) == 2, (
            f"Datum {datum['MetricName']} has {len(datum['Dimensions'])} dimensions, expected 2"
        )


def test_emit_metrics_no_vin_in_any_dimension():
    """No dimension name or value may contain a VIN-shaped string.

    This is the security invariant from spec § D5 + security review — metrics
    carry no VIN dimension at all.  The test uses a synthetic 17-char VIN-shaped
    string as a sentinel to prove none slips through.
    """
    import re

    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=5, artifacts_written=3, duplicates_skipped=2)

    vin_pattern = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
    for datum in cw.calls[0]["MetricData"]:
        for dim in datum["Dimensions"]:
            for field_val in (dim["Name"], dim["Value"]):
                assert not vin_pattern.search(str(field_val)), (
                    f"VIN-shaped value found in dimension {dim['Name']!r}={dim['Value']!r}"
                )


# ---------------------------------------------------------------------------
# Failure safety — a raising client must not propagate
# ---------------------------------------------------------------------------


def test_emit_metrics_swallows_put_metric_data_exception():
    """A raising CloudWatch client must not propagate — spec § D5 hard requirement."""
    cw = _RaisingCWClient()
    # Must not raise — losing a metric cannot fail a run that did real work.
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=5, artifacts_written=3, duplicates_skipped=2)


def test_emit_metrics_swallows_arbitrary_exception_type():
    """Any Exception subtype must be swallowed (not BaseException — we don't suppress SystemExit)."""
    class _WeirdError(Exception):
        pass

    class _WeirdRaisingClient:
        def put_metric_data(self, **kwargs: Any) -> None:
            raise _WeirdError("unexpected error type")

    cw = _WeirdRaisingClient()
    # Must not raise, regardless of Exception subtype.
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=1, artifacts_written=1, duplicates_skipped=0)


def test_emit_metrics_none_client_is_noop():
    """A None client skips the call entirely — no error, no side effect."""
    # Should not raise and should not try to call any method on None.
    emit_metrics(None, _STAGE, _COMPONENT,
                 vins_selected=5, artifacts_written=5, duplicates_skipped=0)


# ---------------------------------------------------------------------------
# Zero-value correctness
# ---------------------------------------------------------------------------


def test_emit_metrics_all_zeros_is_valid():
    """Emitting all-zero counts is valid — it represents a run that selected nothing."""
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=0, artifacts_written=0, duplicates_skipped=0)
    assert len(cw.calls) == 1
    for datum in cw.calls[0]["MetricData"]:
        assert datum["Value"] == 0.0


# ---------------------------------------------------------------------------
# Runner integration — emit_metrics wired end-to-end
# ---------------------------------------------------------------------------

from agents.tier2.runner import run as _run


class _FakeSource:
    """Minimal source double for runner integration tests."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def fetch_findings(self, vins: Optional[list] = None) -> list[dict]:
        if vins is None:
            return self._rows
        return [r for r in self._rows if r.get("vin") in vins]

    def select_vins(self, *, cursor: Optional[str], limit: int):
        return ([], None)


class _FakeWriter:
    def __init__(self, always_new: bool = True) -> None:
        self.puts: list[dict] = []
        self.always_new = always_new

    def write_finding(
        self, finding: dict, *, utc_now_fn=None, standing=None
    ) -> dict:
        """D2: route write through write_finding; record in puts for metrics assertions."""
        self.puts.append(finding)
        action = "put" if self.always_new else "noop"
        return {"action": action, "finding_id": finding.get("finding_id", "FIND-fake")}

    def put(self, artifact: dict) -> bool:
        self.puts.append(artifact)
        return self.always_new

    def put_raw(self, item: dict) -> None:
        pass


def _simple_agent_fn(vin: str, rows: list[dict]) -> dict:
    return {
        "finding": "ok",
        "severity_hint": "routine",
        "confidence": "high",
        "evidence": [],
    }


def test_runner_calls_emit_metrics_on_normal_run():
    """runner.run() must call put_metric_data when cloudwatch_client is provided."""
    cw = _CapturingCWClient()
    source = _FakeSource([{"vin": "VIN-METRICS-TEST-001", "state_of_health_pct": "88"}])
    writer = _FakeWriter()

    _run(
        "staging",
        source=source,
        writer=writer,
        vins=["VIN-METRICS-TEST-001"],
        component="battery",
        agent_fn=_simple_agent_fn,
        cloudwatch_client=cw,
    )

    assert len(cw.calls) == 1, "Expected exactly one PutMetricData call after a normal run"
    names = {d["MetricName"] for d in cw.calls[0]["MetricData"]}
    assert names == {"VinsSelected", "ArtifactsWritten", "DuplicatesSkipped",
                     "AvxWriteFailures", "OwnerIntegrityDegraded"}


def test_runner_emit_metrics_artifacts_written_count():
    """ArtifactsWritten datum reflects the actual count of new artifacts."""
    cw = _CapturingCWClient()
    source = _FakeSource([
        {"vin": "VIN-METRICS-A001", "state_of_health_pct": "80"},
        {"vin": "VIN-METRICS-A002", "state_of_health_pct": "75"},
    ])
    writer = _FakeWriter(always_new=True)

    _run(
        "staging",
        source=source,
        writer=writer,
        vins=["VIN-METRICS-A001", "VIN-METRICS-A002"],
        component="battery",
        agent_fn=_simple_agent_fn,
        cloudwatch_client=cw,
    )

    datum = next(d for d in cw.calls[0]["MetricData"] if d["MetricName"] == "ArtifactsWritten")
    assert datum["Value"] == 2.0, (
        f"Expected ArtifactsWritten=2.0 for 2 new artifacts, got {datum['Value']}"
    )


def test_runner_emit_metrics_duplicates_skipped_count():
    """DuplicatesSkipped datum reflects the actual count of idempotent puts."""
    cw = _CapturingCWClient()
    source = _FakeSource([
        {"vin": "VIN-METRICS-D001", "state_of_health_pct": "90"},
    ])
    writer = _FakeWriter(always_new=False)  # every put is a duplicate

    _run(
        "staging",
        source=source,
        writer=writer,
        vins=["VIN-METRICS-D001"],
        component="battery",
        agent_fn=_simple_agent_fn,
        cloudwatch_client=cw,
    )

    datum_written = next(
        d for d in cw.calls[0]["MetricData"] if d["MetricName"] == "ArtifactsWritten"
    )
    datum_skipped = next(
        d for d in cw.calls[0]["MetricData"] if d["MetricName"] == "DuplicatesSkipped"
    )
    assert datum_written["Value"] == 0.0
    assert datum_skipped["Value"] == 1.0


def test_runner_raising_cloudwatch_client_does_not_propagate():
    """A raising CloudWatch client must not cause run() to raise.

    This is the hard requirement from spec § D5: losing a metric must never
    fail a run that did real work.  The run's actual output is the artifact.
    """
    cw = _RaisingCWClient()
    source = _FakeSource([{"vin": "VIN-METRICS-R001", "state_of_health_pct": "85"}])
    writer = _FakeWriter()

    # Must not raise, even though the CloudWatch client always raises.
    result = _run(
        "staging",
        source=source,
        writer=writer,
        vins=["VIN-METRICS-R001"],
        component="battery",
        agent_fn=_simple_agent_fn,
        cloudwatch_client=cw,
    )

    # The run still completed successfully and wrote its artifact.
    assert result["status"] == "completed"
    assert result["artifacts_written"] == 1


def test_runner_none_cloudwatch_client_does_not_raise():
    """When cloudwatch_client is None, run() completes without error."""
    source = _FakeSource([{"vin": "VIN-METRICS-N001", "state_of_health_pct": "92"}])
    writer = _FakeWriter()

    result = _run(
        "staging",
        source=source,
        writer=writer,
        vins=["VIN-METRICS-N001"],
        component="battery",
        agent_fn=_simple_agent_fn,
        cloudwatch_client=None,
    )

    assert result["status"] == "completed"


def test_runner_emit_metrics_stage_dimension_matches():
    """The 'stage' dimension in emitted metrics matches the run's stage."""
    cw = _CapturingCWClient()
    source = _FakeSource([{"vin": "VIN-METRICS-S001", "state_of_health_pct": "88"}])
    writer = _FakeWriter()

    _run(
        "prod",
        source=source,
        writer=writer,
        vins=["VIN-METRICS-S001"],
        component="tires",
        agent_fn=_simple_agent_fn,
        cloudwatch_client=cw,
    )

    for datum in cw.calls[0]["MetricData"]:
        dim_map = {d["Name"]: d["Value"] for d in datum["Dimensions"]}
        assert dim_map.get("stage") == "prod", (
            f"Expected stage=prod in datum {datum['MetricName']}, got {dim_map}"
        )
        assert dim_map.get("component") == "tires"


def test_emit_metrics_owner_integrity_degraded_value():
    """Fix Group 21 W2: the datum carries the real value, not a placeholder.

    A name-only assertion would pass against a datum hardcoded to 0.0, which is
    the shape this counter is most likely to regress into — its default IS 0.
    """
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=0, artifacts_written=0, duplicates_skipped=0,
                 owner_integrity_degraded=3)
    datum = next(d for d in cw.calls[0]["MetricData"]
                 if d["MetricName"] == "OwnerIntegrityDegraded")
    assert datum["Value"] == 3.0
    assert datum["Unit"] == "Count"


def test_emit_metrics_owner_integrity_degraded_defaults_to_zero():
    """Omitting the kwarg must emit 0, not omit the datum — an absent datum
    cannot be alarmed on, which was the original defect."""
    cw = _CapturingCWClient()
    emit_metrics(cw, _STAGE, _COMPONENT,
                 vins_selected=1, artifacts_written=1, duplicates_skipped=0)
    datum = next(d for d in cw.calls[0]["MetricData"]
                 if d["MetricName"] == "OwnerIntegrityDegraded")
    assert datum["Value"] == 0.0




# ---------------------------------------------------------------------------
# Fix Group 23 W1 — the runner -> CloudWatch VALUE path, not just the name set
# ---------------------------------------------------------------------------
#
# Review cycle 3 found that dropping `owner_integrity_degraded=` from all four
# emit_metrics call sites in runner.py left the entire tier2 suite green: the
# name-set assertion does not change (the kwarg defaults to 0, so the datum is
# still emitted) and the two value-asserting tests call emit_metrics directly,
# bypassing the runner. So the threading was presence-guarded only.
#
# It also noted the sibling `avx_write_failures` has the identical gap — the
# "mirrors avx_write_failures exactly" claim was accurate, weakness included.
# Both are closed here, because fixing only the new one would leave the older
# and more load-bearing counter unguarded for the same reason.
#
# Shape copied from test_runner_emit_metrics_artifacts_written_count above, which
# is the pattern already established in this file for runner -> datum value.


def _run_with_flapping_owner(cw):
    """Drive one VIN whose owner resolution disagrees with itself.

    Produces exactly one owner-integrity degradation through the real runner
    path, so the datum's value has to travel runner -> emit_metrics -> CloudWatch.
    """
    calls: list[str] = []

    def _flapping(vin: str):
        calls.append(vin)
        return "CUST-004000DD" if len(calls) == 1 else "OWNER-WRONG"

    source = _FakeSource([{"vin": "VIN-METRICS-OID1", "state_of_health_pct": "72"}])
    _run(
        "staging",
        source=source,
        writer=_FakeWriter(always_new=True),
        vins=["VIN-METRICS-OID1"],
        component="battery",
        agent_fn=_simple_agent_fn,
        cloudwatch_client=cw,
        owner_resolver=_flapping,
    )


def test_runner_emit_metrics_owner_integrity_degraded_count():
    """The OwnerIntegrityDegraded datum reflects the count the run actually saw.

    Mutation (Fix Group 23 W1): delete `owner_integrity_degraded=record...` from
    runner.py's emit_metrics call sites -> this MUST fail with 0.0 != 1.0. Before
    this test, that mutation left 462 tests green.
    """
    cw = _CapturingCWClient()
    _run_with_flapping_owner(cw)

    datum = next(d for d in cw.calls[0]["MetricData"]
                 if d["MetricName"] == "OwnerIntegrityDegraded")
    assert datum["Value"] == 1.0, (
        f"Expected OwnerIntegrityDegraded=1.0 after one unstable owner resolution, "
        f"got {datum['Value']} — the runner is not threading the counter through"
    )


def test_runner_emit_metrics_owner_integrity_degraded_zero_on_clean_run():
    """Positive control: a stable resolver must report 0.0, not 1.0.

    Without this, a datum hardcoded to 1.0 would satisfy the test above.
    """
    cw = _CapturingCWClient()
    source = _FakeSource([{"vin": "VIN-METRICS-OID2", "state_of_health_pct": "72"}])
    _run(
        "staging",
        source=source,
        writer=_FakeWriter(always_new=True),
        vins=["VIN-METRICS-OID2"],
        component="battery",
        agent_fn=_simple_agent_fn,
        cloudwatch_client=cw,
        owner_resolver=lambda _vin: "CUST-004000DD",
    )
    datum = next(d for d in cw.calls[0]["MetricData"]
                 if d["MetricName"] == "OwnerIntegrityDegraded")
    assert datum["Value"] == 0.0


def test_runner_emit_metrics_avx_write_failures_count():
    """Closes the identical gap on the sibling counter (cycle 3 W1).

    `avx_write_failures` had the same presence-only guard: dropping its threading
    from runner.py was invisible. An AVX writer that always raises produces
    exactly one failure per VIN.

    Mutation: delete `avx_write_failures=record.avx_write_failures` from the
    emit_metrics call sites -> this MUST fail with 0.0 != 1.0.
    """
    class _AlwaysFailingAvxWriter:
        def put(self, artifact):  # pragma: no cover
            raise AssertionError("put() on the AVX writer")

        def put_raw(self, item):  # pragma: no cover
            raise AssertionError("put_raw() on the AVX writer")

        def write_finding(self, finding, *, utc_now_fn=None, standing=None):
            raise RuntimeError("simulated AVX write failure")

    cw = _CapturingCWClient()
    source = _FakeSource([{"vin": "VIN-METRICS-AWF1", "state_of_health_pct": "72"}])
    _run(
        "staging",
        source=source,
        writer=_FakeWriter(always_new=True),
        vins=["VIN-METRICS-AWF1"],
        component="battery",
        agent_fn=_simple_agent_fn,
        cloudwatch_client=cw,
        avx_writer=_AlwaysFailingAvxWriter(),
    )
    datum = next(d for d in cw.calls[0]["MetricData"]
                 if d["MetricName"] == "AvxWriteFailures")
    assert datum["Value"] == 1.0, (
        f"Expected AvxWriteFailures=1.0 after one failed AVX write, got {datum['Value']}"
    )
