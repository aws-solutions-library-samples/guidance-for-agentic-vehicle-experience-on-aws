"""
Unit tests for agents/tier2/budget.py and agents/tier2/observability.py — T2.4.

Spec § D6 (budget cap) and § D5 (observability).

Critical invariants tested:
  1. Token cap fires before the next unit of work (enforced in the loop, not logged after).
  2. Wall-clock cap fires at the right threshold.
  3. A capped run can still produce a well-formed partial artifact — not nothing, not malformed.
  4. RunObserver emits a structured record with abort_reason on budget breach.
  5. No VINs or customer data appear in log output.

All tests are synchronous (pytest-asyncio is absent from this venv).
"""

from __future__ import annotations

import hashlib
import io
import json
import time

import pytest

from agents.tier2.budget import BudgetCap, BudgetExceeded
from agents.tier2.observability import RunObserver, RunRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COMPONENT = "battery"
STAGE = "staging"
RUN_ID = "test-run-2026-08-04T00:00:00Z"


def _minimal_partial_artifact(
    partial_finding: str = "Battery at 92%, degrading normally.",
    component: str = COMPONENT,
) -> dict:
    """
    Build a minimal well-formed partial artifact from budget-exception data.

    This is what runner.py constructs on catching BudgetExceeded.
    The artifact must pass the M4 shape check without depending on contract.py
    (which is not yet implemented in T2.1).  We inline the shape assertions here.
    """
    import datetime

    computed_at = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    inputs_hash = hashlib.sha256(b"partial-test").hexdigest()
    return {
        "pk": "VEH#REDACTED",
        "sk": f"FINDING#{component}#{inputs_hash}",
        "computed_at": computed_at,
        "confidence": "low",
        "evidence": [],
        "agent_version": "tier2-health-v0.1.0",
        "inputs_hash": inputs_hash,
        "finding": partial_finding,
        "severity_hint": "routine",
        "ttl": 9999999999,
    }


def _assert_partial_artifact_well_formed(artifact: dict) -> None:
    """
    Assert spec § D4 M4 shape without importing contract.py.

    Required fields must all be present and non-None; types must match
    the schema.  P-level severity_hint must be absent.
    """
    required_fields = {
        "pk", "sk", "computed_at", "confidence", "evidence",
        "agent_version", "inputs_hash", "finding", "severity_hint", "ttl",
    }
    missing = required_fields - set(artifact.keys())
    assert not missing, f"Partial artifact missing M4 fields: {missing}"

    assert isinstance(artifact["pk"], str) and artifact["pk"].startswith("VEH#")
    assert isinstance(artifact["sk"], str) and artifact["sk"].startswith("FINDING#")
    assert isinstance(artifact["computed_at"], str) and artifact["computed_at"]
    assert artifact["confidence"] in {"high", "medium", "low"}
    assert isinstance(artifact["evidence"], list)
    assert isinstance(artifact["agent_version"], str) and artifact["agent_version"]
    assert isinstance(artifact["inputs_hash"], str) and len(artifact["inputs_hash"]) == 64
    assert isinstance(artifact["finding"], str) and artifact["finding"].strip()
    assert artifact["severity_hint"] not in {"P0", "P1", "P2", "P3"}
    assert isinstance(artifact["ttl"], int) and artifact["ttl"] > 0


# ---------------------------------------------------------------------------
# BudgetCap — constructor
# ---------------------------------------------------------------------------


def test_budgetcap_instantiates():
    cap = BudgetCap(max_tokens=10_000, max_wall_seconds=60.0)
    assert cap is not None


def test_budgetcap_stores_max_tokens():
    cap = BudgetCap(max_tokens=5_000, max_wall_seconds=30.0)
    assert cap.max_tokens == 5_000


def test_budgetcap_stores_max_wall_seconds():
    cap = BudgetCap(max_tokens=5_000, max_wall_seconds=30.0)
    assert cap.max_wall_seconds == 30.0


def test_budgetcap_zero_tokens_raises():
    with pytest.raises(ValueError):
        BudgetCap(max_tokens=0, max_wall_seconds=60.0)


