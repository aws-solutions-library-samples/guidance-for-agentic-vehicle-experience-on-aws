"""A fake staging for ``live-verify-fi-rebalancing.py --part agent``, built from the real code.

Review cycle 1 W7: the first tests built findings, run records and alarm history by hand, so
the checks passed on shapes the real code never writes. Here:

- DynamoDB, S3, SSM and Cognito are moto (real request/response shapes; real Query, Scan, GSI
  and paging semantics). Tables carry the live key schemas (``vsa-tier2-stack.ts``).
- The Tier 2 Lambda is the real ``agents.tier2.runner.run_rebalance`` with the real
  ``ArtifactWriter`` and ``write_run_record`` against moto, so every finding and run record the
  script reads was written by the production writer. The agent's ADP reads are served from the
  fixture below (the shapes ``agents/fi_rebalancing/tests/test_agent.py`` binds against the real
  reader signatures); its CMS location read is the real ``sources.read_locations`` against moto,
  and its live-hash read is the real ``write.read_live_hash`` (what makes a re-run idempotent).
- Athena, for the script's own reads, serves the same fixture as Athena returns it: every value
  a string, header row first, two rows per page. A gaps query without ``horizon_days = 7`` in
  both places is refused (C2c).
- The run-failure alarm, its history, the operator topic's subscriptions and the broker's log
  are small fakes holding the live shapes: the alarm definition and an ``Action`` history item
  captured 2026-10-09 (``HistoryData.actionState`` "Succeeded", the summary "Successfully
  executed action ..."), and the broker's own format string
  ``broker: skipping — fi_item finding_id=%s kind=%s agent_id=%s``.

What is not real code: the agent's ADP reads (fixture) and the CloudWatch/SNS/Logs fakes.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import threading
import http.server
from typing import Any, Iterator

STAGE, REGION, ADP_REGION, ACCOUNT = "staging", "us-west-2", "us-east-1", "123456789012"
FN = f"vsa-{STAGE}-tier2-health-agent-{REGION}"
FINDINGS = f"vsa-{STAGE}-avx-findings"
RUNS = f"vsa-{STAGE}-avx-agent-runs"
LOCS = f"cms-{STAGE}-storage-fleet-locations"
LAKE = f"adp-{STAGE}-foundation-lake-{ACCOUNT}-{ADP_REGION}"
PLANTS_KEY = "restricted/fleet_bookings/answer_key/plants/fleet_bookings_answer_key.json"
ALARM = f"vsa-{STAGE}-tier2-run-failure-{REGION}"
TOPIC_ARN = f"arn:aws:sns:{REGION}:{ACCOUNT}:vsa-{STAGE}-tier2-operator-alerts"
RENTAL, DELIVERY = "demo-rental-carshare", "demo-lastmile-delivery"
POLICY_PARAM = f"/vsa/{STAGE}/avx/fi-rebalancing/policy"

# Two metros. In metro "sea", loc-def (airport) is short and loc-sur (downtown) has spare
# vehicles, so the run moves one vehicle; in "phx", loc-nsp is short with nothing spare, so the
# run writes a gap item (the answer key's no-spare plant).
LOCATIONS = [
    {"locationId": "loc-def", "name": "SEA Airport", "metro": "sea", "locationType": "airport",
     "latitude": "47.40", "longitude": "-122.30", "parkingSpaces": "20", "operatingHours": "8-18"},
    {"locationId": "loc-sur", "name": "SEA Downtown", "metro": "sea", "locationType": "downtown",
     "latitude": "47.45", "longitude": "-122.32", "parkingSpaces": "20", "operatingHours": "8-18"},
    {"locationId": "loc-nsp", "name": "PHX Hub 2", "metro": "phx", "locationType": "carshare_hub",
     "latitude": "33.45", "longitude": "-112.07", "parkingSpaces": "10", "operatingHours": "6-22"},
]
VINS = {"VINELIG001": "loc-sur", "VINELIG002": "loc-sur", "VINDEF001": "loc-def",
        "VINNSP001": "loc-nsp"}


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def gaps(today: dt.date) -> list[dict]:
    d = today.isoformat()
    return [
        {"location_id": "loc-def", "status": "deficit", "vehicles_short": 1, "vehicles_spare": 0,
         "supply_vehicles": 3, "forecast_hours": 45.0, "forecast_utilization_pct": 0.95,
         "data_as_of": d},
        {"location_id": "loc-sur", "status": "surplus", "vehicles_short": 0, "vehicles_spare": 2,
         "supply_vehicles": 10, "forecast_hours": 20.0, "forecast_utilization_pct": 0.20,
         "data_as_of": d},
        {"location_id": "loc-nsp", "status": "deficit", "vehicles_short": 2, "vehicles_spare": 0,
         "supply_vehicles": 2, "forecast_hours": 40.0, "forecast_utilization_pct": 1.25,
         "data_as_of": d},
    ]


def forecast(today: dt.date) -> list[dict]:
    hours = {"loc-def": 45.0, "loc-sur": 20.0, "loc-nsp": 40.0}
    return [{"location_id": loc, "forecast_date": (today + dt.timedelta(days=k)).isoformat(),
             "forecast_hours": h, "method": "ml", "mape_28d": 0.10,
             "run_date": today.isoformat(), "data_as_of": today.isoformat()}
            for loc, h in hours.items() for k in range(1, 10)]


def plants(today: dt.date) -> dict:
    start = (today - dt.timedelta(days=3)).isoformat()
    return {"plants": [
        {"location_id": "loc-def", "start_date": start, "factor": 1.4, "kind": "demand_surge",
         "note": "conference week"},
        {"location_id": "loc-nsp", "start_date": start, "factor": 1.5, "kind": "demand_surge",
         "note": "no-spare-vehicle: Phoenix hub-2 (spec D10)"},
        {"location_id": "loc-sur", "start_date": start, "factor": 0.6, "kind": "demand_drop",
         "note": "drop"},
    ]}



class FakeTime:
    """The script's ``time`` module with a clock that ``sleep`` advances, so its poll loops
    end at their deadlines without real waiting."""

    def __init__(self) -> None:
        import time as _t
        self._t, self.offset, self.slept = _t, 0.0, 0.0

    def time(self) -> float:
        return self._t.time() + self.offset

    def monotonic(self) -> float:
        return self._t.monotonic() + self.offset

    def sleep(self, s: float) -> None:
        self.offset += s
        self.slept += s


# ---------------------------------------------------------------------------
# moto resources (live key schemas)
# ---------------------------------------------------------------------------


def create_resources(boto3_session: Any, today: dt.date, policy_value: str) -> None:
    """Tables, the plants key, the policy parameter, the CMS pool and its client and group."""
    ddb = boto3_session.client("dynamodb", region_name=REGION)
    s = lambda n: {"AttributeName": n, "AttributeType": "S"}  # noqa: E731
    gsi = lambda name, pk, sk=None: {  # noqa: E731
        "IndexName": name, "Projection": {"ProjectionType": "ALL"},
        "KeySchema": [{"AttributeName": pk, "KeyType": "HASH"}]
        + ([{"AttributeName": sk, "KeyType": "RANGE"}] if sk else [])}
    ddb.create_table(
        TableName=FINDINGS, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[s(n) for n in ("finding_id", "owner_id", "vin", "vehicle_id",
                                             "fleet_id", "dealership_id", "district_id",
                                             "agent_id", "computed_at", "finding_key")],
        KeySchema=[{"AttributeName": "finding_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[gsi("by_owner", "owner_id"), gsi("by_vin", "vin"),
                                gsi("by_vehicle", "vehicle_id"), gsi("by_fleet", "fleet_id"),
                                gsi("by_dealership", "dealership_id"),
                                gsi("by_district", "district_id"),
                                gsi("by_agent_computed_at", "agent_id", "computed_at"),
                                gsi("by_finding_key", "finding_key")])
    ddb.create_table(
        TableName=RUNS, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[s("agent_component"), s("started_at")],
        KeySchema=[{"AttributeName": "agent_component", "KeyType": "HASH"},
                   {"AttributeName": "started_at", "KeyType": "RANGE"}])
    ddb.create_table(
        TableName=LOCS, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[s("fleetId"), s("locationId")],
        KeySchema=[{"AttributeName": "fleetId", "KeyType": "HASH"},
                   {"AttributeName": "locationId", "KeyType": "RANGE"}])
    for loc in LOCATIONS:
        item = {"fleetId": {"S": RENTAL}}
        for k, v in loc.items():
            item[k] = {"N": v} if k in ("latitude", "longitude", "parkingSpaces") else {"S": v}
        ddb.put_item(TableName=LOCS, Item=item)

    s3 = boto3_session.client("s3", region_name=ADP_REGION)
    s3.create_bucket(Bucket=LAKE)
    s3.put_object(Bucket=LAKE, Key=PLANTS_KEY, Body=json.dumps(plants(today)).encode())

    boto3_session.client("ssm", region_name=REGION).put_parameter(
        Name=POLICY_PARAM, Value=policy_value, Type="String")



# ---------------------------------------------------------------------------
# The Tier 2 Lambda: the real runner against moto
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def agent_reads(monkeypatch: Any, today: dt.date, *, gaps_rows: list | None = None) -> Iterator[None]:
    """Serve the agent's ADP reads from the fixture; keep its CMS and live-hash reads real."""
    import inspect

    from agents.fi_rebalancing import sources as src

    g = gaps(today) if gaps_rows is None else gaps_rows
    f = forecast(today)
    vd = [{"vin": v, "location_id": loc, "available_hours": 10.0} for v, loc in VINS.items()]
    util = [{"vin": v, "snapshot_date": today.isoformat(), "available_hours": 10.0,
             "on_rent_hours": 0.0} for v in VINS]

    def bound(real, value):
        def fake(*a, **kw):
            inspect.signature(real).bind(*a, **kw)
            return value(*a, **kw) if callable(value) else value
        return fake

    patches = {
        "read_gaps": g, "read_forecast_days": f, "read_outcomes": [],
        "read_vehicle_days": vd, "read_vehicle_utilization": util,
        "read_busy_windows": [], "read_events": [], "read_open_ros": [],
        "read_service_soon": [],
        "read_request_lengths": [{"location_id": "loc-def", "mean_request_hours": 24.0},
                                 {"location_id": "loc-nsp", "mean_request_hours": 12.0}],
        "read_grounding": (False, False),
        "read_open_items": ([], False),
    }
    for name, value in patches.items():
        monkeypatch.setattr(src, name, bound(getattr(src, name), value))
    yield


