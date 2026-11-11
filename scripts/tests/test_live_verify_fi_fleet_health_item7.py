"""Tests for item 7 of scripts/live-verify-fi-fleet-health.py.

Fakes built from the real code/data shapes (source citations below).
A positive control per property; at least one bad world per property.

Source citations:
  alarm shape:      boto3 CloudWatch describe_alarms MetricAlarms[0]
                    (vsa-tier2-stack.ts:2249,2264-2277; facts.md § Item 7)
  topic shape:      boto3 SNS list_topics Topics[0] (facts.md § Item 7)
  subscription:     boto3 SNS list_subscriptions_by_topic Subscriptions[0]
  alarm history:    boto3 CloudWatch describe_alarm_history AlarmHistoryItems
                    (StateUpdate HistoryData newState/oldState; Action HistoryData actionState)
  Lambda cfg:       boto3 Lambda get_function_configuration Environment.Variables
                    (scripts/live-verify-fi-recall-warranty.py item9 pattern)
  invoke response:  boto3 Lambda invoke FunctionError field
                    (scripts/live-verify-fi-recall-warranty.py:3908)
  run records:      agents/tier2/run_records.py:120 pk=agent_component sk=started_at
                    (agent_component = 'agent-fi-fleet-health#service')

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_item7.py -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load module under test
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-fleet-health.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_fleet_health", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

Results = _mod.Results
_item_7 = _mod._item_7
_FH_COMPONENT = _mod._FH_COMPONENT
_FH_RUN_PK = _mod._FH_RUN_PK
_FH_ALARM_SUFFIX = _mod._FH_ALARM_SUFFIX
_FH_ALARM_CW_NAMESPACE = _mod._FH_ALARM_CW_NAMESPACE
_FH_ALARM_CW_METRIC = _mod._FH_ALARM_CW_METRIC
_FH_ALARM_EVAL_PERIODS = _mod._FH_ALARM_EVAL_PERIODS
_FI_GUARDRAIL_ENV = _mod._FI_GUARDRAIL_ENV
_FI_MODEL_ENABLED_ENV = _mod._FI_MODEL_ENABLED_ENV

# ---------------------------------------------------------------------------
# Fake shapes
# ---------------------------------------------------------------------------

_STAGE = "staging"
_REGION = "us-west-2"
_ACCOUNT = "123456789012"
_TOPIC_ARN = f"arn:aws:sns:{_REGION}:{_ACCOUNT}:vsa-{_STAGE}-tier2-operator-alerts"
_ALARM_NAME = f"vsa-{_STAGE}-{_FH_ALARM_SUFFIX}-{_REGION}"
_FN_NAME = f"vsa-{_STAGE}-tier2-health-agent-{_REGION}"
_FN_ARN = f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:{_FN_NAME}"


def _good_alarm(state: str = "OK", actions_enabled: bool = True,
                alarm_actions: list[str] | None = None) -> dict:
    """Fake MetricAlarm in describe_alarms shape.

    Source: boto3 CloudWatch describe_alarms MetricAlarms[0];
    vsa-tier2-stack.ts:2249,2264-2277; facts.md § Item 7 staleness alarm config.
    Uses Metrics list (math expression) matching the live alarm.
    """
    if alarm_actions is None:
        alarm_actions = [_TOPIC_ARN]
    return {
        "AlarmName": _ALARM_NAME,
        "StateValue": state,
        "ActionsEnabled": actions_enabled,
        "AlarmActions": alarm_actions,
        # metric expression: FILL(m1,0) with m1 = CVX/Tier2 RunRecordsWritten
        "Metrics": [
            {
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": _FH_ALARM_CW_NAMESPACE,
                        "MetricName": _FH_ALARM_CW_METRIC,
                        "Dimensions": [
                            {"Name": "stage", "Value": _STAGE},
                            {"Name": "component", "Value": _FH_COMPONENT},
                        ],
                    },
                    "Period": 3600,
                    "Stat": "Sum",
                },
            },
            {"Id": "e1", "Expression": "FILL(m1,0)"},
        ],
        "EvaluationPeriods": _FH_ALARM_EVAL_PERIODS,
        "DatapointsToAlarm": _FH_ALARM_EVAL_PERIODS,
        "Threshold": 1.0,
        "ComparisonOperator": "LessThanThreshold",
        "TreatMissingData": "breaching",
    }


def _good_topic_list() -> dict:
    """Fake SNS list_topics response.

    Source: boto3 SNS list_topics Topics[0].
    """
    return {"Topics": [{"TopicArn": _TOPIC_ARN}], "NextToken": None}


def _good_subscriptions() -> dict:
    """Fake SNS list_subscriptions_by_topic with one confirmed email subscription.

    Source: boto3 SNS list_subscriptions_by_topic Subscriptions[0].
    SubscriptionArn starts with 'arn:' when confirmed.
    """
    return {
        "Subscriptions": [
            {
                "SubscriptionArn": f"arn:aws:sns:{_REGION}:{_ACCOUNT}:vsa-{_STAGE}-tier2-operator-alerts:abc123",
                "Protocol": "email",
                "Endpoint": "ops@example.com",
                "TopicArn": _TOPIC_ARN,
            }
        ]
    }


def _good_lambda_cfg(extra_vars: dict[str, str] | None = None) -> dict:
    """Fake Lambda get_function_configuration response.

    Source: boto3 Lambda get_function_configuration Environment.Variables.
    """
    vars_: dict[str, str] = {
        _FI_GUARDRAIL_ENV: "real-guardrail-id",
        "FI_COST_GUARDRAIL_VERSION": "1",
        "STAGE": _STAGE,
    }
    if extra_vars:
        vars_.update(extra_vars)
    return {
        "FunctionName": _FN_NAME,
        "FunctionArn": _FN_ARN,
        "LastModified": "2026-10-17T02:00:00+00:00",
        "CodeSha256": "abc123",
        "RevisionId": "rev-1",
        "LastUpdateStatus": "Successful",
        "Environment": {"Variables": vars_},
    }


def _good_invoke_error() -> dict:
    """Fake invoke response with FunctionError set.

    Source: boto3 Lambda invoke response FunctionError field;
    scripts/live-verify-fi-recall-warranty.py:3908.
    """
    return {
        "StatusCode": 200,
        "FunctionError": "Unhandled",
        "Payload": MagicMock(read=lambda: b'{"errorMessage":"FI_COST_GUARDRAIL_ID must be set"}'),
    }


def _ddb_no_runs() -> dict:
    """Fake DDB Query response with no items (no run records yet).

    Source: agents/tier2/run_records.py:120 table shape.
    """
    return {"Items": [], "Count": 0, "ScannedCount": 0}


def _sched_good() -> tuple[bool, str]:
    """_check_scheduler_timing returns True (outside window)."""
    return True, "1 enabled schedule(s) checked"


def _alarm_history_ok_to_alarm(since: dt.datetime) -> dict:
    """Fake describe_alarm_history StateUpdate: one OK->ALARM transition.

    Source: boto3 CloudWatch describe_alarm_history AlarmHistoryItems;
    HistoryData newState/oldState stateValue fields.
    """
    ts = since + dt.timedelta(hours=28)
    return {
        "AlarmHistoryItems": [
            {
                "AlarmName": _ALARM_NAME,
                "Timestamp": ts,
                "HistoryItemType": "StateUpdate",
                "HistorySummary": "Alarm updated from OK to ALARM",
                "HistoryData": json.dumps({
                    "oldState": {"stateValue": "OK", "stateReason": "threshold crossed"},
                    "newState": {"stateValue": "ALARM", "stateReason": "breaching"},
                }),
            }
        ]
    }


def _alarm_history_action_succeeded(since: dt.datetime) -> dict:
    """Fake describe_alarm_history Action: one Succeeded notification.

    Source: boto3 CloudWatch describe_alarm_history AlarmHistoryItems;
    HistoryData actionState field (Succeeded / Failed).
    """
    ts = since + dt.timedelta(hours=28, seconds=5)
    return {
        "AlarmHistoryItems": [
            {
                "AlarmName": _ALARM_NAME,
                "Timestamp": ts,
                "HistoryItemType": "Action",
                "HistorySummary": f"Successfully executed action {_TOPIC_ARN}",
                "HistoryData": json.dumps({
                    "actionState": "Succeeded",
                    "notificationResource": _TOPIC_ARN,
                }),
            }
        ]
    }


# ---------------------------------------------------------------------------
# Helper: build ctx dict
# ---------------------------------------------------------------------------

def _ctx(
    cw: Any, sns: Any,
    lam: Any | None = None,
    sched: Any | None = None,
    ddb: Any | None = None,
    phase: str = "force",
    since: str | None = None,
    run: str | None = None,
    tmp_path: Path | None = None,
) -> dict:
    import secrets as _sec
    # Unique run per test so backup files don't collide across tests.
    run_id = run or ("T" + _sec.token_hex(4).upper())
    args = MagicMock()
    args.item7_phase = phase
    args.item7_since = since
    manifest_dir = tmp_path or Path(f"/tmp/fi-fh-d-g6lv-{run_id}-test")
    manifest_dir.mkdir(mode=0o700, exist_ok=True)
    manifest = manifest_dir / "manifest.json"
    import os as _os
    fd = _os.open(str(manifest), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
    with _os.fdopen(fd, "w") as fh:
        fh.write(f'{{"run_id":"{run_id}","items":{{}}}}')

    return {
        "stage": _STAGE,
        "region": _REGION,
        "account": _ACCOUNT,
        "manifest": manifest,
        "args": args,
        "run": run_id,
    }


# ---------------------------------------------------------------------------
# Tests: force phase — positive control
# ---------------------------------------------------------------------------

def test_item7_force_positive(tmp_path: Path) -> None:
    """Full force phase happy path: all static checks pass, invoke errors, env restored."""
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    original_cfg = _good_lambda_cfg()
    original_vars = original_cfg["Environment"]["Variables"]
    # override_vars mirrors what _item_7 builds before calling update
    override_vars = dict(original_vars)
    override_vars[_FI_MODEL_ENABLED_ENV] = "true"
    override_vars[_FI_GUARDRAIL_ENV] = ""

    get_cfg_calls = [0]

    def _get_cfg(**_kw: Any) -> dict:
        get_cfg_calls[0] += 1
        if get_cfg_calls[0] == 1:
            return original_cfg  # initial backup
        if get_cfg_calls[0] == 2:
            # pre-restore check: live holds the override vars (normal case)
            return {**original_cfg, "Environment": {"Variables": override_vars}}
        return original_cfg  # post-restore readback

    lam = MagicMock()
    lam.get_function_configuration.side_effect = _get_cfg
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()

    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
    ):
        res = Results()
        _item_7(res, **ctx)

    assert not res.failed, res.rows
    assert res.check_exists("7-alarm-exists")
    assert res.check_exists("7-alarm-actions-enabled")
    assert res.check_exists("7-alarm-metric")
    assert res.check_exists("7-invoke-error")
    assert res.check_exists("7-no-run-record")
    assert res.check_exists("7-env-restored")


# ---------------------------------------------------------------------------
# Bad world: ActionsEnabled = False
# ---------------------------------------------------------------------------

def test_item7_force_actions_disabled(tmp_path: Path) -> None:
    """ActionsEnabled=False → 7-alarm-actions-enabled fails."""
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm(actions_enabled=False)]}

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    # Lambda/sched/ddb never reached since static checks fail early — but we
    # still need valid alarm_not_alarm so the function proceeds past that gate.
    lam = MagicMock()
    lam.get_function_configuration.return_value = _good_lambda_cfg()
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
    ):
        res = Results()
        _item_7(res, **ctx)

    # 7-alarm-actions-enabled must fail
    failed_ids = _failed_ids(res)
    assert "7-alarm-actions-enabled" in failed_ids, res.rows


# ---------------------------------------------------------------------------
# Bad world: extra AlarmAction
# ---------------------------------------------------------------------------

def test_item7_force_extra_alarm_action(tmp_path: Path) -> None:
    """AlarmActions has an extra ARN → 7-alarm-actions-exact fails."""
    extra = _TOPIC_ARN + "-extra"
    cw = MagicMock()
    cw.describe_alarms.return_value = {
        "MetricAlarms": [_good_alarm(alarm_actions=[_TOPIC_ARN, extra])]
    }

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    lam = MagicMock()
    lam.get_function_configuration.return_value = _good_lambda_cfg()
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
    ):
        res = Results()
        _item_7(res, **ctx)

    failed_ids = _failed_ids(res)
    assert "7-alarm-actions-exact" in failed_ids, res.rows


# ---------------------------------------------------------------------------
# Bad world: alarm is already ALARM (refuse)
# ---------------------------------------------------------------------------

def test_item7_force_alarm_already_alarm(tmp_path: Path) -> None:
    """If alarm is already ALARM, 7-alarm-not-alarm fails and no invoke occurs."""
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm(state="ALARM")]}

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    lam = MagicMock()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
    ):
        res = Results()
        _item_7(res, **ctx)

    failed_ids = _failed_ids(res)
    assert "7-alarm-not-alarm" in failed_ids, res.rows
    # No invoke should have been made
    lam.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Bad world: invoke succeeded (no FunctionError)
# ---------------------------------------------------------------------------

def test_item7_force_invoke_succeeded(tmp_path: Path) -> None:
    """If Lambda invoke returns no FunctionError, 7-invoke-error fails."""
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    lam = MagicMock()
    lam.get_function_configuration.return_value = _good_lambda_cfg()
    lam.update_function_configuration.return_value = {}
    # Return success (no FunctionError)
    lam.invoke.return_value = {"StatusCode": 200, "FunctionError": ""}
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
    ):
        res = Results()
        _item_7(res, **ctx)

    failed_ids = _failed_ids(res)
    assert "7-invoke-error" in failed_ids, res.rows


# ---------------------------------------------------------------------------
# Bad world: restore read-back differs
# ---------------------------------------------------------------------------

def test_item7_force_restore_readback_differs(tmp_path: Path) -> None:
    """Restored env doesn't match original → 7-restore fails, cleanup_failed."""
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    original_cfg = _good_lambda_cfg()
    original_vars = original_cfg["Environment"]["Variables"]

    get_cfg_calls = [0]

    def _get_cfg(**_kw: Any) -> dict:
        get_cfg_calls[0] += 1
        if get_cfg_calls[0] == 1:
            return original_cfg  # first call: backup
        # All subsequent calls (post-restore readback): return wrong env
        wrong = dict(original_vars)
        wrong["EXTRA_KEY"] = "injected"
        return {**original_cfg, "Environment": {"Variables": wrong}}

    lam = MagicMock()
    lam.get_function_configuration.side_effect = _get_cfg
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
        # Cap the retry deadline so the test doesn't spin for 120 s
        patch.object(_mod.time, "monotonic", side_effect=_monotonic_expired()),
        patch.object(_mod.time, "sleep"),
    ):
        res = Results()
        _item_7(res, **ctx)

    failed_ids = _failed_ids(res)
    assert "7-restore" in failed_ids or res._cleanup_failed, res.rows


