"""StalenessAlarm — CloudWatch staleness alarm for CVX/Tier2::RunRecordsWritten.

Mirrors fi_rw_world/alarm.py:RunFailureAlarm for the service component.

Alarm definition: vsa-tier2-stack.ts:2249 runStalenessWindows entry for
'service'.  FILL(m1,0) at period=1h; fires when all 27 consecutive 1-hour
periods have value < 1 (i.e., no heartbeat emitted).

observability.emit_run_record_heartbeat() emits MetricName=RunRecordsWritten
Namespace=CVX/Tier2 Dimensions=[stage=staging, component=service].  The
StalenessAlarm captures those PutMetricData calls via the Router and evaluates
the 27-period window on every world pump.

moto does not implement DescribeAlarmHistory; this class records history and
serves it via the Router's cloudwatch.DescribeAlarmHistory handler.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from scripts.tests.fi_rw_world.awsfake import Router

_PERIOD = 3600       # 1 hour
_EVAL_PERIODS = 27   # source: vsa-tier2-stack.ts:2249 emptyHours=27
_NAMESPACE = "CVX/Tier2"
_METRIC = "RunRecordsWritten"
_COMPONENT = "service"


class StalenessAlarm:
    """FILL(m1,0) < 1 for 27 consecutive 1-hour periods → ALARM.

    Fires when the service component has emitted no RunRecordsWritten heartbeat
    in the last 27 complete 1-hour buckets.
    """

    def __init__(self, world: Any, alarm_name: str, topic_arn: str,
                 stage: str, region: str) -> None:
        self.w = world
        self.name = alarm_name
        self.topic_arn = topic_arn
        self.stage = stage
        self.region = region
        self.state = "OK"
        self.history: list[dict[str, Any]] = []
        self.action_state = "Succeeded"           # tests may set "Failed"
        self._heartbeat_epochs: list[float] = []  # epoch of each service heartbeat

    def create(self, cw: Any) -> None:
        """Register the alarm in moto and seed it at OK."""
        cw.put_metric_alarm(
            AlarmName=self.name,
            AlarmDescription=(
                f"Tier 2 (service): no run record stored in "
                f"vsa-{self.stage}-avx-agent-runs for {_EVAL_PERIODS} hours."
            ),
            Metrics=[
                {
                    "Id": "m1",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": _NAMESPACE,
                            "MetricName": _METRIC,
                            "Dimensions": [
                                {"Name": "stage", "Value": self.stage},
                                {"Name": "component", "Value": _COMPONENT},
                            ],
                        },
                        "Period": _PERIOD,
                        "Stat": "Sum",
                    },
                },
                {"Id": "e1", "Expression": "FILL(m1,0)"},
            ],
            EvaluationPeriods=_EVAL_PERIODS,
            DatapointsToAlarm=_EVAL_PERIODS,
            Threshold=1.0,
            ComparisonOperator="LessThanThreshold",
            TreatMissingData="breaching",
            AlarmActions=[self.topic_arn],
            OKActions=[self.topic_arn],
            ActionsEnabled=True,
        )
        cw.set_alarm_state(AlarmName=self.name, StateValue="OK",
                           StateReason="world start")

    # ------------------------------------------------------------------
    # heartbeat capture
    # ------------------------------------------------------------------

    def _put_metric_data_handler(self, params: dict[str, Any]) -> None:
        """Observe PutMetricData; record service RunRecordsWritten; let moto serve."""
        if params.get("Namespace") != _NAMESPACE:
            return None
        for datum in params.get("MetricData", []):
            if datum.get("MetricName") != _METRIC:
                continue
            dims = {d["Name"]: d["Value"] for d in datum.get("Dimensions", [])}
            if dims.get("component") == _COMPONENT:
                self._heartbeat_epochs.append(self.w.clock.epoch())
        return None  # let moto process the put_metric_data

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------

    def _breaching(self, now: float) -> bool:
        """True iff all 27 most-recent complete 1-hour buckets have no heartbeat.

        Evaluates [bucket_now-27h, bucket_now); the current incomplete bucket
        [bucket_now, bucket_now+3600) is excluded, matching FILL(m1,0) semantics.
        """
        bucket_now = int(now // _PERIOD) * _PERIOD
        window_start = bucket_now - _EVAL_PERIODS * _PERIOD
        return not any(window_start <= t < bucket_now
                       for t in self._heartbeat_epochs)

    def pump(self) -> None:
        """Evaluate state; emit StateUpdate + Action history on transition."""
        new = "ALARM" if self._breaching(self.w.clock.epoch()) else "OK"
        if new == self.state:
            return
        old, self.state = self.state, new
        cw = self.w.internal("cloudwatch")
        cw.set_alarm_state(AlarmName=self.name, StateValue=new,
                           StateReason=f"world: RunRecordsWritten {new}")
        ts = self.w.clock.now()
        self.history.append({
            "AlarmName": self.name, "AlarmType": "MetricAlarm",
            "Timestamp": ts, "HistoryItemType": "StateUpdate",
            "HistorySummary": f"Alarm updated from {old} to {new}",
            "HistoryData": json.dumps({"oldState": {"stateValue": old},
                                       "newState": {"stateValue": new}}),
        })
        alarm = cw.describe_alarms(AlarmNames=[self.name])["MetricAlarms"][0]
        targets = alarm.get("AlarmActions" if new == "ALARM" else "OKActions") or []
        for target in targets:
            self.history.append({
                "AlarmName": self.name, "AlarmType": "MetricAlarm",
                "Timestamp": ts, "HistoryItemType": "Action",
                "HistorySummary": (
                    f"Successfully executed action {target}"
                    if self.action_state == "Succeeded"
                    else f"Failed to execute action {target}"
                ),
                "HistoryData": json.dumps({
                    "actionState": self.action_state,
                    "notificationResource": target,
                    "publishedMessage": json.dumps(
                        {"default": json.dumps({"AlarmName": self.name,
                                               "NewStateValue": new})}),
                    "error": None,
                }),
            })

    # ------------------------------------------------------------------
    # DescribeAlarmHistory handler (not in moto)
    # ------------------------------------------------------------------

    def describe_alarm_history(self, p: dict[str, Any]) -> dict[str, Any]:
        """Serve DescribeAlarmHistory for the staleness alarm."""
        items = [h for h in self.history
                 if h["AlarmName"] == p.get("AlarmName", self.name)]
        if p.get("HistoryItemType"):
            items = [h for h in items
                     if h["HistoryItemType"] == p["HistoryItemType"]]
        start, end = p.get("StartDate"), p.get("EndDate")
        if isinstance(start, _dt.datetime):
            items = [h for h in items if h["Timestamp"] >= start]
        if isinstance(end, _dt.datetime):
            items = [h for h in items if h["Timestamp"] <= end]
        items.sort(key=lambda h: h["Timestamp"],
                   reverse=p.get("ScanBy") != "TimestampAscending")
        return {"AlarmHistoryItems": items[: int(p.get("MaxRecords") or 100)]}

    def register(self, router: Router) -> None:
        """Register DescribeAlarmHistory and PutMetricData handlers."""
        router.add("cloudwatch", "DescribeAlarmHistory", self.describe_alarm_history)
        router.add("cloudwatch", "PutMetricData", self._put_metric_data_handler)
