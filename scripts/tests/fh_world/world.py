"""fh_world/world.py — FhWorld: in-process world for fi_fleet_health item tests.

Subclasses fi_rw_world.World, overriding only what differs:
  - script path (live-verify-fi-fleet-health.py)
  - ADP lake  (install_fleet_health_tables from adp_lake.py)
  - health Lambda env (health_agent_env.json with group6_changes + pool_id)
  - schedules (fh_world/data/schedules.json, service ENABLED for 6.2)
  - nightly runs (battery 02:00, tires 02:20, brakes 02:40, service 05:40)

Everything else — DMS/AVX APIs, consumer, broker, Cognito — is inherited unchanged.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys as _sys
from pathlib import Path
from typing import Any

from scripts.tests.fi_rw_world.world import (
    ACCOUNT, ADP_REGION, ADJUDICATOR_FN, ALARM, AVX_ROOT, BROKER_LOG_GROUP,
    DEALER_PROGRESS_FN, DMS_ROOT, HEALTH_FN, LAKE_BUCKET, REGION, SHAPES,
    STAGE, TOPIC, WORKGROUP, World, _load_file,
)
from scripts.tests.fi_rw_world.alarm import RunFailureAlarm
from scripts.tests.fi_rw_world.awsfake import AwsError
from scripts.tests.fh_world.alarm import StalenessAlarm
from scripts.tests.fh_world.adp_lake import (
    _adp_root, install_fleet_health_tables,
)
from scripts.tests.fh_world import seed as _fh_seed

# ---------------------------------------------------------------------------
# Module-level constants (loaded once; no AWS calls)
# ---------------------------------------------------------------------------
FH_SCRIPT = AVX_ROOT / "scripts" / "live-verify-fi-fleet-health.py"

_FH_DATA = Path(__file__).parent / "data"
_FH_ENV_RAW: dict = json.loads((_FH_DATA / "health_agent_env.json").read_text())
# group6_changes applied on top of base (TIER2_MAX_VINS_BATTERY 375 → 600)
_FH_HEALTH_BASE_ENV: dict[str, str] = {
    **_FH_ENV_RAW["Environment"],
    **_FH_ENV_RAW["group6_changes"],
}

_FH_SCHEDULES: list[dict] = json.loads(
    (_FH_DATA / "schedules.json").read_text()
)["schedules"]

_VIN_CMS_FLEET_COLS = [("vin", "string"), ("fleet_id", "string"), ("tier", "string")]


# ---------------------------------------------------------------------------
# FhWorld
# ---------------------------------------------------------------------------
class FhWorld(World):
    """Shared in-process world for fi_fleet_health item tests.

    Opens with real battery/tires/brakes/service nightly runs so every test
    starts from a steady-state findings + run-records picture.

    live_defects: frozenset of names that inject observable defects into the
    world so tests can verify the script detects them.  Supported values::

        ``"pm_view_flip"`` — after each view recompute, flip VIN_PM_OVERDUE/
        TIRE_ROTATION status to ``"current"`` in pm_vehicle_status (the live
        view).  The oracle retains ``"overdue"``, so 4-view-match FAILs.
    """

    def __init__(self, *args: Any, live_defects: frozenset[str] = frozenset(),
                 **kwargs: Any) -> None:
        known = {"pm_view_flip"}
        unknown = set(live_defects) - known
        if unknown:
            raise ValueError(f"unknown FhWorld live defects {unknown}")
        self.fh_live_defects: frozenset[str] = frozenset(live_defects)
        super().__init__(*args, **kwargs)

    # -- script path -------------------------------------------------------
    def load_script(self) -> Any:
        import uuid
        from scripts.tests.fi_rw_world.clock import ScriptTime
        mod = _load_file(FH_SCRIPT, f"lv_fh_world_{uuid.uuid4().hex[:8]}")
        mod.time = ScriptTime(self.clock)
        self.clock.patch_loaded_modules([str(FH_SCRIPT)])
        return mod

    def run_main(self, argv: list[str], mod: Any = None, *,
                 live: bool = True) -> tuple[int, str, Any]:
        """Override to use FH_SCRIPT as argv[0] and suppress --dms-repo injection.

        The fleet health script has no --dms-repo argument; the base class
        unconditionally appends it, which causes argparse to exit(2).
        """
        import contextlib
        import io
        import os
        import signal
        mod = mod or self.load_script()
        old_argv = _sys.argv
        out = io.StringIO()
        if live:
            os.environ["LIVE_TESTS"] = "1"
        self.calls_before_main = len(self.router.all_calls)
        self.invocations_before_main = len(self.lam.invocations)
        saved_signals = {s: signal.getsignal(s)
                         for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            # Fleet health script uses no --dms-repo; set argv[0] correctly.
            _sys.argv = [str(FH_SCRIPT), *argv]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    mod._install_signal_handlers()
                    rc = mod.main()
                except SystemExit as e:
                    rc = e.code if isinstance(e.code, int) else 1
                    self.system_exit = rc
        finally:
            _sys.argv = old_argv
            os.environ.pop("LIVE_TESTS", None)
            for s, h in saved_signals.items():
                signal.signal(s, h)
        text = out.getvalue()
        for line in text.splitlines():
            if line.startswith("manifest:"):
                self.manifests.append(Path(line.split(":", 1)[1].strip()))
        return rc, text, mod

    # -- DynamoDB tables ---------------------------------------------------
    def _create_tables(self) -> None:
        super()._create_tables()
        # vsa-staging-health-findings: pk/sk strings, no GSIs.
        # Agent-2 artifact writer (put) and cursor store (put_raw) use this table.
        self.internal("dynamodb").create_table(
            TableName="vsa-staging-health-findings",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    # -- seed FhWorld VINs in CMS / DMS tables ----------------------------
    def _seed_data(self) -> None:
        super()._seed_data()  # fi_rw_world VINs + DMS warranty + candidates job
        now_iso = self.clock.iso()
        # Ensure FhWorld fleets exist (may not overlap with fi_rw_world FLEETS)
        existing = {r["fleetId"] for r in self.scan("cms-staging-storage-fleets")}
        for f in _fh_seed.FLEETS:
            if f not in existing:
                self.put("cms-staging-storage-fleets", {"fleetId": f, "name": f})
        # CMS vehicles and DMS fleet membership for FhWorld VINs.
        # vehicleId uses VEHICLE_IDS override when set (mirrors staging VEH-MRDN-* ids);
        # vin-index projection is KEYS_ONLY so owner_resolver Query → GetItem works.
        for v, flt in _fh_seed.VIN_FLEET.items():
            vid = _fh_seed.VEHICLE_IDS.get(v, v)
            self.put("cms-staging-storage-vehicles", {
                "vehicleId": vid, "vin": v, "fleetId": flt,
                "make": "Meridian", "model": "Azimuth", "year": 2023})
            self.put("dms-staging-fleet-membership", {
                "vehicle_vin": v, "fleet_id": flt, "synced_at": now_iso})
        self._refresh_marker()

    # -- ADP lake: fleet-health Athena tables + vin_cms_fleet --------------
    def _create_lake_tables(self) -> None:
        """Add fleet-health tables on top of base recall / DMS setup."""
        super()._create_lake_tables()
        adp = _adp_root()
        install_fleet_health_tables(self.athena, self.today, adp)
        # adp_{stage}_cms_ingest.vin_cms_fleet — Agent-2 managed-VIN pool counts
        # (battery joins energy_usage × managed_vehicles; tires/brakes likewise)
        self.athena.create_table(
            f"adp_{STAGE}_cms_ingest.vin_cms_fleet",
            _VIN_CMS_FLEET_COLS,
            [{"vin": v, "fleet_id": f, "tier": "managed"}
             for v, f in _fh_seed.VIN_FLEET.items()],
        )

    def _refresh_lake(self) -> None:
        """Extends base refresh; recomputes pm_vehicle_status and vehicle_fault_status
        views from current raw data after each DDB sync.

        In live ADP both are views computed at query time over the raw tables
        (incl. dms_{stage}.repair_orders); only the *_daily tables are snapshots.
        We mirror that: after the base class rebuilds dms_staging.repair_orders from DDB,
        re-insert the PM seed ROs and recompute both view tables so that DMS service ROs
        created by item 3 are visible to item 4's Athena queries.  The *_daily tables
        are written ONLY at open() (mirrors the nightly snapshot job) and are NEVER
        rewritten here.
        """
        super()._refresh_lake()
        from scripts.tests.fh_world.adp_lake import (
            _adp_root, _ensure_adp_importable, _repair_orders_df, _insert_pm_repair_orders,
            _fleet_ops_faults_df, _fault_codes_df, _vin_cms_fleet_df, _code_labor_op_map,
            _service_records_df, _pm_plans_df, _cms_vehicles_df, _daily_usage_df,
        )
        _insert_pm_repair_orders(self.athena, _repair_orders_df())
        s = STAGE
        ro_df = self.athena.con.execute(
            f'SELECT ro_id, vehicle_vin, status, updated_at, "_seq_no", lines'
            f' FROM dms_{s}.repair_orders'
        ).df()
        # view_runner expects lines as [{labor_op: ...}] only; strip extra DMS fields
        import json as _json

        def _slim_lines(v: object) -> str:
            try:
                rows = _json.loads(v) if v else []
                return _json.dumps(
                    [{"labor_op": r.get("labor_op", "")} for r in rows if isinstance(r, dict)]
                )
            except Exception:  # noqa: BLE001
                return "[]"

        ro_df["lines"] = ro_df["lines"].apply(_slim_lines)
        adp = _adp_root()
        _ensure_adp_importable(adp)
        from fleet_fault_events.view_runner import (  # type: ignore
            run_vehicle_fault_status, run_pm_vehicle_status,
        )
        vfs_df = run_vehicle_fault_status(
            fleet_ops_faults=_fleet_ops_faults_df(),
            fault_codes=_fault_codes_df(adp),
            vin_cms_fleet=_vin_cms_fleet_df(),
            repair_orders=ro_df,
            code_labor_op_map=_code_labor_op_map(adp),
            data_as_of=self.today,
            service_records=_service_records_df(),
        )
        pm_df = run_pm_vehicle_status(
            pm_plans=_pm_plans_df(),
            cms_vehicles=_cms_vehicles_df(),
            service_records=_service_records_df(),
            repair_orders=ro_df,
            vin_cms_fleet=_vin_cms_fleet_df(),
            daily_usage=_daily_usage_df(),
            code_labor_op_map=_code_labor_op_map(adp),
            data_as_of=self.today,
        )
        from scripts.tests.fh_world.adp_lake import _install_view_tables_only
        # Always refresh VIEW tables only; dailies written once at open().
        _install_view_tables_only(self.athena, vfs_df, pm_df, self.today)
        if "pm_view_flip" in self.fh_live_defects:
            # Simulate a view-level defect: flip VIN_PM_OVERDUE/TIRE_ROTATION to
            # "current" in the live view so oracle and view disagree → 4-view-match FAIL.
            self.athena.con.execute(
                "UPDATE adp_staging_fleet_health.pm_vehicle_status "
                "SET status = 'current', next_due_date = '2027-03-01' "
                "WHERE vin = ? AND task_code = 'TIRE_ROTATION'",
                [_fh_seed.VIN_PM_OVERDUE],
            )

    def _write_answer_key(self) -> None:
        """Super writes recall answer key (also creates LAKE_BUCKET).
        Then upload config parquet via ADP's real write_config_tables."""
        super()._write_answer_key()
        adp = _adp_root()
        import sys
        adp_pkg = str(adp / "platform-foundation/source/data-products/fleet_fault_events")
        if adp_pkg not in sys.path:
            sys.path.insert(0, adp_pkg)
        from fleet_fault_events.daily import write_config_tables  # type: ignore
        write_config_tables(
            LAKE_BUCKET, self.today, s3_client=self.internal("s3"))

    # -- Lambda environment -----------------------------------------------
    def env_of(self, key: str) -> dict[str, str]:
        if key != "health_agent":
            return super().env_of(key)
        env = dict(_FH_HEALTH_BASE_ENV)
        env["CMS_USER_POOL_ID"] = self.pool.pool_id
        return env

    # -- Infrastructure (schedules from fh_world data, all ENABLED) ------
    def pump(self) -> None:
        """Pump all world components including the staleness alarm."""
        super().pump()
        self.staleness_alarm.pump()

    def _create_infra(self) -> None:
        iam = self.internal("iam")
        role = iam.create_role(
            RoleName="fh-world-role",
            AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        lam = self.internal("lambda")
        runner = self.handlers["runner"]
        stand_ins = self.handlers["fi_stand_ins"]
        self.lam.create(lam, role, HEALTH_FN, "agents.tier2.runner.lambda_handler",
                        self.env_of("health_agent"), runner.lambda_handler, timeout=900)
        self.lam.create(lam, role, DEALER_PROGRESS_FN,
                        "source.simulation.fi_stand_ins.dealer_progress_handler",
                        self.env_of("dms_dealer_progress"),
                        stand_ins.dealer_progress_handler, timeout=300)
        self.lam.create(lam, role, ADJUDICATOR_FN,
                        "source.simulation.fi_stand_ins.warranty_adjudicator_handler",
                        self.env_of("dms_adjudicator"),
                        stand_ins.warranty_adjudicator_handler, timeout=300)
        self.health_arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{HEALTH_FN}"
        sch = self.internal("scheduler")
        for s in _FH_SCHEDULES:
            kw: dict[str, Any] = {
                "Name": s["Name"], "GroupName": s["GroupName"],
                "ScheduleExpression": s["ScheduleExpression"],
                "State": "ENABLED",   # world models staging after 6.2 (service enabled)
                "FlexibleTimeWindow": {"Mode": "OFF"},
                "Target": {"Arn": self.health_arn, "RoleArn": role,
                           "Input": s["Target"]["Input"],
                           "RetryPolicy": s["Target"]["RetryPolicy"]},
            }
            if s.get("ScheduleExpressionTimezone"):
                kw["ScheduleExpressionTimezone"] = s["ScheduleExpressionTimezone"]
            sch.create_schedule(**kw)
        sns = self.internal("sns")
        self.topic_arn = sns.create_topic(Name=TOPIC)["TopicArn"]
        sns.subscribe(TopicArn=self.topic_arn, Protocol="email",
                      Endpoint="operator@example.invalid")
        self.alarm = RunFailureAlarm(self, ALARM, HEALTH_FN, self.topic_arn)
        self.alarm.create(self.internal("cloudwatch"))
        self.alarm.register(self.router)
        # Staleness alarm: vsa-{stage}-avx-run-stale-service-{region}
        # (vsa-tier2-stack.ts:2249, runStalenessWindows entry for 'service')
        stale_name = f"vsa-{STAGE}-avx-run-stale-service-{REGION}"
        self.staleness_alarm = StalenessAlarm(
            self, stale_name, self.topic_arn, STAGE, REGION)
        self.staleness_alarm.create(self.internal("cloudwatch"))
        self.staleness_alarm.register(self.router)  # overwrites DescribeAlarmHistory
        self.internal("logs").create_log_group(logGroupName=BROKER_LOG_GROUP)
        ssm = self.internal("ssm")
        ssm.put_parameter(Name=f"/cvx/{STAGE}/voice-auth-user-pool-id",
                          Value=self.pool.pool_id, Type="String")
        for k, v in (("apns-severity-threshold", "p0"), ("cardinality-cap", "15"),
                     ("rate-limit", '{"day":10,"hour":3}')):
            ssm.put_parameter(Name=f"/vsa/{STAGE}/avx/broker/{k}", Value=v, Type="String")
        cfn = self.internal("cloudformation")
        for stack, key, url in (
            (f"vsa-{STAGE}-api", "RestApiUrl", self.avx_url),
            (f"DmsApiStack-{STAGE}", "ApiUrl", self.dms_url),
        ):
            cfn.create_stack(StackName=stack, TemplateBody=json.dumps({
                "Resources": {"Placeholder": {"Type": "AWS::SNS::Topic"}},
                "Outputs": {key: {"Value": url}}}))
        self._model_lambda_revision_ids()

    def _model_lambda_revision_ids(self) -> None:
        """Lambda's RevisionId, which moto 5.2.3 lacks.

        Live ``GetFunctionConfiguration`` returns a ``RevisionId``, and an
        ``UpdateFunctionConfiguration`` whose ``RevisionId`` is not the latest is refused
        (``PreconditionFailedException``, HTTP 412). Each successful update gives a new id.
        Served from moto's own function state; moto still applies the update."""
        import uuid
        from moto.awslambda.models import lambda_backends
        from moto.core import DEFAULT_ACCOUNT_ID
        revs: dict[str, str] = {}

        def _name(p: dict[str, Any]) -> str:
            return str(p["FunctionName"]).split(":function:")[-1].split(":")[0]

        def _rev(name: str) -> str:
            return revs.setdefault(name, str(uuid.uuid4()))

        def get_cfg(p: dict[str, Any]) -> dict[str, Any]:
            name = _name(p)
            fn = lambda_backends[DEFAULT_ACCOUNT_ID][REGION].get_function(name)
            cfg = dict(fn.get_configuration())
            cfg["RevisionId"] = _rev(name)
            return cfg

        def update_cfg(p: dict[str, Any]) -> None:
            name = _name(p)
            want = p.get("RevisionId")
            if want is not None and want != _rev(name):
                raise AwsError("PreconditionFailedException",
                               "The Revision Id provided does not match the latest Revision "
                               "Id. Call the GetFunction/GetAlias API to retrieve the latest "
                               "Revision Id", status=412)
            revs[name] = str(uuid.uuid4())
            return None  # moto applies the update

        self.router.add("lambda", "GetFunctionConfiguration", get_cfg)
        self.router.add("lambda", "UpdateFunctionConfiguration", update_cfg)
        self._register_schedule_pump()

    def _register_schedule_pump(self) -> None:
        """Register an on_advance hook that fires ENABLED EventBridge schedules.

        Reads each schedule's State and ScheduleExpression from moto's scheduler
        at the time of the advance, so a schedule the test has disabled via
        update-schedule does not fire.  The clock is set to each schedule's fire
        time before running the handler, so heartbeats land inside the staleness
        window.  Lazy-initialised on the first advance so clock.set() calls
        during world setup are not counted.
        """
        import re as _re
        import datetime as _dt
        state: dict[str, Any] = {"prev": None}

        def _pump() -> None:
            now = self.clock.epoch()
            if state["prev"] is None:
                state["prev"] = now          # first advance: record baseline, fire nothing
                return
            old = state["prev"]
            state["prev"] = now
            if now <= old:
                return
            sch = self.internal("scheduler")
            try:
                items = sch.list_schedules(GroupName="default").get("Schedules", [])
            except Exception:  # noqa: BLE001
                return
            fires: list[tuple[_dt.datetime, dict]] = []
            for item in items:
                name, grp = item["Name"], item.get("GroupName", "default")
                try:
                    s = sch.get_schedule(Name=name, GroupName=grp)
                except Exception:  # noqa: BLE001
                    continue
                if s.get("State") != "ENABLED":
                    continue
                m = _re.match(r"cron\((\d+)\s+(\d+)\s+", s.get("ScheduleExpression", ""))
                if not m:
                    continue
                minute, hour = int(m.group(1)), int(m.group(2))
                old_dt = _dt.datetime.fromtimestamp(old, tz=_dt.timezone.utc)
                end_dt = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
                d = old_dt.date()
                while d <= end_dt.date():
                    fire = _dt.datetime.combine(d, _dt.time(hour, minute), _dt.timezone.utc)
                    if old < fire.timestamp() <= now:
                        fires.append((fire, json.loads(s["Target"]["Input"])))
                    d += _dt.timedelta(days=1)
            if not fires:
                return
            fires.sort()
            restore = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
            for fire_dt, event in fires:
                self.clock.set(fire_dt)     # heartbeat recorded at fire time, not now
                self.lam.run(HEALTH_FN, event)
            self.clock.set(restore)          # restore; self.pump() runs after via on_advance

        self.clock.on_advance(_pump)

    # -- Nightly runs: battery/tires/brakes/service -----------------------
    def open(self) -> None:
        """Same setup as base; replaces recall/warranty runs with fh nightly runs."""
        saved = self.run_scheduled
        self.run_scheduled = False      # suppress base's recall/warranty scheduled runs
        super().open()                  # full world setup (moto, tables, lake, infra)
        self.run_scheduled = saved
        # array_join(arr, sep, null_repl) is Trino/Athena syntax; DuckDB uses
        # array_to_string(arr, sep) (2-arg).  Patch the router's handler so the
        # brakes source SQL runs in FakeAthena without changing the producer.
        import re
        _orig_sqe_fn = self.router._handlers.get(("athena", "StartQueryExecution"))

        def _fh_sqe(p: dict[str, Any]) -> dict[str, Any]:
            qs = re.sub(
                r"\barray_join\s*\(([^,]+),\s*('[^']*'|[^,)]+),\s*(?:'[^']*'|[^)]+)\)",
                r"array_to_string(\1, \2)",
                p.get("QueryString", ""),
            )
            return _orig_sqe_fn({**p, "QueryString": qs})  # type: ignore[misc]

        self.router.add("athena", "StartQueryExecution", _fh_sqe)
        if not self.run_scheduled:
            return
        self.stream.prime()
        # Battery at 02:00, tires at 02:20, brakes at 02:40; service at 05:40.
        # sched_day: today if test starts after 02:30Z, else yesterday.
        sched_day = (self.today if self.start.time() >= _dt.time(2, 30)
                     else (self.today - _dt.timedelta(days=1)))
        bat_base = _dt.datetime.combine(sched_day, _dt.time(2, 0), _dt.timezone.utc)
        for days_back in range(self.prior_days, -1, -1):
            at = bat_base - _dt.timedelta(days=days_back)
            for mins, comp in ((0, "battery"), (20, "tires"), (40, "brakes")):
                self.clock.set(at + _dt.timedelta(minutes=mins))
                self.lam.run(HEALTH_FN, {"stage": STAGE, "component": comp})
                self.pump()
            svc_at = _dt.datetime.combine(at.date(), _dt.time(5, 40), _dt.timezone.utc)
            self.clock.set(svc_at)
            self.lam.run(HEALTH_FN, {"stage": STAGE, "component": "service"})
            self.pump()
        self.clock.set(self.start)
        self.pump()