# ---------------------------------------------------------------------------
# Tests: verify phase — positive control
# ---------------------------------------------------------------------------

def test_item7_verify_positive(tmp_path: Path) -> None:
    """Verify phase: OK->ALARM history + Succeeded action → all pass."""
    since_iso = "2026-10-18T03:00:00Z"
    since_dt = dt.datetime(2026, 10, 18, 3, 0, 0, tzinfo=dt.timezone.utc)

    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}

    def _describe_alarm_history(**kw: Any) -> dict:
        hist_type = kw.get("HistoryItemType", "")
        if hist_type == "StateUpdate":
            return _alarm_history_ok_to_alarm(since_dt)
        if hist_type == "Action":
            return _alarm_history_action_succeeded(since_dt)
        return {"AlarmHistoryItems": []}

    cw.describe_alarm_history.side_effect = _describe_alarm_history

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    ctx = _ctx(cw, sns, phase="verify", since=since_iso, tmp_path=tmp_path / "run")

    with patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns)):
        res = Results()
        _item_7(res, **ctx)

    assert not res.failed, res.rows
    assert res.check_exists("7-verify-ok-to-alarm")
    assert res.check_exists("7-verify-action-succeeded")


# ---------------------------------------------------------------------------
# Bad world: verify — no Succeeded action in history
# ---------------------------------------------------------------------------

