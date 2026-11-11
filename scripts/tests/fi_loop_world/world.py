"""``FiLoopWorld``: extends fi_rw_world.World with the four loop-script pieces.

Pieces added (each importable independently via their own module):

1. ``InvestigationsHandler`` — real ``lambdas/api-avx-investigations/handler.py``
   on the AVX REST API (new ``/investigations`` routes).

2. ``FiQualityBedrock`` — Bedrock Converse fake for the fi_quality agent's
   ``record_root_cause`` + ``record_design_options`` forced-tool calls.  Wired
   into the Router so the Tier 2 Lambda's SQS investigation path exercises the
   real ``run_investigation`` / ``validate`` code.

3. ``AdpWriterHandler`` — real ADP digital_thread_writer loaded from
   ``~/automotive-data-platform-on-aws`` at commit ``9bb82e96``.  Registered as
   a Lambda in moto so ``AdpExecutor`` can invoke it cross-region.

4. ``CmsGroundingsHandler`` — real ``services/fleet_intelligence/index.py``
   covering POST /groundings and POST /groundings/{vehicleId}/return-to-service,
   behind a ``FakeRestApi``-compatible shim.

Usage::

    with FiLoopWorld() as w:
        # POST an investigation
        resp = w.post_investigation(pattern_id="pat-001", sub="eng-001")
        assert resp["statusCode"] == 202

        # Wait for the Tier 2 Lambda to process the SQS message
        w.pump_investigation(resp["investigation_id"])

        # POST a grounding
        w.post_grounding(vehicle_id="VH-001", campaign_id="MRD-2026-501", ...)

        # Post return-to-service after repair is recorded
        w.return_to_service("VH-001", sub="fleet-op-001")
"""
from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path
from typing import Any

# WorldClock.patch_loaded_modules patches `datetime.datetime` references (including
# any we save here as module-level names), but leaves `time.time` alone.
# W2 fix: seed_recall_cache now uses self.clock.now() so CMS's recall_source._utcnow()
# (which reads the world clock) sees the same time and the staleness check passes.
from scripts.tests.fi_rw_world import world as _rw
from scripts.tests.fi_rw_world.world import World as _RwWorld
from scripts.tests.fi_rw_world.lambdas import lambda_env
from scripts.tests.fi_loop_world.fi_quality_bedrock import FiQualityBedrock
from scripts.tests.fi_loop_world import adp_writer as _adp_writer
from scripts.tests.fi_loop_world import cms_fi as _cms_fi
from scripts.tests.fi_loop_world.dlq_alarm import DlqDepthAlarm

STAGE = _rw.STAGE
REGION = _rw.REGION
ADP_REGION = _rw.ADP_REGION
ACCOUNT = _rw.ACCOUNT

AVX_ROOT = _rw.AVX_ROOT
INVESTIGATIONS_QUEUE = "vsa-staging-avx-investigations"
INVESTIGATIONS_DLQ = "vsa-staging-avx-investigations-dlq"
INVESTIGATIONS_TABLE = "vsa-staging-avx-investigations"

# Stage-3 constants: DLQ alarm and ESM
DLQ_ALARM = f"vsa-{_rw.STAGE}-avx-investigations-dlq-depth-{_rw.REGION}"
INVESTIGATIONS_ESM_UUID = "fi-loop-world-esm-uuid-11df5e08"  # fake, not a real UUID shape
ADP_WRITER_FN_NAME = "adp-staging-digital-thread-writer"
ADP_WRITER_ARN = f"arn:aws:lambda:{ADP_REGION}:{ACCOUNT}:function:{ADP_WRITER_FN_NAME}"

# ATHENA_OUTPUT_LOC for the CMS FI handler (live read 2026-10-21, scrubbed).
CMS_ATHENA_OUTPUT_LOC = f"s3://cms-staging-athena-results-{ACCOUNT}-us-east-1/fleet-intelligence/"
CMS_ATHENA_BUCKET = f"cms-staging-athena-results-{ACCOUNT}-us-east-1"
CMS_ATHENA_PREFIX = "fleet-intelligence/"

_NEW_AVX_ROUTES = [
    ("/investigations", "POST", "investigations"),
    ("/investigations/{investigationId}", "GET", "investigations"),
]


def _translate_athena_to_duckdb(sql: str) -> str:
    """Translate Athena/Presto SQL functions to DuckDB equivalents.

    The fi_quality investigation readers use Athena-specific builtins.
    Applied before every ``start_query_execution`` in the FiLoopWorld.

    Translations (each with a source citation):
      array_join(arr, sep)             → array_to_string(arr, sep)
        [readers.py:_PATTERN_SQL line with array_join(affected_vins_sample)]
      CONTAINS(arr, val)               → list_contains(arr, val)
        [recall_vehicle_status.sql: CONTAINS(c.models, vi.model)]
      MIN_BY(x, y)                     → arg_min(x, y)
        [recall_vehicle_status.sql remedied_dms CTE]
      date_add('day', N, expr)         → (expr + INTERVAL N DAYS)
        [readers.py:_CO_OCCURRING_SQL, _CONTROLS_SQL lookback window]
      from_iso8601_timestamp(s)        → CAST(s AS TIMESTAMPTZ)
        [recall_vehicle_status.sql: DATE(from_iso8601_timestamp(updated_at))]
      json_extract_scalar(j, p)        → json_extract_string(j, p)
        [recall_vehicle_status.sql: json_extract_scalar(line, '$.pay_type')]
      CAST(... AS ARRAY(JSON))         → (left as CAST ... AS JSON[])
        [recall_vehicle_status.sql: CAST(json_parse(lr.lines) AS ARRAY(JSON))]
      json_parse(s)                    → CAST(s AS JSON)
        [recall_vehicle_status.sql]
    """
    import re
    sql = sql.replace("array_join(", "array_to_string(")
    sql = re.sub(r"\bCONTAINS\s*\(", "list_contains(", sql)
    sql = re.sub(r"\bMIN_BY\s*\(", "arg_min(", sql)
    sql = re.sub(
        r"date_add\('day',\s*(-?[\w.]+),\s*([^)]+?)\)",
        lambda m: f"({m.group(2).strip()} + ({m.group(1).strip()}) * INTERVAL '1 DAY')",
        sql,
    )
    sql = re.sub(
        r"\bfrom_iso8601_timestamp\s*\(([^)]+)\)",
        r"CAST(\1 AS TIMESTAMPTZ)",
        sql,
    )
    sql = re.sub(r"\bjson_extract_scalar\s*\(", "json_extract_string(", sql)
    # json_parse(s) → s::JSON
    sql = re.sub(r"\bjson_parse\s*\(([^)]+)\)", r"(\1)::JSON", sql)
    # ARRAY(JSON) → JSON[]
    sql = sql.replace("ARRAY(JSON)", "JSON[]")
    return sql

_CMS_FI_ROUTES = [
    ("/api/v1/fleet-intelligence/groundings", "POST", "cms_fi"),
    ("/api/v1/fleet-intelligence/groundings/{vehicleId}/return-to-service",
     "POST", "cms_fi"),
    ("/api/v1/fleet-intelligence/recalls", "GET", "cms_fi"),
    ("/api/v1/fleet-intelligence/recalls/{campaignId}/vehicles", "GET", "cms_fi"),
    ("/api/v1/fleet-intelligence/vehicles/{vin}/recalls", "GET", "cms_fi"),
]


class _GuardedIdpProxy:
    """Wrap ``_FakeIDP`` so un-allowlisted Cognito ops raise ``GuardViolation``.

    W6 fix: ``make_guarded_session`` previously returned ``_FakeIDP`` directly for
    cognito-idp, bypassing the allowlist guard entirely.  Now every call goes
    through this proxy first, so removing an op from the allowlist causes the
    guard to fire even when the client is the in-process shim rather than a
    real boto3 client.

    The ``op`` name uses PascalCase (e.g. ``AdminDeleteUser``) to match
    ``_PART_ALLOWLISTS`` keys, consistent with how ``_make_guarded_bcsession``
    reads ``model.name`` from the botocore event.
    """

    def __init__(
        self,
        inner: Any,
        allowed: frozenset,
        violation_cls: type,
    ) -> None:
        self._inner = inner
        self._allowed = allowed
        self._violation_cls = violation_cls

    def _check(self, op: str) -> None:
        if ("cognito-idp", op) not in self._allowed:
            raise self._violation_cls(
                f"cognito-idp.{op} is not on this part's allowlist "
                f"(allowlist size {len(self._allowed)})"
            )

    def __getattr__(self, name: str) -> Any:
        # name is snake_case (e.g. ``admin_delete_user``); op is PascalCase.
        op = "".join(w.capitalize() for w in name.split("_"))
        inner = self._inner
        check = self._check

        def _method(**kw: Any) -> Any:
            check(op)
            return getattr(inner, name)(**kw)

        return _method