class FakeBedrock:
    """``bedrock-runtime`` for the agent's model path (G6 G3, prompt 40).

    Converse replies are built from the request's own item context (the last line of the user
    text, which is the bundle JSON), so they name the run's real candidate and gap ids:

    - ``"move"``: one move on the first candidate with its first eligible VIN.
    - ``"invalid"``: a move naming a VIN in no candidate's pool. Validation fails, the agent
      retries once and then takes the rules fallback (prompt 37's Atlanta).
    - ``"hold"``: a ``returns_expected`` hold on the first gap id and no move.

    Every reply reports ``usage`` of :attr:`USAGE`. The guardrail passes everything
    (``action`` "NONE", the shape ``llm._apply_guardrail`` reads).
    """

    USAGE = {"inputTokens": 100, "outputTokens": 50}

    def __init__(self, mode: str) -> None:
        assert mode in ("move", "invalid", "hold"), mode
        self.mode = mode
        self.converse_calls: list[dict] = []
        self.guardrail_calls: list[dict] = []

    def converse(self, **kw: Any) -> dict:
        from agents.fi_rebalancing.llm import TOOL_NAME

        self.converse_calls.append(kw)
        ctx = json.loads(kw["messages"][0]["content"][0]["text"].splitlines()[-1])
        cand = ctx["candidates"][0] if ctx["candidates"] else None
        if self.mode == "hold":
            tool = {"moves": [], "holds": [{"gap_id": ctx["gap_ids"][0],
                                            "reason": "returns_expected",
                                            "detail": "Returns are due before the shortfall."}]}
        else:
            vin = cand["eligible_vins"][0] if self.mode == "move" else "VIN-IN-NO-POOL"
            tool = {"moves": [{"candidate_id": cand["candidate_id"], "vehicle_vins": [vin],
                               "timing": "today",
                               "detail": "Move one spare vehicle to cover the shortfall.",
                               "evidence_refs": ["rebalance.forecast", "rebalance.supply"]}],
                    "holds": []}
        return {"output": {"message": {"content": [{"toolUse": {"name": TOOL_NAME,
                                                                 "input": tool}}]}},
                "stopReason": "tool_use", "usage": dict(self.USAGE)}

    def apply_guardrail(self, **kw: Any) -> dict:
        self.guardrail_calls.append(kw)
        return {"action": "NONE"}