def test_item7_verify_no_succeeded_action(tmp_path: Path) -> None:
    """Verify phase: OK->ALARM exists but all actions Failed → 7-verify-action-succeeded fails."""
    since_iso = "2026-10-18T03:00:00Z"
    since_dt = dt.datetime(2026, 10, 18, 3, 0, 0, tzinfo=dt.timezone.utc)

    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}

    alarm_ts = since_dt + dt.timedelta(hours=28)

    def _describe_alarm_history(**kw: Any) -> dict:
        hist_type = kw.get("HistoryItemType", "")
        if hist_type == "StateUpdate":
            return _alarm_history_ok_to_alarm(since_dt)
        if hist_type == "Action":
            # Same timestamp, but Failed
            return {
                "AlarmHistoryItems": [
                    {
                        "AlarmName": _ALARM_NAME,
                        "Timestamp": alarm_ts + dt.timedelta(seconds=5),
                        "HistoryItemType": "Action",
                        "HistorySummary": "Action failed",
                        "HistoryData": json.dumps({
                            "actionState": "Failed",
                            "notificationResource": _TOPIC_ARN,
                        }),
                    }
                ]
            }
        return {"AlarmHistoryItems": []}

    cw.describe_alarm_history.side_effect = _describe_alarm_history

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    ctx = _ctx(cw, sns, phase="verify", since=since_iso, tmp_path=tmp_path / "run")

    with patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns)):
        res = Results()
        _item_7(res, **ctx)

    failed_ids = _failed_ids(res)
    assert "7-verify-action-succeeded" in failed_ids, res.rows