class _FakeIDP:
    """Boto3-like cognito-idp shim backed by ``FakeUserPool``.

    Avoids importing moto's ``cognitoidp`` backend, which does
    ``from joserfc import jwk, jwt`` at module load time (not in venv).
    Tokens are HMAC-signed (same as ``FakeUserPool.mint``); the API Gateway
    emulation in the world uses ``pool.verify``, so tokens from this shim
    are accepted by the world's AVX/CMS handlers.
    """

    def __init__(self, pool: Any) -> None:  # pool: FakeUserPool
        self._pool = pool

    def _call(self, op: str, **kw: Any) -> dict[str, Any]:
        # F6c: raise what boto3 raises.  FakeUserPool signals errors with AwsError
        # (no ``.response``); the real client raises botocore ClientError, which is
        # how the script recognises UserNotFoundException as "already deleted".
        from botocore.exceptions import ClientError  # noqa: PLC0415
        from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
        try:
            return getattr(self._pool, op)(kw)
        except AwsError as exc:
            api = "".join(part.title() for part in op.split("_"))
            raise ClientError(
                {"Error": {"Code": exc.code, "Message": exc.message},
                 "ResponseMetadata": {"HTTPStatusCode": exc.status}},
                api,
            ) from exc

    def list_user_pools(self, **kw: Any) -> dict[str, Any]:  # noqa: ARG002
        # Script checks ``p["Name"] == _CMS_POOL_NAME`` ("cms-staging-ui-users").
        return {"UserPools": [{"Id": self._pool.pool_id, "Name": "cms-staging-ui-users"}]}

    def list_user_pool_clients(self, **kw: Any) -> dict[str, Any]:
        return self._call("list_user_pool_clients", **kw)

    def describe_user_pool_client(self, **kw: Any) -> dict[str, Any]:
        return self._call("describe_user_pool_client", **kw)

    def admin_create_user(self, **kw: Any) -> dict[str, Any]:
        return self._call("admin_create_user", **kw)

    def admin_set_user_password(self, **kw: Any) -> dict[str, Any]:
        return self._call("admin_set_user_password", **kw)

    def admin_add_user_to_group(self, **kw: Any) -> dict[str, Any]:
        return self._call("admin_add_user_to_group", **kw)

    def initiate_auth(self, **kw: Any) -> dict[str, Any]:
        return self._call("initiate_auth", **kw)

    def admin_delete_user(self, **kw: Any) -> dict[str, Any]:
        return self._call("admin_delete_user", **kw)

    def admin_get_user(self, **kw: Any) -> dict[str, Any]:
        return self._call("admin_get_user", **kw)

    def list_users(self, **kw: Any) -> dict[str, Any]:
        return self._call("list_users", **kw)


