"""``World``: start every fake, seed the data, run the scheduled jobs, hand back URLs.

Use as a context manager around a test::

    with World() as w:
        rc = w.run_main(["--stage", "staging", "--item", "1"])

``run_main`` loads a fresh copy of the script, points its ``time`` at the world
clock and calls ``main()`` with ``LIVE_TESTS=1``. Everything the script reaches is
in-process: moto, the routers in ``awsfake.py`` and the two local APIs.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import importlib
import importlib.util
import io
import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from scripts.tests.fi_rw_world import seed
from scripts.tests.fi_rw_world.alarm import RunFailureAlarm
from scripts.tests.fi_rw_world.apigw import FakeRestApi
from scripts.tests.fi_rw_world.athena import FakeAthena
from scripts.tests.fi_rw_world.awsfake import AwsError, Router, streaming
from scripts.tests.fi_rw_world.bus import EventBus, FindingsStream
from scripts.tests.fi_rw_world.clock import ScriptTime, WorldClock
from scripts.tests.fi_rw_world.cognito import FakeUserPool
from scripts.tests.fi_rw_world.lambdas import FakeLambda, lambda_env

AVX_ROOT = Path(__file__).resolve().parents[3]
DMS_ROOT = Path(os.environ.get("FI_RW_WORLD_DMS_REPO",
                               str(Path.home() / "guidance-for-dealer-management-system-on-aws")))
#: W12: the DMS stand-in must be loaded from this exact commit, not the working tree.
#: DMS main de6c181 (2026-10-21): is_fi_booking = is_fi_recall_booking | is_fi_fleet_health_booking.
#: The working tree may be at a later commit with a different fi_stand_ins.py (+10/-47 vs 15231e4).
DMS_STAND_IN_COMMIT = "de6c181"
SCRIPT = AVX_ROOT / "scripts" / "live-verify-fi-recall-warranty.py"
SHAPES = json.loads((Path(__file__).parent / "data" / "live_shapes.json").read_text())

STAGE, REGION, ADP_REGION, ACCOUNT = "staging", "us-west-2", "us-east-1", "123456789012"

#: adp_{stage}_engineering.recall_campaigns_issued (FI stage D 9.1): ADP 9bb82e96
#: platform-foundation/source/lambda/digital_thread_writer/handler.py ISSUED_RECALL_COLUMNS,
#: which FleetHealthStack's Glue table is pinned to. Not in live_shapes.json: the table is
#: created by the 9.3 FleetHealthStack deploy.
_ISSUED_RECALL_COLUMNS: list[tuple[str, str]] = [
    ("campaign_id", "string"), ("thread_id", "string"), ("status", "string"),
    ("manufacturer", "string"), ("recall_class", "string"), ("component", "string"),
    ("summary", "string"), ("consequence", "string"), ("remedy", "string"),
    ("do_not_drive", "boolean"), ("park_outside", "boolean"), ("models", "array<string>"),
    ("model_year_min", "int"), ("model_year_max", "int"), ("plants", "array<string>"),
    ("build_from", "date"), ("build_to", "date"), ("component_lots", "array<string>"),
    ("fault_codes", "array<string>"), ("signature_lookback_days", "int"),
    ("remedy_labor_op", "string"), ("remedy_part_numbers", "array<string>"),
    ("remedy_flat_rate_hours", "decimal(5,2)"), ("remedy_available_from", "date"),
    ("received_date", "date"), ("catalog_version", "string"), ("created_at", "string"),
    ("issued_at", "string"), ("affected_vins_count", "int"),
]
HEALTH_FN = f"vsa-{STAGE}-tier2-health-agent-{REGION}"
DEALER_PROGRESS_FN = f"dms-{STAGE}-fi-dealer-progress"
ADJUDICATOR_FN = f"dms-{STAGE}-fi-warranty-adjudicator"
ALARM = f"vsa-{STAGE}-tier2-run-failure-{REGION}"
TOPIC = f"vsa-{STAGE}-tier2-operator-alerts"
BROKER_LOG_GROUP = f"/aws/lambda/vsa-{STAGE}-avx-broker-{REGION}"
LAKE_BUCKET = f"adp-{STAGE}-foundation-lake-{ACCOUNT}-{ADP_REGION}"
AK_PREFIX = "restricted/recall_campaigns/answer_key/"
WORKGROUP = f"cvx-{STAGE}-analytics"

_DMS_ROUTES = [  # live DmsApiStack-staging routes (get-resources 2026-10-10) -> handler
    ("/api/dms/fleet/repair-orders", "POST", "repair_orders.create_fleet_repair_order"),
    ("/api/dms/fleet/repair-orders", "GET", "repair_orders.list_fleet_repair_orders"),
    ("/api/dms/fleet/repair-orders/{roId}", "GET", "repair_orders.get_fleet_repair_order"),
    ("/api/dms/fleet/warranty-claims", "POST", "warranty_claims.create_fleet_warranty_claim"),
    ("/api/dms/fleet/dealers", "GET", "repair_orders.list_fleet_dealers"),
    ("/api/dms/dealers/{dealerId}/repair-orders", "GET", "repair_orders.list_repair_orders"),
    ("/api/dms/dealers/{dealerId}/repair-orders", "POST", "repair_orders.create_repair_order"),
    ("/api/dms/dealers/{dealerId}/repair-orders/{roId}", "GET", "repair_orders.get_repair_order"),
    ("/api/dms/dealers/{dealerId}/repair-orders/{roId}", "PATCH", "repair_orders.update_repair_order"),
    ("/api/dms/dealers/{dealerId}/warranty-claims", "GET", None),
    ("/api/dms/dealers/{dealerId}/warranty-claims", "POST", None),
    ("/api/dms/dealers/{dealerId}/warranty-claims/{claimId}", "GET", None),
    ("/api/dms/dealers/{dealerId}/warranty-claims/{claimId}/submit", "POST", None),
    ("/api/dms/dealers/{dealerId}/warranty-claims/{claimId}/validate", "POST", None),
    ("/api/dms/repair-orders", "GET", None),
    ("/api/dms/repair-orders/summary", "GET", None),
    ("/api/dms/repair-orders/{roId}", "GET", None),
    ("/api/dms/repair-orders/{roId}/notes", "POST", None),
]
_AVX_ROUTES = [  # live vsa-staging-api routes (get-resources 2026-10-10)
    ("/findings/me", "GET", "findings"), ("/findings/vehicle/{vin}", "GET", "findings"),
    ("/findings/owner/{owner_id}", "GET", "findings"),
    ("/findings/owner-fleet/{owner_id}", "GET", "findings"),
    ("/findings/fleet/{fleet_id}", "GET", "findings"),
    ("/findings/dealership/{dealer_id}", "GET", "findings"),
    ("/findings/district/{district_id}", "GET", "findings"),
    ("/findings/{finding_id}/approve", "POST", "findings"),
    ("/findings/{finding_id}/decline", "POST", "findings"),
    ("/assistant/chat", "POST", "chat"),
    ("/actions/fleet/{fleet_id}", "GET", None), ("/actions/{action_id}", "PATCH", None),
    ("/actions/{action_id}/dismiss", "POST", None),
]


def _load_file(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_dms_stand_ins() -> Any:
    """Load source.simulation.fi_stand_ins from DMS_STAND_IN_COMMIT via git archive.

    W12 fix: the world must run the exact fi_stand_ins.py at DMS_STAND_IN_COMMIT
    (de6c181), not the working tree.  de6c181 has is_fi_booking = recall | fleet-health
    (both branches); the current working-tree main (6daf21e) has +10/-47 vs 15231e4 and
    its is_fi_booking lacks the fleet-health branch.

    Fails loud (RuntimeError) if git archive fails so test failures are diagnostic,
    not silent wrong-code passes.

    Source: DMS de6c181 source/simulation/fi_stand_ins.py
    """
    import subprocess  # noqa: PLC0415
    import tarfile  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    cache_attr = "_dms_stand_in_extract_root"
    if not hasattr(_load_dms_stand_ins, cache_attr):
        setattr(_load_dms_stand_ins, cache_attr, None)

    extract_root: str | None = getattr(_load_dms_stand_ins, cache_attr)

    if extract_root is None:
        if not DMS_ROOT.exists():
            raise RuntimeError(
                f"W12: DMS repo not found at {DMS_ROOT}; "
                f"cannot load fi_stand_ins from commit {DMS_STAND_IN_COMMIT!r}. "
                f"Set FI_RW_WORLD_DMS_REPO or clone guidance-for-dealer-management-system-on-aws."
            )
        tmpdir = tempfile.mkdtemp(prefix=f"dms_stand_in_{DMS_STAND_IN_COMMIT[:8]}_")
        try:
            archive_path = tmpdir + "/dms_archive.tar"
            result = subprocess.run(
                ["git", "-C", str(DMS_ROOT), "archive", DMS_STAND_IN_COMMIT,
                 "--", "source/simulation/fi_stand_ins.py"],
                capture_output=True, check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"W12: git archive {DMS_STAND_IN_COMMIT!r} failed "
                    f"(exit {result.returncode}): {result.stderr.decode()!r}. "
                    f"Commit {DMS_STAND_IN_COMMIT!r} must exist in {DMS_ROOT}."
                )
            with open(archive_path, "wb") as fh:
                fh.write(result.stdout)
            with tarfile.open(archive_path) as tf:
                tf.extractall(tmpdir)
            extract_root = tmpdir
            setattr(_load_dms_stand_ins, cache_attr, extract_root)
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"W12: failed to extract fi_stand_ins from {DMS_STAND_IN_COMMIT!r}: {exc}"
            ) from exc

    # Add the extracted source root to sys.path temporarily so the import works.
    src_root = extract_root + "/source"
    sim_root = extract_root + "/source/simulation"

    # Remove any working-tree source.simulation from sys.modules first.
    for mod_name in list(sys.modules.keys()):
        if mod_name == "source.simulation.fi_stand_ins" or mod_name == "source.simulation":
            del sys.modules[mod_name]

    for p in (sim_root, src_root):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        stand_ins = importlib.import_module("source.simulation.fi_stand_ins")
    finally:
        for p in (sim_root, src_root):
            try:
                sys.path.remove(p)
            except ValueError:
                pass
    return stand_ins


def _ddb_json(v: Any) -> Any:
    from boto3.dynamodb.types import TypeSerializer
    return TypeSerializer().serialize(v)


def _py(v: Any) -> Any:
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _py(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_py(x) for x in v]
    return v



class World:
    """One staging, in-process. ``start``: the clock when the test begins (UTC)."""

    def __init__(self, start: _dt.datetime | None = None, *, run_scheduled: bool = True,
                 groundings_table: bool = False, live_defects: frozenset[str] = frozenset(),
                 model_mode: str = "ok", prior_days: int = 1) -> None:
        real = _dt.datetime.now(_dt.timezone.utc)
        self.start = start or real.replace(hour=12, minute=0, second=0, microsecond=0)
        self.today = self.start.date()
        self.run_scheduled = run_scheduled
        self.prior_days = prior_days
        self.groundings_table = groundings_table
        # Defects observed in live staging on 2026-10-10, off by default so the world
        # models a working staging; a test turns one on to see the script report it.
        #   "dms_marker_stale": DMS's hourly fleet-membership reconcile fails (kms:Decrypt
        #     denied on the table key), so the sync marker ages past FLEET_SYNC_MAX_AGE
        #     (3600 s) and every DMS fleet route answers the uniform 404.
        #   "dms_claim_double_serialized": DMS create_fleet_warranty_claim serializes the
        #     TransactWriteItems itself and then sends them through resource.meta.client,
        #     whose boto3 high-level injector serializes them again; the write fails.
        unknown = set(live_defects) - {"dms_marker_stale", "dms_claim_double_serialized"}
        if unknown:
            raise ValueError(f"unknown live defects {unknown}")
        self.live_defects = frozenset(live_defects)
        self.model_mode = model_mode
        self.region = REGION
        self.account = ACCOUNT
        self._clients: dict[tuple[str, str], Any] = {}
        self._env_saved: dict[str, str] = {}
        self._sys_path_added: list[str] = []
        self.manifests: list[Path] = []
        self.handlers: dict[str, Any] = {}
        self.calls_before_main = 0
        self.invocations_before_main = 0
        self.system_exit: int | None = None

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> "World":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def open(self) -> None:
        self._env_saved = dict(os.environ)
        for k in [k for k in os.environ if k.startswith("AWS_")]:
            del os.environ[k]
        os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
                          AWS_SESSION_TOKEN="testing", AWS_DEFAULT_REGION=REGION,
                          AWS_EC2_METADATA_DISABLED="true")
        os.environ.pop("LIVE_TESTS", None)
        from moto import mock_aws
        self._mock = mock_aws()
        self._mock.start()
        # The 05:00Z/05:20Z scheduled runs happen before the test starts: today's if the
        # test starts after 05:30Z, else yesterday's.
        sched_day = self.today if self.start.time() >= _dt.time(5, 30) else (
            self.today - _dt.timedelta(days=1))
        scheduled_at = _dt.datetime.combine(sched_day, _dt.time(5, 0), _dt.timezone.utc)
        self.clock = WorldClock(scheduled_at)
        self.router = Router()
        self.router.install()
        self.pool = FakeUserPool(self.clock, REGION)
        self.pool.register(self.router)
        self.athena = FakeAthena(self.clock, {WORKGROUP, f"dms-{STAGE}-fi-candidates"})
        self.athena.register(self.router)
        self.athena.before_query.append(self._refresh_lake)
        self.lam = FakeLambda(self)
        self.lam.register(self.router)
        self.router.add("bedrock-runtime", "Converse", self._converse)
        self.router.add("bedrock-runtime", "ApplyGuardrail", self._apply_guardrail)
        self.router.add("bedrock-runtime", "InvokeModel", self._model_unavailable)
        self.router.add("bedrock-agentcore", "InvokeAgentRuntime", self._agent_runtime)
        self.router.add("bedrock-agentcore-control", "ListAgentRuntimes", self._list_runtimes)
        self.router.add("bedrock-agentcore-control", "GetAgentRuntime", self._get_runtime)
        self._paths()
        self._create_tables()
        self._seed_data()
        self._load_handlers()
        self._create_infra()
        self.clock.install([str(AVX_ROOT), str(DMS_ROOT)])
        self.clock.on_advance(self.pump)
        if self.run_scheduled:
            self.stream.prime()
            # Live has run both schedules every day since stage C deployed, so the
            # Findings table is in its steady state. A first-ever run is not: it sees no
            # open items, and the next run's evidence (vehicle.open_items) differs from
            # it. So the world runs the previous day's pair first, then the latest pair.
            for days_back in range(self.prior_days, -1, -1):
                at = scheduled_at - _dt.timedelta(days=days_back)
                self.clock.set(at)
                self.lam.run(HEALTH_FN, {"stage": STAGE, "component": "recall"})
                self.pump()
                self.clock.set(at + _dt.timedelta(minutes=20))
                self.lam.run(HEALTH_FN, {"stage": STAGE, "component": "warranty"})
                self.pump()
        self.clock.set(self.start)
        self.pump()

    def close(self) -> None:
        for api in (getattr(self, "avx_api", None), getattr(self, "dms_api", None)):
            if api is not None:
                api.close()
        if hasattr(self, "clock"):
            self.clock.uninstall()
        if hasattr(self, "router"):
            self.router.uninstall()
        if hasattr(self, "_mock"):
            self._mock.stop()
        for p in self._sys_path_added:
            with contextlib.suppress(ValueError):
                sys.path.remove(p)
        os.environ.clear()
        os.environ.update(self._env_saved)
        import shutil
        for m in self.manifests:
            shutil.rmtree(m.parent, ignore_errors=True)

    def pump(self) -> None:
        if "dms_marker_stale" not in self.live_defects:
            self._refresh_marker()
        self.lam.pump()
        self.bus.pump()
        self.stream.pump()
        self.alarm.pump()

    def _refresh_marker(self) -> None:
        from source.handlers import fleet_membership as fm  # DMS's own marker key
        self.put("dms-staging-fleet-membership", {
            "vehicle_vin": fm._SYNC_MARKER_PK, "fleet_id": fm._SYNC_MARKER_SK,
            "synced_at": self.clock.now().isoformat(), "row_count": len(seed.VIN_FLEET)})

    # ------------------------------------------------------------------ clients
    def internal(self, service: str, region: str | None = None) -> Any:
        import boto3
        r = region or (ADP_REGION if service in ("s3", "athena") else REGION)
        key = (service, r)
        if key not in self._clients:
            self._clients[key] = boto3.client(service, region_name=r)
        return self._clients[key]

    def table(self, name: str) -> Any:
        import boto3
        return boto3.resource("dynamodb", region_name=REGION).Table(name)

    def scan(self, name: str) -> list[dict[str, Any]]:
        t, items, kw = self.table(name), [], {}
        while True:
            r = t.scan(**kw)
            items += r.get("Items", [])
            if "LastEvaluatedKey" not in r:
                return items
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def _paths(self) -> None:
        for p in (str(AVX_ROOT), str(DMS_ROOT)):
            if p not in sys.path:
                sys.path.append(p)
                self._sys_path_added.append(p)
        for name in [m for m in sys.modules if m == "source" or m.startswith("source.")]:
            del sys.modules[name]


    # ------------------------------------------------------------------ tables
    def _create_tables(self) -> None:
        ddb = self.internal("dynamodb")
        defs = dict(SHAPES["tables"])
        # dms-staging-parts-inventory: live describe-table (2026-10-10), read by the
        # dealer-progress stand-in to choose Ready or AwaitingParts.
        defs["dms-staging-parts-inventory"] = {
            "TableName": "dms-staging-parts-inventory",
            "KeySchema": [{"AttributeName": "dealer_id", "KeyType": "HASH"},
                          {"AttributeName": "part_number", "KeyType": "RANGE"}],
            "AttributeDefinitions": [{"AttributeName": "dealer_id", "AttributeType": "S"},
                                     {"AttributeName": "part_number", "AttributeType": "S"},
                                     {"AttributeName": "below_par", "AttributeType": "S"}],
            "GlobalSecondaryIndexes": [{"IndexName": "low-stock-index", "KeySchema": [
                {"AttributeName": "dealer_id", "KeyType": "HASH"},
                {"AttributeName": "below_par", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"}}]}
        if self.groundings_table:  # CMS spec 6.2: pk vehicleId, sk actionId (not live yet)
            defs["cms-staging-storage-vehicle-groundings"] = {
                "TableName": "cms-staging-storage-vehicle-groundings",
                "KeySchema": [{"AttributeName": "vehicleId", "KeyType": "HASH"},
                              {"AttributeName": "actionId", "KeyType": "RANGE"}],
                "AttributeDefinitions": [{"AttributeName": "vehicleId", "AttributeType": "S"},
                                         {"AttributeName": "actionId", "AttributeType": "S"}]}
        for name, d in defs.items():
            kw: dict[str, Any] = {"TableName": name, "KeySchema": d["KeySchema"],
                                  "AttributeDefinitions": d["AttributeDefinitions"],
                                  "BillingMode": "PAY_PER_REQUEST"}
            if d.get("GlobalSecondaryIndexes"):
                kw["GlobalSecondaryIndexes"] = [
                    {"IndexName": g["IndexName"], "KeySchema": g["KeySchema"],
                     "Projection": g["Projection"]} for g in d["GlobalSecondaryIndexes"]]
            ddb.create_table(**kw)

    def put(self, table: str, item: dict[str, Any]) -> None:
        self.table(table).put_item(Item=_py(item))

    # ------------------------------------------------------------------ data
    def _seed_data(self) -> None:
        from source.simulation import dealer_profiles as dp  # DMS's own roster
        today = self.today
        now_iso = self.clock.iso()
        self.roster = [r[0] for r in dp.ROSTER]
        # CMS fleets, vehicles, fleet locations (one assigned location per fleet)
        for i, f in enumerate(seed.FLEETS):
            self.put("cms-staging-storage-fleets", {"fleetId": f, "name": f})
            self.put("cms-staging-storage-fleet-locations", {
                "fleetId": f, "locationId": f"loc-{i}", "latitude": 38.58 + i * 0.1,
                "longitude": -121.49 - i * 0.1, "name": f"{f} depot"})
        for i, flt in seed.VIN_FLEET.items():
            self.put("cms-staging-storage-vehicles", {
                "vehicleId": seed.vehicle_id(i), "vin": seed.vin(i), "fleetId": flt,
                "locationId": f"loc-{seed.FLEETS.index(flt)}", "make": "Meridian",
                "model": "Azimuth", "year": 2023})
            self.put("dms-staging-fleet-membership", {
                "vehicle_vin": seed.vin(i), "fleet_id": flt, "synced_at": now_iso})
        self._refresh_marker()
        # DMS rooftops (DynamoDB + lake), parts in stock for every remedy part
        rooftops = []
        for row in dp.rooftop_items():  # DMS's own rooftop rows
            row = {**row, "seeded_at": now_iso}
            rooftops.append(row)
            self.put("dms-staging-rooftops", row)
        self.athena_tables = {"rooftops": rooftops}
        parts = [{"dealer_id": d, "part_number": p, "qty_on_hand": 5, "is_recall_remedy": True,
                  "part_name": p, "status": "active", "below_par": "false", "par_level": 2,
                  "record_kind": "part"}
                 for d in self.roster for p in ("MRD-P-002", "MRD-P-004", "MRD-P-005")]
        for row in parts:
            self.put("dms-staging-parts-inventory", row)
        # warranty: DMS's own plant loader writes the planted ROs (and the candidates row
        # of an already-claimed line); DMS's own candidates job (run below, after the
        # lake tables exist) computes every other candidate with DMS's rules. Terms rows
        # as live (dms-staging-warranty-terms scan, 2026-10-10).
        seeder = _load_file(DMS_ROOT / "deployment" / "scripts" / "seed_fleet_warranty_repairs.py",
                            f"fi_rw_world_dms_seeder_{uuid.uuid4().hex[:8]}")
        from source.handlers.warranty_claims import _load_terms
        dms_terms = _load_terms()
        self.plants = seed.warranty_plants(today)
        terms = [{"terms_version": "1.0.0", "coverage_group": g, "effective_from": "2024-01-01",
                  "term_years": v["years"], "term_miles": v["miles"], "labor_rate_usd": 150,
                  "filing_window_days": 30} for g, v in dms_terms["coverage_groups"].items()]
        for t in terms:
            self.put("dms-staging-warranty-terms", t)
        tv, window = seeder._load_terms_version(), seeder._load_terms_filing_window()
        for p in self.plants:
            self.put("dms-staging-repair-orders", seeder._build_ro_item(p))
            if p["already_claimed"]:
                self.put("dms-staging-warranty-candidates",
                         seeder._build_candidates_item(p, tv, window, now_iso))
        candidates: list[dict[str, Any]] = []
        self.athena_tables.update({"parts_inventory": parts, "warranty_terms": terms,
                                   "warranty_candidates": candidates})
        # ADP lake: campaigns, managed vehicles, view base
        self.campaigns = seed.campaigns(today)
        self.view_base = seed.VIEW_BASE
        self.managed_as_of = today
        self._create_lake_tables()
        self._write_answer_key()

    def _glue_cols(self, qualified: str) -> list[tuple[str, str]]:
        return [tuple(c) for c in SHAPES["glue"][qualified]["Columns"]]  # type: ignore[misc]

    def _create_lake_tables(self) -> None:
        a = self.athena
        rc = self._glue_cols("adp_staging_fleet_recall.recall_campaigns_received")
        a.create_table("adp_staging_fleet_recall.recall_campaigns_received", rc, self.campaigns)
        # FI stage D 9.3: the recall query LEFT JOINs the issued-recall table for lots and
        # reads Stage C's base catalog to keep an issued row reusing a Stage C id out. The
        # world's catalog is all Stage C and all received, so the base table holds the same
        # rows; the issued table is empty (none issued live yet), its columns ADP's
        # ISSUED_RECALL_COLUMNS (ADP 9bb82e96 digital_thread_writer/handler.py).
        a.create_table("adp_staging_fleet_recall.recall_campaigns", rc, self.campaigns)
        a.create_table("adp_staging_engineering.recall_campaigns_issued", _ISSUED_RECALL_COLUMNS, [])
        a.create_table("adp_staging_fleet_recall.managed_vehicles",
                       self._glue_cols("adp_staging_fleet_recall.managed_vehicles"),
                       [{"vin": seed.vin(i), "fleet_id": f, "snapshot_date": self.managed_as_of}
                        for i, f in seed.VIN_FLEET.items()])
        # adp_staging_dimensions.vins (glue get-table 2026-10-10): DMS's candidates job
        # reads in_service_date from it.
        a.create_table("adp_staging_dimensions.vins",
                       [("vin", "string"), ("model_year", "int"), ("model", "string"),
                        ("manufacture_date", "date"), ("in_service_date", "date")],
                       [{"vin": seed.vin(i), "model_year": 2023, "model": "Azimuth",
                         "manufacture_date": _dt.date(2023, 2, 1),
                         "in_service_date": _dt.date(2023, 3, 1)} for i in seed.VIN_FLEET])
        self._run_candidates_job()
        self.athena_tables["warranty_candidates"] = self.scan("dms-staging-warranty-candidates")
        for t in ("warranty_candidates", "warranty_terms", "parts_inventory", "rooftops"):
            a.create_table(f"dms_staging.{t}", self._glue_cols(f"dms_staging.{t}"),
                           [{**r, "_change_kind": "upsert", "_seq_no": "1"}
                            for r in self.athena_tables[t]])
        self._refresh_lake()

    def _run_candidates_job(self) -> None:
        """DMS's own warranty candidates job (live 04:40Z), with its live environment."""
        env = {"FLEET_MEMBERSHIP_TABLE_NAME": "dms-staging-fleet-membership",
               "DEPLOYMENT_STAGE": STAGE, "DMS_AWS_REGION": REGION,
               "ADP_ATHENA_OUTPUT_LOCATION": f"s3://dms-staging-athena-results-{ACCOUNT}-us-east-1/fi-candidates/",
               "ADP_VIN_DIMS_TABLE": "vins", "ATHENA_DATABASE": "dms_staging",
               "ADP_ATHENA_WORKGROUP": f"dms-{STAGE}-fi-candidates", "ADP_REGION": ADP_REGION,
               "ADP_DIMENSIONS_DATABASE": f"adp_{STAGE}_dimensions", "AGENT_VERSION": ""}
        with lambda_env(env, f"dms-{STAGE}-fi-warranty-candidates", REGION):
            job = importlib.import_module("source.handlers.warranty_candidates")
            self.candidates_result = job.handler({}, None)
        cands = {c["ro_id"]: c for c in self.scan("dms-staging-warranty-candidates")}
        for p in self.plants:
            got = cands.get(p["ro_id"], {}).get("verdict_status")
            want = "approved" if p["already_claimed"] else p["verdict_plant"]
            if got != want:
                raise AssertionError(f"world plant {p['ro_id']}: DMS candidates job says {got!r}, "
                                     f"plant says {want!r}; result={self.candidates_result}")

    def view_rows(self) -> list[dict[str, Any]]:
        """recall_vehicle_status per interface.md § 3, remedied rules a (DMS) and b (ADP)."""
        ros = self.scan("dms-staging-repair-orders")
        rows = []
        for cid, i, basis, adp_remedy in self.view_base:
            v = seed.vin(i)
            remedy_ref = remedied_on = None
            # recall_vehicle_status.sql: COALESCE(rdms.remedy_ref, radp.remedy_ref) — the
            # DMS remedy (the earliest Ready/Delivered/Invoiced recall RO, MIN_BY) wins
            # over the ADP service-record remedy (review rebuild c1 WB1).
            dms = sorted(
                (r for r in ros if r.get("vehicle_vin") == v
                 and r.get("status") in ("Ready", "Delivered", "Invoiced")
                 and any(ln.get("pay_type") == "recall" and ln.get("recall_campaign_id") == cid
                         for ln in (r.get("lines") or []))),
                key=lambda r: str(r.get("updated_at")))
            if dms:
                remedy_ref = dms[0]["ro_id"]
                remedied_on = _dt.date.fromisoformat(str(dms[0]["updated_at"])[:10])
            elif adp_remedy:
                remedy_ref, remedied_on = f"SVC-WORLD-{cid}-{i}", self.today - _dt.timedelta(days=60)
            rows.append({
                "campaign_id": cid, "vin": v, "fleet_id": seed.VIN_FLEET[i], "match_basis": basis,
                "confirming_code": "P0AB6" if basis == "CONFIRMED" and cid == "MRD-2026-004"
                else ("C1A45" if basis == "CONFIRMED" else None),
                "confirming_seen_on": self.today - _dt.timedelta(days=12) if basis == "CONFIRMED" else None,
                "confirming_source": "service_records" if basis == "CONFIRMED" else None,
                "remedied": remedy_ref is not None, "remedy_ref": remedy_ref,
                "remedied_on": remedied_on, "rule_version": "1.0.0",
                "managed_as_of": self.managed_as_of})
        return rows

    def _refresh_lake(self) -> None:
        """Re-materialize the view and the DMS repair-orders lake copy before a query
        (the live merge runs every 30 minutes; the world merges on every read)."""
        self.athena.create_table("adp_staging_fleet_recall.recall_vehicle_status",
                                 self._glue_cols("adp_staging_fleet_recall.recall_vehicle_status"),
                                 self.view_rows())
        ros = [{**r, "_change_kind": "upsert", "_seq_no": "1",
                "lines": json.dumps(r.get("lines") or [], default=str),
                "pay_types": json.dumps(r.get("pay_types") or [], default=str)}
               for r in self.scan("dms-staging-repair-orders")]
        self.athena.create_table("dms_staging.repair_orders",
                                 self._glue_cols("dms_staging.repair_orders"), ros)
        cands = [{**c, "_change_kind": "upsert", "_seq_no": "1"}
                 for c in self.scan("dms-staging-warranty-candidates")]
        self.athena.create_table("dms_staging.warranty_candidates",
                                 self._glue_cols("dms_staging.warranty_candidates"), cands)

    def answer_key(self) -> dict[str, Any]:
        rows = [r for r in self.view_rows()]
        per: dict[str, dict[str, Any]] = {}
        for c in self.campaigns:
            cid = c["campaign_id"]
            mine = [(cid2, i, b, a) for cid2, i, b, a in self.view_base if cid2 == cid]
            per[cid] = {
                "confirmed_vins": sorted(seed.vin(i) for _, i, b, _ in mine if b == "CONFIRMED"),
                "remedied_vins": sorted(seed.vin(i) for _, i, _, a in mine if a),
                "population": sum(1 for r in rows if r["campaign_id"] == cid
                                  and r["match_basis"] == "POPULATION")}
        return per

    def _write_answer_key(self) -> None:
        import duckdb
        per = self.answer_key()
        recs = []
        for cid, d in per.items():
            recs.append((None, "campaign_summary", cid, "SUMMARY",
                         f"confirmed_vins={d['confirmed_vins']},remedied_vins={d['remedied_vins']},"
                         f"population_count={d['population']}", self.today.isoformat()))
            for v in d["confirmed_vins"]:
                recs.append((v, "recall_campaign", cid, "CONFIRMED", "confirming_code=P0AB6",
                             self.today.isoformat()))
            for v in d["remedied_vins"]:
                recs.append((v, "recall_campaign", cid, "POPULATION", "remedied (kind 1b)",
                             self.today.isoformat()))
        for p in self.plants:
            recs.append((p["vin"], "warranty_repair", p["ro_id"],
                         "already_claimed" if p["already_claimed"] else p["verdict_plant"],
                         "", self.today.isoformat()))
        con = duckdb.connect()
        con.execute("CREATE TABLE ak (vin VARCHAR, entity_kind VARCHAR, entity_id VARCHAR, "
                    "expected_verdict VARCHAR, notes VARCHAR, planted_at VARCHAR)")
        con.executemany("INSERT INTO ak VALUES (?, ?, ?, ?, ?, ?)", recs)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ak.parquet")
            con.execute(f"COPY ak TO '{path}' (FORMAT PARQUET)")
            parquet = Path(path).read_bytes()
        s3 = self.internal("s3")
        s3.create_bucket(Bucket=LAKE_BUCKET)
        s3.put_object(Bucket=LAKE_BUCKET, Key=AK_PREFIX + "recall_plants_answer_key.parquet",
                      Body=parquet)
        s3.put_object(Bucket=LAKE_BUCKET, Key=AK_PREFIX + "recall_plants_dms_seed.jsonl",
                      Body="\n".join(json.dumps(p) for p in self.plants).encode())
        s3.put_object(Bucket=LAKE_BUCKET, Key=AK_PREFIX + "plants_live_manifest.json",
                      Body=json.dumps({"per_campaign": {
                          k: {"confirmed": len(v["confirmed_vins"]), "remedied": len(v["remedied_vins"]),
                              "population": v["population"]} for k, v in per.items()},
                          "snapshot_date": self.managed_as_of.isoformat()}).encode())


    # ------------------------------------------------------------------ functions
    def env_of(self, key: str) -> dict[str, str]:
        env = dict(SHAPES["functions"][key]["Env"])
        sub = {"<cms-pool-id>": self.pool.pool_id, "<guardrail-id>": "gr-world-0001"}
        for k, v in env.items():
            for a, b in sub.items():
                v = v.replace(a, b)
            env[k] = v
        if key == "avx_findings":
            env["VSA_DMS_API_ENDPOINT"] = self.dms_url
            env["VSA_CMS_API_ENDPOINT"] = "http://127.0.0.1:1/prod/"
        return env

    def _load_handlers(self) -> None:
        # The API URLs are needed in the handlers' environment, so bind the servers first.
        self.avx_api = FakeRestApi("vsa-staging-api", STAGE, self.pool.verify, self._invoke_api)
        self.dms_api = FakeRestApi("DmsApiStack-staging", STAGE, self.pool.verify, self._invoke_api)
        self.avx_url, self.dms_url = self.avx_api.url, self.dms_api.url
        n = uuid.uuid4().hex[:8]
        self._api_env = {"findings": self.env_of("avx_findings"), "chat": self.env_of("avx_chat")}
        with lambda_env(self._api_env["findings"], "vsa-staging-api-avx-findings", REGION):
            self.handlers["findings"] = _load_file(
                AVX_ROOT / "lambdas" / "api-avx-findings" / "handler.py", f"fi_rw_world_findings_{n}")
        chat_dir = str(AVX_ROOT / "lambdas" / "api-assistant-chat")
        sys.path.insert(0, chat_dir)
        try:
            with lambda_env(self._api_env["chat"], "vsa-staging-api-assistant-chat", REGION):
                self.handlers["chat"] = _load_file(Path(chat_dir) / "handler.py", f"fi_rw_world_chat_{n}")
        finally:
            sys.path.remove(chat_dir)
        consumer_env = self.env_of("avx_dms_event_consumer")
        with lambda_env(consumer_env, "vsa-staging-avx-dms-event-consumer-us-west-2", REGION):
            consumer = _load_file(AVX_ROOT / "lambdas" / "api-avx-dms-event-consumer" / "handler.py",
                                  f"fi_rw_world_consumer_{n}")
        self.handlers["consumer"] = consumer
        dms_env = self.env_of("dms_api")
        self._api_env["dms"] = dms_env
        with lambda_env(dms_env, "DmsApiStack-staging", REGION):
            ro = importlib.import_module("source.handlers.repair_orders")
            wc = importlib.import_module("source.handlers.warranty_claims")
        with lambda_env(self.env_of("dms_dealer_progress"), DEALER_PROGRESS_FN, REGION):
            stand_ins = _load_dms_stand_ins()
        self.handlers.update(repair_orders=ro, warranty_claims=wc, fi_stand_ins=stand_ins)
        import agents.tier2.runner as runner
        broker_env = self.env_of("avx_broker")
        with lambda_env(broker_env, f"vsa-{STAGE}-avx-broker-{REGION}", REGION):
            broker = _load_file(AVX_ROOT / "agents" / "supervisor" / "lambdas" / "avx_broker.py",
                                f"fi_rw_world_broker_{n}")
        self.handlers.update(broker=broker, runner=runner)
        for tmpl, method, kind in _AVX_ROUTES:
            fn = None if kind is None else self.handlers[kind].handler
            self.avx_api.add(tmpl, method, kind or tmpl, fn)
        for tmpl, method, ref in _DMS_ROUTES:
            fn = None
            if ref:
                mod, attr = ref.split(".")
                fn = getattr(self.handlers[mod], attr)
            self.dms_api.add(tmpl, method, f"dms:{ref or tmpl}", fn)

        def run_broker(event: dict, ctx: Any) -> Any:
            with lambda_env(broker_env, f"vsa-{STAGE}-avx-broker-{REGION}", REGION):
                return broker.handler(event, ctx)

        def run_consumer(event: dict, ctx: Any) -> Any:
            with lambda_env(consumer_env, "vsa-staging-avx-dms-event-consumer-us-west-2", REGION):
                return consumer.handler(event, ctx)

        self.bus = EventBus(self, ACCOUNT, run_consumer)
        self.bus.register(self.router)
        self.stream = FindingsStream(self, f"vsa-{STAGE}-avx-findings", run_broker, BROKER_LOG_GROUP)

    def _invoke_api(self, name: str, fn: Any, event: dict[str, Any], ctx: Any) -> dict[str, Any]:
        env = self._api_env["dms"] if name.startswith("dms:") else self._api_env[name]
        with lambda_env(env, name, REGION), self._claim_route_fixed(name):
            return fn(event, ctx)

    @contextlib.contextmanager
    def _claim_route_fixed(self, name: str) -> Iterator[None]:
        """The claim route's TransactWriteItems, as staging serves it.

        Off (default): the route's pre-serialized items pass the resource injector once,
        as a plain client would (DMS main at 7ba93d8 still sends them through
        ``resource.meta.client``; the fix on DMS's branch uses a plain client, where
        this patch is a no-op).
        On (``dms_claim_double_serialized``): the transaction fails as live staging
        failed it on 2026-10-10 (``TransactionCanceledException ... [TypeError]``,
        DMS issue 2026-10-10-create-fleet-warranty-claim-double-serializes), whatever
        DMS version the world runs, so the test keeps meaning after DMS's fix lands."""
        if name != "dms:warranty_claims.create_fleet_warranty_claim":
            yield
            return
        if "dms_claim_double_serialized" in self.live_defects:
            prev = self.router._handlers.get(("dynamodb", "TransactWriteItems"))

            def cancel(api: dict[str, Any]) -> Any:
                raise AwsError("TransactionCanceledException",
                               "Transaction cancelled, please refer cancellation reasons "
                               "for specific reasons [TypeError]", status=400)
            self.router.add("dynamodb", "TransactWriteItems", cancel)
            try:
                yield
            finally:
                if prev is None:
                    self.router._handlers.pop(("dynamodb", "TransactWriteItems"), None)
                else:
                    self.router.add("dynamodb", "TransactWriteItems", prev)
            return
        from boto3.dynamodb.transform import TransformationInjector
        orig = TransformationInjector.inject_attribute_value_input

        def inject(inj: Any, params: Any, model: Any, **kw: Any) -> Any:
            if model.name == "TransactWriteItems":
                return None
            return orig(inj, params, model, **kw)
        TransformationInjector.inject_attribute_value_input = inject  # type: ignore[method-assign]
        try:
            yield
        finally:
            TransformationInjector.inject_attribute_value_input = orig  # type: ignore[method-assign]

    def _create_infra(self) -> None:
        iam = self.internal("iam")
        role = iam.create_role(RoleName="fi-rw-world-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        lam = self.internal("lambda")
        runner = self.handlers["runner"]
        stand_ins = self.handlers["fi_stand_ins"]
        self.lam.create(lam, role, HEALTH_FN, "agents.tier2.runner.lambda_handler",
                        self.env_of("health_agent"), runner.lambda_handler, timeout=900)
        self.lam.create(lam, role, DEALER_PROGRESS_FN, "source.simulation.fi_stand_ins.dealer_progress_handler",
                        self.env_of("dms_dealer_progress"), stand_ins.dealer_progress_handler, timeout=300)
        self.lam.create(lam, role, ADJUDICATOR_FN, "source.simulation.fi_stand_ins.warranty_adjudicator_handler",
                        self.env_of("dms_adjudicator"), stand_ins.warranty_adjudicator_handler, timeout=300)
        self.health_arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{HEALTH_FN}"
        sch = self.internal("scheduler")
        for s in SHAPES["schedules"]:
            kw = {"Name": s["Name"], "GroupName": s["GroupName"],
                  "ScheduleExpression": s["ScheduleExpression"], "State": s["State"],
                  "FlexibleTimeWindow": {"Mode": "OFF"},
                  "Target": {"Arn": self.health_arn, "RoleArn": role,
                             "Input": s["Target"]["Input"], "RetryPolicy": s["Target"]["RetryPolicy"]}}
            if s.get("ScheduleExpressionTimezone"):
                kw["ScheduleExpressionTimezone"] = s["ScheduleExpressionTimezone"]
            sch.create_schedule(**kw)
        sns = self.internal("sns")
        self.topic_arn = sns.create_topic(Name=TOPIC)["TopicArn"]
        sns.subscribe(TopicArn=self.topic_arn, Protocol="email", Endpoint="operator@example.invalid")
        self.alarm = RunFailureAlarm(self, ALARM, HEALTH_FN, self.topic_arn)
        self.alarm.create(self.internal("cloudwatch"))
        self.alarm.register(self.router)
        self.internal("logs").create_log_group(logGroupName=BROKER_LOG_GROUP)
        ssm = self.internal("ssm")
        ssm.put_parameter(Name=f"/cvx/{STAGE}/voice-auth-user-pool-id",
                          Value=self.pool.pool_id, Type="String")
        # broker policy, as live (get-parameters-by-path /vsa/staging/avx/broker/, 2026-10-10)
        for k, v in (("apns-severity-threshold", "p0"), ("cardinality-cap", "15"),
                     ("rate-limit", '{"day":10,"hour":3}')):
            ssm.put_parameter(Name=f"/vsa/{STAGE}/avx/broker/{k}", Value=v, Type="String")
        cfn = self.internal("cloudformation")
        for stack, key, url in ((f"vsa-{STAGE}-api", "RestApiUrl", self.avx_url),
                                (f"DmsApiStack-{STAGE}", "ApiUrl", self.dms_url)):
            cfn.create_stack(StackName=stack, TemplateBody=json.dumps({
                "Resources": {"Placeholder": {"Type": "AWS::SNS::Topic"}},
                "Outputs": {key: {"Value": url}}}))

    # ------------------------------------------------------------------ model fakes
    def _model_unavailable(self, p: dict[str, Any]) -> dict[str, Any]:
        raise AwsError("AccessDeniedException", "world: no model; the rules path runs", status=403)

    def _converse(self, p: dict[str, Any]) -> dict[str, Any]:
        """A model that follows the tool contract (``agents/fi_recall_warranty/llm.py``):
        it fills the forced tool's required fields from the item context it was sent.
        ``model_mode="unavailable"`` makes every call fail, so the rules path runs."""
        self.model_calls = getattr(self, "model_calls", 0) + 1
        if self.model_mode == "unavailable":
            return self._model_unavailable(p)
        tool = p["toolConfig"]["toolChoice"]["tool"]["name"]
        text = p["messages"][0]["content"][0]["text"]
        ctx = json.loads(text[text.index("{"):])
        kinds = ctx.get("evidence_kinds") or []
        if tool == "record_recall_plan":
            spec = p["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
            dealers = spec["properties"]["dealer_id"]["enum"]
            inp = {"dealer_id": dealers[0], "combine_with": [],
                   "urgency": (ctx.get("allowed_urgencies") or ["this_week"])[0],
                   "detail": "Book the remedy at the nearest rooftop with the part in stock.",
                   "evidence_refs": kinds[:1], "confidence": "medium"}
        else:
            inp = {"narrative": "Complaint: fault reported. Cause: component failure. "
                                "Correction: component replaced.",
                   "proceed": "file", "detail": "File the claim with the repair order lines.",
                   "evidence_refs": kinds[:1], "confidence": "medium"}
        return {"output": {"message": {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": uuid.uuid4().hex, "name": tool, "input": inp}}]}},
                "stopReason": "tool_use", "usage": {"inputTokens": 120, "outputTokens": 40,
                                                    "totalTokens": 160}, "metrics": {"latencyMs": 5}}

    def _apply_guardrail(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"action": "NONE", "outputs": [], "assessments": [], "usage": {}}

    def _list_runtimes(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"agentRuntimes": [{"agentRuntimeId": "vsa_supervisor_text_staging-World00001",
                                   "agentRuntimeName": f"vsa_supervisor_text_{STAGE}",
                                   "agentRuntimeArn": self._api_env["chat"]["CVX_RUNTIME_ARN"],
                                   "agentRuntimeVersion": "1", "status": "READY",
                                   "lastUpdatedAt": self.clock.now()}]}

    def _get_runtime(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"agentRuntimeId": p.get("agentRuntimeId"), "agentRuntimeName": f"vsa_supervisor_text_{STAGE}",
                "environmentVariables": {"AVX_FLEET_IDS_CLAIM_TRUSTED": "true"}, "status": "READY"}

    def _agent_runtime(self, p: dict[str, Any]) -> dict[str, Any]:
        """The text runtime, reduced to its fleet_manager tool path.

        The prompt names the recommendation; the real ``fleet_recommendation`` tool
        reads it with the caller's JWT. The reply quotes ``safety_text`` word for word
        (the fleet_manager prompt rule, spec D18) or says why the tool refused.
        """
        import re
        payload = p.get("payload") or b"{}"
        body = json.loads(payload.decode() if isinstance(payload, (bytes, bytearray)) else payload)
        self.chat_payloads = getattr(self, "chat_payloads", []) + [body]
        prompt = str(body.get("prompt") or "")
        persona = body.get("personaId") or body.get("persona_id")
        m = re.search(r"\b(FIND-[0-9a-f-]{36}|LIVETEST-[A-Za-z0-9-]+)\b", prompt)
        reply = "I can help with your fleet."
        if persona == "fleet_manager" and m:
            from agents.supervisor.tools.fleet_recommendation import make_fleet_recommendation_tool

            class _S:
                jwt = body.get("jwt") or ""
            env = {"AVX_STAGE": STAGE, "DYNAMODB_REGION": REGION,
                   **self._get_runtime({})["environmentVariables"]}
            with lambda_env(env, "vsa_supervisor_text_staging", REGION):
                tool = make_fleet_recommendation_tool(_S(), None)
                out = tool(finding_id=m.group(1))
            self.tool_results = getattr(self, "tool_results", []) + [out]
            if out.get("available") and out.get("safety_text"):
                reply = f"{out['safety_text']}\n\nQueue status: {out.get('queue_status')}."
            elif out.get("available"):
                reply = f"Queue status: {out.get('queue_status')}."
            else:
                reply = f"I can't open that recommendation: {out.get('reason')}."
        resp = json.dumps({"result": reply, "sessionId": body.get("sessionId")}).encode()
        return {"response": streaming(resp), "contentType": "application/json", "statusCode": 200}

    # ------------------------------------------------------------------ the script
    def load_script(self) -> Any:
        mod = _load_file(SCRIPT, f"lv_fi_rw_world_{uuid.uuid4().hex[:8]}")
        mod.time = ScriptTime(self.clock)
        self.clock.patch_loaded_modules([str(SCRIPT)])
        return mod

    def run_main(self, argv: list[str], mod: Any = None, *, live: bool = True) -> tuple[int, str, Any]:
        mod = mod or self.load_script()
        old_argv = sys.argv
        out = io.StringIO()
        if live:
            os.environ["LIVE_TESTS"] = "1"
        self.calls_before_main = len(self.router.all_calls)
        self.invocations_before_main = len(self.lam.invocations)
        import signal
        saved_signals = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            # The script loads DMS's stand-in code from --dms-repo; point it at the same
            # DMS tree the world's handlers run from, so both halves are one DMS version.
            dms = [] if "--dms-repo" in argv else ["--dms-repo", str(DMS_ROOT)]
            sys.argv = [str(SCRIPT), *argv, *dms]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    # as the script's __main__ does: SIGINT/SIGTERM exit 143 via SystemExit
                    mod._install_signal_handlers()
                    rc = mod.main()
                except SystemExit as e:  # the script's own signal handler exits 143
                    rc = e.code if isinstance(e.code, int) else 1
                    self.system_exit = rc
        finally:
            sys.argv = old_argv
            os.environ.pop("LIVE_TESTS", None)
            for s, h in saved_signals.items():
                signal.signal(s, h)
        text = out.getvalue()
        for line in text.splitlines():
            if line.startswith("manifest:"):
                self.manifests.append(Path(line.split(":", 1)[1].strip()))
        return rc, text, mod

    def manifest(self) -> dict[str, Any]:
        return json.loads(self.manifests[-1].read_text())

    # -- helpers for tests -------------------------------------------------
    _WRITE_OPS = frozenset({
        "PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems",
        "CreateBackup", "Invoke", "UpdateFunctionConfiguration", "AdminCreateUser",
        "AdminSetUserPassword", "AdminAddUserToGroup", "AdminDeleteUser", "PutEvents",
        "StartQueryExecution", "PutObject", "DeleteObject"})

    def main_calls(self) -> list[tuple[str, str, dict[str, Any]]]:
        """AWS calls made since the last ``run_main`` started (script and real code)."""
        return self.router.all_calls[self.calls_before_main:]

    def main_writes(self) -> list[tuple[str, str]]:
        return [(s, o) for s, o, _ in self.main_calls()
                if o in self._WRITE_OPS and not (s == "athena" and o == "StartQueryExecution")]

    def main_invocations(self, function: str = HEALTH_FN) -> list[dict[str, Any]]:
        return [i for i in self.lam.invocations[self.invocations_before_main:]
                if i["function"] == function]

    def set_function_env(self, function: str, **changes: str) -> dict[str, str]:
        lam = self.internal("lambda")
        env = dict(lam.get_function_configuration(FunctionName=function)["Environment"]["Variables"])
        env.update(changes)
        lam.update_function_configuration(FunctionName=function, Environment={"Variables": env})
        return env

    def function_env(self, function: str = HEALTH_FN) -> dict[str, str]:
        cfg = self.internal("lambda").get_function_configuration(FunctionName=function)
        return dict(cfg["Environment"]["Variables"])

    def requests(self, api: str, method: str | None = None, route: str | None = None
                 ) -> list[dict[str, Any]]:
        reqs = (self.avx_api if api == "avx" else self.dms_api).requests
        return [r for r in reqs if (method is None or r["method"] == method)
                and (route is None or r.get("route") == route)]

    def observe(self, service: str, operation: str, fn: Any) -> None:
        """Call ``fn(params)`` on every ``service``/``operation`` call, then serve it as
        before (the world handler if there is one, else moto)."""
        prev = self.router._handlers.get((service, operation))

        def handler(api: dict[str, Any]) -> Any:
            fn(api)
            return prev(api) if prev is not None else None
        self.router.add(service, operation, handler)

    def rows(self, text: str) -> dict[str, str]:
        """Summary rows of a run: label -> status (last one wins)."""
        out: dict[str, str] = {}
        for line in text.splitlines():
            s = line.strip()
            for st in ("PASS", "FAIL", "SKIP", "NOTE"):
                if s.startswith(f"[{st}] "):
                    out[s[len(st) + 3:].split(":", 1)[0]] = st
        return out
