"""The Tier 2 run-failure alarm, driven by the health-agent Lambda's failed attempts.

Live definition (``describe-alarms``, 2026-10-10): ``Errors`` (AWS/Lambda, Sum) on
``FunctionName=vsa-staging-tier2-health-agent-us-west-2``, Period 300, 1 evaluation
period, threshold >= 1, ``TreatMissingData=notBreaching``; ``AlarmActions`` and
``OKActions`` both the operator topic. Live history (2026-10-09): an error at 22:26
gave ALARM at 22:31:23 and OK at 22:39:23, each with one ``Action`` item whose
``HistoryData`` has ``actionState: Succeeded`` and the published message. This model
evaluates the last complete 5-minute period, 60 s after it ends, on every world pump;
moto holds the state (``SetAlarmState``) and ``DescribeAlarmHistory`` (not in moto)
is served from the items recorded here.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from scripts.tests.fi_rw_world.awsfake import Router

_PERIOD = 300
_DELAY = 60


class RunFailureAlarm:
    def __init__(self, world: Any, alarm_name: str, function_name: str, topic_arn: str) -> None:
        self.w = world
        self.name, self.function, self.topic_arn = alarm_name, function_name, topic_arn
        self.state = "OK"
        self.history: list[dict[str, Any]] = []
        self.action_state = "Succeeded"   # tests can set "Failed"

    def create(self, cw: Any) -> None:
        cw.put_metric_alarm(
            AlarmName=self.name, MetricName="Errors", Namespace="AWS/Lambda",
            Statistic="Sum", Period=_PERIOD, EvaluationPeriods=1, Threshold=1.0,
            ComparisonOperator="GreaterThanOrEqualToThreshold", TreatMissingData="notBreaching",
            Dimensions=[{"Name": "FunctionName", "Value": self.function}],
            AlarmActions=[self.topic_arn], OKActions=[self.topic_arn],
            AlarmDescription="Tier 2 health agent Lambda error rate > 0 in a 5-minute window.")
        cw.set_alarm_state(AlarmName=self.name, StateValue="OK", StateReason="world start")

    def redefine(self, cw: Any, *, alarm_actions: list[str], ok_actions: list[str]) -> None:
        """Change the alarm's actions (as a stack update would); state stays as it is."""
        cw.put_metric_alarm(
            AlarmName=self.name, MetricName="Errors", Namespace="AWS/Lambda",
            Statistic="Sum", Period=_PERIOD, EvaluationPeriods=1, Threshold=1.0,
            ComparisonOperator="GreaterThanOrEqualToThreshold", TreatMissingData="notBreaching",
            Dimensions=[{"Name": "FunctionName", "Value": self.function}],
            AlarmActions=alarm_actions, OKActions=ok_actions,
            AlarmDescription="Tier 2 health agent Lambda error rate > 0 in a 5-minute window.")
        cw.set_alarm_state(AlarmName=self.name, StateValue=self.state, StateReason="world redefine")

    def _breaching(self, now: float) -> bool:
        end = ((now - _DELAY) // _PERIOD) * _PERIOD
        start = end - _PERIOD
        return any(fn == self.function and start <= t < end for fn, t in self.w.lam.errors)

    def pump(self) -> None:
        new = "ALARM" if self._breaching(self.w.clock.epoch()) else "OK"
        if new == self.state:
            return
        old, self.state = self.state, new
        cw = self.w.internal("cloudwatch")
        cw.set_alarm_state(
            AlarmName=self.name, StateValue=new, StateReason=f"world: Errors period {new}")
        ts = self.w.clock.now()
        self.history.append({
            "AlarmName": self.name, "AlarmType": "MetricAlarm", "Timestamp": ts,
            "HistoryItemType": "StateUpdate",
            "HistorySummary": f"Alarm updated from {old} to {new}",
            "HistoryData": json.dumps({"oldState": {"stateValue": old},
                                       "newState": {"stateValue": new}})})
        message = json.dumps({"AlarmName": self.name, "NewStateValue": new,
                              "OldStateValue": old})
        # One Action item per configured action of the new state, as CloudWatch does;
        # an alarm with no actions for the state records none.
        alarm = cw.describe_alarms(AlarmNames=[self.name])["MetricAlarms"][0]
        targets = alarm.get("AlarmActions" if new == "ALARM" else "OKActions") or []
        for target in targets:
            self.history.append({
                "AlarmName": self.name, "AlarmType": "MetricAlarm", "Timestamp": ts,
                "HistoryItemType": "Action",
                "HistorySummary": (f"Successfully executed action {target}"
                                   if self.action_state == "Succeeded"
                                   else f"Failed to execute action {target}"),
                "HistoryData": json.dumps({"actionState": self.action_state,
                                           "notificationResource": target,
                                           "publishedMessage": json.dumps({"default": message}),
                                           "error": None})})

    def describe_alarm_history(self, p: dict[str, Any]) -> dict[str, Any]:
        items = [h for h in self.history if h["AlarmName"] == p.get("AlarmName", self.name)]
        if p.get("HistoryItemType"):
            items = [h for h in items if h["HistoryItemType"] == p["HistoryItemType"]]
        start, end = p.get("StartDate"), p.get("EndDate")
        if isinstance(start, _dt.datetime):
            items = [h for h in items if h["Timestamp"] >= start]
        if isinstance(end, _dt.datetime):
            items = [h for h in items if h["Timestamp"] <= end]
        items.sort(key=lambda h: h["Timestamp"], reverse=p.get("ScanBy") != "TimestampAscending")
        return {"AlarmHistoryItems": items[: int(p.get("MaxRecords") or 100)]}

    def register(self, router: Router) -> None:
        router.add("cloudwatch", "DescribeAlarmHistory", self.describe_alarm_history)