class FakeLambda:
    """``lambda.invoke`` for the Tier 2 function: runs the real ``run_rebalance``.

    Returns what the Lambda service returns: ``StatusCode`` 200, a ``Payload`` stream holding
    the handler's return value, or on an exception ``FunctionError`` "Unhandled" with
    ``errorType`` / ``errorMessage``. Each failed invoke is recorded for the alarm fake.

    With *bedrock* (a :class:`FakeBedrock`), the runner gets the model path's four arguments,
    as ``_run_rebalance_from_env`` passes them when the stack sets the model and guardrail.
    """

    def __init__(self, session: Any, alarm: "FakeCloudWatch",
                 bedrock: "FakeBedrock | None" = None) -> None:
        self.session, self.alarm, self.bedrock = session, alarm, bedrock
        self.invocations: list[dict] = []

    def invoke(self, *, FunctionName: str, InvocationType: str, Payload: bytes) -> dict:
        from agents.tier2 import runner
        from agents.tier2.artifact_writer import ArtifactWriter
        from agents.tier2.run_records import write_run_record

        self.invocations.append({"FunctionName": FunctionName, "InvocationType": InvocationType,
                                 "Payload": json.loads(Payload)})
        assert FunctionName == FN and InvocationType == "RequestResponse"
        ddb = self.session.client("dynamodb", region_name=REGION)

        # write_run_record takes a DynamoDB *resource*, as production's default writer passes.
        ddb_resource = self.session.resource("dynamodb", region_name=REGION)

        def record_writer(**kw: Any) -> bool:
            return write_run_record(ddb_client=ddb_resource, table_name=RUNS, **kw)

        model = ({} if self.bedrock is None else
                 {"bedrock_client": self.bedrock, "model_id": "MID", "guardrail_id": "GID",
                  "guardrail_version": "DRAFT"})
        try:
            out = runner.run_rebalance(
                STAGE, athena_client=object(), workgroup=f"cvx-{STAGE}-analytics",
                results_s3_path="s3://results/tier2/fi-rebalancing/", ddb_client=ddb,
                findings_table=FINDINGS, vehicles_table=f"cms-{STAGE}-storage-vehicles",
                groundings_table=f"cms-{STAGE}-storage-vehicle-groundings",
                fleet_locations_table=LOCS,
                avx_writer=ArtifactWriter(table_name=FINDINGS, dynamodb_client=ddb),
                run_record_writer=record_writer,
                ssm_client=self.session.client("ssm", region_name=REGION), **model)
        except Exception as exc:  # noqa: BLE001 — the Lambda runtime's own behaviour
            self.alarm.lambda_error()
            body = {"errorMessage": str(exc), "errorType": type(exc).__name__}
            return {"StatusCode": 200, "FunctionError": "Unhandled",
                    "Payload": io.BytesIO(json.dumps(body).encode())}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(out, default=str).encode())}