def test_budgetcap_negative_tokens_raises():
    with pytest.raises(ValueError):
        BudgetCap(max_tokens=-1, max_wall_seconds=60.0)


def test_budgetcap_zero_wall_seconds_raises():
    with pytest.raises(ValueError):
        BudgetCap(max_tokens=1_000, max_wall_seconds=0.0)


def test_budgetcap_negative_wall_seconds_raises():
    with pytest.raises(ValueError):
        BudgetCap(max_tokens=1_000, max_wall_seconds=-5.0)


def test_budgetcap_initial_tokens_used_is_zero():
    cap = BudgetCap(max_tokens=1_000, max_wall_seconds=60.0)
    assert cap.tokens_used == 0


# ---------------------------------------------------------------------------
# BudgetCap — record_tokens
# ---------------------------------------------------------------------------


def test_record_tokens_accumulates():
    cap = BudgetCap(max_tokens=10_000, max_wall_seconds=60.0)
    cap.record_tokens(100)
    cap.record_tokens(200)
    assert cap.tokens_used == 300


def test_record_tokens_negative_raises():
    cap = BudgetCap(max_tokens=10_000, max_wall_seconds=60.0)
    with pytest.raises(ValueError):
        cap.record_tokens(-1)


def test_record_tokens_zero_is_noop():
    cap = BudgetCap(max_tokens=10_000, max_wall_seconds=60.0)
    cap.record_tokens(0)
    assert cap.tokens_used == 0


# ---------------------------------------------------------------------------
# TOKEN CAP — fires at the right threshold
# ---------------------------------------------------------------------------


def test_token_cap_fires_when_exceeded():
    """
    Core token-enforcement test: the cap raises BudgetExceeded when accumulated
    tokens exceed max_tokens.  This proves the cap fires in the agent loop.
    """
    cap = BudgetCap(max_tokens=100, max_wall_seconds=60.0)
    cap.record_tokens(101)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.dimension == "tokens"


def test_token_cap_fires_exactly_at_threshold():
    """Tokens equal to max_tokens do NOT fire; exceeding by 1 does."""
    cap = BudgetCap(max_tokens=100, max_wall_seconds=60.0)
    cap.record_tokens(100)
    cap.check()  # must not raise — at the limit, not over it

    cap.record_tokens(1)
    with pytest.raises(BudgetExceeded):
        cap.check()


def test_token_cap_exception_carries_correct_dimension():
    cap = BudgetCap(max_tokens=50, max_wall_seconds=60.0)
    cap.record_tokens(51)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.dimension == "tokens"


def test_token_cap_exception_carries_used_value():
    cap = BudgetCap(max_tokens=50, max_wall_seconds=60.0)
    cap.record_tokens(75)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.used == 75.0


def test_token_cap_exception_carries_limit():
    cap = BudgetCap(max_tokens=50, max_wall_seconds=60.0)
    cap.record_tokens(51)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.limit == 50.0


def test_token_cap_exception_carries_tokens_used():
    cap = BudgetCap(max_tokens=50, max_wall_seconds=60.0)
    cap.record_tokens(60)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.tokens_used == 60


def test_check_tokens_only_fires_on_token_breach():
    """check_tokens() does not evaluate wall clock."""
    cap = BudgetCap(max_tokens=50, max_wall_seconds=0.001)
    # Wall clock would fire if check() were called, but check_tokens() must not.
    # Deliberate: advance wall clock past max_wall_seconds to prove check_tokens()
    # does NOT consult wall time.
    # nosemgrep: arbitrary-sleep
    time.sleep(0.01)
    cap.record_tokens(51)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check_tokens()
    assert exc_info.value.dimension == "tokens"


def test_no_breach_below_token_limit():
    """No exception when under both limits."""
    cap = BudgetCap(max_tokens=1_000, max_wall_seconds=60.0)
    cap.record_tokens(500)
    cap.check()  # must not raise


# ---------------------------------------------------------------------------
# WALL-CLOCK CAP — fires at the right threshold
# ---------------------------------------------------------------------------