# ===========================================================================
# Guard: item 7 CloudWatch ops pass (endpoint_prefix vs service_name)
# ===========================================================================

def test_guard_item7_cloudwatch_ops_pass() -> None:
    """Guard uses service_name so cloudwatch.* passes; before-send halts network."""
    class _NoNet(Exception):
        pass

    def _halt(**_kw: Any) -> None:
        raise _NoNet

    cw = _mod._client(
        "cloudwatch", "us-east-1",
        allowed_ops=frozenset({"cloudwatch.DescribeAlarms", "cloudwatch.DescribeAlarmHistory"}),
    )
    cw.meta.events.register("before-send", _halt)

    for call_fn in [
        lambda: cw.describe_alarms(AlarmNames=[]),
        lambda: cw.describe_alarm_history(AlarmName="x"),
    ]:
        raised: Exception | None = None
        try:
            call_fn()
        except Exception as exc:
            raised = exc
        # Before fix: GuardViolation (endpoint_prefix='monitoring' ≠ 'cloudwatch')
        assert type(raised) is _NoNet, (
            f"expected _NoNet from before-send but got {type(raised).__name__}: {raised}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_item7_force_override_uses_revision_id(tmp_path: Path) -> None:
    """Override call passes RevisionId from original config.

    Source: review W1 '[item 7] The override and the restore are blind writes —
    pass RevisionId=original_cfg["RevisionId"] on the override (a
    PreconditionFailedException means someone else changed it: stop, no write)'.
    """
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    lam = MagicMock()
    original_cfg = _good_lambda_cfg()  # RevisionId = "rev-1"
    original_vars = original_cfg["Environment"]["Variables"]
    override_vars = dict(original_vars)
    override_vars[_FI_MODEL_ENABLED_ENV] = "true"
    override_vars[_FI_GUARDRAIL_ENV] = ""

    get_cfg_calls = [0]

    def _get_cfg(**_kw: Any) -> dict:
        get_cfg_calls[0] += 1
        if get_cfg_calls[0] == 1:
            return original_cfg
        if get_cfg_calls[0] == 2:
            return {**original_cfg, "Environment": {"Variables": override_vars}}
        return original_cfg

    lam.get_function_configuration.side_effect = _get_cfg
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
    ):
        res = Results()
        _item_7(res, **ctx)

    assert not res.failed, res.rows
    # First update_function_configuration call is the override
    override_call = lam.update_function_configuration.call_args_list[0]
    assert override_call.kwargs.get("RevisionId") == "rev-1", (
        f"override missing RevisionId: {override_call.kwargs}"
    )