# ---------------------------------------------------------------------------
# Athena for the script's own reads
# ---------------------------------------------------------------------------


class FakeAthena:
    """Athena as the script sees it: string values, header first, two rows per page."""

    def __init__(self, today: dt.date, *, gaps_rows: list | None = None) -> None:
        self.gaps_rows = gaps(today) if gaps_rows is None else gaps_rows
        self.snapshots: dict[str, list[dict]] = {}   # earlier gaps snapshots, by data_as_of
        self.forecast_rows = forecast(today)
        self.queries: list[str] = []
        self._results: dict[str, list[list[str | None]]] = {}

    def start_query_execution(self, *, QueryString: str, WorkGroup: str) -> dict:
        import re
        self.queries.append(QueryString)
        qid = str(len(self.queries))
        if "supply_demand_gaps_snapshot" in QueryString:
            if QueryString.count("horizon_days = 7") != 2:
                raise AssertionError("gaps query must filter horizon_days = 7 in both places")
            dated = re.search(r"data_as_of = DATE '([0-9-]{10})'", QueryString)
            if dated:   # one data_as_of's newest materialisation (G2, a move's cited snapshot)
                day = dated.group(1)
                if QueryString.count(f"data_as_of = DATE '{day}'") != 2:
                    raise AssertionError("a dated gaps query must pin data_as_of in both places")
                cols = ["location_id", "supply_vehicles"]
                src = ([r for r in self.gaps_rows if str(r.get("data_as_of")) == day]
                       or self.snapshots.get(day, []))
            else:       # the newest materialisation (G1, as the agent reads it)
                cols = ["location_id", "status", "supply_vehicles", "vehicles_short",
                        "vehicles_spare", "data_as_of"]
                src = self.gaps_rows
            rows = [[None if r.get(c) is None else str(r.get(c)) for c in cols] for r in src]
        elif "fleet_bookings_forecast" in QueryString:
            m = re.search(r"run_date = DATE '([0-9-]{10})'", QueryString)
            cols = ["location_id", "forecast_date", "forecast_hours"]
            rows = [[r["location_id"], r["forecast_date"], str(r["forecast_hours"])]
                    for r in self.forecast_rows if m and r["run_date"] == m.group(1)]
        else:
            raise AssertionError(f"unexpected query: {QueryString[:120]}")
        self._results[qid] = [cols] + rows
        return {"QueryExecutionId": qid}

    def get_query_execution(self, *, QueryExecutionId: str) -> dict:
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, *, QueryExecutionId: str, NextToken: str | None = None) -> dict:
        rows = self._results[QueryExecutionId]
        start = int(NextToken or 0)
        page = rows[start:start + 2]
        out = {"ResultSet": {"Rows": [{"Data": [{} if v is None else {"VarCharValue": v}
                                                for v in r]} for r in page]}}
        if start + 2 < len(rows):
            out["NextToken"] = str(start + 2)
        return out