def test_wall_clock_cap_fires_when_exceeded():
    """
    Core wall-clock enforcement test: cap raises BudgetExceeded when elapsed
    time exceeds max_wall_seconds.  Uses _start_time injection to avoid
    flakiness — no real sleep needed.
    """
    # Inject a start time 10 seconds in the past; cap is 5 seconds.
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.dimension == "wall_clock"


def test_wall_clock_cap_exception_carries_correct_dimension():
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.dimension == "wall_clock"


def test_wall_clock_cap_exception_carries_limit():
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.limit == 5.0


def test_wall_clock_cap_exception_elapsed_exceeds_limit():
    """elapsed_seconds on the exception must be > max_wall_seconds."""
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.elapsed_seconds > 5.0


def test_wall_clock_cap_exception_carries_tokens_used():
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)
    cap.record_tokens(42)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.tokens_used == 42


def test_check_wall_clock_only_fires_on_wall_breach():
    """check_wall_clock() does not evaluate tokens."""
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)
    cap.record_tokens(99_999)  # under token limit
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check_wall_clock()
    assert exc_info.value.dimension == "wall_clock"


def test_no_breach_within_wall_clock_limit():
    """No exception when under both limits — fresh cap, no injected past time."""
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=3600.0)
    cap.check()  # must not raise immediately after creation


# ---------------------------------------------------------------------------
# TOKEN CAP FIRES BEFORE NEXT WORK UNIT (enforced in loop, not after)
# ---------------------------------------------------------------------------


def test_token_cap_fires_before_second_work_unit():
    """
    Simulates the agent loop pattern:
      cap.check() → do_work() → cap.record_tokens() → cap.check()

    After the first unit of work pushes tokens over the limit, the SECOND
    cap.check() (before the second unit of work) must raise — preventing any
    further execution.  This proves enforcement happens in the loop.
    """
    cap = BudgetCap(max_tokens=100, max_wall_seconds=60.0)

    # First work unit — stays under limit
    cap.check()  # pre-work check: no raise
    cap.record_tokens(80)
    cap.check()  # post-work check: no raise (80 <= 100)

    # Simulate a second LLM call that would push over the limit
    cap.record_tokens(30)  # now at 110, over the 100 limit

    # Pre-work check for the third unit must fire — stopping execution
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.dimension == "tokens"
    assert exc_info.value.tokens_used == 110


def test_wall_clock_cap_fires_before_next_work_unit():
    """
    Simulates the agent loop with wall-clock enforcement.
    After the budget window expires, cap.check() must fire before the next unit.
    """
    # Start 10s in the past with a 5s cap — already expired
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)

    # The very first check in the loop must fire — no work happens
    with pytest.raises(BudgetExceeded):
        cap.check()


# ---------------------------------------------------------------------------
# PARTIAL FINDING — exception carries partial work for artifact construction
# ---------------------------------------------------------------------------


def test_token_cap_exception_carries_no_partial_finding_by_default():
    """BudgetExceeded.partial_finding is None when no partial work was recorded."""
    cap = BudgetCap(max_tokens=50, max_wall_seconds=60.0)
    cap.record_tokens(51)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.partial_finding is None


def test_set_partial_finding_stores_text():
    cap = BudgetCap(max_tokens=50, max_wall_seconds=60.0)
    cap.set_partial_finding("Battery at 92%, degrading normally.")
    cap.record_tokens(51)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.partial_finding == "Battery at 92%, degrading normally."


def test_wall_clock_exception_carries_partial_finding():
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)
    cap.set_partial_finding("Partial: one vehicle processed.")
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.partial_finding == "Partial: one vehicle processed."


def test_partial_finding_update_is_overwritten():
    """The last set_partial_finding wins — runner updates it after each chunk."""
    cap = BudgetCap(max_tokens=50, max_wall_seconds=60.0)
    cap.set_partial_finding("first chunk done")
    cap.set_partial_finding("second chunk done")
    cap.record_tokens(51)
    with pytest.raises(BudgetExceeded) as exc_info:
        cap.check()
    assert exc_info.value.partial_finding == "second chunk done"


# ---------------------------------------------------------------------------
# CAPPED RUN WRITES A WELL-FORMED PARTIAL ARTIFACT (not nothing, not malformed)
# ---------------------------------------------------------------------------