def test_item7_force_restore_conflict_live_not_override(tmp_path: Path) -> None:
    """Pre-restore check: if live vars ≠ override_vars, mark cleanup failed, no restore write.

    Source: review W1 '[item 7] The override and the restore are blind writes —
    before restoring, read the config and require its Variables to equal
    override_vars (else stop, print both maps and the backup path, mark cleanup failed)'.
    """
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    original_cfg = _good_lambda_cfg()
    original_vars = original_cfg["Environment"]["Variables"]

    # Build override_vars as the function would: copy + set FI_SERVICE_MODEL_ENABLED
    override_vars = dict(original_vars)
    override_vars[_FI_MODEL_ENABLED_ENV] = "true"
    override_vars[_FI_GUARDRAIL_ENV] = ""

    # After override, a concurrent writer changed the env
    concurrent_vars = dict(original_vars)
    concurrent_vars["INJECTED"] = "concurrent"

    call_count = [0]

    def _get_cfg(**_kw: Any) -> dict:
        call_count[0] += 1
        if call_count[0] == 1:
            return original_cfg  # initial backup
        # At restore pre-check: live env differs from override (concurrent write)
        return {**original_cfg, "Environment": {"Variables": concurrent_vars}}

    lam = MagicMock()
    lam.get_function_configuration.side_effect = _get_cfg
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
        patch.object(_mod.time, "monotonic", side_effect=_monotonic_expired()),
        patch.object(_mod.time, "sleep"),
    ):
        res = Results()
        _item_7(res, **ctx)

    assert res._cleanup_failed or "7-restore" in _failed_ids(res), res.rows
    # update_function_configuration should have been called exactly once (the override),
    # NOT a second time for the restore (conflict detected before writing)
    update_calls = lam.update_function_configuration.call_args_list
    assert len(update_calls) == 1, (
        f"expected only the override call (1), got {len(update_calls)}: {update_calls}"
    )


