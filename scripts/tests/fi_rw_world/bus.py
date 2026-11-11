"""Event delivery between the real producers and the real consumers.

EventBridge: the live rule ``vsa-staging-avx-ro-status-changed`` on bus
``dms-staging-events`` (``events list-rules``, 2026-10-10) matches
``source=dms.service-lane``, ``detail-type=dms.ro.status_changed`` and the account.
Matching ``PutEvents`` entries from DMS's real publisher are delivered, on the next
world pump, to the real AVX consumer (``lambdas/api-avx-dms-event-consumer``).

Findings stream: the broker (``agents.supervisor.lambdas.avx_broker.handler``) is a
DynamoDB-stream consumer of ``vsa-{stage}-avx-findings`` (NEW_IMAGE). On every pump
the table is diffed against the last snapshot and new or changed items go to the
real broker as INSERT/MODIFY records; the broker's log lines land in moto's
CloudWatch Logs group ``/aws/lambda/vsa-{stage}-avx-broker-{region}`` with Lambda's
default line format and world-clock timestamps.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import uuid
from typing import Any, Callable

from scripts.tests.fi_rw_world.awsfake import Router


class EventBus:
    def __init__(self, world: Any, account: str, consumer: Callable[[dict, Any], Any]) -> None:
        self.w = world
        self.account = account
        self.consumer = consumer
        self.entries: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []

    def put_events(self, p: dict[str, Any]) -> dict[str, Any]:
        out = []
        for e in p.get("Entries") or []:
            self.entries.append(e)
            eid = str(uuid.uuid4())
            out.append({"EventId": eid})
            if (e.get("Source") == "dms.service-lane"
                    and e.get("DetailType") == "dms.ro.status_changed"
                    and e.get("EventBusName") in ("dms-staging-events", None, "")):
                self._pending.append({
                    "version": "0", "id": eid, "detail-type": e["DetailType"],
                    "source": e["Source"], "account": self.account,
                    "time": self.w.clock.iso(), "region": self.w.region, "resources": [],
                    "detail": json.loads(e.get("Detail") or "{}")})
        return {"FailedEntryCount": 0, "Entries": out}

    def pump(self) -> None:
        pending, self._pending = self._pending, []
        for ev in pending:
            self.delivered.append({"event": ev, "result": self.consumer(ev, None)})

    def register(self, router: Router) -> None:
        router.add("events", "PutEvents", self.put_events)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class FindingsStream:
    def __init__(self, world: Any, table: str, broker: Callable[[dict, Any], Any],
                 log_group: str) -> None:
        self.w = world
        self.table, self.broker, self.log_group = table, broker, log_group
        self._seen: dict[str, str] = {}
        self.batches: list[dict[str, Any]] = []

    def _scan(self) -> list[dict[str, Any]]:
        ddb = self.w.internal("dynamodb")
        items, kw = [], {"TableName": self.table}
        while True:
            r = ddb.scan(**kw)
            items += r.get("Items", [])
            if not r.get("LastEvaluatedKey"):
                return items
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def prime(self) -> None:
        for it in self._scan():
            self._seen[it["finding_id"]["S"]] = hashlib.sha256(
                json.dumps(it, sort_keys=True).encode()).hexdigest()

    def pump(self) -> None:
        records = []
        for it in self._scan():
            fid = it["finding_id"]["S"]
            h = hashlib.sha256(json.dumps(it, sort_keys=True).encode()).hexdigest()
            if self._seen.get(fid) == h:
                continue
            records.append({"eventName": "MODIFY" if fid in self._seen else "INSERT",
                            "eventSource": "aws:dynamodb",
                            "dynamodb": {"NewImage": it, "Keys": {"finding_id": it["finding_id"]},
                                         "SequenceNumber": str(len(self.batches) * 1000 + len(records)),
                                         "StreamViewType": "NEW_IMAGE"}})
            self._seen[fid] = h
        if not records:
            return
        cap = _Capture()
        log = logging.getLogger()
        old_level = log.level
        log.addHandler(cap)
        log.setLevel(logging.INFO)
        try:
            result = self.broker({"Records": records}, None)
        finally:
            log.removeHandler(cap)
            log.setLevel(old_level)
        self.batches.append({"records": len(records), "result": result, "lines": cap.lines})
        logs = self.w.internal("logs")
        stream = f"{self.w.clock.now():%Y/%m/%d}/[$LATEST]{uuid.uuid4().hex}"
        logs.create_log_stream(logGroupName=self.log_group, logStreamName=stream)
        ts = int(self.w.clock.epoch() * 1000)
        rid = str(uuid.uuid4())
        when = _dt.datetime.fromtimestamp(ts / 1000, _dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        events = [{"timestamp": ts, "message": f"[INFO]\t{when}\t{rid}\t{line}\n"}
                  for line in cap.lines]
        if events:
            logs.put_log_events(logGroupName=self.log_group, logStreamName=stream,
                                logEvents=events)