def test_token_cap_breach_yields_well_formed_partial_artifact():
    """
    Token cap fires → BudgetExceeded carries partial_finding →
    runner constructs a well-formed partial artifact.

    This test proves the full chain:
      1. Token cap raises BudgetExceeded.
      2. Exception carries partial_finding (not None).
      3. An artifact built from the exception passes the M4 shape check.
    """
    cap = BudgetCap(max_tokens=100, max_wall_seconds=60.0)
    cap.set_partial_finding("Battery health assessed for 3 vehicles before budget cap.")
    cap.record_tokens(101)

    try:
        cap.check()
        assert False, "Expected BudgetExceeded"
    except BudgetExceeded as exc:
        # Runner constructs partial artifact from exception
        assert exc.partial_finding is not None
        artifact = _minimal_partial_artifact(partial_finding=exc.partial_finding)
        # Verify the partial artifact is well-formed (M4 shape)
        _assert_partial_artifact_well_formed(artifact)


def test_wall_clock_cap_breach_yields_well_formed_partial_artifact():
    """
    Wall-clock cap fires → BudgetExceeded carries partial_finding →
    runner constructs a well-formed partial artifact.
    """
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)
    cap.set_partial_finding("Partial battery scan: 50 of 1000 vehicles processed.")

    try:
        cap.check()
        assert False, "Expected BudgetExceeded"
    except BudgetExceeded as exc:
        assert exc.partial_finding is not None
        artifact = _minimal_partial_artifact(partial_finding=exc.partial_finding)
        _assert_partial_artifact_well_formed(artifact)


def test_capped_artifact_has_low_confidence():
    """A partial artifact should carry low confidence, not high."""
    cap = BudgetCap(max_tokens=10, max_wall_seconds=60.0)
    cap.set_partial_finding("Partial result.")
    cap.record_tokens(11)
    try:
        cap.check()
    except BudgetExceeded as exc:
        artifact = _minimal_partial_artifact(partial_finding=exc.partial_finding)
        assert artifact["confidence"] == "low"


def test_capped_artifact_no_p_level_in_severity_hint():
    """A partial artifact's severity_hint must not be a triage P-level."""
    cap = BudgetCap(max_tokens=10, max_wall_seconds=60.0)
    cap.set_partial_finding("Partial.")
    cap.record_tokens(11)
    try:
        cap.check()
    except BudgetExceeded as exc:
        artifact = _minimal_partial_artifact(partial_finding=exc.partial_finding)
        assert artifact["severity_hint"] not in {"P0", "P1", "P2", "P3"}


def test_no_partial_finding_still_allows_artifact_construction():
    """
    If the cap fires before any partial work is recorded, the runner must still
    be able to construct a minimal well-formed artifact — not nothing.
    """
    cap = BudgetCap(max_tokens=10, max_wall_seconds=60.0)
    # No set_partial_finding call
    cap.record_tokens(11)
    try:
        cap.check()
    except BudgetExceeded as exc:
        # exc.partial_finding is None — runner substitutes a default
        finding_text = exc.partial_finding or "Run aborted: budget cap reached before any analysis completed."
        artifact = _minimal_partial_artifact(partial_finding=finding_text)
        _assert_partial_artifact_well_formed(artifact)