def test_item7_force_static_fail_stops_write(tmp_path: Path) -> None:
    """ActionsEnabled=False → update_function_configuration never called.

    Source: review W2 '[item 7] A failed static precondition does not stop
    the force phase'.  The override (update_function_configuration) must not
    be reached when any alarm static check fails.
    """
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm(actions_enabled=False)]}

    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    lam = MagicMock()
    lam.get_function_configuration.return_value = _good_lambda_cfg()
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
    ):
        res = Results()
        _item_7(res, **ctx)

    lam.update_function_configuration.assert_not_called()
    assert "7-alarm-actions-enabled" in _failed_ids(res), res.rows


def test_item7_backup_contains_only_variables(tmp_path: Path) -> None:
    """Backup file written by _item7_write_backup contains only {"Variables": ...}.

    Source: brief § Env overrides: 'back up the whole Variables map (0600,
    {"Variables":…} only)'.  Suggestion: 'drop the extras'.
    """
    env = {"FOO": "bar", "BAZ": "qux"}
    cfg = _good_lambda_cfg()
    path = _mod._item7_write_backup(env, cfg, tmp_path, "my-fn", _REGION)
    data = json.loads(path.read_text())
    assert set(data.keys()) == {"Variables"}, f"unexpected keys: {set(data.keys())}"
    assert data["Variables"] == env


