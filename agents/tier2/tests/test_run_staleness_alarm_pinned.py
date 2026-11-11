"""Pin the run-staleness alarms in vsa-tier2-stack.ts to the Python heartbeat (spec D10).

The stack creates one alarm per Tier 2 component on ``CVX/Tier2`` /
``RunRecordsWritten`` / ``{stage, component}`` with missing data breaching. If the
emitter's namespace, metric name or dimension names drift from the stack, every
alarm watches an empty series and fires forever; if a component is added to
``contract.VALID_COMPONENTS`` without an alarm, its missed runs reach nobody.

Python, because this directory runs in the gating CI job and jest runs in none.
The stack is read with a regex, not compiled.
"""
from __future__ import annotations

import os
import re

from agents.tier2 import observability
from agents.tier2.contract import VALID_COMPONENTS
from agents.tier2.tests._tier2_stack_text import (
    names_quoted_anywhere,
    scheduled_components,
    staleness_windows,
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_STACK = os.path.join(_REPO_ROOT, "infrastructure", "lib", "vsa-tier2-stack.ts")


def _stack_src() -> str:
    with open(_STACK) as fh:
        return fh.read()


def _staleness_block() -> str:
    src = _stack_src()
    m = re.search(r"const runStalenessWindows:.*?const runStalenessAlarms", src, re.S)
    assert m, "runStalenessWindows block not found in vsa-tier2-stack.ts"
    return m.group(0)


def _stack_components() -> set[str]:
    """CHANGED by FI stage C Group 3 (fourth review W2): the shared, comment-stripped reader
    that sees every entry form (it was literal ``{ component: 'x'`` entries only)."""
    return staleness_windows()


def _emitted_datum() -> dict:
    class _Cw:
        def __init__(self):
            self.calls: list = []

        def put_metric_data(self, **kw):
            self.calls.append(kw)

    cw = _Cw()
    observability.emit_run_record_heartbeat(cw, "staging", "battery")
    (call,) = cw.calls
    (datum,) = call["MetricData"]
    return {"namespace": call["Namespace"], **datum}


#: Valid components nothing invokes yet, so no staleness alarm (a BREACHING alarm with no
#: schedule pages daily). FI stage C task 4.3 emptied this set for recall and warranty
#: (schedules AND alarms). FI stage D task 4.3 adds ``service`` (scheduled) and
#: ``investigation`` (SQS event-triggered, not scheduled) and removes both from it.
#: Rebase onto AVX main 9770dbd0 (stage E Group 4): ``rebalance`` stays exempt; stage E
#: task 5.2 adds its schedule and must remove it (the second test below fails as soon as it
#: gets a schedule).
#: Valid components nothing invokes yet, so no staleness alarm (a BREACHING alarm with no
#: schedule pages daily). FI stage C task 4.3 emptied this set for recall and warranty
#: (schedules AND alarms). FI stage D task 4.3 landed service with its schedule and alarm
#: (B2), and routed investigation to the DLQ alarm instead (_EVENT_TRIGGERED). FI stage E
#: task 5.2 added rebalance's schedule and staleness alarm in the SAME change as this
#: removal — the pair is enforced by test_rebalance_has_a_schedule_and_a_staleness_alarm
#: below, which fails if the set still contains rebalance OR if the stack lacks either
#: of the two resources (mutation: dropping either side flips the pair of tests).
_NOT_YET_SCHEDULED: frozenset[str] = frozenset()

#: CHANGED by task 4.3 B2: event-triggered components have an SQS event source, a DLQ
#: alarm, and the two AVX/Investigations failure alarms — but NO schedule and NO
#: staleness window (a staleness alarm would page whenever nobody raises an investigation
#: for a day, which is normal; task43-notes.md item 2).
_EVENT_TRIGGERED: frozenset[str] = frozenset({"investigation"})


def test_every_component_has_a_staleness_alarm_and_no_alarm_is_for_an_unknown_one():
    """Every valid component except event-triggered ones has a staleness alarm.

    CHANGED by task 4.3 B2: investigation moves to _EVENT_TRIGGERED (no staleness alarm; it
    has a DLQ alarm instead).  CHANGED by task 5.2: rebalance was in _NOT_YET_SCHEDULED;
    its schedule and staleness alarm land together in the same change (spec D8), so
    _NOT_YET_SCHEDULED is empty.
    """
    stack = _stack_components()
    expected = set(VALID_COMPONENTS) - _EVENT_TRIGGERED - _NOT_YET_SCHEDULED
    assert stack == expected, (
        f"no staleness alarm for {sorted(expected - stack)}; "
        f"alarms for unknown or exempt components {sorted(stack - expected)}"
    )


def test_unscheduled_exemptions_really_have_no_schedule():
    """``_NOT_YET_SCHEDULED`` must stay empty — every valid non-event-triggered component
    has a schedule.

    CHANGED by task 5.2: rebalance moved out of _NOT_YET_SCHEDULED; its schedule lives
    in componentSchedules now.  CHANGED by task 4.3 B2: service and investigation are
    no longer exempt either.
    """
    assert _NOT_YET_SCHEDULED == frozenset(), (
        "every valid non-event-triggered component is scheduled now; nothing should be in "
        "_NOT_YET_SCHEDULED (stage D task 4.3 landed service; stage E task 5.2 landed "
        "rebalance; stage C task 4.3 landed recall and warranty)"
    )
    sched = scheduled_components()
    assert not (_NOT_YET_SCHEDULED & sched), (
        f"exempt components now have a schedule: {sorted(_NOT_YET_SCHEDULED & sched)} — "
        "remove them from _NOT_YET_SCHEDULED and add their staleness alarms")
    missing = set(VALID_COMPONENTS) - _EVENT_TRIGGERED - _NOT_YET_SCHEDULED - sched
    assert not missing, (
        f"valid components without a schedule: {sorted(missing)} — "
        "add their schedules and alarms to componentSchedules and runStalenessWindows"
    )


def test_event_triggered_component_contract():
    """CHANGED by task 4.3 B2: investigation is event-triggered (SQS), not scheduled.

    Contract (task43-notes.md item 2):
    - Exactly _EVENT_TRIGGERED = {"investigation"}.
    - Each event-triggered component: has an SQS EventSourceMapping on the Tier 2 Lambda;
      has a DLQ alarm; has neither a ComponentSchedules entry nor a staleness window.
    Fails closed when any pattern is not found.
    """
    import os
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    stack_path = os.path.join(repo_root, "infrastructure", "lib", "vsa-tier2-stack.ts")
    with open(stack_path) as fh:
        raw = fh.read()

    assert _EVENT_TRIGGERED == frozenset({"investigation"}), (
        "_EVENT_TRIGGERED must be exactly {'investigation'} for task 4.3 B2")

    from agents.tier2.tests._tier2_stack_text import strip_comments, scheduled_components, staleness_windows
    clean = strip_comments(raw)
    sched = scheduled_components(clean)
    windows = staleness_windows(clean)

    for comp in _EVENT_TRIGGERED:
        # Must bind SqsEventSource on avxInvestigationsQueue to healthAgentFn — comment-stripped.
        assert re.search(
            r"healthAgentFn\.addEventSource\s*\(\s*new\s+lambdaEventSources\.SqsEventSource\s*\(\s*avxInvestigationsQueue",
            clean,
        ), (
            f"event-triggered component {comp!r}: SqsEventSource on avxInvestigationsQueue "
            "bound to healthAgentFn not found in comment-stripped source"
        )
        # Must have a DLQ depth alarm by logical ID — comment-stripped.
        assert "AvxInvestigationsDlqDepthAlarm" in clean, (
            f"event-triggered component {comp!r}: AvxInvestigationsDlqDepthAlarm missing "
            "from comment-stripped source")
        # Must NOT have a schedule (schedule would invoke every day, not on-demand).
        assert comp not in sched, (
            f"event-triggered component {comp!r} has a schedule — must not (task43-notes.md item 2)")
        # Must NOT have a staleness window (would page whenever nobody investigates for a day).
        assert comp not in windows, (
            f"event-triggered component {comp!r} has a staleness window — must not")


def test_the_alarm_watches_the_series_the_heartbeat_writes():
    datum = _emitted_datum()
    src = _stack_src()
    alarm_start = src.index("const runStalenessAlarms")
    alarm_block = src[alarm_start:src.index("runStalenessAlarms.push(alarm)", alarm_start)]
    assert f"namespace:     '{datum['namespace']}'" in alarm_block
    assert f"metricName:    '{datum['MetricName']}'" in alarm_block
    names = [d["Name"] for d in datum["Dimensions"]]
    assert names == ["stage", "component"]
    assert "dimensionsMap: { stage, component }" in alarm_block
    assert "TreatMissingData.BREACHING" in alarm_block
    # Review Cycle 10 W1: the heartbeat is only ever 1, so the alarm must make missing
    # hours real zeros, and fire only when every hour in the window is empty.
    assert "expression: 'FILL(m1, 0)'" in alarm_block
    assert "datapointsToAlarm:  emptyHours" in alarm_block
    assert "evaluationPeriods:  emptyHours" in alarm_block


def test_both_sides_are_non_empty():
    """Anti-vacuity: an empty parse on both sides would compare equal."""
    assert {"battery", "tires", "brakes", "diagnostics"} <= _stack_components()
    assert _emitted_datum()["MetricName"] == "RunRecordsWritten"


def _int(pattern: str, text: str) -> int:
    m = re.search(pattern, text, re.S)
    assert m, f"not found in vsa-tier2-stack.ts: {pattern}"
    return int(m.group(1))


def test_the_daily_window_outlasts_the_worst_gap_between_good_runs():
    """Review Cycle 11 S1: CI runs no jest, so the window property is pinned here too.

    Worst gap between two successful daily runs: 24 h, plus the scheduler's retry window
    (maxEventAge), plus the first attempt and Lambda's 2 default async retries each
    running to the timeout, plus about 3 minutes of async backoff. A window at or under
    that would flap on a late but successful run. With 30 minutes of margin on top,
    24, 25 or 26 hours fail here.
    """
    src = _stack_src()
    block = _staleness_block()
    daily_hours = _int(r"\(\{ component, emptyHours: (\d+)[^}]*\}\)", block)
    diagnostics_hours = _int(r"component: 'diagnostics', emptyHours: (\d+)", block)
    health_fn = src[src.index("new lambda.Function(this, 'Tier2HealthAgentFn'"):]
    health_fn = health_fn[:health_fn.index("new lambda.Function(", 1)]   # this function only
    timeout_min = _int(r"timeout: cdk\.Duration\.minutes\((\d+)\)", health_fn)
    schedules = src[src.index("for (const { component, hour, minute, enabled } of componentSchedules)"):]  # task 4.3: enabled added
    max_event_age_h = _int(r"maxEventAge: cdk\.Duration\.hours\((\d+)\)", schedules[:2000])
    assert "EventInvokeConfig" not in src and "configureAsyncInvoke" not in src, (
        "an async-invoke config changes the retry count; update this bound")
    worst_gap_s = 86400 + max_event_age_h * 3600 + 3 * timeout_min * 60 + 180
    # Plus 30 minutes for scheduler jitter and the work done before the record is written.
    assert daily_hours * 3600 > worst_gap_s + 1800, (daily_hours, worst_gap_s)
    assert daily_hours * 3600 <= 7 * 86400          # CloudWatch limit for 1-hour periods
    assert 1 <= diagnostics_hours <= 3              # 12 to 36 missed 5-minute runs