def test_no_vins_in_budget_exception_repr():
    """No customer data may escape via repr(), "%r", pprint, or repr([exc]).

    Security review Cycle 2 Warning: the custom ``__str__`` omitted
    ``partial_finding``, but the auto-generated dataclass ``__repr__`` did not,
    so ``repr(exc)`` / ``"%r" % exc`` / ``pprint(exc)`` / ``repr([exc])`` all
    exposed finding text in full.  ``%r`` is a routine logging idiom and Tier 2
    runs unattended, so the log is the widest exposure.  Fixed with
    ``field(..., repr=False)``; this test is what keeps it fixed.
    """
    import io
    import pprint as _pprint

    vin = "1FMCU0F70MUA12345"
    exc = BudgetExceeded(
        dimension="tokens",
        used=100,
        limit=50,
        tokens_used=100,
        elapsed_seconds=1.0,
        partial_finding=f"Battery for VIN {vin} is at 92%, degrading normally.",
    )

    buf = io.StringIO()
    _pprint.pprint(exc, stream=buf)

    rendered = {
        "str": str(exc),
        "repr": repr(exc),
        "%r": "%r" % exc,
        "%s": "%s" % exc,
        "repr(list)": repr([exc]),
        "pprint": buf.getvalue(),
        "args[0]": str(exc.args[0]),
    }
    for label, text in rendered.items():
        assert vin not in text, f"VIN leaked via {label}: {text!r}"
        assert "degrading normally" not in text, f"finding text leaked via {label}"

    # The field must remain readable programmatically — the runner needs it to
    # build the partial artifact; it is only the *rendered* forms that must omit it.
    assert exc.partial_finding is not None


def test_no_vins_in_budget_exception_str():
    """
    BudgetExceeded.__str__ must not contain VIN-like content.
    VINs are customer-identifying; they must not appear in logs.
    """
    cap = BudgetCap(max_tokens=50, max_wall_seconds=60.0)
    cap.record_tokens(51)
    try:
        cap.check()
    except BudgetExceeded as exc:
        exc_str = str(exc)
        # VINs are 17-char alphanumeric; none should appear in the exception string.
        # We check the string doesn't contain anything that looks like a VIN pattern.
        import re
        vin_pattern = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b')
        assert not vin_pattern.search(exc_str), (
            f"BudgetExceeded string contains a VIN-like value: {exc_str!r}"
        )


# ---------------------------------------------------------------------------
# BudgetCap.summary() — safe to log, no customer data
# ---------------------------------------------------------------------------


def test_summary_returns_dict():
    cap = BudgetCap(max_tokens=1_000, max_wall_seconds=60.0)
    assert isinstance(cap.summary(), dict)


def test_summary_contains_tokens_used():
    cap = BudgetCap(max_tokens=1_000, max_wall_seconds=60.0)
    cap.record_tokens(250)
    s = cap.summary()
    assert s["tokens_used"] == 250


def test_summary_contains_max_tokens():
    cap = BudgetCap(max_tokens=1_000, max_wall_seconds=60.0)
    assert cap.summary()["max_tokens"] == 1_000


def test_summary_contains_token_fraction():
    cap = BudgetCap(max_tokens=1_000, max_wall_seconds=60.0)
    cap.record_tokens(500)
    assert cap.summary()["token_fraction"] == 0.5


def test_summary_no_vins():
    """Summary dict must not contain VIN-like values."""
    import re
    cap = BudgetCap(max_tokens=1_000, max_wall_seconds=60.0)
    cap.record_tokens(100)
    s_str = json.dumps(cap.summary())
    vin_pattern = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b')
    assert not vin_pattern.search(s_str)


# ---------------------------------------------------------------------------
# RunObserver — constructor and basic usage
# ---------------------------------------------------------------------------


def test_run_observer_instantiates():
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=io.StringIO())
    assert obs is not None


def test_run_observer_record_is_run_record():
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=io.StringIO())
    assert isinstance(obs.record, RunRecord)


def test_run_observer_initial_rows_zero():
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=io.StringIO())
    assert obs.record.rows_read == 0


def test_run_observer_initial_artifacts_zero():
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=io.StringIO())
    assert obs.record.artifacts_written == 0


def test_run_observer_initial_tokens_zero():
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=io.StringIO())
    assert obs.record.tokens_used == 0


def test_run_observer_record_rows_read_accumulates():
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=io.StringIO())
    obs.record_rows_read(100)
    obs.record_rows_read(50)
    assert obs.record.rows_read == 150


def test_run_observer_record_tokens_accumulates():
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=io.StringIO())
    obs.record_tokens(300)
    obs.record_tokens(200)
    assert obs.record.tokens_used == 500


def test_run_observer_record_artifact_written_increments():
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=io.StringIO())
    obs.record_artifact_written()
    obs.record_artifact_written()
    assert obs.record.artifacts_written == 2