def test_item7_verify_argv_not_skipped() -> None:
    """Printed verify cmd argv (--allow-paging, --item7-phase verify, no --allow-writes)
    must result in item 7 being in to_run, not to_skip.

    Source: review W3 '[item 7] The printed verify command cannot run — treat
    --item7-phase verify as read-only in _compute_skips and the write-window gate'.
    """
    import sys

    argv = [
        "--stage", "staging",
        "--item", "7",
        "--allow-paging",
        "--item7-phase", "verify",
        "--since", "2026-10-18T03:00:00Z",
    ]
    orig_argv = sys.argv[:]
    sys.argv = ["script"] + argv
    try:
        args = _mod._parse_args()
    finally:
        sys.argv = orig_argv

    items_requested: frozenset[int] = frozenset([7])
    item7_verify = getattr(args, "item7_phase", "force") == "verify"

    # Before fix: _compute_skips doesn't know about item7_verify, so
    # item 7 is always in _WRITE_ITEMS and gets skipped without --allow-writes.
    to_run, to_skip = _mod._compute_skips(
        items_requested,
        allow_writes=args.allow_writes,
        allow_managed_writes=args.allow_managed_writes,
        allow_paging=args.allow_paging,
        item7_verify=item7_verify,
    )
    assert 7 not in to_skip, f"item 7 unexpectedly skipped: {to_skip}"
    assert 7 in to_run, f"item 7 not in to_run: {to_run}"


def test_item7_force_restore_readback_differs_precheck_passes(tmp_path: Path) -> None:
    """Pre-restore check passes (live == override_vars) but readback ≠ original_env → FAIL.

    Catches mutant: `if restored == original_env:` → `if True:` at the post-restore
    readback check. The pre-restore check sees override_vars (normal), so the concurrent-
    write guard doesn't fire; only the readback comparison is exercised.
    """
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}
    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()

    original_cfg = _good_lambda_cfg()
    original_vars = original_cfg["Environment"]["Variables"]
    override_vars = dict(original_vars)
    override_vars[_FI_MODEL_ENABLED_ENV] = "true"
    override_vars[_FI_GUARDRAIL_ENV] = ""

    get_cfg_calls = [0]

    def _get_cfg(**_kw: Any) -> dict:
        get_cfg_calls[0] += 1
        if get_cfg_calls[0] == 1:
            return original_cfg                          # initial backup
        if get_cfg_calls[0] == 2:
            # pre-restore check: live == override_vars (no concurrent write)
            return {**original_cfg, "Environment": {"Variables": override_vars}}
        # post-restore readback: wrong — restore didn't actually take
        wrong = dict(original_vars)
        wrong["DRIFT_KEY"] = "bad"
        return {**original_cfg, "Environment": {"Variables": wrong}}

    lam = MagicMock()
    lam.get_function_configuration.side_effect = _get_cfg
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
        patch.object(_mod.time, "monotonic", side_effect=_monotonic_expired()),
        patch.object(_mod.time, "sleep"),
    ):
        res = Results()
        _item_7(res, **ctx)

    assert res._cleanup_failed or "7-restore" in _failed_ids(res), (
        f"expected 7-restore FAIL or cleanup_failed; rows={res.rows}"
    )


def _make_client_factory(cw: Any, sns: Any, lam: Any = None,
                         sched: Any = None, ddb: Any = None) -> Any:
    """Return a _client replacement that hands back the right mock per service."""
    def _factory(service: str, *_args: Any, **_kw: Any) -> Any:
        mapping = {
            "cloudwatch": cw,
            "sns": sns,
            "lambda": lam or MagicMock(),
            "scheduler": sched or MagicMock(),
            "dynamodb": ddb or MagicMock(),
        }
        return mapping.get(service, MagicMock())
    return _factory


