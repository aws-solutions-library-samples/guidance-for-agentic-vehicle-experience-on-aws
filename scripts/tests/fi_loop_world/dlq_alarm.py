"""DLQ depth alarm for the investigations queue.

Fires when a message lands in the DLQ (ApproximateNumberOfMessagesVisible >= 1).
Records ``StateUpdate`` and ``Action`` history items (same shape as ``RunFailureAlarm``)
so the script's ``describe_alarm_history`` checks pass against a real
``DlqDepthAlarm``.

``pump(dlq_depth)`` is called by the world after each simulated SQS redrive.
``set_depth(depth)`` is called by the world's DLQ SQS intercepts whenever a message
is sent to or deleted from the DLQ; it records timestamps for time-based
auto-transition and calls ``pump`` immediately only when the explicit path ran first.
``describe_alarm_history(p)`` handles CloudWatch API calls routed via the world's
botocore router.

Live name: ``vsa-{stage}-avx-investigations-dlq-depth-{region}``
Live metric: Namespace=AWS/SQS, MetricName=ApproximateNumberOfMessagesVisible,
             Dimensions=[{QueueName: <dlq_name>}], Statistic=Maximum, Period=300,
             EvaluationPeriods=1, Threshold>=1.

Item-9 extensions (stage 2a)
-----------------------------
``set_depth(depth)`` — record depth-change timestamps for time-based auto-transition.
``_auto_transition()`` — fired lazily from ``describe_alarm_history``; transitions
   alarm state after ``alarm_delay_s`` (ALARM) or ``recover_delay_s`` (OK) of world-
   clock time has elapsed since the depth changed direction.
``never_fires`` / ``never_recovers`` — test switches that suppress transitions.
``set_already_alarm()`` — helper for pre-check SKIP tests; sets alarm to ALARM and
   appends a history StateUpdate + Action item at current world-clock time.
``add_before_run_state_update()`` — helper for link-3 history-item-before-run tests;
   appends a StateUpdate at current world-clock time (which will be before any
   ``send_at − 60 s`` the script uses as a StartDate filter).
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from scripts.tests.fi_rw_world.awsfake import Router


class DlqDepthAlarm:
    """Alarm for DLQ depth; fired when a message lands in the DLQ."""

    def __init__(
        self,
        world: Any,
        alarm_name: str,
        dlq_queue_name: str,
        topic_arn: str,
        *,
        alarm_delay_s: float = 30.0,
        recover_delay_s: float = 60.0,
        action_lag_s: float = 0.0,
    ) -> None:
        self.w = world
        self.name = alarm_name
        self.dlq_queue_name = dlq_queue_name
        self.topic_arn = topic_arn
        self.state = "OK"
        self.history: list[dict[str, Any]] = []
        self.action_state = "Succeeded"  # tests can set "Failed"

        # Time-based auto-transition parameters (item-9 extensions)
        self.alarm_delay_s = alarm_delay_s
        self.recover_delay_s = recover_delay_s
        # action_lag_s: how many world-clock seconds after the StateUpdate the Action
        # item becomes visible in describe_alarm_history.  When > 0, a single read
        # immediately after the StateUpdate returns nothing for HistoryItemType=Action
        # (simulating the CloudWatch write lag described in W2).
        self.action_lag_s = action_lag_s

        # Suppression switches (item-9 extensions); tests set these directly.
        self.never_fires: bool = False
        self.never_recovers: bool = False

        # W7 fake switch (lane-Y): when True, the alarm fires even after the DLQ
        # message is deleted (depth returns to 0) — simulating a delayed CloudWatch
        # metric that captures the visibility window before the delete.
        self.alarm_after_delete: bool = False

        # Internal depth-tracking for auto-transition
        self._dlq_depth: int = 0
        self._depth_became_positive_at: _dt.datetime | None = None
        self._depth_became_zero_at: _dt.datetime | None = None

    # ------------------------------------------------------------------
    # setup

    def create(self, cw: Any) -> None:
        self.w.router.all_calls.append(("_span", "_world_start", {}))
        try:
            cw.put_metric_alarm(
                AlarmName=self.name,
                MetricName="ApproximateNumberOfMessagesVisible",
                Namespace="AWS/SQS",
                Statistic="Maximum",
                Period=300,
                EvaluationPeriods=1,
                Threshold=1.0,
                ComparisonOperator="GreaterThanOrEqualToThreshold",
                TreatMissingData="notBreaching",
                Dimensions=[{"Name": "QueueName", "Value": self.dlq_queue_name}],
                AlarmActions=[self.topic_arn],
                AlarmDescription="DLQ depth alarm for investigations queue.",
            )
            cw.set_alarm_state(AlarmName=self.name, StateValue="OK", StateReason="world start")
        finally:
            self.w.router.all_calls.append(("_span", "_world_end", {}))

    # ------------------------------------------------------------------
    # item-9 helpers for test setup

    def set_already_alarm(self) -> None:
        """Configure the alarm to start in ALARM state.

        Used by pre-check SKIP tests: the script reads the alarm, finds it
        already ALARM, and returns SKIP rc 2 without writing anything.
        Adds a StateUpdate and Action history item at the current world-clock
        time so ``describe_alarm_history`` returns a non-empty list.
        """
    def set_already_alarm(self) -> None:
        """Configure the alarm to start in ALARM state.

        Used by pre-check SKIP tests: the script reads the alarm, finds it
        already ALARM, and returns SKIP rc 2 without writing anything.
        Adds a StateUpdate and Action history item at the current world-clock
        time so ``describe_alarm_history`` returns a non-empty list.
        """
        self.w.router.all_calls.append(("_span", "_world_start", {}))
        try:
            cw = self.w.internal("cloudwatch")
            cw.set_alarm_state(
                AlarmName=self.name,
                StateValue="ALARM",
                StateReason="world: pre-set to ALARM for test",
            )
        finally:
            self.w.router.all_calls.append(("_span", "_world_end", {}))
        old_state, self.state = self.state, "ALARM"
        ts = self.w.clock.now()
        self.history.append({
            "AlarmName": self.name,
            "AlarmType": "MetricAlarm",
            "Timestamp": ts,
            "HistoryItemType": "StateUpdate",
            "HistorySummary": f"Alarm updated from {old_state} to ALARM",
            "HistoryData": json.dumps({
                "oldState": {"stateValue": old_state},
                "newState": {"stateValue": "ALARM"},
            }),
        })
        self.history.append({
            "AlarmName": self.name,
            "AlarmType": "MetricAlarm",
            "Timestamp": ts,
            "HistoryItemType": "Action",
            "HistorySummary": f"Successfully executed action {self.topic_arn}",
            "HistoryData": json.dumps({
                "actionState": self.action_state,
                "notificationResource": self.topic_arn,
                "publishedMessage": json.dumps({
                    "default": json.dumps({
                        "AlarmName": self.name,
                        "NewStateValue": "ALARM",
                        "OldStateValue": old_state,
                    })
                }),
                "error": None,
            }),
        })

    def add_before_run_state_update(self) -> None:
        """Add a StateUpdate item well before the run's send_at.

        Used by link-3 tests for the "history item only before the run" scenario:
        the item is placed 120 s before the current world-clock time so it falls
        before any ``send_at − 60 s`` StartDate the script passes, and is filtered
        out correctly.  Calling this before the script runs ensures the item's
        timestamp is earlier than the send_at the script records.
        """
        ts = self.w.clock.now() - _dt.timedelta(seconds=120)
        self.history.append({
            "AlarmName": self.name,
            "AlarmType": "MetricAlarm",
            "Timestamp": ts,
            "HistoryItemType": "StateUpdate",
            "HistorySummary": "Alarm updated from OK to ALARM (pre-run fixture)",
            "HistoryData": json.dumps({
                "oldState": {"stateValue": "OK"},
                "newState": {"stateValue": "ALARM"},
            }),
        })

    # ------------------------------------------------------------------
    # item-9 time-based depth tracking

    def set_depth(self, depth: int) -> None:
        """Update the tracked DLQ depth and record direction-change timestamps.

        Called by the world's DLQ SQS SendMessage / DeleteMessage intercepts
        whenever a message is added to or removed from the DLQ.  The timestamps
        drive ``_auto_transition``'s clock-based ALARM / OK transitions.
        """
        old = self._dlq_depth
        self._dlq_depth = depth
        now = self.w.clock.now()
        if old == 0 and depth > 0:
            # DLQ became non-empty for the first time (or after an empty period)
            self._depth_became_positive_at = now
            self._depth_became_zero_at = None
        elif old > 0 and depth == 0:
            # DLQ became empty
            self._depth_became_zero_at = now
            # When alarm_after_delete is set the alarm must still fire after the
            # delete (simulating a delayed CloudWatch metric that captured the
            # visibility window).  Keeping _depth_became_positive_at lets
            # _auto_transition fire the ALARM even after depth drops to zero.
            if not self.alarm_after_delete:
                self._depth_became_positive_at = None

    def _auto_transition(self) -> None:
        """Lazily fire ALARM or recover to OK based on world-clock elapsed time.

        Called from ``describe_alarm_history`` so polling tests see state changes
        without explicit ``pump()`` calls.  The script's ``time.sleep(30)``
        (patched via ScriptTime) advances the world clock, so each poll iteration
        moves the clock by 30 s; after ``alarm_delay_s / 30`` iterations the
        alarm fires.
        """
        now = self.w.clock.now()

        # Transition OK → ALARM: fires when depth > 0, or when alarm_after_delete
        # is set (depth already 0 after delete, but we still fire — simulates a
        # delayed CloudWatch metric that captured the visibility window).
        if (
            self.state == "OK"
            and (self._dlq_depth > 0 or self.alarm_after_delete)
            and not self.never_fires
            and self._depth_became_positive_at is not None
        ):
            elapsed = (now - self._depth_became_positive_at).total_seconds()
            if elapsed >= self.alarm_delay_s:
                self.pump(max(self._dlq_depth, 1))
                # S20: after alarm_after_delete fires (depth=0), clear the epoch
                # so the alarm fires exactly once — the next poll won't re-trigger.
                if self._dlq_depth == 0:
                    self._depth_became_positive_at = None

        # Transition ALARM → OK
        elif (
            self.state == "ALARM"
            and self._dlq_depth == 0
            and not self.never_recovers
            and self._depth_became_zero_at is not None
        ):
            elapsed = (now - self._depth_became_zero_at).total_seconds()
            if elapsed >= self.recover_delay_s:
                self.pump(0)

    # ------------------------------------------------------------------
    # core alarm mechanics

    def pump(self, dlq_depth: int) -> None:
        """Update alarm state from the DLQ depth; record history items."""
        new = "ALARM" if dlq_depth >= 1 else "OK"
        if new == self.state:
            return
        old, self.state = self.state, new
        self.w.router.all_calls.append(("_span", "_world_start", {}))
        try:
            cw = self.w.internal("cloudwatch")
            cw.set_alarm_state(
                AlarmName=self.name,
                StateValue=new,
                StateReason=f"world: DLQ depth={dlq_depth} → {new}",
            )
            ts = self.w.clock.now()
            action_ts = (
                ts + _dt.timedelta(seconds=self.action_lag_s)
                if self.action_lag_s > 0 else ts
            )
            self.history.append({
                "AlarmName": self.name,
                "AlarmType": "MetricAlarm",
                "Timestamp": ts,
                "HistoryItemType": "StateUpdate",
                "HistorySummary": f"Alarm updated from {old} to {new}",
                "HistoryData": json.dumps({
                    "oldState": {"stateValue": old},
                    "newState": {"stateValue": new},
                }),
            })
            alarm = cw.describe_alarms(AlarmNames=[self.name])["MetricAlarms"][0]
            # Use self.topic_arn for ALARM actions so tests that change it after
            # creation (test_g5_action_wrong_topic) see the right ARN in history.
            if new == "ALARM":
                targets = [self.topic_arn] if self.topic_arn else []
            else:
                targets = alarm.get("OKActions") or []
            for target in targets:
                self.history.append({
                    "AlarmName": self.name,
                    "AlarmType": "MetricAlarm",
                    "Timestamp": action_ts,
                    "HistoryItemType": "Action",
                    "HistorySummary": (
                        f"Successfully executed action {target}"
                        if self.action_state == "Succeeded"
                        else f"Failed to execute action {target}"
                    ),
                    "HistoryData": json.dumps({
                        "actionState": self.action_state,
                        "notificationResource": target,
                    "publishedMessage": json.dumps({
                        "default": json.dumps({
                            "AlarmName": self.name,
                            "NewStateValue": new,
                            "OldStateValue": old,
                        })
                    }),
                    "error": None,
                }),
            })
        finally:
            self.w.router.all_calls.append(("_span", "_world_end", {}))

    def describe_alarm_history(self, p: dict[str, Any]) -> dict[str, Any]:
        # Auto-transition lazily so poll tests see state changes as the world
        # clock advances between calls.
        self._auto_transition()

        now = self.w.clock.now()
        items = [
            h for h in self.history
            if h["AlarmName"] == p.get("AlarmName", self.name)
            # Only include items whose timestamp has been "reached" by the world
            # clock.  Normally all items satisfy this (action_lag_s == 0);
            # when action_lag_s > 0 the Action item's timestamp is in the future
            # relative to the moment pump() was called, hiding it until enough
            # sleep calls advance the clock (W2 fake).
            and h["Timestamp"] <= now
        ]
        if p.get("HistoryItemType"):
            items = [h for h in items if h["HistoryItemType"] == p["HistoryItemType"]]
        start, end = p.get("StartDate"), p.get("EndDate")
        if isinstance(start, _dt.datetime):
            items = [h for h in items if h["Timestamp"] >= start]
        if isinstance(end, _dt.datetime):
            items = [h for h in items if h["Timestamp"] <= end]
        items.sort(
            key=lambda h: h["Timestamp"],
            reverse=p.get("ScanBy") != "TimestampAscending",
        )
        return {"AlarmHistoryItems": items[: int(p.get("MaxRecords") or 100)]}

    def register(self, router: Router, other_alarm: Any = None) -> None:
        """Register ``DescribeAlarmHistory``; multiplexes with *other_alarm* when given."""
        if other_alarm is not None:
            dlq_self = self

            def _dispatch(p: dict[str, Any]) -> dict[str, Any]:
                if p.get("AlarmName") == dlq_self.name:
                    return dlq_self.describe_alarm_history(p)
                return other_alarm.describe_alarm_history(p)

            router.add("cloudwatch", "DescribeAlarmHistory", _dispatch)
        else:
            router.add("cloudwatch", "DescribeAlarmHistory", self.describe_alarm_history)
