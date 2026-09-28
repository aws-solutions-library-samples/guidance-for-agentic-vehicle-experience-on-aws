"""Tests for T16.2 / T17.1–T17.4 — AVX dual-write in runner.py.

Verifies:
- Legacy writer.put() is called for each VIN (legacy path unchanged).
- avx_writer.write_finding() is called after each legacy write.
- An AVX write failure does NOT abort the run; the legacy write still commits.
- An AVX write failure emits a distinct ``event=avx_write_failed`` log signal.
- An AVX write failure appears in the run summary as ``avx_write_failures`` count.
- An AVX write failure emits an ``AvxWriteFailures`` metric via RunObserver.
- The VIN hash in the log is stable across invocations (not Python hash()).
- Severing the production wiring (deleting ``avx_writer=avx_writer`` from
  lambda_handler) is detected by a test (M-A guard).
- registry.avx_findings_table_env_var is populated (not dead).
- run() with avx_writer=None (default) completes normally (backward compat).

Mutation record (required by testing.md § "Mutation testing at the green boundary"):
  M-A: delete avx_writer=avx_writer from lambda_handler → test_lambda_handler_wires_avx_writer FAILS.
  (a) AVX write raises → legacy write commits AND event=avx_write_failed emitted.
      Tested by: test_avx_write_failure_does_not_abort_run,
                 test_avx_write_failure_emits_observable_signal.
  (b) Signal swallowed (bare ``except: pass``) → test_avx_write_failure_emits_observable_signal FAILS.
  (c) Drop obs.record_avx_write_failed() call → test_avx_write_failure_appears_in_run_summary FAILS.
  (d) vin_hash with hash() → test_vin_hash_is_stable_across_invocations FAILS (subprocess run).

T17.1 fix: AGENT_VERSION="tier2-health-v0.1.0" is NOT patched in any test.
AVX_AGENT_VERSION="0.1.0" is derived at import time from AGENT_VERSION and satisfies the schema.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any, Optional

import pytest

import agents.tier2.runner as _runner_module
from agents.tier2.runner import run

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# NOTE: AGENT_VERSION is NOT patched in any test in this file.
# AVX_AGENT_VERSION is derived from AGENT_VERSION at runner import time.
# If the derivation fails (e.g. wrong format), the import itself raises —
# no test can hide that.  All tests in this file exercise the REAL constant.


# ---------------------------------------------------------------------------
# Test doubles (local — intentionally not shared from test_runner.py so
# this file is self-contained and the task's "new file" scope is clear)
# ---------------------------------------------------------------------------


def _make_rows(vin: str) -> list[dict]:
    return [
        {
            "vin": vin,
            "state_of_health_pct": "88.0",
            "battery_age_days": "800",
        }
    ]


def _make_agent_fields(vin: str) -> dict:
    return {
        "finding": f"Battery for {vin} looks fine.",
        "severity_hint": "routine",
        "confidence": "high",
        "evidence": [
            {"source": "adp.battery.soh", "value": {"soh": 88.0}, "provenance": "live"}
        ],
    }


class _FakeSource:
    """Source double that serves pre-loaded rows for a list of VINs."""

    def __init__(self, vins: list[str]) -> None:
        self._vins = vins

    def fetch_findings(self, vins: Optional[list[str]]) -> list[dict]:
        rows: list[dict] = []
        targets = vins if vins is not None else self._vins
        for v in targets:
            rows.extend(_make_rows(v))
        return rows

    def select_vins(self, *, cursor: Optional[str], limit: int):
        batch = self._vins[:limit]
        return batch, (batch[-1] if batch else None)


class _FakeLegacyWriter:
    """Legacy writer double — records put() calls, write_finding is a no-op stub."""

    def __init__(self) -> None:
        self.put_calls: list[dict] = []

    def put(self, artifact: dict) -> bool:
        self.put_calls.append(artifact)
        return True

    def put_raw(self, item: dict) -> None:
        pass  # cursor writes — not under test here

    def write_finding(
        self, finding: dict, *, utc_now_fn: Any = None, standing: Any = None
    ) -> dict:
        # The legacy writer should NOT be called via write_finding in the T16.2
        # path — that goes to avx_writer.  A call here is an ordering error.
        raise AssertionError(
            "write_finding() called on the legacy writer; "
            "AVX writes must go through avx_writer, not the legacy health-findings writer."
        )


class _FakeAvxWriter:
    """AVX writer double that records write_finding() calls."""

    def __init__(self) -> None:
        self.write_finding_calls: list[dict] = []
        # The StandingFacts forwarded alongside each Finding.  Recorded
        # separately because it is deliberately NOT a key on the Finding dict.
        self.write_finding_owner_ids: list[Any] = []

    def put(self, artifact: dict) -> bool:
        raise AssertionError("put() called on AVX writer; use write_finding()")

    def put_raw(self, item: dict) -> None:
        raise AssertionError("put_raw() called on AVX writer")

    def write_finding(
        self, finding: dict, *, utc_now_fn: Any = None, standing: Any = None
    ) -> dict:
        self.write_finding_calls.append(finding)
        self.write_finding_owner_ids.append(standing)
        return {"action": "put", "finding_id": finding.get("finding_id", "FIND-fake")}


class _FailingAvxWriter:
    """AVX writer double whose write_finding() always raises."""

    def put(self, artifact: dict) -> bool:
        raise AssertionError("put() called on failing AVX writer")

    def put_raw(self, item: dict) -> None:
        raise AssertionError("put_raw() called on failing AVX writer")

    def write_finding(
        self, finding: dict, *, utc_now_fn: Any = None, standing: Any = None
    ) -> dict:
        raise RuntimeError("Simulated AVX DynamoDB write failure")


# ---------------------------------------------------------------------------
# Happy path — exercises real AGENT_VERSION (no patching)
# ---------------------------------------------------------------------------


def test_avx_writer_write_finding_called_for_each_vin():
    """write_finding() is called on avx_writer once per VIN that completes.

    T17.1 fix: runs against the REAL AGENT_VERSION constant.  The transform
    now uses AVX_AGENT_VERSION (derived plain semver) so schema validation
    passes without patching.
    """
    vins = ["VIN-AVX-001", "VIN-AVX-002"]
    source = _FakeSource(vins)
    legacy_writer = _FakeLegacyWriter()
    avx_writer = _FakeAvxWriter()

    result = run(
        "staging",
        source=source,
        writer=legacy_writer,
        vins=vins,
        agent_fn=lambda v, rows: _make_agent_fields(v),
        avx_writer=avx_writer,
    )

    assert result["status"] == "completed"
    # Legacy path: one put() per VIN.
    assert len(legacy_writer.put_calls) == 2
    # AVX path: one write_finding() per VIN — after the legacy write.
    assert len(avx_writer.write_finding_calls) == 2, (
        f"Expected 2 AVX write_finding() calls, got {len(avx_writer.write_finding_calls)}. "
        "If this is 0, the schema validator is rejecting the Finding — check AVX_AGENT_VERSION."
    )


def test_avx_write_finding_count_is_nonzero_with_real_agent_version():
    """Dual-write against the REAL AGENT_VERSION writes at least one AVX Finding.

    This is the regression guard for Critical C1 (review-group16-cycle1.md):
    before T17.1, the real AGENT_VERSION caused schema validation to raise inside
    health_artifact_to_finding(), the except block swallowed it, and zero Findings
    were written.

    M-A proxy: if this test passes, the transform is reaching write_finding().
    Combine with test_lambda_handler_wires_avx_writer for the full M-A guard.
    """
    vins = ["VIN-REAL-VERSION-001"]
    source = _FakeSource(vins)
    legacy_writer = _FakeLegacyWriter()
    avx_writer = _FakeAvxWriter()

    result = run(
        "staging",
        source=source,
        writer=legacy_writer,
        vins=vins,
        agent_fn=lambda v, rows: _make_agent_fields(v),
        avx_writer=avx_writer,
    )

    assert result["status"] == "completed"
    assert len(avx_writer.write_finding_calls) > 0, (
        "Zero AVX Findings written against the REAL AGENT_VERSION. "
        "This is the Critical C1 defect: schema validation raises on "
        f"AGENT_VERSION={_runner_module.AGENT_VERSION!r} but AVX_AGENT_VERSION "
        f"should be {_runner_module.AVX_AGENT_VERSION!r}. "
        "Check that finding_transform.py uses AVX_AGENT_VERSION, not artifact['agent_version']."
    )


def test_legacy_write_still_commits_when_avx_writer_is_none():
    """avx_writer=None (default) leaves legacy path fully intact."""
    vins = ["VIN-NOAVX-001"]
    source = _FakeSource(vins)
    legacy_writer = _FakeLegacyWriter()

    result = run(
        "staging",
        source=source,
        writer=legacy_writer,
        vins=vins,
        agent_fn=lambda v, rows: _make_agent_fields(v),
        # avx_writer omitted — defaults to None
    )

    assert result["status"] == "completed"
    assert len(legacy_writer.put_calls) == 1


def test_avx_write_finding_receives_schema_valid_finding():
    """The dict passed to avx_writer.write_finding() satisfies the AVX Finding schema."""
    from jsonschema import Draft202012Validator
    import json
    from pathlib import Path

    schema_path = (
        Path(__file__).resolve().parent.parent / "schemas" / "finding.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    vins = ["VIN-SCHEMA-001"]
    source = _FakeSource(vins)
    legacy_writer = _FakeLegacyWriter()
    avx_writer = _FakeAvxWriter()

    run(
        "staging",
        source=source,
        writer=legacy_writer,
        vins=vins,
        agent_fn=lambda v, rows: _make_agent_fields(v),
        avx_writer=avx_writer,
    )

    assert avx_writer.write_finding_calls, "No AVX Finding written"
    finding = avx_writer.write_finding_calls[0]
    errors = list(validator.iter_errors(finding))
    assert not errors, f"AVX Finding failed schema validation: {[e.message for e in errors]}"


def test_avx_write_ordering_legacy_before_avx():
    """Legacy write always precedes the AVX write (spec.md:512 ordering constraint)."""
    call_order: list[str] = []

    class _OrderedLegacyWriter:
        def put(self, artifact: dict) -> bool:
            call_order.append("legacy_put")
            return True

        def put_raw(self, item: dict) -> None:
            pass

        def write_finding(
        self, finding: dict, *, utc_now_fn: Any = None, standing: Any = None
    ) -> dict:
            raise AssertionError("write_finding on legacy writer")

    class _OrderedAvxWriter:
        def write_finding(
        self, finding: dict, *, utc_now_fn: Any = None, standing: Any = None
    ) -> dict:
            call_order.append("avx_write_finding")
            return {"action": "put", "finding_id": "FIND-ordered"}

        def put(self, a: dict) -> bool:
            raise AssertionError

        def put_raw(self, a: dict) -> None:
            raise AssertionError

    vins = ["VIN-ORDER-001"]
    source = _FakeSource(vins)

    run(
        "staging",
        source=source,
        writer=_OrderedLegacyWriter(),
        vins=vins,
        agent_fn=lambda v, rows: _make_agent_fields(v),
        avx_writer=_OrderedAvxWriter(),
    )

    assert call_order == ["legacy_put", "avx_write_finding"], (
        f"Write ordering violated; expected ['legacy_put', 'avx_write_finding'], got {call_order}"
    )


# ---------------------------------------------------------------------------
# T17.2 — Production wiring assertion (M-A guard)
# ---------------------------------------------------------------------------


def test_lambda_handler_wires_avx_writer():
    """M-A guard: lambda_handler must pass avx_writer=avx_writer to run().

    Mutation M-A: delete ``avx_writer=avx_writer,`` from the run() call in
    lambda_handler → this test MUST fail.

    Before T17.2, deleting that kwarg left 435/435 tests green because avx_writer
    defaulted to None and the None path was a silent skip.

    Implementation: inspect the runner AST to confirm that lambda_handler's run()
    call explicitly passes avx_writer.  Combined with test_avx_write_finding_count_is_nonzero_with_real_agent_version,
    this closes the wiring gap end-to-end.
    """
    import ast
    from pathlib import Path

    runner_src = (Path(__file__).parent.parent / "runner.py").read_text()
    tree = ast.parse(runner_src)

    # Find the lambda_handler function definition, then look for a Call to run()
    # that passes avx_writer as a keyword argument.
    found_wiring = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "lambda_handler"):
            continue
        # Walk inside lambda_handler only.
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            # Match: run(...) or any call named 'run'
            func = inner.func
            call_name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if call_name != "run":
                continue
            # Check that 'avx_writer' appears as a keyword argument.
            for kw in inner.keywords:
                if kw.arg == "avx_writer":
                    found_wiring = True
                    break

    assert found_wiring, (
        "lambda_handler does not pass avx_writer=... to run().  "
        "Mutation M-A: deleting this kwarg makes avx_writer default to None "
        "(silent skip) and writes ZERO AVX Findings.  "
        "The D2 ratchet cannot catch this — it counts call sites, not wiring.  "
        "Restore avx_writer=avx_writer (or avx_writer=<non-None>) in lambda_handler's run() call."
    )


# ---------------------------------------------------------------------------
# D2 guard docstring correction note
# ---------------------------------------------------------------------------

def test_d2_guard_counts_call_sites_not_wiring():
    """Document that the D2 guard counts SOURCE call sites, not live wiring.

    The test in test_d2_writer_wired_once_table_exists.py counts AST Call nodes
    for write_finding() in production files.  A call site in runner.py counts
    even when avx_writer=None makes it unreachable — i.e., the guard passes
    even when zero Findings are written.

    This test does NOT change the D2 guard; it provides the missing property
    assertion (test_lambda_handler_wires_avx_writer above) and documents the
    trade-off so a future reader does not expect the D2 guard to catch M-A.
    """
    # The D2 guard is in tests/architecture/test_d2_writer_wired_once_table_exists.py.
    # Its docstring now correctly states it counts source call sites.
    # No assertion needed here — this test is documentation.
    assert True, "See test_lambda_handler_wires_avx_writer for the missing property."


# ---------------------------------------------------------------------------
# Mutation (a): AVX write raises → legacy commits AND signal emitted
# ---------------------------------------------------------------------------


def test_avx_write_failure_does_not_abort_run():
    """Mutation (a): even when avx_writer.write_finding() raises, the run completes.

    The legacy write (writer.put()) must have committed BEFORE the AVX write
    attempt, and the run must return status='completed' with artifacts_written >= 1.
    """
    vins = ["VIN-FAIL-001"]
    source = _FakeSource(vins)
    legacy_writer = _FakeLegacyWriter()
    failing_avx_writer = _FailingAvxWriter()

    result = run(
        "staging",
        source=source,
        writer=legacy_writer,
        vins=vins,
        agent_fn=lambda v, rows: _make_agent_fields(v),
        avx_writer=failing_avx_writer,
    )

    # Run must complete normally — not abort due to AVX failure.
    assert result["status"] == "completed", (
        f"Run aborted; AVX failure must not propagate. Got: {result}"
    )
    # Legacy write must have committed.
    assert len(legacy_writer.put_calls) == 1, (
        f"Legacy write did not commit; expected 1 put() call, got {len(legacy_writer.put_calls)}"
    )


def test_avx_write_failure_emits_observable_signal(caplog):
    """Mutation (b) guard: an AVX write failure MUST emit a distinct log signal.

    This test is the load-bearing mutation guard for the swallowed-exception path.
    If the ``except`` block is changed to a bare ``pass``, this test fails.
    """
    vins = ["VIN-SIGNAL-001"]
    source = _FakeSource(vins)
    legacy_writer = _FakeLegacyWriter()
    failing_avx_writer = _FailingAvxWriter()

    with caplog.at_level(logging.WARNING, logger="agents.tier2.runner"):
        run(
            "staging",
            source=source,
            writer=legacy_writer,
            vins=vins,
            agent_fn=lambda v, rows: _make_agent_fields(v),
            avx_writer=failing_avx_writer,
        )

    # The distinct, countable signal must be present.
    avx_fail_records = [
        r for r in caplog.records if "event=avx_write_failed" in r.getMessage()
    ]
    assert avx_fail_records, (
        "No 'event=avx_write_failed' log record found after AVX write failure.  "
        "Mutation (b) guard: a swallowed AVX write produces a silent nightly failure "
        "indistinguishable from a quiet night (VFO failure mode).  "
        "Restore the log.warning() call with 'event=avx_write_failed' in runner.py."
    )


def test_avx_write_failure_appears_in_run_summary(caplog):
    """T17.4 W1 guard (mutation c): AVX failures must appear in the run summary.

    Mutation (c): drop obs.record_avx_write_failed() → avx_write_failures stays 0
    in the summary and this test fails.

    100% AVX failure is invisible in every aggregate view if only a log line is
    emitted — it must also appear in the structured tier2_run summary dict so an
    operator can filter or alert on it.
    """
    import io
    from agents.tier2.observability import RunObserver

    vins = ["VIN-METRIC-001", "VIN-METRIC-002"]
    source = _FakeSource(vins)
    legacy_writer = _FakeLegacyWriter()
    failing_avx_writer = _FailingAvxWriter()

    buf = io.StringIO()
    obs_output: list[dict] = []

    # Intercept the run summary by checking the returned summary dict.
    result = run(
        "staging",
        source=source,
        writer=legacy_writer,
        vins=vins,
        agent_fn=lambda v, rows: _make_agent_fields(v),
        avx_writer=failing_avx_writer,
    )

    # The run summary must carry avx_write_failures.
    assert "avx_write_failures" in result, (
        f"Run summary is missing 'avx_write_failures' key. Got keys: {sorted(result.keys())}. "
        "T17.4 W1: AVX failures must appear in the structured summary, not only in raw logs."
    )
    assert result["avx_write_failures"] == len(vins), (
        f"Expected avx_write_failures={len(vins)}, got {result.get('avx_write_failures')}. "
        "Each failing AVX write must increment the counter."
    )


def test_avx_write_failure_emits_cloudwatch_metric():
    """T17.4 W1 guard: an AvxWriteFailures datum must be PRESENT in the payload.

    Scope, stated precisely because the original docstring overstated it: this
    asserts the datum's **presence**, not its value.

    The original claim was "Mutation: drop avx_write_failures from the
    emit_metrics() call ... -> this test fails." That is false. The kwarg defaults
    to 0, so dropping the threading still emits an ``AvxWriteFailures`` datum with
    value 0.0 — the name set is unchanged and this test stays green. Review cycle 3
    of Fix Group 15 proved it by mutation.

    The **value** is guarded by
    ``test_observability_metrics.py::test_runner_emit_metrics_avx_write_failures_count``,
    added in Fix Group 23 for exactly that gap. Dropping the threading fails
    there, not here.
    """
    class _FakeCloudWatch:
        def __init__(self):
            self.calls: list[dict] = []

        def put_metric_data(self, **kwargs):
            self.calls.append(kwargs)

    vins = ["VIN-CW-001"]
    source = _FakeSource(vins)
    legacy_writer = _FakeLegacyWriter()
    failing_avx_writer = _FailingAvxWriter()
    cw = _FakeCloudWatch()

    run(
        "staging",
        source=source,
        writer=legacy_writer,
        vins=vins,
        agent_fn=lambda v, rows: _make_agent_fields(v),
        avx_writer=failing_avx_writer,
        cloudwatch_client=cw,
    )

    assert cw.calls, "emit_metrics() never called PutMetricData"
    metric_names = {
        d["MetricName"]
        for call in cw.calls
        for d in call.get("MetricData", [])
    }
    assert "AvxWriteFailures" in metric_names, (
        f"AvxWriteFailures metric not emitted.  Metrics seen: {sorted(metric_names)}. "
        "T17.4 W1: AVX failures must appear in the PutMetricData payload."
    )


def test_avx_write_failure_with_multiple_vins_all_legacy_commits(caplog):
    """AVX failure on one VIN does not prevent legacy commits for subsequent VINs."""
    vins = ["VIN-MULTI-001", "VIN-MULTI-002", "VIN-MULTI-003"]
    source = _FakeSource(vins)
    legacy_writer = _FakeLegacyWriter()
    failing_avx_writer = _FailingAvxWriter()

    with caplog.at_level(logging.WARNING, logger="agents.tier2.runner"):
        result = run(
            "staging",
            source=source,
            writer=legacy_writer,
            vins=vins,
            agent_fn=lambda v, rows: _make_agent_fields(v),
            avx_writer=failing_avx_writer,
        )

    assert result["status"] == "completed"
    # All 3 legacy writes committed despite per-VIN AVX failures.
    assert len(legacy_writer.put_calls) == 3
    # One signal per failing VIN.
    avx_fail_records = [
        r for r in caplog.records if "event=avx_write_failed" in r.getMessage()
    ]
    assert len(avx_fail_records) == 3, (
        f"Expected 3 avx_write_failed signals (one per VIN), got {len(avx_fail_records)}"
    )


# ---------------------------------------------------------------------------
# T17.4 W2 — VIN hash stability across invocations
# ---------------------------------------------------------------------------


def test_vin_hash_is_stable_across_invocations(caplog, tmp_path):
    """T17.4 W2 guard: the VIN hash in log records is stable across interpreter runs.

    Python's built-in hash() is per-process randomised (PYTHONHASHSEED).  Two
    Lambda invocations for the same VIN would produce different fragments, making
    the hash useless for correlation.  The fix uses hashlib.sha256 which is stable.

    Mutation (d): revert vin_hash to ``hash(vin) & 0xFFFF`` → two subprocess
    invocations produce different values and this test fails.
    """
    # Write a small helper script to a temp file so we can run it twice.
    script = tmp_path / "extract_vin_hash.py"
    script.write_text(
        """\