def _monotonic_expired():
    """Generator: always returns a past deadline so restore retries exit immediately."""
    # Return 0.0 on first call (starting value), then a huge number so
    # `time.monotonic() < restore_deadline` is False immediately.
    import itertools
    return itertools.chain([0.0], itertools.repeat(9999999.0))


# Monkey-patch helpers onto Results for the test assertions
# rows entries are (label, status, detail) — status is "PASS"/"FAIL"/"SKIP"/"NOTE"
Results.check_exists = lambda self, cid: any(r[0] == cid for r in self.rows)


def _failed_ids(res: Results) -> set[str]:
    return {r[0] for r in res.rows if r[1] == "FAIL"}

def test_item7_force_pending_subscriber_stops_write(tmp_path: Path) -> None:
    """Only PendingConfirmation subscriptions → update_function_configuration never called.

    Bad world (before fix): sub_ok result not stored; gate is
    `if not (enabled_ok and actions_ok and metric_ok)` — subscriber failure is ignored,
    override proceeds, Lambda env is modified even though nobody can receive alerts.
    After fix: `sub_ok = res.check(...)` stored and added to the gate;
    `update_function_configuration` is never called.
    Source: review W4 [item 7, force].
    """
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}

    # All subscriptions are PendingConfirmation — SubscriptionArn is NOT 'arn:...'
    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = {
        "Subscriptions": [
            {
                "SubscriptionArn": "PendingConfirmation",
                "Protocol": "email",
                "Endpoint": "ops@example.com",
                "TopicArn": _TOPIC_ARN,
            }
        ]
    }

    lam = MagicMock()
    lam.get_function_configuration.return_value = _good_lambda_cfg()
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()

    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")

    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
    ):
        res = Results()
        _item_7(res, **ctx)

    lam.update_function_configuration.assert_not_called()
    assert "7-topic-subscriber" in _failed_ids(res), (
        f"expected FAIL '7-topic-subscriber' with pending-only subs; rows={res.rows}"
    )


def test_item7_force_refuses_without_revision_id(tmp_path: Path) -> None:
    """No RevisionId in GetFunctionConfiguration -> 7-override FAIL, and no write at all.

    Live Lambda always returns RevisionId; without it the override would be a blind write
    over whatever is there. Kills cycle 2 mutant item7-revisionid-refusal-dropped.
    """
    cw = MagicMock()
    cw.describe_alarms.return_value = {"MetricAlarms": [_good_alarm()]}
    sns = MagicMock()
    sns.list_topics.return_value = _good_topic_list()
    sns.list_subscriptions_by_topic.return_value = _good_subscriptions()
    lam = MagicMock()
    cfg = _good_lambda_cfg()
    cfg.pop("RevisionId", None)
    lam.get_function_configuration.return_value = cfg
    lam.update_function_configuration.return_value = {}
    lam.invoke.return_value = _good_invoke_error()
    sched = MagicMock()
    ddb = MagicMock()
    ddb.query.return_value = _ddb_no_runs()
    ctx = _ctx(cw, sns, lam=lam, sched=sched, ddb=ddb, phase="force",
               tmp_path=tmp_path / "run")
    with (
        patch.object(_mod, "_client", side_effect=_make_client_factory(cw, sns, lam, sched, ddb)),
        patch.object(_mod, "_check_scheduler_timing", return_value=_sched_good()),
        patch.object(_mod, "_wait_fn_update"),
        patch.object(_mod, "_run_started_ats", return_value=set()),
    ):
        res = Results()
        _item_7(res, **ctx)
    rows = {r[0]: r[1] for r in res.rows}
    assert rows.get("7-override") == "FAIL", res.rows
    assert lam.update_function_configuration.call_count == 0, (
        lam.update_function_configuration.call_args_list)
    assert lam.invoke.call_count == 0, lam.invoke.call_args_list
    # nothing was overridden, so nothing is "restored" or reported as a concurrent write
    assert rows.get("7-env-restored") == "PASS", res.rows
    assert "7-restore" not in rows, res.rows