class FiLoopWorld(_RwWorld):
    """Extends ``fi_rw_world.World`` with investigations, ADP writer, and CMS FI."""

    def __init__(self, *, auto_pump: bool = False, daily_cap: int = 20,
                 late_handler_write: bool = False,
                 restore_live_tests: bool = False, **kwargs: Any) -> None:
        # groundings_table must be True so the groundings table is created
        kwargs.setdefault("groundings_table", True)
        # auto_pump: synchronously process POST /investigations in the test world.
        # When True, after the investigations route returns 202, pump_investigation()
        # is called immediately so pollers find status=completed without waiting.
        self._auto_pump = auto_pump
        self._daily_cap = daily_cap
        # W1 lane-Y fake switch: when True, FakeLambda.invoke on the tier2 health
        # function raises EndpointConnectionError without executing the handler,
        # simulating a connection reset that leaves the Lambda running server-side.
        # The test plants the "recreated" row (status=failed, no pattern_id) in DDB
        # directly after calling main(), to simulate _set_failed running late.
        self.late_handler_write: bool = late_handler_write
        # restore_live_tests: when True, _extend_world re-sets LIVE_TESTS=1 after
        # fi_rw_world.World.open() pops it.  Item-9 tests opt in because they call
        # lv.main() (which requires LIVE_TESTS=1) from inside the world context.
        # After-6 tests leave this False so main() exits at the guard with rc=2,
        # which is the expected rc for their assertions.
        self._restore_live_tests: bool = restore_live_tests
        # Pattern IDs whose SQS messages should be drained after auto-pump.
        # Populated by seed_pattern() so only pre-seeded patterns (item 2's
        # investigations) are drained; fresh item-9a patterns are NOT drained
        # because item-9b needs the message to stay in the queue.
        self._auto_drain_pattern_ids: set[str] = set()
        super().__init__(**kwargs)

    # ------------------------------------------------------------------ open
    def open(self) -> None:
        super().open()
        self._extend_world()

    def _extend_world(self) -> None:
        """Add the four loop-script pieces on top of the base world.

        Also registers ADP 9.1 view SQL in the FakeAthena's DuckDB (adp_views piece).
        When ``restore_live_tests=True`` (item-9 tests only), re-sets LIVE_TESTS=1 after
        ``fi_rw_world.World.open()`` pops it, so ``lv.main()`` calls inside the world
        succeed.  After-6 tests leave this False so the LIVE_TESTS guard keeps rc=2.
        """
        import os as _os  # noqa: PLC0415
        # W8 write-filter span (6.5 fix round 3): the base world registers its own
        # pump as a clock on-advance callback, and its _refresh_marker writes a
        # fleet-membership PutItem on every advance.  Bracket that callback with
        # world-span markers HERE, in FiLoopWorld only, so other suites built on
        # fi_rw_world see an unchanged router log.
        _pumps = self.clock._pumps
        _base_pump = self.pump
        if _base_pump in _pumps:
            _span_world = self

            def _pump_with_span() -> None:
                _span_world.router.all_calls.append(("_span", "_world_start", {}))
                try:
                    _base_pump()
                finally:
                    _span_world.router.all_calls.append(("_span", "_world_end", {}))

            _pumps[_pumps.index(_base_pump)] = _pump_with_span
        if self._restore_live_tests:
            _os.environ["LIVE_TESTS"] = "1"
        ddb = self.internal("dynamodb")
        # The DMS stand-in's schedule, which the loop script's window guard reads. It's added
        # here, not to fi_rw_world's shared live_shapes.json (the Stage C world and its script's
        # tests read that file). Shape: g93lv-facts.md § Schedules (live read 2026-10-20,
        # cron(40 5 * * ? *), timezone UTC, default group).
        from scripts.tests.fi_rw_world.world import DEALER_PROGRESS_FN as _DP_FN
        self.internal("scheduler").create_schedule(
            Name="dms-staging-fi-dealer-progress", GroupName="default",
            ScheduleExpression="cron(40 5 * * ? *)", ScheduleExpressionTimezone="UTC",
            State="ENABLED", FlexibleTimeWindow={"Mode": "OFF"},
            Target={"Arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{_DP_FN}",
                    "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/fi-loop-world-scheduler",
                    "Input": "{}",
                    "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 2}},
        )
        # W4 fix: also add service and rebalance schedules (not in live_shapes.json).
        # The script's _SCHEDULES now guards all cron-based Tier 2 schedules.
        # Shape: vsa-tier2-stack.ts:2086 (service cron(40 5 * * ? *)) and :2092 (rebalance
        # cron(0 6 * * ? *)).
        _svc_target = {
            "Arn": self.health_arn,
            "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/fi-loop-world-scheduler",
            "Input": "{}",
            "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 2},
        }
        self.internal("scheduler").create_schedule(
            Name=f"vsa-{STAGE}-tier2-daily-service-{REGION}", GroupName="default",
            ScheduleExpression="cron(40 5 * * ? *)", ScheduleExpressionTimezone="Etc/UTC",
            State="ENABLED", FlexibleTimeWindow={"Mode": "OFF"}, Target=_svc_target,
        )
        self.internal("scheduler").create_schedule(
            Name=f"vsa-{STAGE}-tier2-daily-rebalance-{REGION}", GroupName="default",
            ScheduleExpression="cron(0 6 * * ? *)", ScheduleExpressionTimezone="Etc/UTC",
            State="ENABLED", FlexibleTimeWindow={"Mode": "OFF"}, Target=_svc_target,
        )
        # Register ADP 9.1 view SQL (recall_campaigns_received_view + vehicle_build_components).
        from scripts.tests.fi_loop_world import adp_views as _adp_views
        _adp_views.register_adp_views(self.athena, stage=STAGE)
        # Create empty fi_quality Athena tables and seed minimal snapshot data so the
        # snapshot freshness guard (which fires BEFORE the fault_patterns query in
        # run_investigation) does not raise AthenaCursorError when no fault_patterns
        # rows are seeded.  Without these tables the investigation exits with
        # error=AthenaCursorError instead of the expected error=PatternNotFound.
        # The adp_{stage}_engineering schema is already created by register_adp_views;
        # _EMPTY_TABLES_DDL creates the other schemas and empty tables idempotently.
        from scripts.tests.fi_loop_world import fi_quality_athena as _fqa  # noqa: PLC0415
        _con = self.athena.con
        for _ddl in _fqa._EMPTY_TABLES_DDL:
            _con.execute(_ddl.format(stage=STAGE))
        _con.execute(_fqa._FAULT_PATTERNS_DDL.format(stage=STAGE))
        # Seed snapshot_runs (vehicle_fault_status_daily uses ADP marker-date path) and
        # managed_vehicles (uses MAX(snapshot_date) directly from the table) so that
        # require_fresh passes.  Both use today's world-clock date, which matches the
        # investigation's _snap_today.  MAX_SNAPSHOT_AGE_DAYS = 1, same_date=False.
        _snap_today = self.clock.now().date().isoformat()
        _con.execute(
            f"INSERT INTO adp_{STAGE}_fleet_health.snapshot_runs "
            f"(table_key, snapshot_date, row_count) VALUES "
            f"('vehicle_fault_status_daily', '{_snap_today}', 0)"
        )
        _con.execute(
            f"INSERT INTO adp_{STAGE}_fleet_recall.managed_vehicles "
            f"(vin, fleet_id, snapshot_date) VALUES "
            f"('WNOADUMMY00000001', 'fleet-world-default', '{_snap_today}')"
        )
        # Wrap FakeAthena.start_query_execution to translate Athena-specific SQL to DuckDB.
        # The fi_quality readers use array_join(), CONTAINS(), date_add() etc.
        # Update the router's handler so the translation applies for all boto3 Athena calls.
        _orig = self.athena.start_query_execution
        # Reset module-level caches in shared lambdas modules so they are rebuilt
        # against the current moto context.  avx_scope is shared across worlds because
        # it's a regular sys.modules entry; its _ddb, _STAGE, and _FINDINGS_TABLE are
        # all set at import time (or on first call) and become stale when a previous
        # test's mock_aws() context imported the module with different env vars
        # (e.g. test 1 imports it without AVX_STAGE → _FINDINGS_TABLE = "vsa-dev-avx-findings").
        # NOTE: the findings handler imports avx_scope as "_lib.avx_scope" (with lambdas/ on
        # sys.path), while test helpers use "lambdas._lib.avx_scope".  Python treats these
        # as DISTINCT module objects even though they point to the same .py file.  Both must
        # be reset, or the handler sees stale _FINDINGS_TABLE / _ddb from the first import.
        import sys as _sys  # noqa: PLC0415
        _findings_env = getattr(self, "_api_env", {}).get("findings", {})
        _correct_table = _findings_env.get("AVX_FINDINGS_TABLE", f"vsa-{STAGE}-avx-findings")
        for _mod_name in ("lambdas._lib.avx_scope", "_lib.avx_scope"):
            _mod = _sys.modules.get(_mod_name)
            if _mod is not None:
                try:
                    _mod._ddb = None
                    _mod._STAGE = STAGE
                    _mod._FINDINGS_TABLE = _correct_table
                except Exception:  # noqa: BLE001
                    pass

        def _translated_start_query_execution(p: dict) -> dict:
            sql = p.get("QueryString", "")
            sql = _translate_athena_to_duckdb(sql)
            return _orig({**p, "QueryString": sql})

        self.athena.start_query_execution = _translated_start_query_execution
        # Re-register the translated function in the router so boto3 calls go through it.
        self.router.add("athena", "StartQueryExecution", _translated_start_query_execution)
        ddb.create_table(
            TableName=INVESTIGATIONS_TABLE,
            KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "investigation_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # SQS queue for investigations (moto)
        sqs = self.internal("sqs")
        sqs.create_queue(
            QueueName=INVESTIGATIONS_QUEUE,
            Attributes={"VisibilityTimeout": "60"},
        )
        q_url = sqs.get_queue_url(QueueName=INVESTIGATIONS_QUEUE)["QueueUrl"]
        self._investigations_queue_url = q_url

        # DLQ for investigations (stage 3: items 9a/9b)
        sqs.create_queue(
            QueueName=INVESTIGATIONS_DLQ,
            Attributes={"VisibilityTimeout": "30"},
        )
        dlq_url = sqs.get_queue_url(QueueName=INVESTIGATIONS_DLQ)["QueueUrl"]
        self._investigations_dlq_url = dlq_url

        # Set RedrivePolicy on the main queue so GetQueueAttributes returns it.
        # Facts: maxReceiveCount=2 (matches SQS_MAX_RECEIVE_COUNT env var).
        dlq_arn_q = sqs.get_queue_attributes(
            QueueUrl=dlq_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        sqs.set_queue_attributes(
            QueueUrl=q_url,
            Attributes={
                "RedrivePolicy": json.dumps({
                    "deadLetterTargetArn": dlq_arn_q,
                    "maxReceiveCount": 2,
                })
            },
        )

        # ESM fake state (stage 3: items 8b/9a) ─────────────────────────────
        # Tracks enabled/disabled state; script reads it via ListEventSourceMappings /
        # GetEventSourceMapping / UpdateEventSourceMapping.
        self._esm_state = "Enabled"
        self._esm_transitional_reads_left = 0  # C2(c): reads to hold transitional state

        def _list_esm(p: dict[str, Any]) -> dict[str, Any]:
            fn = p.get("FunctionName", "")
            arn = p.get("EventSourceArn", "")
            mapping: dict[str, Any] = {
                "UUID": INVESTIGATIONS_ESM_UUID,
                "EventSourceArn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:{INVESTIGATIONS_QUEUE}",
                "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{_rw.HEALTH_FN}",
                "State": self._esm_state,
                "FunctionResponseTypes": ["ReportBatchItemFailures"],
                "BatchSize": 1,
            }
            # Match by FunctionName (tier2 lambda) or EventSourceArn (queue ARN)
            if fn and _rw.HEALTH_FN not in fn:
                return {"EventSourceMappings": []}
            if arn and INVESTIGATIONS_QUEUE not in arn:
                return {"EventSourceMappings": []}
            return {"EventSourceMappings": [mapping]}

        def _get_esm(p: dict[str, Any]) -> dict[str, Any]:
            uid = p.get("UUID", "")
            if uid != INVESTIGATIONS_ESM_UUID:
                from scripts.tests.fi_rw_world.awsfake import AwsError
                raise AwsError("ResourceNotFoundException",
                               f"ESM {uid!r} not found in fake world", status=404)
            return {
                "UUID": INVESTIGATIONS_ESM_UUID,
                "EventSourceArn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:{INVESTIGATIONS_QUEUE}",
                "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{_rw.HEALTH_FN}",
                "State": self._esm_state,
                "FunctionResponseTypes": ["ReportBatchItemFailures"],
                "BatchSize": 1,
            }

        def _update_esm(p: dict[str, Any]) -> dict[str, Any]:
            uid = p.get("UUID", "")
            if uid != INVESTIGATIONS_ESM_UUID:
                from scripts.tests.fi_rw_world.awsfake import AwsError
                raise AwsError("ResourceNotFoundException",
                               f"ESM {uid!r} not found in fake world", status=404)
            enabled = p.get("Enabled")
            # C5 rest: raise ResourceInUseException when update arrives mid-transition
            transitional = {"Disabling", "Enabling", "Creating", "Updating", "Deleting"}
            if self._esm_state in transitional:
                from scripts.tests.fi_rw_world.awsfake import AwsError
                raise AwsError(
                    "ResourceInUseException",
                    f"ESM {uid!r} is in transitional state {self._esm_state!r}; "
                    f"retry after it settles",
                    status=400,
                )
            # C5 rest: pass through transitional state; C2(c): hold for
            # _esm_transitional_reads_left GetEventSourceMapping reads before advancing.
            if enabled is True:
                self._esm_state = "Enabling"
            elif enabled is False:
                self._esm_state = "Disabling"
            self._esm_transitional_reads_left = 3  # C2(c): default N=3 reads
            return {
                "UUID": INVESTIGATIONS_ESM_UUID,
                "State": self._esm_state,
            }

        def _get_esm_advance(p: dict[str, Any]) -> dict[str, Any]:
            """Hold transitional states for _esm_transitional_reads_left reads, then advance."""
            uid = p.get("UUID", "")
            if uid == INVESTIGATIONS_ESM_UUID:
                if self._esm_state == "Enabling":
                    if self._esm_transitional_reads_left > 0:
                        self._esm_transitional_reads_left -= 1
                    else:
                        self._esm_state = "Enabled"
                elif self._esm_state == "Disabling":
                    if self._esm_transitional_reads_left > 0:
                        self._esm_transitional_reads_left -= 1
                    else:
                        self._esm_state = "Disabled"
            return {
                "UUID": INVESTIGATIONS_ESM_UUID,
                "EventSourceArn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:{INVESTIGATIONS_QUEUE}",
                "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{_rw.HEALTH_FN}",
                "State": self._esm_state,
                "FunctionResponseTypes": ["ReportBatchItemFailures"],
                "BatchSize": 1,
            }

        self.router.add("lambda", "ListEventSourceMappings", _list_esm)
        self.router.add("lambda", "GetEventSourceMapping", _get_esm_advance)
        self.router.add("lambda", "UpdateEventSourceMapping", _update_esm)

        # W7 fix: honour WaitTimeSeconds for SQS ReceiveMessage by advancing the
        # world clock when the receive returns empty.  Production SQS blocks for up to
        # WaitTimeSeconds; moto returns immediately.  A re-entrancy guard prevents the
        # internal call from triggering this handler recursively.
        import threading as _threading  # noqa: PLC0415
        _sqs_recv_lock = _threading.local()
        _world_ref = self

        def _sqs_receive_clock_advance(params: dict[str, Any]) -> dict[str, Any]:
            """Observe ReceiveMessage; advance clock by WaitTimeSeconds when empty."""
            if getattr(_sqs_recv_lock, "active", False):
                # Re-entrant call from the internal client — let moto handle it.
                return None  # type: ignore[return-value]
            _sqs_recv_lock.active = True
            try:
                # Call the internal (moto-backed) SQS client directly.
                sqs_i = _world_ref.internal("sqs")
                # Build kwargs from the API params dict (no boto3 internal keys).
                call_kw: dict[str, Any] = {
                    k: v for k, v in params.items()
                    if k not in ("_region",)
                }
                # moto blocks for WaitTimeSeconds in REAL time; the world models the
                # wait on its own clock instead, so moto gets a zero wait.
                wait_s = int(params.get("WaitTimeSeconds", 0))
                call_kw["WaitTimeSeconds"] = 0
                result = sqs_i.receive_message(**call_kw)
                if not result.get("Messages") and wait_s > 0:
                    _world_ref.clock.advance(wait_s)
                # Strip ResponseMetadata (added by boto3; router adds its own wrapper)
                return {k: v for k, v in result.items() if k != "ResponseMetadata"}
            finally:
                _sqs_recv_lock.active = False

        self.router.add("sqs", "ReceiveMessage", _sqs_receive_clock_advance)

        # DLQ depth alarm (stage 3: item 9b) ─────────────────────────────────
        self.dlq_alarm = DlqDepthAlarm(
            self, DLQ_ALARM, INVESTIGATIONS_DLQ, self.topic_arn
        )
        self.dlq_alarm.create(self.internal("cloudwatch"))
        # Re-register describe_alarm_history to multiplex DLQ alarm + run-failure alarm
        self.dlq_alarm.register(self.router, other_alarm=self.alarm)

        # W3 (lane X): Return a confirmed subscription so the script's confirmed-only
        # filter passes in the happy path.  moto email subscriptions remain
        # PendingConfirmation; tests that need a different list add their own override
        # (router.add is last-write-wins, so test overrides take precedence).
        _topic_ref = self.topic_arn
        _confirmed_sub_arn = (
            f"arn:aws:sns:{REGION}:{_rw.ACCOUNT}:{_rw.TOPIC}"
            f":00000000-0000-0000-0000-000000000001"
        )
        self.router.add(
            "sns", "ListSubscriptionsByTopic",
            # S17: only answer "confirmed" for self.topic_arn; return empty for
            # any other topic so the subscription check is tied to the operator topic.
            lambda p: (
                {"Subscriptions": [{
                    "SubscriptionArn": _confirmed_sub_arn,
                    "TopicArn": _topic_ref,
                    "Protocol": "email",
                    "Endpoint": "operator@example.invalid",
                    "Owner": _rw.ACCOUNT,
                }]}
                if p.get("TopicArn") == _topic_ref
                else {"Subscriptions": []}
            ),
        )

        # ── Item-9 extensions (stage 2a) ──────────────────────────────────
        # 1. Update the tier2 Lambda's moto env with investigation-specific vars so
        #    FakeLambda.invoke() (used by --part item9's direct Lambda call) has them in
        #    scope.  The base world's env_of("health_agent") omits AVX_INVESTIGATIONS_TABLE
        #    and SQS_MAX_RECEIVE_COUNT; pump_investigation() sets them manually via a fresh
        #    lambda_env context, but the script's own boto3 lambda.invoke call uses
        #    FakeLambda._env() which reads from moto, so we patch moto here.
        _lam_w = self.internal("lambda", region=REGION)
        try:
            _existing_cfg = _lam_w.get_function_configuration(FunctionName=_rw.HEALTH_FN)
            _existing_env = dict(
                (_existing_cfg.get("Environment") or {}).get("Variables") or {}
            )
            _lam_w.update_function_configuration(
                FunctionName=_rw.HEALTH_FN,
                Environment={"Variables": {
                    **_existing_env,
                    "AVX_INVESTIGATIONS_TABLE": INVESTIGATIONS_TABLE,
                    "AVX_INVESTIGATIONS_QUEUE_URL": self._investigations_queue_url,
                    "SQS_MAX_RECEIVE_COUNT": "2",
                    "ADP_THREAD_WRITER_ARN": ADP_WRITER_ARN,
                }},
            )
        except Exception:  # noqa: BLE001
            pass  # should never fail; silently skip if moto behaviour changes

        # 2. DLQ-aware SQS SendMessage / DeleteMessage intercepts.
        #    When the script (--part item9) sends a message to the DLQ or the cleanup
        #    deletes it, track the new depth so DlqDepthAlarm._auto_transition() fires at
        #    the right clock time.  Non-DLQ queues fall through to moto unmodified.
        #    These intercepts are observe-only: they read the current depth, predict the
        #    post-operation depth, update the alarm, then return None so moto handles
        #    the actual send/delete without a re-entrant inner call.
        _dlq_alarm_ref = self.dlq_alarm
        _dlq_url_ref = self._investigations_dlq_url

        def _sqs_send_dlq_intercept(params: dict[str, Any]) -> None:
            if not params.get("QueueUrl", "").endswith(INVESTIGATIONS_DLQ):
                return None  # not the DLQ → moto handles normally
            sqs_i = _world_ref.internal("sqs")
            attrs = sqs_i.get_queue_attributes(
                QueueUrl=_dlq_url_ref,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            depth = int(attrs["Attributes"].get("ApproximateNumberOfMessages", "0")) + 1
            _dlq_alarm_ref.set_depth(depth)
            return None  # moto handles the actual send

        def _sqs_delete_dlq_intercept(params: dict[str, Any]) -> None:
            if not params.get("QueueUrl", "").endswith(INVESTIGATIONS_DLQ):
                return None  # not the DLQ → moto handles normally
            sqs_i = _world_ref.internal("sqs")
            attrs = sqs_i.get_queue_attributes(
                QueueUrl=_dlq_url_ref,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            depth = max(0, int(attrs["Attributes"].get("ApproximateNumberOfMessages", "0")) - 1)
            _dlq_alarm_ref.set_depth(depth)
            return None  # moto handles the actual delete

        self.router.add("sqs", "SendMessage", _sqs_send_dlq_intercept)
        self.router.add("sqs", "DeleteMessage", _sqs_delete_dlq_intercept)

        # W1 lane-Y / lane-Q: when late_handler_write is set, override the Lambda
        # invoke for the health function to raise EndpointConnectionError AND
        # schedule a one-shot DeleteItem hook that plants the "recreated" row on the
        # next GetItem AFTER the initial delete succeeds.  This simulates _set_failed
        # arriving after the client's connection reset: the script's first conditional
        # delete removes the original row (pattern_id present), THEN _set_failed runs
        # an UpdateItem that adds back status=failed/error_message with no pattern_id.
        # Using update_item preserves the key while matching real _set_failed behavior.
        if self.late_handler_write:
            _lhw_orig_invoke = self.lam.invoke
            _lhw_fn = _rw.HEALTH_FN
            _lhw_table = INVESTIGATIONS_TABLE
            _lhw_world = _world_ref

            def _late_handler_raise(p: dict[str, Any]) -> dict[str, Any]:
                fn = (p.get("FunctionName") or "").split(":function:")[-1].split(":")[0]
                if fn == _lhw_fn:
                    # Extract investigation_id from the synthetic SQS event body.
                    try:
                        raw = p.get("Payload", b"{}")
                        payload = json.loads(raw if isinstance(raw, (str, bytes)) else "{}")
                        _lhw_inv_id = payload.get("Records", [{}])[0].get("body", "")
                    except Exception:  # noqa: BLE001
                        _lhw_inv_id = ""

                    if _lhw_inv_id:
                        # Register a one-shot DeleteItem hook: after the script's
                        # first conditional delete on this investigation_id, plant
                        # the "recreated" row via update_item (SET #s = :f, ...) so
                        # that pattern_id is absent but the row exists again.
                        # The hook registers a GetItem hook rather than planting
                        # directly, so the plant fires AFTER moto processes the
                        # delete (hook returns None → moto deletes → next GetItem
                        # plants).  This mirrors _set_failed landing after the
                        # client's delete: the row is gone, then the late UpdateItem
                        # re-creates it without pattern_id.
                        _planted = [False]
                        _lhw_w = _lhw_world

                        def _plant_on_delete(pp: dict[str, Any]) -> None:
                            if _planted[0]:
                                return None
                            key_val = (pp.get("Key") or {}).get("investigation_id")
                            actual_id = (
                                key_val.get("S", "") if isinstance(key_val, dict)
                                else str(key_val) if key_val else ""
                            )
                            if actual_id == _lhw_inv_id:
                                _planted[0] = True
                                # The delete will complete after this hook returns.
                                # Chain a one-shot GetItem hook that plants the row
                                # on the next GetItem for the same key — simulating
                                # _set_failed's UpdateItem arriving after the delete.
                                _get_planted = [False]

                                def _plant_on_next_get(gp: dict[str, Any]) -> None:
                                    if _get_planted[0]:
                                        return None
                                    gk = (gp.get("Key") or {}).get("investigation_id")
                                    gid = (
                                        gk.get("S", "") if isinstance(gk, dict)
                                        else str(gk) if gk else ""
                                    )
                                    if gid == _lhw_inv_id:
                                        _get_planted[0] = True
                                        try:
                                            _lhw_w.table(_lhw_table).update_item(
                                                Key={"investigation_id": _lhw_inv_id},
                                                UpdateExpression="SET #s = :f, error_message = :e",
                                                ExpressionAttributeNames={"#s": "status"},
                                                ExpressionAttributeValues={
                                                    ":f": "failed",
                                                    ":e": "LateHandlerWrite",
                                                },
                                            )
                                        except Exception:  # noqa: BLE001
                                            pass
                                    return None  # moto serves the GetItem

                                _lhw_world.router.add("dynamodb", "GetItem",
                                                       _plant_on_next_get)
                            return None  # moto handles the actual DeleteItem

                        _lhw_world.router.add("dynamodb", "DeleteItem", _plant_on_delete)

                    from botocore.exceptions import EndpointConnectionError  # noqa: PLC0415
                    raise EndpointConnectionError(
                        endpoint_url="https://lambda.us-west-2.amazonaws.com"
                    )
                return _lhw_orig_invoke(p)

            self.router.add("lambda", "Invoke", _late_handler_raise)

        # W8 write-filter span markers: wrap the Lambda Invoke handler so that
        # all AWS calls made BY the handler are bracketed by ("_span",
        # "_handler_start", {}) / ("_span", "_handler_end", {}) sentinels in
        # router.all_calls.  The write filter in tests can then skip handler-
        # owned calls (Athena, CloudWatch PutMetricData, DDB UpdateItem, etc.)
        # without hardcoding every handler-owned operation.
        #
        # This wraps whichever handler is currently registered (either the
        # late_handler_raise above or the FakeLambda's normal invoke).
        _span_invoke_orig = self.router._handlers.get(("lambda", "Invoke"))
        if _span_invoke_orig is not None:
            _span_fn = _rw.HEALTH_FN
            _span_world = _world_ref

            def _span_invoke_wrapper(p: dict[str, Any]) -> Any:
                fn = (p.get("FunctionName") or "").split(":function:")[-1].split(":")[0]
                if fn == _span_fn:
                    _span_world.router.all_calls.append(
                        ("_span", "_handler_start", {"fn": fn})
                    )
                    try:
                        return _span_invoke_orig(p)
                    finally:
                        _span_world.router.all_calls.append(
                            ("_span", "_handler_end", {"fn": fn})
                        )
                return _span_invoke_orig(p)

            self.router.add("lambda", "Invoke", _span_invoke_wrapper)


        ddb_east = self.internal("dynamodb", region=ADP_REGION)
        for tdef in _adp_writer.TABLE_DEFS:
            kw: dict[str, Any] = {
                "TableName": tdef["TableName"],
                "KeySchema": tdef["KeySchema"],
                "AttributeDefinitions": tdef["AttributeDefinitions"],
                "BillingMode": "PAY_PER_REQUEST",
            }
            if tdef.get("GlobalSecondaryIndexes"):
                kw["GlobalSecondaryIndexes"] = [
                    {
                        "IndexName": g["IndexName"],
                        "KeySchema": g["KeySchema"],
                        "Projection": g["Projection"],
                    }
                    for g in tdef["GlobalSecondaryIndexes"]
                ]
            ddb_east.create_table(**kw)

        # S3 bucket for ADP writer (us-east-1)
        s3_east = self.internal("s3", region=ADP_REGION)
        lake_bucket = _rw.LAKE_BUCKET
        try:
            s3_east.create_bucket(
                Bucket=lake_bucket,
                CreateBucketConfiguration={"LocationConstraint": ADP_REGION},
            )
        except Exception:
            pass  # already exists from base World
        # F6e: the deployed writer's VIN_BUCKET is its own engineering-vins bucket, not
        # the lake bucket (adp-staging-digital-thread-writer env, read 2026-10-23).  The
        # world used the lake bucket, so the script's VIN-list cleanup (which deletes from
        # the engineering-vins bucket, as live) could never be exercised here.
        self.vin_bucket = f"adp-{STAGE}-engineering-vins-{ACCOUNT}-{ADP_REGION}"
        # us-east-1 takes no LocationConstraint; don't swallow a failed create.
        s3_east.create_bucket(Bucket=self.vin_bucket)

        # CMS S3 bucket for the lifecycle_cache recall views
        s3 = self.internal("s3")
        try:
            s3.create_bucket(
                Bucket=CMS_ATHENA_BUCKET,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        except Exception:
            pass

        # Load the investigations handler
        n = uuid.uuid4().hex[:8]
        inv_env = {
            "AVX_INVESTIGATIONS_TABLE": INVESTIGATIONS_TABLE,
            "AVX_INVESTIGATIONS_QUEUE_URL": q_url,
            "AVX_INVESTIGATIONS_DAILY_CAP": str(self._daily_cap),
            "AVX_STAGE": STAGE,
        }
        with lambda_env(inv_env, "vsa-staging-api-avx-investigations", REGION):
            inv_handler = _rw._load_file(
                AVX_ROOT / "lambdas" / "api-avx-investigations" / "handler.py",
                f"fi_loop_world_investigations_{n}",
            )
        self.handlers["investigations"] = inv_handler
        self._inv_env = inv_env
        # Register in _api_env so _invoke_api can dispatch with the right lambda_env.
        self._api_env["investigations"] = inv_env
        for tmpl, method, kind in _NEW_AVX_ROUTES:
            fn = self.handlers[kind].handler
            # When auto_pump is enabled, wrap POST /investigations to synchronously
            # process the investigation via pump_investigation().  This avoids the
            # need for a background thread in test scenarios where the script polls.
            if self._auto_pump and method == "POST" and tmpl == "/investigations":
                _w = self
                _orig_fn = fn

                def _auto_pump_fn(
                    event: Any,
                    ctx: Any,
                    _orig: Any = _orig_fn,
                    _world: Any = _w,
                ) -> Any:
                    result = _orig(event, ctx)
                    if isinstance(result, dict) and result.get("statusCode") == 202:
                        try:
                            body = json.loads(result.get("body", "{}"))
                            inv_id = body.get("investigation_id", "")
                            req_body = json.loads(event.get("body") or "{}")
                            req_pat = (
                                req_body.get("pattern_id", "")
                                if isinstance(req_body, dict)
                                else ""
                            )
                            if inv_id and req_pat in _world._auto_drain_pattern_ids:
                                # Only pump + drain for seeded patterns (item 2's
                                # investigations).  Item 9a posts a fresh LIVETEST-9
                                # pattern that is NOT in _auto_drain_pattern_ids, so:
                                #   • pump_investigation is NOT called → investigation
                                #     stays queued; no extra finding is created.
                                #   • _drain_queue_message_for is NOT called → message
                                #     stays in the queue for item-9b to receive.
                                _world.pump_investigation(inv_id)
                                _world._drain_queue_message_for(inv_id)
                        except Exception:  # noqa: BLE001
                            pass  # processing failure will surface via poll
                    return result

                self.avx_api.add(tmpl, method, kind, _auto_pump_fn)
            else:
                self.avx_api.add(tmpl, method, kind, fn)

        # Register the fi_quality Bedrock fake (replaces the RW fake for quality calls)
        self.fi_quality_bedrock = FiQualityBedrock()
        self.router.add("bedrock-runtime", "Converse", self._converse_dispatch)

        # Load the ADP writer handler
        _cms_fi.ensure_path()
        self._adp_writer_mod = _adp_writer.load_handler()
        self._adp_writer_env = {
            "STAGE": STAGE,
            "VIN_BUCKET": self.vin_bucket,
            "ISSUED_RECALLS_BUCKET": lake_bucket,
            # digital-thread lake (handler at 02b8716f requires this; refuses writes without it).
            # Mapped to the world's lake bucket (same region, already created above).
            "THREAD_LAKE_BUCKET": lake_bucket,
        }
        adp_handler_fn = _adp_writer.make_lambda_handler(self._adp_writer_mod)
        # F6b clock fix: patch the writer's _now to use the world clock so
        # received_date in the issued-recall row matches the script's _utc_now.
        # Without this, the writer uses the real wall-clock date (via datetime.datetime.now)
        # while the script's _utc_now is patched by tests to a fixed date, causing
        # _check_ddb_issued_row's received_date <= next_run_date comparison to fail
        # after the pinned date has passed.
        _w_clock = self.clock
        self._adp_writer_mod._now = lambda: _w_clock.now()
        self.handlers["adp_writer"] = adp_handler_fn
        # Register as a Lambda in moto (us-east-1) so AdpExecutor can invoke it
        iam_east = self.internal("iam", region=ADP_REGION)
        try:
            role = iam_east.create_role(
                RoleName="fi-loop-adp-writer-role",
                AssumeRolePolicyDocument="{}",
            )["Role"]["Arn"]
        except Exception:
            role = f"arn:aws:iam::{ACCOUNT}:role/fi-loop-adp-writer-role"
        lam_east = self.internal("lambda", region=ADP_REGION)
        try:
            self.lam.create(
                lam_east, role, ADP_WRITER_FN_NAME,
                "digital_thread_writer.handler.handle_event",
                self._adp_writer_env,
                adp_handler_fn,
                timeout=60,
            )
        except Exception:
            pass
        # Also register in the world's default (us-west-2) Lambda store so
        # FakeLambda._env(name) (which uses w.internal("lambda") = us-west-2) can
        # look up the function's environment variables without ResourceNotFoundException.
        lam_west = self.internal("lambda", region=REGION)
        try:
            lam_west.create_function(
                FunctionName=ADP_WRITER_FN_NAME,
                Runtime="python3.12",
                Role=role,
                Handler="digital_thread_writer.handler.handle_event",
                Code={"ZipFile": b"stub"},
                Timeout=60,
                Environment={"Variables": self._adp_writer_env},
            )
        except Exception:
            pass
        # Patch the findings handler's ADP executor with the correct writer ARN
        self._patch_adp_executor_arn()

        # Load the CMS FI handler
        cms_env = _cms_fi.make_lambda_env()
        cms_env["ATHENA_OUTPUT_LOC"] = CMS_ATHENA_OUTPUT_LOC
        self._cms_fi_env = cms_env
        _cms_fi.ensure_path()
        with lambda_env(cms_env, "cms-staging-ui-FleetIntelligenceFn", REGION):
            try:
                cms_fi_index = importlib.import_module(
                    "services.fleet_intelligence.index"
                )
            except ImportError:
                # Try the path style
                cms_fi_index = _rw._load_file(
                    Path(_cms_fi._CMS_ROOT) / "services" / "fleet_intelligence" / "index.py",
                    f"fi_loop_world_cms_fi_{n}",
                )
        self.handlers["cms_fi"] = cms_fi_index
        self._cms_fi_index = cms_fi_index
        # Add a CMS FI fake REST API
        self.cms_api = _rw.FakeRestApi(
            "cms-staging-ui", STAGE, self.pool.verify, self._invoke_cms_api
        )
        self.cms_url = self.cms_api.url
        for tmpl, method, kind in _CMS_FI_ROUTES:
            fn = getattr(cms_fi_index, "handler", None)
            self.cms_api.add(tmpl, method, kind, fn)

        # Redirect the CMS executor's endpoint to the world's CMS API server.
        # agents.executors.cms._cms_base_endpoint() validates https:// (security review C1);
        # the world runs HTTP, so we replace the function while the world is open.
        # This is a world-layer fix: the real production code is unchanged; tests exercise
        # the full executor dispatch path (body building, retries, status mapping) against
        # the real CMS FI handler behind the world server.
        import agents.executors.cms as _cms_exec  # noqa: PLC0415
        _world_cms_url = self.cms_url.rstrip("/")
        self._cms_exec_mod = _cms_exec
        self._cms_base_endpoint_orig = _cms_exec._cms_base_endpoint

        def _world_cms_base_endpoint() -> str:
            return _world_cms_url

        _cms_exec._cms_base_endpoint = _world_cms_base_endpoint
        # Also update the findings handler's env so that lambda_env context exposes it.
        if hasattr(self, "_api_env") and "findings" in self._api_env:
            self._api_env["findings"]["VSA_CMS_API_ENDPOINT"] = _world_cms_url

        # Patch time on every loaded copy of the fi-quality script so that poll loops
        # (``_poll_investigation``, queue-drain in ``_item_6``) use the world clock
        # instead of real wall-clock time.  Without this, a test file that imports the
        # script under its own name (e.g. ``lv_s2a_mod``) never gets ``ScriptTime``
        # (``load_script`` only patches the module it loads internally for ``run_main``).
        # When all 8 test files run together, ``patch_loaded_modules`` accumulates
        # hundreds of stale ``fi_loop_world_investigations_*`` entries; ``pump_investigation``
        # occasionally exceeds the 30-second HTTP timeout and leaves the investigation in
        # ``"queued"`` state.  The poll then waits ``_INV_POLL_MAX_S`` (600) real seconds.
        from scripts.tests.fi_rw_world.clock import ScriptTime as _ScriptTime  # noqa: PLC0415
        _fq_script = str(AVX_ROOT / "scripts" / "live-verify-fi-quality.py")
        import sys as _sys  # noqa: PLC0415
        self._fq_time_saved: list[tuple[Any, Any]] = []
        _patched: set[int] = set()  # track patched modules by id to avoid duplicates
        for _m in list(_sys.modules.values()):
            _f = getattr(_m, "__file__", None) or ""
            if _f == _fq_script:
                _orig_time = getattr(_m, "time", None)
                _m.time = _ScriptTime(self.clock)
                self._fq_time_saved.append((_m, _orig_time))
                _patched.add(id(_m))

        # With --import-mode=importlib, pytest may load the item9 test module a second
        # time, which re-runs its module-level code and overwrites
        # sys.modules["lv_item9_mod"] with a new lv object (lv_2).  The original lv
        # (lv_1), which _call_main uses, is no longer a direct value in sys.modules —
        # it's an attribute of the test module container.  Find it via that path.
        for _container in list(_sys.modules.values()):
            _lv_attr = getattr(_container, "lv", None)
            if _lv_attr is None or id(_lv_attr) in _patched:
                continue
            _f_attr = getattr(_lv_attr, "__file__", None) or ""
            if _f_attr == _fq_script:
                _orig_attr_time = getattr(_lv_attr, "time", None)
                _lv_attr.time = _ScriptTime(self.clock)
                self._fq_time_saved.append((_lv_attr, _orig_attr_time))
                _patched.add(id(_lv_attr))

    # ------------------------------------------------------------------ dispatcher

    def _converse_dispatch(self, p: dict[str, Any]) -> dict[str, Any]:
        """Route Converse calls to the appropriate fake.

        The fi_quality agent uses ``record_root_cause`` / ``record_design_options``
        tools.  All other calls go to the base world's ``_converse`` (the
        recall/warranty fake that fills ``record_recall_plan`` / ``record_warrant_plan``).
        """
        tool_name = (
            p.get("toolConfig", {}).get("toolChoice", {}).get("tool", {}).get("name", "")
        )
        if tool_name in ("record_root_cause", "record_design_options"):
            return self.fi_quality_bedrock.converse(p)
        return self._converse(p)

    def _patch_adp_executor_arn(self) -> None:
        """Update agents.executors.registry.EXECUTORS["adp"]._arn to the moto ARN.

        The findings handler imports the registry lazily (``from agents.executors import
        registry`` inside ``_dispatch_action``).  The registry is a module-level singleton
        whose ``AdpExecutor`` reads ``_arn`` from the env-var ``ADP_THREAD_WRITER_ARN``
        when ``_arn`` is blank.  Setting ``_arn`` directly avoids any env-var race.
        Also stores the ARN in ``_findings_env`` so the findings handler's
        ``lambda_env`` context carries it (registry picks it up via os.environ).
        Also resets ``_lambda_client_injected`` so a stale boto3 Lambda client from a
        previous mock_aws context is not reused in the current world.
        """
        import agents.executors.registry as _reg  # already imported at world open time
        adp_exec = _reg.EXECUTORS.get("adp")
        if adp_exec is not None and hasattr(adp_exec, "_arn"):
            adp_exec._arn = ADP_WRITER_ARN
            adp_exec._lambda_client_injected = None  # reset stale client from prior world
        # Also inject into the findings handler's env dict so lambda_env exposes it.
        if hasattr(self, "_api_env") and "findings" in self._api_env:
            self._api_env["findings"]["ADP_THREAD_WRITER_ARN"] = ADP_WRITER_ARN

    def _invoke_cms_api(self, name: str, fn: Any, event: dict[str, Any], ctx: Any) -> dict[str, Any]:
        with lambda_env(self._cms_fi_env, name, REGION):
            return fn(event, ctx)

    # ------------------------------------------------------------------ lake refresh override
    def _refresh_lake(self) -> None:
        """Override base _refresh_lake to re-register ADP 9.1 VIEWs after the base recreates them as tables.

        W11 fix: the base world's _refresh_lake() calls athena.create_table with
        CREATE OR REPLACE TABLE for recall_vehicle_status.  Since we register it as
        a VIEW, DuckDB refuses to replace a VIEW with a TABLE.

        This override:
        1. DROPs the VIEWs first so the base's CREATE OR REPLACE TABLE succeeds.
        2. Calls super()._refresh_lake() to update dms_staging.repair_orders etc.
        3. Re-registers recall_vehicle_status and recall_campaigns_received as VIEWs.

        The _refresh_lake row re-injection of issued-recall rows is no longer needed
        — the VIEWs read recall_campaigns_issued directly.
        """
        from scripts.tests.fi_loop_world import adp_views as _adp_views  # noqa: PLC0415
        # Drop whatever exists (TABLE or VIEW) so base's CREATE OR REPLACE TABLE succeeds.
        # Check information_schema first to avoid using the wrong DROP type.
        con = self.athena.con
        for _schema, _name in (
            ("adp_staging_fleet_recall", "recall_vehicle_status"),
            ("adp_staging_fleet_recall", "recall_campaigns_received"),
        ):
            _row = con.execute(
                "SELECT table_type FROM information_schema.tables "
                f"WHERE table_schema='{_schema}' AND table_name='{_name}'"
            ).fetchone()
            if _row is not None:
                if _row[0] == "VIEW":
                    con.execute(f"DROP VIEW {_schema}.{_name}")
                else:
                    con.execute(f"DROP TABLE {_schema}.{_name}")
        super()._refresh_lake()
        # Re-register VIEWs after super created them as data tables.
        _adp_views.register_adp_views(self.athena, stage=STAGE)

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        # Restore ScriptTime patches on fi-quality module copies (patched in _extend_world).
        for _m, _orig in getattr(self, "_fq_time_saved", []):
            if _orig is not None:
                _m.time = _orig
        # Restore the CMS executor's endpoint function (patched in _extend_world).
        if hasattr(self, "_cms_exec_mod") and hasattr(self, "_cms_base_endpoint_orig"):
            self._cms_exec_mod._cms_base_endpoint = self._cms_base_endpoint_orig
        for api in (getattr(self, "cms_api", None),):
            if api is not None:
                api.close()
        super().close()

    # ------------------------------------------------------------------ helpers

    def _mint_engineer_token(self, sub: str = "eng-001") -> str:
        """Mint a product-engineer ID token for the given sub.

        Registers the user in the pool first (required: pool.verify checks sub in users).
        Idempotent: re-registering the same sub is a no-op in the moto CIP store.
        """
        user = {
            "sub": sub,
            "attrs": {"email": f"{sub}@example.invalid"},
            "groups": {"product-engineer"},
            "password": None,
            "status": "CONFIRMED",
            "created": self.clock.now(),
        }
        # Register into the pool's user dict so verify() can find the sub.
        if sub not in self.pool.users:
            self.pool.users[sub] = user
        return self.pool.mint(user)

    def _mint_fleet_operator_token(self, sub: str, fleet_id: str) -> str:
        """Mint a fleet-operator ID token scoped to *fleet_id*.

        Registers the user in the pool first (required: pool.verify checks sub in users).
        """
        user = {
            "sub": sub,
            "attrs": {
                "email": f"{sub}@example.invalid",
                "custom:fleetIds": fleet_id,
            },
            "groups": {"fleet-operator"},
            "password": None,
            "status": "CONFIRMED",
            "created": self.clock.now(),
        }
        if sub not in self.pool.users:
            self.pool.users[sub] = user
        return self.pool.mint(user)

    def post_investigation(
        self,
        *,
        pattern_id: str,
        sub: str = "eng-001",
    ) -> dict[str, Any]:
        """POST /investigations via the AVX investigations route.

        Returns the parsed response body.  Raises if the handler returns non-2xx.
        """
        import urllib.request as _urllib
        import urllib.error as _urlerr
        token = self._mint_engineer_token(sub)
        body_bytes = json.dumps({"pattern_id": pattern_id}).encode()
        req = _urllib.Request(
            f"{self.avx_url}investigations",
            data=body_bytes,
            method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            with _urllib.urlopen(req) as resp:
                return json.loads(resp.read())
        except _urlerr.HTTPError as e:
            err_bytes = e.read()
            raise AssertionError(
                f"POST /investigations returned {e.code}: {err_bytes.decode()}"
            ) from e

    def pump_investigation(self, investigation_id: str) -> dict[str, Any]:
        """Drive the Tier 2 Lambda with an SQS investigation record.

        Invokes the Lambda with a synthetic SQS event carrying *investigation_id*
        as the message body, matching the live ESM event shape.
        Returns the investigation row from DDB after processing.
        """
        sqs_event = {
            "Records": [{
                "messageId": f"msg-{uuid.uuid4().hex[:8]}",
                "receiptHandle": "handle-1",
                "body": investigation_id,
                "attributes": {
                    "ApproximateReceiveCount": "1",
                    "SentTimestamp": "1698000000000",
                    "SenderId": "123456789012",  # placeholder account (no IAM-id-shaped value)
                    "ApproximateFirstReceiveTimestamp": "1698000000001",
                },
                "messageAttributes": {},
                "md5OfBody": "abc",
                "eventSource": "aws:sqs",
                "eventSourceARN": f"arn:aws:sqs:{REGION}:{ACCOUNT}:{INVESTIGATIONS_QUEUE}",
                "awsRegion": REGION,
            }],
        }
        inv_lambda_env = dict(self.function_env(_rw.HEALTH_FN))
        inv_lambda_env["AVX_INVESTIGATIONS_TABLE"] = INVESTIGATIONS_TABLE
        inv_lambda_env["AVX_INVESTIGATIONS_QUEUE_URL"] = self._investigations_queue_url
        inv_lambda_env["SQS_MAX_RECEIVE_COUNT"] = "2"
        inv_lambda_env["ADP_THREAD_WRITER_ARN"] = ADP_WRITER_ARN
        with lambda_env(inv_lambda_env, _rw.HEALTH_FN, REGION):
            result = self.handlers["runner"].lambda_handler(sqs_event, None)
        # Read the investigation row
        t = self.table(INVESTIGATIONS_TABLE)
        item = t.get_item(
            Key={"investigation_id": investigation_id}, ConsistentRead=True
        ).get("Item", {})
        return {"result": result, "investigation": item}
    def seed_pattern(
        self,
        *,
        pattern_id: str,
        vin_list: list[str],
        failure_mode_id: str,
        cohort_dimension: str = "supplier_lot",
        cohort_value: str = "SUP-A/LOT-001",
        component_name: str = "Front Suspension Bushing",
        code_family: str = "P1AB",
    ) -> None:
        """Seed the ADP fault-patterns and engineering-corpus tables (DDB and Athena).

        Seeds both:
        - DDB tables (for the ADP digital-thread writer): ``adp_writer.seed_pattern``
          and ``adp_writer.seed_failure_mode``.
        - Athena tables (for the fi_quality investigation agent):
          ``fi_quality_athena.seed_fi_quality_tables``.

        F6a fix (brief §World): seed_pattern writes exactly the producer's attribute
        set (no failure_mode_id).  The corpus row carries cause_dimension="supplier_lot"
        and real-shaped symptom_codes so _rules_match_failure_mode finds the FM.
        """
        ddb_east = self.internal("dynamodb", region=ADP_REGION)
        _adp_writer.seed_pattern(
            ddb_east,
            pattern_id=pattern_id,
            vin_list=vin_list,
            cohort_dimension=cohort_dimension,
            cohort_value=cohort_value,
            code_family=code_family,
        )
        # Seed corpus with real-shaped fields so _rules_match_failure_mode works.
        # symptom_codes uses code_family prefix so the rules path picks this FM.
        # cause_dimension="supplier_lot" + do_not_drive=False satisfies the safe rule.
        _adp_writer.seed_failure_mode(
            ddb_east,
            failure_mode_id=failure_mode_id,
            component_name=component_name,
            do_not_drive=False,   # C10 fix: not a do_not_drive fm; safe to recall
            park_outside=False,
            options=[
                {
                    "remedy_option_id": "opt-recall-001",
                    "remedy_type": "hardware_recall",
                    "parts_cost_per_vehicle_usd": 120,
                    "labor_hours_per_vehicle": 2,
                    "lead_time_weeks": 8,
                    "over_the_air": False,
                    "recommended": True,
                    "component_lots": cohort_value,
                },
                {
                    "remedy_option_id": "opt-monitor-001",
                    "remedy_type": "monitor",
                    "recommended": False,
                },
            ],
        )
        # Also write extended corpus fields used by _rules_match_failure_mode:
        # cause_dimension, component_id, symptom_codes.
        # seed_failure_mode above writes to pk=fm#<id> — update in place via put_item.
        import boto3 as _b3
        ddb_r = _b3.resource("dynamodb", region_name=ADP_REGION)
        corpus_tbl = ddb_r.Table(f"adp-{STAGE}-engineering-corpus")
        # Fetch what was written, then add the extra fields.
        existing = corpus_tbl.get_item(
            Key={"pk": f"fm#{failure_mode_id}"}, ConsistentRead=True
        ).get("Item") or {}
        # symptom_codes: use 5-char codes whose 4-char prefix matches code_family.
        sym_code = code_family + "0"  # e.g. "P1AB" → "P1AB0"
        existing.update({
            "cause_dimension": "supplier_lot",
            # component_id must match what fi_quality_athena seeds in vehicle_build_components
            # (see fi_quality_athena.py: component_id = "suspension_bushing").
            "component_id": "suspension_bushing",
            "symptom_codes": [sym_code, code_family + "1"],
        })
        corpus_tbl.put_item(Item=existing)
        # Seed Athena tables for the fi_quality investigation agent.
        from scripts.tests.fi_loop_world import fi_quality_athena as _fqa
        _fqa.seed_fi_quality_tables(
            self.athena,
            stage=STAGE,
            pattern_id=pattern_id,
            vin_list=vin_list,
            failure_mode_id=failure_mode_id,
            cohort_dimension=cohort_dimension,
            cohort_value=cohort_value,
            code_family=code_family,
            option_id="opt-recall-001",
        )
        # Register this pattern_id so the auto_pump wrapper will drain its SQS
        # messages after pump_investigation.  Only seeded patterns are drained;
        # item-9a's fresh pattern is NOT in this set so its message stays in the
        # queue for item-9b to receive.
        self._auto_drain_pattern_ids.add(pattern_id)

    def idp_client(self) -> "_FakeIDP":
        """Return a boto3-like cognito-idp shim backed by the world's FakeUserPool.

        Use this instead of ``w.internal("cognito-idp")`` in tests.
        moto's ``cognitoidp`` backend imports ``joserfc`` at module load time,
        which is not in the venv.  The shim routes every call directly to
        ``FakeUserPool`` (HMAC-signed tokens, no joserfc needed).
        ``list_user_pools`` is added by the shim (not in FakeUserPool.register)
        and returns the world pool under the name ``cms-staging-ui-users`` so
        ``_resolve_pool_id`` in the script finds exactly one match.
        """
        return _FakeIDP(self.pool)

    def seed_recall_cache(
        self,
        *,
        fleet_id: str,
        vin_list: list[str],
        campaign_id: str | None = None,
    ) -> None:
        """Seed the CMS recall cache in moto S3 so RTS can proceed.

        When ``campaign_id`` is given the cache slice includes it in
        ``campaigns`` and in ``vehicle_status`` (one row per VIN), so the
        after-run check can confirm CMS's recall list shows the campaign.
        When omitted the campaigns list is empty (original RTS-only behaviour).

        ``computed_at`` uses the world clock (``self.clock.now()``).
        CMS's ``recall_source._utcnow()`` also reads the world clock, because
        ``WorldClock.install`` swaps ``datetime.datetime`` on the ``datetime``
        module itself; seeding from real wall-clock time makes the slice appear
        up to 5 min in the future once the world and real clocks diverge (W2 fix).
        """
        s3 = self.internal("s3")
        computed_at = self.clock.now().isoformat()
        _cms_fi.seed_recall_cache_s3(
            s3,
            bucket=CMS_ATHENA_BUCKET,
            s3_prefix=CMS_ATHENA_PREFIX,
            fleet_id=fleet_id,
            vin_list=vin_list,
            computed_at=computed_at,
            campaign_id=campaign_id,
        )

    def seed_health_faults_view(
        self,
        *,
        vin: str,
        fault_code: str,
        computed_at: str,
        repair_ref: str | None = None,
        repaired_on: str | None = None,
        last_seen: str | None = None,
        data_as_of: str | None = None,
    ) -> None:
        """Seed the CMS health-faults lifecycle_cache view in moto S3.

        C7 real-route fix: ``return_to_service`` calls ``_health_views("faults",
        "faults", shard=vin_shard(vin))`` which reads a lifecycle_cache manifest
        and vin-shard view.  This method writes those objects so the real CMS
        handler can answer based on whether ``repair_ref`` is present.

        ``computed_at`` must be a UTC ISO-8601 string (e.g.
        ``datetime.utcnow().isoformat()``).

        When ``repair_ref`` is given, the item has all required fields and
        ``fault_repair_blockers`` returns ``(None, [])`` (vehicle returned).
        When ``repair_ref`` is None, the item exists but has no repair — the
        fault is ``fault_not_repaired``.

        ``data_as_of`` defaults to today's UTC date (``YYYY-MM-DD``).
        """
        import gzip as _gz  # noqa: PLC0415
        import hashlib as _hash  # noqa: PLC0415
        import json as _j  # noqa: PLC0415
        import datetime as _dt2  # noqa: PLC0415

        s3 = self.internal("s3")
        bucket = CMS_ATHENA_BUCKET
        athena_prefix = CMS_ATHENA_PREFIX  # e.g. "fleet-intelligence/"

        # Health views root: {prefix}_cache/fi-health-faults-v1/
        faults_prefix = f"{athena_prefix}_cache/fi-health-faults-v1/"

        # Use a fixed generation format (matches _GENERATION_RE)
        generation = "20261022T100000000000Z-00000001"

        today = _dt2.datetime.now(_dt2.timezone.utc).date().isoformat()
        data_as_of_val = data_as_of if data_as_of is not None else today

        # Compute the VIN shard (sha256(vin)[:4] % 16)
        shard = int.from_bytes(
            _hash.sha256(vin.encode("utf-8")).digest()[:4], "big"
        ) % 16

        # Build the item for this vin+code
        item: dict = {
            "vin": vin,
            "code": fault_code,
            "dataAsOf": data_as_of_val,
        }
        if repair_ref is not None:
            item["repairRef"] = repair_ref
            item["repairedOn"] = repaired_on or today
            item["lastSeen"] = last_seen or today

        # Manifest
        manifest_obj = {
            "schemaVersion": 2,
            "computedAt": computed_at,
            "generation": generation,
            "windowMonths": 0,
            "seriesShards": 16,
            "kinds": ["health"],
            "fleets": [],
        }
        s3.put_object(
            Bucket=bucket,
            Key=f"{faults_prefix}manifest.json",
            Body=_j.dumps(manifest_obj).encode("utf-8"),
        )

        # Vin-shard view
        view_obj = {
            "schemaVersion": 2,
            "kind": "health",
            "generation": generation,
            "windowMonths": 0,
            "scope": {"dataset": "faults", "shard": shard},
            "computed_at": computed_at,
            "items": [item],
        }
        view_key = (
            f"{faults_prefix}{generation}/health/"
            f"faults-vin-shard-{shard:02d}.json.gz"
        )
        s3.put_object(
            Bucket=bucket,
            Key=view_key,
            Body=_gz.compress(_j.dumps(view_obj).encode("utf-8")),
        )

    def make_guarded_session(self, part: str) -> Any:
        """Return a boto3.Session-like object that enforces the part's allowlist.

        W11 fix: world e2e tests must not replace ``_client`` entirely (which
        bypasses the allowlist guard).  Instead they set ``_mod._SESSION`` to the
        return value of this method before calling ``main()``.

        The session goes through the real guard (``_make_guarded_bcsession``) for
        every service that moto can handle.  For ``cognito-idp`` specifically, moto
        requires ``joserfc`` which is not in the venv; the session returns
        ``self.idp_client()`` (the ``_FakeIDP`` shim) for that service so the
        allowlist guard fires on botocore's ``before-parameter-build`` event but
        the actual HTTP call goes to the in-process fake, not moto.

        moto intercepts all other services at the HTTP transport layer, so calls
        from this session's boto3 clients land in moto exactly as they would from
        an unguarded ``boto3.client(...)`` call within ``mock_aws()``.

        Usage::

            _mod._SESSION = None          # force re-init for this part
            _mod._SESSION = w.make_guarded_session("after-run")
            main(["--stage", STAGE, "--part", "after-run", "--run-id", RUN_ID])
        """
        import boto3 as _b3  # noqa: PLC0415
        # Import the live-verify module the test loads as _mod.
        # AVX_ROOT is the repo root; live-verify-fi-loop.py is at scripts/.
        # The test imports it via importlib so we can reach it the same way.
        import importlib.util as _ilu  # noqa: PLC0415
        import sys as _sys  # noqa: PLC0415

        _script_path = AVX_ROOT / "scripts" / "live-verify-fi-loop.py"
        _spec_lv = _ilu.spec_from_file_location("_lv_tmp", str(_script_path))
        if _spec_lv is None:
            raise RuntimeError(f"cannot find live-verify-fi-loop.py at {_script_path}")
        # Use the already-imported module if the test already imported it.
        _mod_name = "live_verify_fi_loop"
        _lv = _sys.modules.get(_mod_name) or _sys.modules.get("scripts.live_verify_fi_loop")
        if _lv is None:
            # Fall back: load fresh (this shouldn't happen in tests since the test
            # module imports the script first).
            _lv_mod = _ilu.module_from_spec(_spec_lv)  # type: ignore[arg-type]
            _spec_lv.loader.exec_module(_lv_mod)  # type: ignore[union-attr]
            _lv = _lv_mod

        allowed = _lv._PART_ALLOWLISTS.get(part, frozenset())  # type: ignore[attr-defined]
        bc = _lv._make_guarded_bcsession(allowed)  # type: ignore[attr-defined]
        base_session = _b3.Session(botocore_session=bc)

        # Wrap ``client()`` so cognito-idp returns the FakeIDP shim instead of the
        # moto backend (which requires joserfc).  All other services go through the
        # guarded boto3 session (moto-intercepted because we are inside mock_aws()).
        # W6 fix: wrap the shim in _GuardedIdpProxy so the guard fires for
        # un-allowlisted Cognito ops (a1/a3/a5 mutant class).
        # The proxy is created ONCE and cached so callers that monkeypatch methods
        # on the returned object (e.g. test_gg10) get consistent behaviour across
        # multiple calls to session.client("cognito-idp", ...).
        _fake_idp_instance = self.idp_client()
        _guarded_idp_instance = _GuardedIdpProxy(
            _fake_idp_instance, allowed, _lv.GuardViolation
        )
        _orig_client = base_session.client

        def _patched_client(service: str, region_name: str | None = None, **kw: Any) -> Any:
            if service == "cognito-idp":
                return _guarded_idp_instance  # same instance every call
            return _orig_client(service, region_name=region_name, **kw)

        base_session.client = _patched_client  # type: ignore[method-assign]
        return base_session


    # ------------------------------------------------------------------ stage-3 helpers

    def _drain_queue_message_for(self, investigation_id: str) -> None:
        """Receive and delete the SQS message for *investigation_id* from the
        investigations queue.

        Called by the auto_pump wrapper after pump_investigation so the actual
        SQS message sent by the /investigations POST handler is removed.
        Without this, items 8b (queue-empty pre-check) and cleanup-only
        (depth=1 guard) see leftover messages from item 2's POSTs and FAIL.

        Uses a short poll (WaitTimeSeconds=0) and a single receive attempt;
        silently skips if the message is not found (e.g. already consumed or
        never arrived due to handler error).

        Any message received that does NOT belong to *investigation_id* has its
        visibility immediately restored (VisibilityTimeout=0) so the helper
        cannot hide messages that belong to other investigations or steps.
        """
        sqs = self.internal("sqs")
        q_url = self._investigations_queue_url
        try:
            resp = sqs.receive_message(
                QueueUrl=q_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=0,
            )
            for msg in resp.get("Messages", []):
                if msg.get("Body", "").strip() == investigation_id:
                    sqs.delete_message(
                        QueueUrl=q_url,
                        ReceiptHandle=msg["ReceiptHandle"],
                    )
                    return
                # Not our message — restore visibility immediately so other
                # consumers (items 8b, cleanup-only, other investigations) can
                # still see it.
                try:
                    sqs.change_message_visibility(
                        QueueUrl=q_url,
                        ReceiptHandle=msg["ReceiptHandle"],
                        VisibilityTimeout=0,
                    )
                except Exception:  # noqa: BLE001
                    pass  # best-effort; caller's test will surface the problem
        except Exception:  # noqa: BLE001
            pass  # best-effort drain; test failures will reveal if this mattered

    def fail_item9_investigation(self, investigation_id: str) -> None:
        """Simulate the quality job failing an investigation twice → DLQ + alarm.

        Drives ``pump_investigation`` twice with receive-count 1 then 2.  On the
        second attempt ``job.py`` sets the investigation row to ``failed``; the
        runner returns ``batchItemFailures``.  After two failures the world
        moves the SQS message from the main queue into the DLQ (matching the live
        SQS redrive policy) and fires the DLQ depth alarm.

        The investigation row for *investigation_id* must already exist in the table
        (posted via POST /investigations) and the pattern named in the row must NOT
        be in the world's fake Athena (so the quality job raises PatternNotFound).
        """
        sqs = self.internal("sqs")
        q_url = self._investigations_queue_url
        dlq_url = self._investigations_dlq_url

        for receive_count in (1, 2):
            sqs_event = {
                "Records": [{
                    "messageId": f"msg-9-{investigation_id[:8]}-rc{receive_count}",
                    "receiptHandle": f"rh-9-rc{receive_count}",
                    "body": investigation_id,
                    "attributes": {
                        "ApproximateReceiveCount": str(receive_count),
                        "SentTimestamp": "1698000000000",
                        "SenderId": "123456789012",
                        "ApproximateFirstReceiveTimestamp": "1698000000001",
                    },
                    "messageAttributes": {},
                    "md5OfBody": "abc",
                    "eventSource": "aws:sqs",
                    "eventSourceARN": (
                        f"arn:aws:sqs:{REGION}:{ACCOUNT}:{INVESTIGATIONS_QUEUE}"
                    ),
                    "awsRegion": REGION,
                }],
            }
            inv_lambda_env = dict(self.function_env(_rw.HEALTH_FN))
            inv_lambda_env["AVX_INVESTIGATIONS_TABLE"] = INVESTIGATIONS_TABLE
            inv_lambda_env["AVX_INVESTIGATIONS_QUEUE_URL"] = q_url
            inv_lambda_env["SQS_MAX_RECEIVE_COUNT"] = "2"
            inv_lambda_env["ADP_THREAD_WRITER_ARN"] = ADP_WRITER_ARN
            with lambda_env(inv_lambda_env, _rw.HEALTH_FN, REGION):
                result = self.handlers["runner"].lambda_handler(sqs_event, None)
            assert "batchItemFailures" in result, (
                f"expected batchItemFailures on receive_count={receive_count}, "
                f"got {result!r}"
            )

        # Simulate SQS moving the message to the DLQ after max receive count. A redrive
        # MOVES the message: take it off the main queue (whatever its visibility; 9a
        # received it with a long visibility timeout) and only then put it on the DLQ.
        # If it is no longer on the main queue there is nothing to redrive (fix round 2).
        from moto.sqs.models import sqs_backends as _sqs_backends  # noqa: PLC0415
        _q = _sqs_backends[ACCOUNT][REGION].get_queue(INVESTIGATIONS_QUEUE)
        _before = len(_q._messages)
        _q._messages = [m for m in _q._messages if m.body.strip() != investigation_id]
        if len(_q._messages) == _before:
            return
        sqs.send_message(QueueUrl=dlq_url, MessageBody=investigation_id)

        # Pump the DLQ depth alarm
        dlq_depth = 1  # we just sent one message
        self.dlq_alarm.pump(dlq_depth)
        # Advance clock by one alarm period (300s) + evaluation delay (60s) so the
        # alarm timestamp is after any StartDate filter the script might use.
        self.clock.advance(361)