import logging
import sys

# Capture log output
logging.basicConfig(level=logging.WARNING, stream=sys.stdout,
                    format="%(message)s")

from agents.tier2.runner import run

class _Src:
    def fetch_findings(self, vins):
        return [{"vin": v, "state_of_health_pct": "88.0", "battery_age_days": "800"}
                for v in (vins or [])]
    def select_vins(self, *, cursor, limit):
        return [], None

class _Legacy:
    def put(self, a): return True
    def put_raw(self, a): pass
    def write_finding(self, f, **kw): raise AssertionError("legacy write_finding")

class _FailAvx:
    def put(self, a): raise AssertionError
    def put_raw(self, a): raise AssertionError
    def write_finding(self, f, **kw): raise RuntimeError("fail")

run("staging", source=_Src(), writer=_Legacy(), vins=["STABLEVINHASH999"],
    agent_fn=lambda v, r: {"finding": "ok", "severity_hint": "routine",
                           "confidence": "low", "evidence": []},
    avx_writer=_FailAvx())
""",
        encoding="utf-8",
    )

    def _run_and_extract_hash() -> str:
        out = subprocess.check_output(
            [sys.executable, str(script)],
            cwd=str(script.parent),
            env={**__import__("os").environ, "PYTHONPATH": str(
                __import__("pathlib").Path(__file__).resolve().parents[3]
            )},
            stderr=subprocess.STDOUT,
        ).decode()
        # Find line with event=avx_write_failed and extract vin_hash=<value>
        for line in out.splitlines():
            if "event=avx_write_failed" in line and "vin_hash=" in line:
                # Parse vin_hash=<value> from the log line
                for part in line.split():
                    if part.startswith("vin_hash="):
                        return part.split("=", 1)[1]
        raise AssertionError(f"vin_hash not found in output:\n{out}")

    hash1 = _run_and_extract_hash()
    hash2 = _run_and_extract_hash()

    assert hash1 == hash2, (
        f"VIN hash is unstable across invocations: {hash1!r} vs {hash2!r}. "
        "Mutation (d): Python's hash() is per-process randomised (PYTHONHASHSEED). "
        "Use hashlib.sha256(vin.encode()).hexdigest()[:8] for a stable correlation key."
    )
    # Also confirm it's the sha256 prefix (8 hex chars).
    import re
    assert re.match(r"^[0-9a-f]{8}$", hash1), (
        f"vin_hash {hash1!r} is not 8 hex chars; expected sha256 prefix."
    )


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registry_avx_findings_table_env_var_is_populated():
    """registry.avx_findings_table_env_var is declared and non-empty."""
    from agents.tier2.registry import AGENT_MODULES

    module = AGENT_MODULES["agent-2-vehicle-health"]
    assert module.avx_findings_table_env_var, (
        "avx_findings_table_env_var is empty/None on the agent-2-vehicle-health "
        "registry entry.  It must be set to 'AVX_FINDINGS_TABLE'."
    )
    assert module.avx_findings_table_env_var == "AVX_FINDINGS_TABLE", (
        f"Expected 'AVX_FINDINGS_TABLE', got {module.avx_findings_table_env_var!r}"
    )


# REMOVED in Fix Group 17 / review Cycle 2: two tests that asserted runtime checks
# which no longer exist, and which Cycle 1/2 identified as weak in their own right.
#
#   test_registry_artifact_table_env_var_now_has_consumer — walked the whole module
#     for an `ast.Assert` node of a given shape (Cycle 1 W4). Unscoped: it would pass
#     with the assert in dead code and fail on a correct refactor into a helper.
#   test_avx_drift_check_runs_after_legacy_writer_construction — asserted AST LINE
#     ORDER, which Cycle 2 showed proved nothing: the check being below the legacy
#     writer's construction did not stop it aborting the run.
#
# Superseded by test_registry_env_var_names_match_runner_literals (binds each registry
# field to a literal runner.py actually reads) and
# test_registry_env_var_drift_is_not_enforced_at_runtime (pins the W3 decision), both
# at the end of this file.

# ─────────────────────────────────────────────────────────────────────────────
# T17.2 addendum (architect, review Cycle 1 C2) — BEHAVIOURAL M-A guard.
#
# `test_lambda_handler_wires_avx_writer` above asserts the wiring by walking
# runner.py's AST. That catches mutation M-A (deleting `avx_writer=avx_writer`),
# but it is a PRESENCE check on source structure, and T17.2's Accept asked for
# something else: "a test that invokes `lambda_handler` (not `run()` directly)
# ... and asserts an AVX Finding write actually occurred".
#
# The distinction is not pedantic. An AST check passes if the kwarg sits in
# unreachable code, and fails if the call is legitimately refactored to
# `run(**kwargs)` or to positional args — so it can be both falsely green and
# falsely red. It is exactly the shape review Cycle 1 criticised in W4, where
# `test_registry_artifact_table_env_var_now_has_consumer` asserts only that an
# `ast.Assert` node exists somewhere in the module.
#
# This test closes the property behaviourally: it invokes the real
# `lambda_handler`, substitutes doubles at the `_production_dependencies` seam,
# and asserts the AVX double actually RECEIVED a write. Deleting the kwarg makes
# the double receive nothing, regardless of how the call is spelled.
# ─────────────────────────────────────────────────────────────────────────────


def test_lambda_handler_actually_writes_through_the_avx_writer(monkeypatch):
    """Invoke lambda_handler for real; assert the AVX writer received a Finding.

    Mutation M-A (delete ``avx_writer=avx_writer`` from lambda_handler's run()
    call) must fail this test by leaving ``avx.written`` empty — and it fails on
    observed behaviour, not on the shape of the source.
    """
    source = _FakeSource(["VINBEHAVIOUR0001"])
    legacy = _FakeLegacyWriter()
    avx = _FakeAvxWriter()

    # Substitute the three production dependencies at their single seam. This is
    # the same tuple lambda_handler unpacks, so the handler's own wiring of
    # avx_writer into run() is what remains under test.
    monkeypatch.setattr(
        _runner_module,
        "_production_dependencies",
        lambda *a, **k: (source, legacy, avx),
    )
    # The handler builds a real CloudWatch client; neutralise AWS access without
    # touching the dual-write path. `boto3` is imported INSIDE lambda_handler, not
    # at module scope, so the patch target is the boto3 module rather than an
    # attribute of runner.
    monkeypatch.setattr("boto3.client", lambda *a, **k: _NoopCloudWatch())
    monkeypatch.setattr(
        _runner_module, "_make_production_cursor_store", lambda _w: None
    )
    # `_default_agent_fn()` returns the judgment callable, whose signature is
    # (vin, rows) -> artifact_fields. The file's helper takes vin only.
    monkeypatch.setattr(
        _runner_module,
        "_default_agent_fn",
        lambda: (lambda vin, _rows: _make_agent_fields(vin)),
    )

    result = _runner_module.lambda_handler(
        {"stage": "staging", "component": "battery", "vins": ["VINBEHAVIOUR0001"]},
        None,
    )

    assert legacy.put_calls, (
        "the legacy writer received nothing — this test's doubles are not reaching "
        "the write path, so its M-A assertion below would be vacuous"
    )
    assert avx.write_finding_calls, (
        "lambda_handler did not write through the AVX writer. Either "
        "`avx_writer=avx_writer` is missing from its run() call (mutation M-A), or "
        "the AVX write raised and was swallowed by the except block. Check "
        f"avx_write_failures in the summary: {result!r}"
    )


class _NoopCloudWatch:
    """Minimal stand-in for the CloudWatch client lambda_handler constructs."""

    def put_metric_data(self, **_kwargs):  # pragma: no cover - trivial
        return {}



# ─────────────────────────────────────────────────────────────────────────────
# Fix Group 17 / review Cycle 2 (W3) — registry-vs-literal drift, enforced here
# instead of at runtime.
#
# Two runtime checks previously lived in `_production_dependencies`: an `assert`
# for the legacy table and a `raise RuntimeError` for the AVX table. Cycle 2 showed
# neither could work:
#   - the AVX `raise` propagates out of `lambda_handler` before `run()` is called,
#     so an AVX-only drift killed the legacy nightly health run — exactly what
#     T16.2's Constraints forbid. Moving it below the legacy writer's CONSTRUCTION
#     did not help, because constructing a writer is not writing with it.
#   - `assert` is stripped under `python -O`, so that guard was enforced by a
#     statement the interpreter is permitted to delete.
#   - degrading to `avx_writer=None` was the only other runtime option, and that
#     silences the AVX write, which is the invisible-failure mode this whole group
#     exists to prevent.
#
# The invariant is a DEVELOPER one — a registry field disagreeing with a literal in
# runner.py cannot arise from data or environment, only from an edit — so a test
# that fails in CI before the code ships is the one placement with a good outcome.
# ─────────────────────────────────────────────────────────────────────────────


def test_registry_env_var_names_match_runner_literals():
    """Each registry env-var field must equal a literal runner.py actually reads.

    Binds the two together rather than asserting known strings: the test reads
    `runner.py`'s source and requires an `os.environ.get("<value>"` for each registry
    field's value. Asserting only `field == "HEALTH_FINDINGS_TABLE"` would keep
    passing if runner.py stopped reading that name, which is the drift that matters.
    """
    import pathlib
    import re

    from agents.tier2.registry import AGENT_MODULES

    module = AGENT_MODULES["agent-2-vehicle-health"]
    source = pathlib.Path(_runner_module.__file__).read_text(encoding="utf-8")

    for field_name in ("artifact_table_env_var", "avx_findings_table_env_var"):
        env_var = getattr(module, field_name)
        assert env_var, f"registry.{field_name} is empty"
        # The literal must appear inside an os.environ.get(...) call in runner.py.
        pattern = re.compile(
            r"os\.environ\.get\(\s*[\"']" + re.escape(env_var) + r"[\"']"
        )
        assert pattern.search(source), (
            f"registry.{field_name} is {env_var!r}, but runner.py contains no "
            f"os.environ.get(\"{env_var}\", ...) call. Either the registry field "
            "drifted, or runner.py stopped reading that env var. Both break the "
            "binding the CDK env-contract test relies on — the stack sets the name "
            "the registry declares, and runner.py must be the thing that reads it."
        )


def test_registry_env_var_drift_is_not_enforced_at_runtime(monkeypatch):
    """A drifted registry value must NOT stop `_production_dependencies` returning.

    BEHAVIOURAL, deliberately. The first version of this test grepped the source for
    `"raise RuntimeError("`, and review Cycle 3 proved that shape check three ways:
    an `assert` passed it (restoring the real hazard, since `AssertionError` also
    propagates and `assert` is the form the file previously used), `raise ValueError`
    passed it, and an unrelated legitimate `raise RuntimeError` elsewhere in the
    function tripped it as a false positive. That is the same defect class as the
    test it replaced — which had been deleted precisely for being a shape check.

    So this asserts the property instead: with the registry drifted, calling
    `_production_dependencies` returns three usable dependencies rather than raising.
    Any exception spelling fails this test, and no exception spelling can pass it.
    """
    import agents.tier2.registry as _registry

    module = _registry.AGENT_MODULES["agent-2-vehicle-health"]
    original = module.avx_findings_table_env_var

    # Neutralise AWS client construction; `boto3` is imported inside the function.
    monkeypatch.setattr("boto3.client", lambda *a, **k: object())
    monkeypatch.setenv("HEALTH_FINDINGS_TABLE", "vsa-staging-health-findings")
    monkeypatch.setenv("AVX_FINDINGS_TABLE", "vsa-staging-avx-findings")

    try:
        module.avx_findings_table_env_var = "DRIFTED_ENV_NAME"
        try:
            source, legacy, avx = _runner_module._production_dependencies(
                "staging", "battery", max_vins=1, max_rows=10
            )
        except Exception as exc:  # noqa: BLE001 — any spelling is the failure
            raise AssertionError(
                "_production_dependencies raised on a drifted registry value "
                f"({type(exc).__name__}: {exc}). That aborts lambda_handler before "
                "run() executes, so an AVX-only drift kills the legacy nightly "
                "health run — the outcome T16.2's Constraints forbid and Cycle 2 "
                "flagged as W3. Enforce the invariant in "
                "test_registry_env_var_names_match_runner_literals instead, which "
                "fails in CI before the code ships."
            ) from exc

        assert legacy is not None, (
            "the legacy writer must still be constructed and returned when the AVX "
            "registry field has drifted — that is the whole property W3 is about"
        )
        assert avx is not None, (
            "_production_dependencies degraded avx_writer to None on a drifted "
            "registry value. That is the SECOND of the three hazard mechanisms "
            "decisions.md rules out: it silences the AVX write, which is the "
            "invisible-failure mode this whole group exists to prevent — the stream "
            "stops firing, the broker goes quiet, and readers serve a receding "
            "computed_at. Drift is a developer error; enforce it in "
            "test_registry_env_var_names_match_runner_literals, which fails in CI "
            "before the code ships, and leave BOTH writers intact at runtime."
        )
    finally:
        module.avx_findings_table_env_var = original