# ---------------------------------------------------------------------------
# CloudWatch run-failure alarm, SNS, broker logs
# ---------------------------------------------------------------------------


class FakeCloudWatch:
    """The run-failure alarm as live (2026-10-09): ``Errors`` Sum on the function, period 300,
    actions to the operator topic. A failed invoke moves it to ALARM and records an ``Action``
    history item with ``HistoryData.actionState`` (``action_state``, default "Succeeded")."""

    def __init__(self) -> None:
        self.state, self.updated = "OK", dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.history: list[dict] = []
        self.action_state = "Succeeded"
        self.fires = True            # tests can make the alarm stay OK
        self.records_action = True   # tests can make the alarm fire with no Action item yet
        self.metric, self.function, self.actions = "Errors", FN, [TOPIC_ARN]

    def lambda_error(self) -> None:
        if not self.fires:
            return
        now = dt.datetime.now(dt.timezone.utc)
        self.state, self.updated = "ALARM", now
        if not self.records_action:
            return
        self.history.insert(0, {
            "AlarmName": ALARM, "AlarmType": "MetricAlarm", "Timestamp": now,
            "HistoryItemType": "Action",
            "HistorySummary": f"Successfully executed action {TOPIC_ARN}",
            "HistoryData": json.dumps({"actionState": self.action_state,
                                       "stateUpdateTimestamp": int(now.timestamp() * 1000),
                                       "notificationResource": TOPIC_ARN})})

    def describe_alarms(self, *, AlarmNames: list[str]) -> dict:
        if AlarmNames != [ALARM]:
            return {"MetricAlarms": []}
        return {"MetricAlarms": [{
            "AlarmName": ALARM, "MetricName": self.metric, "Namespace": "AWS/Lambda",
            "Statistic": "Sum", "Period": 300, "EvaluationPeriods": 1, "Threshold": 1.0,
            "Dimensions": [{"Name": "FunctionName", "Value": self.function}],
            "AlarmActions": list(self.actions), "OKActions": list(self.actions),
            "StateValue": self.state, "StateUpdatedTimestamp": self.updated}]}

    def describe_alarm_history(self, *, AlarmName: str, HistoryItemType: str, MaxRecords: int,
                               ScanBy: str) -> dict:
        assert AlarmName == ALARM and HistoryItemType == "Action"
        return {"AlarmHistoryItems": self.history[:MaxRecords]}