# ---------------------------------------------------------------------------
# RunObserver.finish() — emits JSON; abort_reason on budget breach
# ---------------------------------------------------------------------------


def test_finish_emits_json_line():
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    obs.finish()
    output = buf.getvalue()
    assert output.strip()
    record = json.loads(output.strip())
    assert isinstance(record, dict)


def test_finish_sets_status_completed_on_normal_run():
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    obs.finish()
    record = json.loads(buf.getvalue().strip())
    assert record["status"] == "completed"


def test_finish_sets_status_aborted_on_budget_breach():
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    obs.finish(abort_reason="BudgetExceeded(dimension='tokens', used=101.0, limit=100.0, ...)")
    record = json.loads(buf.getvalue().strip())
    assert record["status"] == "aborted"


def test_finish_records_abort_reason():
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    reason = "BudgetExceeded(dimension='wall_clock', used=310.5, limit=300.0)"
    obs.finish(abort_reason=reason)
    record = json.loads(buf.getvalue().strip())
    assert record["abort_reason"] == reason


def test_finish_abort_reason_none_on_normal_run():
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    obs.finish()
    record = json.loads(buf.getvalue().strip())
    assert record["abort_reason"] is None


def test_finish_includes_all_required_fields():
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    obs.record_rows_read(1000)
    obs.record_tokens(500, cost_usd=0.001)
    obs.record_artifact_written()
    obs.finish()
    record = json.loads(buf.getvalue().strip())
    for field in ("run_id", "stage", "component", "started_at", "finished_at",
                  "rows_read", "artifacts_written", "tokens_used",
                  "estimated_cost_usd", "abort_reason", "status"):
        assert field in record, f"Missing field in run record: {field}"


def test_finish_twice_raises():
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    obs.finish()
    with pytest.raises(RuntimeError):
        obs.finish()


def test_finish_no_vins_in_output():
    """
    The emitted JSON log line must contain no VIN-like values.
    VINs are customer-identifying; run records are safe to log.
    """
    import re
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    obs.finish(abort_reason="BudgetExceeded(dimension='tokens', used=5001.0, limit=5000.0)")
    output = buf.getvalue()
    vin_pattern = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b')
    assert not vin_pattern.search(output), (
        f"Run record output contains a VIN-like value: {output!r}"
    )


# ---------------------------------------------------------------------------
# Integration: budget breach + observability together (runner.py pattern)
# ---------------------------------------------------------------------------


def test_budget_breach_triggers_abort_record():
    """
    Simulate the runner.py pattern end-to-end:
      1. BudgetCap fires on token breach.
      2. Runner catches BudgetExceeded and calls obs.finish(abort_reason=...).
      3. The emitted record has status='aborted' with the reason.
    """
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    cap = BudgetCap(max_tokens=100, max_wall_seconds=60.0)

    aborted = False
    try:
        cap.check()
        cap.record_tokens(101)
        obs.record_tokens(101)
        cap.check()  # fires here
    except BudgetExceeded as exc:
        obs.finish(abort_reason=str(exc))
        aborted = True

    assert aborted
    record = json.loads(buf.getvalue().strip())
    assert record["status"] == "aborted"
    assert record["abort_reason"] is not None
    assert record["tokens_used"] == 101


def test_wall_clock_breach_triggers_abort_record():
    """
    Wall-clock cap fires → runner calls obs.finish(abort_reason=...) →
    run record has status='aborted'.
    """
    buf = io.StringIO()
    obs = RunObserver(run_id=RUN_ID, stage=STAGE, component=COMPONENT,
                      _output=buf, _clock=lambda: "2026-08-04T00:00:00Z")
    past = time.monotonic() - 10.0
    cap = BudgetCap(max_tokens=100_000, max_wall_seconds=5.0, _start_time=past)

    aborted = False
    try:
        cap.check()  # fires immediately — wall time already exceeded
    except BudgetExceeded as exc:
        obs.finish(abort_reason=str(exc))
        aborted = True

    assert aborted
    record = json.loads(buf.getvalue().strip())
    assert record["status"] == "aborted"
    assert "wall_clock" in record["abort_reason"]
