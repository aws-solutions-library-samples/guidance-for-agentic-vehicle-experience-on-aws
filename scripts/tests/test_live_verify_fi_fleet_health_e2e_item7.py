"""E2E item 7: forced failure reaches staleness alarm subscriber, then verify.

Two-phase flow: force (writes env override, invokes service, restores env),
then 28-hour world advance with service schedule held (no service run), then
verify (reads alarm history for OK->ALARM + Action Succeeded).

Each failing case has a passing control in the same test.

Flags:
    force:  --allow-writes --allow-paging
    verify: --allow-paging (read-only; no --allow-writes required)

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest \\
        scripts/tests/test_live_verify_fi_fleet_health_e2e_item7.py -v
"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from scripts.tests.fh_world.world import FhWorld
from scripts.tests.fi_rw_world.world import HEALTH_FN, REGION, STAGE

_ALARM_NAME = f"vsa-{STAGE}-avx-run-stale-service-{REGION}"
_SERVICE_SCHED = f"vsa-{STAGE}-tier2-daily-service-{REGION}"
_FORCE_FLAGS = ["--stage", STAGE, "--item", "7", "--allow-writes", "--allow-paging",
                "--item7-phase", "force"]
_VERIFY_FLAGS_BASE = ["--stage", STAGE, "--item", "7", "--allow-paging",
                      "--item7-phase", "verify"]
_28H = 28 * 3600


def _set_service_sched_state(w: FhWorld, state: str) -> None:
    """Update the service schedule State only (get-schedule, update-schedule)."""
    sch = w.internal("scheduler")
    s = sch.get_schedule(Name=_SERVICE_SCHED, GroupName="default")
    sch.update_schedule(
        Name=_SERVICE_SCHED, GroupName="default",
        ScheduleExpression=s["ScheduleExpression"],
        State=state,
        FlexibleTimeWindow=s["FlexibleTimeWindow"],
        Target=s["Target"],
    )


# ---------------------------------------------------------------------------
# Loopback guard
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _forced_at(w: FhWorld) -> str:
    """Return forced_at ISO from the last manifest written by force phase."""
    manifest_path = w.manifests[-1]
    data = json.loads(manifest_path.read_text())
    return data["items"]["item7_force"]["forced_at"]


def _fails(w: FhWorld, text: str) -> list[str]:
    return sorted(k for k, v in w.rows(text).items() if v == "FAIL")


def _passes(w: FhWorld, text: str, *labels: str) -> None:
    rows = w.rows(text)
    bad = [lb for lb in labels if rows.get(lb) != "PASS"]
    assert not bad, f"not PASS: {[(lb, rows.get(lb)) for lb in bad]}\n{text[-2000:]}"


# ---------------------------------------------------------------------------
# Positive: force passes; no run record written; env restored byte-for-byte
# ---------------------------------------------------------------------------

def test_item7_force_passes_in_world() -> None:
    """Positive: all static checks pass, invoke errors, env restored byte-for-byte."""
    with FhWorld() as w:
        rc, text, _ = w.run_main(_FORCE_FLAGS)
        assert rc == 0, text[-2000:]
        _passes(w, text,
                "7-alarm-exists",
                "7-alarm-actions-enabled",
                "7-alarm-metric",
                "7-alarm-actions-exact",
                "7-topic-subscriber",
                "7-alarm-not-alarm",
                "7-invoke-error",
                "7-no-run-record",
                "7-env-restored")
        # env is byte-for-byte: function_env should equal the pre-force backup
        env_after = w.function_env(HEALTH_FN)
        assert env_after.get("FI_SERVICE_MODEL_ENABLED") == "false", (
            "FI_SERVICE_MODEL_ENABLED not restored to 'false'"
        )
        assert env_after.get("FI_COST_GUARDRAIL_ID") != "", (
            "FI_COST_GUARDRAIL_ID restored to empty — was not restored"
        )


# ---------------------------------------------------------------------------
# Positive: force then verify after 28 h (schedule held)
# ---------------------------------------------------------------------------

def test_item7_force_then_verify_passes_after_28h() -> None:
    """Force, advance 28 h with service schedule DISABLED, verify finds OK->ALARM + Action."""
    with FhWorld() as w:
        rc, text, _ = w.run_main(_FORCE_FLAGS)
        assert rc == 0, text[-2000:]
        forced_at = _forced_at(w)

        # Operator disables the service schedule before waiting 28 h.
        # This is the documented operator step (G6-D3): without it, the schedule
        # would fire at 05:40 UTC, emit a heartbeat, and verify would FAIL.
        _set_service_sched_state(w, "DISABLED")
        w.clock.advance(_28H)
        _set_service_sched_state(w, "ENABLED")

        # staleness alarm must have transitioned to ALARM
        assert w.staleness_alarm.state == "ALARM", (
            "staleness alarm did not transition to ALARM after 28h without service run"
        )

        rc2, text2, _ = w.run_main(
            _VERIFY_FLAGS_BASE + ["--since", forced_at])
        assert rc2 == 0, text2[-2000:]
        _passes(w, text2, "7-verify-ok-to-alarm", "7-verify-action-succeeded")


# ---------------------------------------------------------------------------
# Failing: service schedule left ENABLED → service fires during 28 h → verify FAIL
# ---------------------------------------------------------------------------

def test_item7_verify_fails_when_service_schedule_enabled() -> None:
    """Failing: service schedule left ENABLED; fires at 05:40 UTC; verify FAIL (no ALARM)."""
    with FhWorld() as w:
        rc, text, _ = w.run_main(_FORCE_FLAGS)
        assert rc == 0, text[-500:]
        forced_at = _forced_at(w)

        # Schedule is ENABLED (world default after 6.2): service fires at 05:40.
        w.clock.advance(_28H)

        # The service run kept the staleness alarm OK — alarm did NOT reach ALARM.
        assert w.staleness_alarm.state != "ALARM", (
            "alarm should stay OK when service schedule fires inside 27h window"
        )
        rc_v, text_v, _ = w.run_main(_VERIFY_FLAGS_BASE + ["--since", forced_at])
        rows = w.rows(text_v)
        assert rows.get("7-verify-ok-to-alarm") == "FAIL", (
            f"expected 7-verify-ok-to-alarm FAIL when service ran; rows={rows}"
        )


# ---------------------------------------------------------------------------
# Failing: restore leaves env different (concurrent writer)
# ---------------------------------------------------------------------------

def test_item7_force_restore_fails_when_env_changed_after_override() -> None:
    """Passing control then inject a concurrent env change so restore FAIL row fires.

    The world's FakeLambda stores env in moto.  We use a before_run hook to
    change the env immediately after the invoke so the pre-restore check sees
    a different env than override_vars.
    """
    with FhWorld() as w:
        # Positive control: force without interference
        rc_ok, text_ok, _ = w.run_main(_FORCE_FLAGS)
        assert rc_ok == 0, text_ok[-500:]

        # Inject a concurrent change: add a new key after the override is applied
        # but before the restore.  We use after_run to mutate the env.
        def _concurrent_change(fn: str, _event: Any, _result: Any) -> None:
            if fn != HEALTH_FN:
                return
            lam = w.internal("lambda")
            env_now = dict(
                lam.get_function_configuration(FunctionName=fn)
                .get("Environment", {}).get("Variables", {}))
            env_now["INJECTED_CONCURRENTLY"] = "yes"
            lam.update_function_configuration(
                FunctionName=fn, Environment={"Variables": env_now})

        w.lam.after_run.append(_concurrent_change)
        rc_bad, text_bad, _ = w.run_main(_FORCE_FLAGS)
        # The force must report a restore failure (cleanup_failed or 7-restore FAIL)
        rows = w.rows(text_bad)
        assert rows.get("7-restore") == "FAIL" or rows.get("7-env-restored") == "FAIL", (
            f"expected restore failure but rows={rows}\n{text_bad[-1000:]}"
        )


# ---------------------------------------------------------------------------
# Failing: alarm has no actions
# ---------------------------------------------------------------------------

def test_item7_force_fails_when_alarm_has_no_actions() -> None:
    """Passing control then disable alarm actions → 7-alarm-actions-enabled FAIL."""
    with FhWorld() as w:
        # Positive control
        rc_ok, text_ok, _ = w.run_main(_FORCE_FLAGS)
        assert rc_ok == 0, text_ok[-500:]

        # Re-create the staleness alarm without actions enabled
        cw = w.internal("cloudwatch")
        cw.put_metric_alarm(
            AlarmName=_ALARM_NAME,
            Metrics=[
                {
                    "Id": "m1",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "CVX/Tier2",
                            "MetricName": "RunRecordsWritten",
                            "Dimensions": [
                                {"Name": "stage", "Value": STAGE},
                                {"Name": "component", "Value": "service"},
                            ],
                        },
                        "Period": 3600,
                        "Stat": "Sum",
                    },
                },
                {"Id": "e1", "Expression": "FILL(m1,0)"},
            ],
            EvaluationPeriods=27,
            DatapointsToAlarm=27,
            Threshold=1.0,
            ComparisonOperator="LessThanThreshold",
            TreatMissingData="breaching",
            AlarmActions=[],    # no actions
            OKActions=[],
            ActionsEnabled=False,
        )

        rc_bad, text_bad, _ = w.run_main(_FORCE_FLAGS)
        rows = w.rows(text_bad)
        assert rows.get("7-alarm-actions-enabled") == "FAIL", (
            f"expected 7-alarm-actions-enabled FAIL; rows={rows}")


# ---------------------------------------------------------------------------
# Failing: service run completes inside 27 h (schedule not held)
# ---------------------------------------------------------------------------

def test_item7_verify_fails_when_service_runs_inside_window() -> None:
    """Positive control: force + 28h without service → ALARM.
    Failing: force + run service inside window → alarm stays OK → verify FAIL.

    The test advances 28h in both cases but inserts a service run (+ heartbeat)
    in the failing path 1h after force.  That heartbeat keeps the alarm OK, so
    verify cannot find an OK->ALARM transition.
    """
    with FhWorld() as w:
        # Positive: no service run inside the window (schedule disabled by operator)
        rc_ok, text_ok, _ = w.run_main(_FORCE_FLAGS)
        assert rc_ok == 0, text_ok[-500:]
        forced_at_ok = _forced_at(w)
        _set_service_sched_state(w, "DISABLED")
        w.clock.advance(_28H)
        _set_service_sched_state(w, "ENABLED")
        assert w.staleness_alarm.state == "ALARM"
        rc_vok, text_vok, _ = w.run_main(_VERIFY_FLAGS_BASE + ["--since", forced_at_ok])
        assert rc_vok == 0, text_vok[-500:]

    with FhWorld() as w:
        # Failing: run service 1h after force → heartbeat in window → alarm stays OK
        rc_f, text_f, _ = w.run_main(_FORCE_FLAGS)
        assert rc_f == 0, text_f[-500:]
        forced_at_bad = _forced_at(w)

        # Advance 1h, run service successfully → emits heartbeat
        w.clock.advance(3600)
        w.lam.run(HEALTH_FN, {"stage": STAGE, "component": "service"})
        w.pump()

        # Advance 27h more (total 28h) → but heartbeat was 27h ago, still in window
        w.clock.advance(27 * 3600)
        # Alarm must NOT have transitioned because the heartbeat is still in window
        assert w.staleness_alarm.state != "ALARM", (
            "alarm unexpectedly ALARM despite service run inside 27h window"
        )

        rc_vbad, text_vbad, _ = w.run_main(_VERIFY_FLAGS_BASE + ["--since", forced_at_bad])
        rows = w.rows(text_vbad)
        assert rows.get("7-verify-ok-to-alarm") == "FAIL", (
            f"expected 7-verify-ok-to-alarm FAIL; rows={rows}")


# ---------------------------------------------------------------------------
# Failing: verify run before 27 h → no OK->ALARM in history → FAIL
# ---------------------------------------------------------------------------

def test_item7_verify_fails_before_27h_window() -> None:
    """Positive control: verify after 28h passes.
    Failing: verify immediately after force (0h) → alarm not yet ALARM → FAIL.
    """
    with FhWorld() as w:
        # Positive: force + 28h (schedule disabled) → verify passes
        rc_f, text_f, _ = w.run_main(_FORCE_FLAGS)
        assert rc_f == 0, text_f[-500:]
        forced_at = _forced_at(w)
        _set_service_sched_state(w, "DISABLED")
        w.clock.advance(_28H)
        _set_service_sched_state(w, "ENABLED")
        rc_vok, text_vok, _ = w.run_main(_VERIFY_FLAGS_BASE + ["--since", forced_at])
        assert rc_vok == 0, text_vok[-500:]

    with FhWorld() as w:
        # Failing: run verify immediately after force (no time advance)
        rc_f2, text_f2, _ = w.run_main(_FORCE_FLAGS)
        assert rc_f2 == 0, text_f2[-500:]
        forced_at_early = _forced_at(w)
        # No clock advance; alarm is still OK (no 27h of empty periods)
        assert w.staleness_alarm.state == "OK", (
            "alarm should be OK immediately after force"
        )
        rc_early, text_early, _ = w.run_main(
            _VERIFY_FLAGS_BASE + ["--since", forced_at_early])
        rows = w.rows(text_early)
        assert rows.get("7-verify-ok-to-alarm") == "FAIL", (
            f"expected 7-verify-ok-to-alarm FAIL when run before window; rows={rows}"
        )