class FakeSns:
    """``list_subscriptions_by_topic`` with paging; subscriptions as SNS returns them."""

    def __init__(self, subs: list[str] | None = None) -> None:
        self.subs = subs if subs is not None else [
            "PendingConfirmation", f"{TOPIC_ARN}:0b6c3d2e-0000-4000-8000-000000000001"]

    def list_subscriptions_by_topic(self, *, TopicArn: str, NextToken: str | None = None) -> dict:
        i = int(NextToken or 0)
        page = [{"SubscriptionArn": a, "TopicArn": TopicArn, "Protocol": "email",
                 "Endpoint": "ops@example.com"} for a in self.subs[i:i + 1]]
        out: dict = {"Subscriptions": page}
        if i + 1 < len(self.subs):
            out["NextToken"] = str(i + 1)
        return out


class FakeLogs:
    """The broker's log: one skip line per rebalancing item the findings table holds, in the
    broker's own format (``avx_broker.py``), two events per page."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.extra: list[str] = []
        self.calls: list[dict] = []

    def filter_log_events(self, **kw: Any) -> dict:
        self.calls.append(kw)
        ddb = self.session.client("dynamodb", region_name=REGION)
        items = ddb.scan(TableName=FINDINGS)["Items"]
        msgs = [f"[INFO]\t2026-10-16T12:00:00.000Z\treq-1\tbroker: skipping — fi_item "
                f"finding_id={i['finding_id']['S']} kind={i['finding_kind']['S']} "
                f"agent_id={i['agent_id']['S']}" for i in items] + self.extra
        start = int(kw.get("nextToken") or 0)
        out: dict = {"events": [{"message": m, "timestamp": 0} for m in msgs[start:start + 2]]}
        if start + 2 < len(msgs):
            out["nextToken"] = str(start + 2)
        return out


# ---------------------------------------------------------------------------
# The CMS user pool (G4b). moto's cognito-idp needs joserfc, which the repo's venv lacks, so a
# small fake with the API's shapes: paged list calls, UserNotFoundException on a missing user.
# ---------------------------------------------------------------------------


class _UserNotFound(Exception):
    pass


class FakeIdp:
    class exceptions:  # noqa: N801 — botocore's client.exceptions shape
        UserNotFoundException = _UserNotFound

    def __init__(self) -> None:
        self.pool_id = f"{REGION}_TESTPOOL1"
        self.users: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.delete_fails = False
        self.calls: list[tuple[str, dict]] = []

    def _c(self, op: str, kw: dict) -> None:
        self.calls.append((op, kw))

    def list_user_pools(self, **kw: Any) -> dict:
        self._c("ListUserPools", kw)
        if not kw.get("NextToken"):
            return {"UserPools": [{"Id": f"{REGION}_OTHER", "Name": "vsa-staging-voice"}],
                    "NextToken": "1"}
        return {"UserPools": [{"Id": self.pool_id, "Name": f"cms-{STAGE}-ui-users"}]}

    def list_user_pool_clients(self, **kw: Any) -> dict:
        self._c("ListUserPoolClients", kw)
        return {"UserPoolClients": [{"ClientId": "c-plain"}, {"ClientId": "c-web"}]}

    def describe_user_pool_client(self, **kw: Any) -> dict:
        flows = ["ALLOW_USER_PASSWORD_AUTH"] if kw["ClientId"] == "c-web" else []
        return {"UserPoolClient": {"ClientId": kw["ClientId"], "ExplicitAuthFlows": flows}}

    def admin_create_user(self, **kw: Any) -> dict:
        self._c("AdminCreateUser", kw)
        assert kw["UserPoolId"] == self.pool_id
        self.users[kw["Username"]] = {"attrs": {a["Name"]: a["Value"] for a in kw["UserAttributes"]},
                                      "groups": [], "password": None}
        return {"User": {"Username": kw["Username"]}}

    def admin_set_user_password(self, **kw: Any) -> dict:
        self.users[kw["Username"]]["password"] = kw["Password"]
        return {}

    def admin_add_user_to_group(self, **kw: Any) -> dict:
        self.users[kw["Username"]]["groups"].append(kw["GroupName"])
        return {}

    def initiate_auth(self, **kw: Any) -> dict:
        self._c("InitiateAuth", kw)
        assert kw["ClientId"] == "c-web" and kw["AuthFlow"] == "USER_PASSWORD_AUTH"
        u = self.users[kw["AuthParameters"]["USERNAME"]]
        assert u["password"] == kw["AuthParameters"]["PASSWORD"]
        return {"AuthenticationResult": {"IdToken": f"token-for-{kw['AuthParameters']['USERNAME']}"}}

    def admin_delete_user(self, **kw: Any) -> dict:
        self._c("AdminDeleteUser", kw)
        if self.delete_fails:
            raise RuntimeError("AccessDeniedException")
        self.users.pop(kw["Username"], None)
        self.deleted.append(kw["Username"])
        return {}

    def admin_get_user(self, **kw: Any) -> dict:
        if kw["Username"] not in self.users:
            raise _UserNotFound(kw["Username"])
        return {"Username": kw["Username"]}


# ---------------------------------------------------------------------------
# AVX findings API (G4b): a local HTTP server
# ---------------------------------------------------------------------------


class FakeFindingsApi:
    """``GET /findings/fleet/{fleet_id}`` as the AVX API answers it: 403 for a fleet outside
    the caller's ``custom:fleetIds`` (``check_fleet_scope``), else the live response shape
    ``{"findings": [...], "next_token": ...}`` one item per page. Records every request's
    path and Authorization header."""

    def __init__(self, own_items: list[dict], allowed: set[str]) -> None:
        self.own_items, self.allowed = own_items, allowed
        self.requests: list[dict] = []
        api = self

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                import urllib.parse as up
                u = up.urlparse(self.path)
                api.requests.append({"path": u.path, "query": up.parse_qs(u.query),
                                     "authorization": self.headers.get("Authorization")})
                fleet = u.path.rsplit("/", 1)[-1]
                if fleet not in api.allowed:
                    code, body = 403, {"error": "insufficient scope"}
                else:
                    i = int(up.parse_qs(u.query).get("next_token", ["0"])[0])
                    nxt = str(i + 1) if i + 1 < len(api.own_items) else None
                    code, body = 200, {"findings": api.own_items[i:i + 1], "next_token": nxt}
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/staging"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
