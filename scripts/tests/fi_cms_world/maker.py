"""CmsWorld W3 maker piece: population caches + investigations.

Population Athena tables required by
cms:services/fleet_intelligence/population_refresh.py:1ce93a75b (_SQL_*):
  quality_signals       (code_family, cohort_dimension, cohort_value, rate_30d, rate_90d,
                         rise_ratio, events_30d, vehicles_affected_30d, first_signal_date,
                         parameters_version)
  digital_thread        (thread_id, version, status, code_family, …, notes; latest-version
                         subquery in _SQL_THREADS)
  ota_campaigns         (campaign_id, campaign_name, release_version, target_model,
                         target_model_year_min, target_model_year_max, dispatch_start_date)
  fleets                (fleet_id, fleet_type) — joined in _SQL_MANAGED_VINS

Investigation infrastructure (W3 piece 2, added by wire_investigations):
  vsa-staging-avx-investigations  DynamoDB (pk=investigation_id)
  vsa-staging-avx-investigations-queue  SQS
  FiQualityBedrock wired into Router for record_root_cause / record_design_options
  ADP digital_thread_writer loaded from ADP 895f7b61 via git show
  AdpExecutor._arn patched to the moto ARN

Ported from: 49ff3719:scripts/tests/fi_loop_world/{world.py,adp_writer.py,
             fi_quality_bedrock.py,fi_quality_athena.py}
ADP commit: 895f7b61 (not 9bb82e96 used by 9.3 world).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from scripts.tests.fi_rw_world.world import STAGE, REGION, ACCOUNT  # type: ignore[attr-defined]
from scripts.tests.fi_rw_world.lambdas import lambda_env  # type: ignore[attr-defined]

_ADP_ROOT = Path(os.environ.get("FI_ADP_REPO",
                                str(Path.home() / "automotive-data-platform-on-aws")))
ADP_COMMIT = "895f7b61"
_HANDLER_PATH = "platform-foundation/source/lambda/digital_thread_writer/handler.py"

INVESTIGATIONS_TABLE = f"vsa-{STAGE}-avx-investigations"
INVESTIGATIONS_QUEUE = f"vsa-{STAGE}-avx-investigations"
INVESTIGATIONS_DLQ = f"vsa-{STAGE}-avx-investigations-dlq"
ADP_WRITER_FUNCTION = f"adp-{STAGE}-digital-thread-writer"
ADP_WRITER_ARN = (f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{ADP_WRITER_FUNCTION}")

# S3 bucket used for VIN lists written by the ADP thread writer.
_VIN_BUCKET = f"adp-{STAGE}-vin-lists-{ACCOUNT}-us-east-1"


# ---------------------------------------------------------------------------
# Population Athena DDL
# ---------------------------------------------------------------------------

def _create_population_tables(con: Any, stage: str) -> None:
    """Create the Athena (DuckDB) tables population_refresh reads.

    Tables already present in adp_faults (W2):
      adp_{stage}_engineering.fault_patterns, vehicle_build_components
      adp_{stage}_vehicle_identity.vehicle_identity
      adp_{stage}_cms_ingest.vin_cms_fleet, fleets(schema only)
    New tables added here:
      quality_signals, digital_thread, ota_campaigns, fleets table.
    """
    s = stage
    stmts = [
        f"CREATE SCHEMA IF NOT EXISTS adp_{s}_engineering",
        f"CREATE SCHEMA IF NOT EXISTS adp_{s}_ota_campaigns",
        f"CREATE SCHEMA IF NOT EXISTS adp_{s}_cms_ingest",
        f"""CREATE TABLE IF NOT EXISTS adp_{s}_engineering.quality_signals (
            code_family VARCHAR, cohort_dimension VARCHAR, cohort_value VARCHAR,
            rate_30d DOUBLE, rate_90d DOUBLE, rise_ratio DOUBLE,
            events_30d INTEGER, vehicles_affected_30d INTEGER,
            first_signal_date VARCHAR, parameters_version VARCHAR)""",
        f"""CREATE TABLE IF NOT EXISTS adp_{s}_engineering.digital_thread (
            thread_id VARCHAR, version INTEGER, status VARCHAR, code_family VARCHAR,
            cause_dimension VARCHAR, cause_value VARCHAR, detection_method VARCHAR,
            rate_ratio DOUBLE, p_value DOUBLE, option_id VARCHAR, option_remedy_type VARCHAR,
            decision_outcome VARCHAR, decided_at VARCHAR, affected_vins_count INTEGER,
            affected_vins_sha256 VARCHAR, affected_vins_s3_key VARCHAR,
            campaign_type VARCHAR, campaign_status VARCHAR,
            software_campaign_id VARCHAR, recall_campaign_id VARCHAR,
            decided_by_kind VARCHAR, notes VARCHAR)""",
        f"""CREATE TABLE IF NOT EXISTS adp_{s}_ota_campaigns.ota_campaigns (
            campaign_id VARCHAR, campaign_name VARCHAR, release_version VARCHAR,
            target_model VARCHAR, target_model_year_min INTEGER,
            target_model_year_max INTEGER, dispatch_start_date VARCHAR,
            status VARCHAR)""",
        f"""CREATE TABLE IF NOT EXISTS adp_{s}_cms_ingest.fleets (
            fleet_id VARCHAR, fleet_type VARCHAR)""",
    ]
    for sql in stmts:
        con.execute(sql)


def wire_population(world: Any) -> None:
    """Seed population Athena tables; create ota-campaigns-issued DDB; VIN S3 bucket."""
    _create_population_tables(world.athena.con, STAGE)
    # Seed fleets table with the seed fleet ids so _SQL_MANAGED_VINS works.
    from scripts.tests.fi_rw_world import seed  # noqa: PLC0415
    for fid in set(seed.VIN_FLEET.values()):
        world.athena.con.execute(
            f"INSERT INTO adp_{STAGE}_cms_ingest.fleets VALUES (?, ?)",
            [fid, "managed"])
    # DDB table for OTA campaign drafts (read by population_refresh._issued_campaigns_table).
    ddb = world.internal("dynamodb", region="us-east-1")
    try:
        ddb.create_table(TableName=f"adp-{STAGE}-ota-campaigns-issued",
            KeySchema=[{"AttributeName": "campaign_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "campaign_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
    except Exception:  # noqa: BLE001 — already exists
        pass
    # S3 bucket for ADP thread-writer VIN lists.
    try:
        world.internal("s3").create_bucket(Bucket=_VIN_BUCKET)
    except Exception:  # noqa: BLE001 — already exists
        pass
    # Inject env var so population_refresh._issued_campaigns_table() finds the table.
    world._fi_env["ADP_REGION"] = "us-east-1"


def refresh_population(world: Any) -> dict[str, Any]:
    """Run the CMS population-cache refresh in-process."""
    return world._call_fi_task("refresh-population-cache")


# ---------------------------------------------------------------------------
# FiQualityBedrock — Bedrock Converse fake for forced-tool investigation calls
# Ported from 49ff3719:scripts/tests/fi_loop_world/fi_quality_bedrock.py
# ---------------------------------------------------------------------------

class FiQualityBedrock:
    """Stateless Bedrock fake for record_root_cause + record_design_options."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def converse(self, p: dict[str, Any]) -> dict[str, Any]:
        name = p.get("toolConfig", {}).get("toolChoice", {}).get("tool", {}).get("name", "")
        self.calls.append(name)
        tools = p.get("toolConfig", {}).get("tools", [{}])
        schema = tools[0].get("toolSpec", {}).get("inputSchema", {}).get("json", {})
        props = schema.get("properties", {})
        if name == "record_root_cause":
            cands = props.get("candidate_id", {}).get("enum", ["c-01"])
            fms = props.get("failure_mode_id", {}).get("enum", ["fm-001"])
            fm = next((m for m in fms if m != "unknown"), fms[0])
            ev = props.get("evidence_refs", {}).get("items", {}).get("enum", ["quality.pattern"])
            conf = props.get("confidence", {}).get("enum", ["medium"])
            inp: dict[str, Any] = {
                "candidate_id": cands[0], "failure_mode_id": fm, "evidence_refs": [ev[0]],
                "reasoning": "Fault pattern matches component failure mode.", "confidence": conf[0],
            }
        elif name == "record_design_options":
            allowed = props.get("options", {}).get("items", {}).get("properties", {}).get(
                "remedy_option_id", {}).get("enum", ["opt-01"])
            opts: list[dict] = [{"remedy_option_id": allowed[0], "tradeoffs": "Standard.", "recommended": True}]
            if len(allowed) >= 2:
                opts.append({"remedy_option_id": allowed[1], "tradeoffs": "Alternative.", "recommended": False})
            inp = {"options": opts}
        else:
            inp = {}
        return {
            "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "fake-id", "name": name, "input": inp}}]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 50, "outputTokens": 40, "totalTokens": 90},
            "metrics": {"latencyMs": 2},
        }


# ---------------------------------------------------------------------------
# ADP digital_thread_writer loader — ADP 895f7b61
# Ported from 49ff3719:scripts/tests/fi_loop_world/adp_writer.py; commit changed.
# ---------------------------------------------------------------------------

def _load_adp_writer() -> Any:
    """Load ADP digital_thread_writer/handler.py at 895f7b61 via git show."""
    import subprocess  # noqa: PLC0415
    proc = subprocess.run(
        ["git", "-C", str(_ADP_ROOT), "show", f"{ADP_COMMIT}:{_HANDLER_PATH}"],
        capture_output=True, check=True,
    )
    with tempfile.NamedTemporaryFile(suffix=".py", prefix="adp_writer_", delete=False) as tf:
        tf.write(proc.stdout)
        tmp = tf.name
    name = f"adp_writer_{uuid.uuid4().hex[:8]}"
    # ADP 895f7b61 digital_thread_writer/handler.py imports no sibling modules;
    # appending the working-tree lambda root to sys.path would pull in unpinned code.
    spec = importlib.util.spec_from_file_location(name, tmp)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    for attr in ("_dynamodb_resource", "_s3_client"):
        if hasattr(mod, attr):
            setattr(mod, attr, None)
    return mod


def _make_writer_handler(mod: Any) -> Any:
    """Wrap the writer so boto3 clients are rebuilt under moto on each call."""
    def _handler(event: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        import boto3 as _boto3  # noqa: PLC0415
        mod._dynamodb_resource = _boto3.resource("dynamodb", region_name="us-east-1")
        mod._s3_client = _boto3.client("s3", region_name="us-east-1")
        for attr, env_key, default in [
            ("_VIN_BUCKET", "VIN_BUCKET", ""),
            ("_ISSUED_RECALLS_BUCKET", "ISSUED_RECALLS_BUCKET", ""),
            ("_STAGE", "STAGE", STAGE),
        ]:
            if hasattr(mod, attr):
                setattr(mod, attr, os.environ.get(env_key, default))
        return mod.handle_event(event, ctx)
    return _handler


def _load_investigations_handler() -> Any:
    """Load lambdas/api-avx-investigations/handler.py from the worktree."""
    avx_root = Path(__file__).resolve().parents[3]
    n = f"avx_inv_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(
        n, avx_root / "lambdas" / "api-avx-investigations" / "handler.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[n] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Seed helpers for patterns (ported from 9.3 adp_writer + fi_quality_athena)
# ---------------------------------------------------------------------------

def seed_pattern(world: Any, *, pattern_id: str, vin_list: list[str],
                 failure_mode_id: str, code_family: str = "P0SW1") -> None:
    """Seed DDB + Athena tables for one pattern with a software_update remedy option.

    DDB (us-east-1): adp-staging-fault-patterns, adp-staging-engineering-corpus.
    Athena (DuckDB): adp_{stage}_engineering.{fault_patterns,vehicle_build_components,
        vehicle_identity,engineering_failure_modes,engineering_remedy_options,
        snapshot_runs,vehicle_fault_status_daily}.
    """
    import hashlib  # noqa: PLC0415
    import datetime  # noqa: PLC0415
    import boto3 as _b  # noqa: PLC0415
    # DDB fault-patterns
    sorted_vins = sorted(set(vin_list))
    sha = hashlib.sha256(json.dumps(sorted_vins).encode()).hexdigest()
    r = _b.resource("dynamodb", region_name="us-east-1")
    # Live shape (adp-staging-fault-patterns, read 2026-10-22): one row per candidate,
    # pattern_group_id == the rank-1 row's pattern_id == the CMS cache patternId, no
    # failure_mode_id. A model_year candidate (as the live pick 702366b561f68391) so the
    # rules path matches failure modes by symptom codes only (job.py:719-761).
    r.Table(f"adp-{STAGE}-fault-patterns").put_item(Item={
        "pattern_id": pattern_id, "pattern_group_id": pattern_id, "candidate_rank": 1,
        "code_family": code_family,
        "cohort_dimension": "model_year", "cohort_value": "2023",
        "affected_vins": sorted_vins,
        "affected_vins_count": len(sorted_vins), "vehicles_affected": len(sorted_vins),
        "affected_vins_sha256": sha, "rate_ratio": "5.0", "p_value": "0.0001",
    })
    # DDB engineering-corpus (failure mode + software_update option), live shape.
    sw_opt_id = f"opt-sw-{pattern_id[:8]}"
    r.Table(f"adp-{STAGE}-engineering-corpus").put_item(Item={
        "pk": f"fm#{failure_mode_id}", "failure_mode_id": failure_mode_id,
        "component_id": "ecu", "symptom_codes": [code_family], "cause_dimension": "build_window",
        "component_name": "ECU firmware", "do_not_drive": False, "park_outside": False,
        "options": [
            {"remedy_option_id": sw_opt_id, "remedy_type": "software_update",
             "over_the_air": True, "lead_time_weeks": 2, "recommended": True},
            {"remedy_option_id": "opt-mon-001", "remedy_type": "monitor", "recommended": False},
        ],
    })
    # Athena tables for fi_quality investigation agent
    con = world.athena.con
    s = STAGE
    today = datetime.date.today()
    for ddl in [
        f"CREATE SCHEMA IF NOT EXISTS adp_{s}_engineering",
        f"CREATE SCHEMA IF NOT EXISTS adp_{s}_vehicle_identity",
        f"CREATE SCHEMA IF NOT EXISTS adp_{s}_fleet_health",
        f"CREATE SCHEMA IF NOT EXISTS adp_{s}_ota_campaigns",
        f"CREATE SCHEMA IF NOT EXISTS adp_{s}_service_records",
        f"CREATE SCHEMA IF NOT EXISTS adp_{s}_fleet_recall",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_engineering.fault_patterns "
        "(pattern_id VARCHAR, code_family VARCHAR, cohort_dimension VARCHAR, cohort_value VARCHAR, "
        "candidate_rank INTEGER, pattern_group_id VARCHAR, vehicles_affected INTEGER, "
        "vehicles_in_service INTEGER, rate DOUBLE, rate_rest DOUBLE, rate_ratio DOUBLE, "
        "p_value DOUBLE, first_detected_month VARCHAR, trend VARCHAR, method VARCHAR, "
        "affected_vins_sample VARCHAR[], computed_at TIMESTAMP, parameters_version VARCHAR)",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_engineering.vehicle_build_components "
        "(vin VARCHAR, component_id VARCHAR, supplier_id VARCHAR, supplier_lot VARCHAR, "
        "component_category VARCHAR, installed_date DATE, part_number VARCHAR)",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_vehicle_identity.vehicle_identity "
        "(vin VARCHAR, model VARCHAR, model_year INTEGER, manufacture_date DATE, assembly_plant VARCHAR)",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_engineering.engineering_failure_modes "
        "(failure_mode_id VARCHAR, component_id VARCHAR, mechanism VARCHAR, "
        "symptom_codes VARCHAR[], cause_dimension VARCHAR, do_not_drive BOOLEAN, park_outside BOOLEAN)",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_engineering.engineering_remedy_options "
        "(failure_mode_id VARCHAR, remedy_option_id VARCHAR, remedy_type VARCHAR, "
        "parts_cost_per_vehicle_usd DOUBLE, labor_hours_per_vehicle DOUBLE, "
        "lead_time_weeks DOUBLE, over_the_air BOOLEAN, trade_off_notes VARCHAR)",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_fleet_health.vehicle_fault_status_daily "
        "(vin VARCHAR, code VARCHAR, status VARCHAR, snapshot_date DATE)",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_fleet_health.snapshot_runs "
        "(table_key VARCHAR, snapshot_date DATE, row_count INTEGER)",
        # Empty tables — fi_quality queries these; graceful if empty.
        f"CREATE TABLE IF NOT EXISTS adp_{s}_ota_campaigns.ota_campaigns "
        "(campaign_id VARCHAR, campaign_name VARCHAR, status VARCHAR, release_version VARCHAR, "
        "target_model VARCHAR, target_model_year_min INTEGER, target_model_year_max INTEGER, "
        "dispatch_start_date VARCHAR)",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_ota_campaigns.ota_campaign_events "
        "(campaign_id VARCHAR, vin VARCHAR)",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_service_records.service_records "
        "(vin VARCHAR, service_date DATE, service_type VARCHAR, dtc_codes VARCHAR[], "
        "total_cost_usd DOUBLE, outcome VARCHAR, linked_campaign_id VARCHAR)",
        f"CREATE TABLE IF NOT EXISTS adp_{s}_fleet_recall.managed_vehicles "
        "(vin VARCHAR, fleet_id VARCHAR, snapshot_date DATE)",
    ]:
        try:
            con.execute(ddl)
        except Exception:  # noqa: BLE001
            pass
    # seed rows
    vin_sample = sorted_vins[:10]
    # Extend tables that adp_faults created with fewer columns.
    for col_typ in [
        ("adp_%s_vehicle_identity.vehicle_identity", "manufacture_date", "DATE"),
        ("adp_%s_vehicle_identity.vehicle_identity", "assembly_plant", "VARCHAR"),
        ("adp_%s_service_records.service_records", "dtc_codes", "VARCHAR[]"),
        ("adp_%s_service_records.service_records", "total_cost_usd", "DOUBLE"),
        ("adp_%s_service_records.service_records", "outcome", "VARCHAR"),
        ("adp_%s_service_records.service_records", "linked_campaign_id", "VARCHAR"),
    ]:
        try:
            con.execute(
                f"ALTER TABLE {col_typ[0] % s} ADD COLUMN {col_typ[1]} {col_typ[2]}")
        except Exception:  # noqa: BLE001 — already exists
            pass
    con.execute(
        f"INSERT INTO adp_{s}_engineering.fault_patterns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [pattern_id, code_family, "model_year", "2023", 1, pattern_id,
         len(sorted_vins), len(sorted_vins) + 100, 0.05, 0.005, 5.0, 0.0001,
         "2026-01", "rising", "cohort", vin_sample,
         datetime.datetime(2026, 10, 1, 5, 0, 0), "1.0"])
    for v in sorted_vins:
        con.execute(
            f"INSERT INTO adp_{s}_engineering.vehicle_build_components "
            f"(vin,component_id,supplier_id,supplier_lot,component_category,installed_date,part_number) "
            f"VALUES (?,?,?,?,?,?,?)",
            [v, "ecu", "SUP-A", "LOT-001", "electronics", datetime.date(2023, 3, 1), "MRD-ECU-1"])
        con.execute(
            f"INSERT INTO adp_{s}_vehicle_identity.vehicle_identity "
            f"(vin, model, model_year, manufacture_date, assembly_plant) VALUES (?,?,?,?,?)",
            [v, "Azimuth", 2023, datetime.date(2023, 2, 1), "CGA"])
    con.execute(
        f"INSERT INTO adp_{s}_engineering.engineering_failure_modes VALUES (?,?,?,?,?,?,?)",
        [failure_mode_id, "ecu", "firmware bug", [code_family], "supplier_lot", False, False])
    con.execute(
        f"INSERT INTO adp_{s}_engineering.engineering_remedy_options VALUES (?,?,?,?,?,?,?,?)",
        [failure_mode_id, sw_opt_id, "software_update", 0.0, 0.5, 2.0, True, "OTA firmware patch"])
    con.execute(
        f"INSERT INTO adp_{s}_engineering.engineering_remedy_options VALUES (?,?,?,?,?,?,?,?)",
        [failure_mode_id, "opt-mon-001", "monitor", 0.0, 0.0, 0.0, False, "Monitor"])
    con.execute(
        f"INSERT INTO adp_{s}_fleet_health.snapshot_runs VALUES (?,?,?)",
        ["vehicle_fault_status_daily", today, 0])


# ---------------------------------------------------------------------------
# wire_investigations — create investigations table/queue, register handlers
# ---------------------------------------------------------------------------

def wire_investigations(world: Any) -> None:
    """Create investigations DDB/SQS, register AVX investigations handler, ADP writer Lambda.

    Called from CmsWorld.open() after super().open() (which creates lam/router/avx_api).

    DDB tables created in us-east-1 (ADP writer region):
      adp-staging-digital-thread  (pk=thread_id, sk=version, GSI action_id-index)
      adp-staging-fault-patterns  (pk=pattern_id)
      adp-staging-engineering-corpus  (pk=pk)
    """
    from scripts.tests.fi_rw_world import world as _rw  # noqa: PLC0415
    # Register DuckDB UDFs for Trino functions used by fi_quality readers.
    # array_join(arr, sep): Trino/Hive function not in DuckDB.
    try:
        world.athena.con.create_function(
            "array_join",
            lambda arr, sep: sep.join(str(x) for x in arr) if arr else "",
            [list[str], str], str)
    except Exception:  # noqa: BLE001 — already registered
        pass
    # Register the tier2 investigation Athena workgroup + S3 results bucket
    health_env = dict(world.lam._env(_rw.HEALTH_FN))
    inv_wg = health_env.get("ATHENA_WORKGROUP", "cvx-staging-analytics")
    world.athena.workgroups.add(inv_wg)
    inv_bucket = health_env.get("ATHENA_RESULTS_BUCKET", "")
    if inv_bucket:
        try:
            world.internal("s3").create_bucket(Bucket=inv_bucket)
        except Exception:  # noqa: BLE001
            pass
    # --- DDB: investigations (us-west-2) ---
    ddb_w = world.internal("dynamodb")
    ddb_w.create_table(TableName=INVESTIGATIONS_TABLE,
        KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    # --- SQS: investigations queue (us-west-2) with DLQ and RedrivePolicy ---
    sqs = world.internal("sqs")
    # Create DLQ first
    dlq_result = sqs.create_queue(QueueName=INVESTIGATIONS_DLQ)
    dlq_arn = sqs.get_queue_attributes(
        QueueUrl=dlq_result["QueueUrl"], AttributeNames=["QueueArn"],
    )["Attributes"]["QueueArn"]
    # Create main queue with RedrivePolicy and VisibilityTimeout matching the real infra
    import json as _json  # noqa: PLC0415
    world._investigations_queue_url = sqs.create_queue(
        QueueName=INVESTIGATIONS_QUEUE,
        Attributes={
            "VisibilityTimeout": "5400",
            "RedrivePolicy": _json.dumps({
                "deadLetterTargetArn": dlq_arn,
                "maxReceiveCount": 2,
            }),
        },
    )["QueueUrl"]
    # Register ESM for the health agent function (needed by _check_sqs_and_event_source).
    # Create it via FakeLambda's boto3 client so it appears in list_event_source_mappings.
    from scripts.tests.fi_rw_world.world import HEALTH_FN as _HEALTH_FN  # noqa: PLC0415
    lam_boto = world.internal("lambda")
    q_arn = sqs.get_queue_attributes(
        QueueUrl=world._investigations_queue_url, AttributeNames=["QueueArn"],
    )["Attributes"]["QueueArn"]
    lam_boto.create_event_source_mapping(
        EventSourceArn=q_arn,
        FunctionName=_HEALTH_FN,
        StartingPosition="TRIM_HORIZON",
    )
    # Patch moto's Lambda backend so the ESM does NOT trigger Docker when a message
    # is sent to the queue.  pump_investigation uses receive_message + delete instead.
    # Remove the ESM from the SQS queue's lambda_event_source_mappings so moto does
    # NOT try to invoke the Lambda via Docker when a message is sent.  The ESM still
    # exists in the Lambda backend's _event_source_mappings (so list_event_source_mappings
    # returns it for _check_sqs_and_event_source).  pump_investigation uses
    # ReceiveMessage + DeleteMessage instead of the ESM trigger.
    from moto.sqs.models import sqs_backends as _sqs_backends_m  # noqa: PLC0415
    _sqs_bkd_m = _sqs_backends_m[ACCOUNT][REGION]
    for _mq in _sqs_bkd_m.queues.values():
        if _mq.name == INVESTIGATIONS_QUEUE:
            _mq.lambda_event_source_mappings.clear()
            break
    # --- DDB: ADP writer tables (us-east-1) ---
    ddb_e = world.internal("dynamodb", region="us-east-1")
    for defn in [
        {"TableName": f"adp-{STAGE}-digital-thread",
         "KeySchema": [{"AttributeName": "thread_id", "KeyType": "HASH"},
                       {"AttributeName": "version", "KeyType": "RANGE"}],
         "AttributeDefinitions": [{"AttributeName": "thread_id", "AttributeType": "S"},
                                   {"AttributeName": "version", "AttributeType": "N"},
                                   {"AttributeName": "action_id", "AttributeType": "S"}],
         "GlobalSecondaryIndexes": [{"IndexName": "action_id-index",
                                      "KeySchema": [{"AttributeName": "action_id", "KeyType": "HASH"}],
                                      "Projection": {"ProjectionType": "ALL"}}],
         "BillingMode": "PAY_PER_REQUEST"},
        {"TableName": f"adp-{STAGE}-fault-patterns",
         "KeySchema": [{"AttributeName": "pattern_id", "KeyType": "HASH"}],
         "AttributeDefinitions": [{"AttributeName": "pattern_id", "AttributeType": "S"}],
         "BillingMode": "PAY_PER_REQUEST"},
        {"TableName": f"adp-{STAGE}-engineering-corpus",
         "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
         "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
         "BillingMode": "PAY_PER_REQUEST"},
    ]:
        try:
            ddb_e.create_table(**defn)
        except Exception:  # noqa: BLE001
            pass
    # --- S3: VIN bucket for ADP writer ---
    try:
        world.internal("s3", region="us-east-1").create_bucket(
            Bucket=_VIN_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-east-1"})
    except Exception:  # noqa: BLE001
        pass
    # --- Load + register ADP writer Lambda ---
    mod = _load_adp_writer()
    writer_fn = _make_writer_handler(mod)
    iam = world.internal("iam")
    try:
        role = iam.create_role(RoleName="adp-writer-role",
                               AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    except Exception:  # noqa: BLE001
        role = f"arn:aws:iam::{ACCOUNT}:role/adp-writer-role"
    lam_w = world.internal("lambda")  # lam._env reads from this region
    writer_env = {"STAGE": STAGE, "VIN_BUCKET": _VIN_BUCKET, "ISSUED_RECALLS_BUCKET": ""}
    world.lam.create(lam_w, role, ADP_WRITER_FUNCTION,
                     "digital_thread_writer.handler.handle_event",
                     writer_env, writer_fn, timeout=60)
    # --- Patch AdpExecutor._arn ---
    import agents.executors.registry as _reg  # noqa: PLC0415
    adp_exec = _reg.EXECUTORS.get("adp")
    if adp_exec is not None and hasattr(adp_exec, "_arn"):
        adp_exec._arn = ADP_WRITER_ARN
        if hasattr(adp_exec, "_lambda_client_injected"):
            adp_exec._lambda_client_injected = None
    world._api_env.setdefault("findings", {})["ADP_THREAD_WRITER_ARN"] = ADP_WRITER_ARN
    # --- FiQualityBedrock: intercept Bedrock Converse for fi_quality agent ---
    world.fi_quality_bedrock = FiQualityBedrock()
    _orig_converse = world.router._handlers.get(("bedrock-runtime", "Converse"))

    def _converse(p: dict[str, Any]) -> dict[str, Any]:
        tool = p.get("toolConfig", {}).get("toolChoice", {}).get("tool", {}).get("name", "")
        if tool in ("record_root_cause", "record_design_options"):
            return world.fi_quality_bedrock.converse(p)
        return _orig_converse(p) if _orig_converse else world._model_unavailable(p)

    world.router.add("bedrock-runtime", "Converse", _converse)
    # --- AVX investigations routes ---
    inv_mod = _load_investigations_handler()
    world.handlers["investigations"] = inv_mod
    world._api_env["investigations"] = {
        "AVX_INVESTIGATIONS_TABLE": INVESTIGATIONS_TABLE,
        "AVX_INVESTIGATIONS_QUEUE_URL": world._investigations_queue_url,
        "AVX_INVESTIGATIONS_DAILY_CAP": "50",
    }
    world.avx_api.add("/investigations", "POST", "investigations", inv_mod.handler)
    world.avx_api.add("/investigations/{investigationId}", "GET", "investigations", inv_mod.handler)

    def _invoke_inv(name: str, fn: Any, event: dict[str, Any], ctx: Any) -> dict[str, Any]:
        env = world._api_env.get("investigations", {})
        with lambda_env(env, name, REGION):
            return fn(event, ctx)

    # Override avx_api's invoke so investigations uses the investigations env.
    _orig_invoke = world.avx_api._invoke

    def _patched_invoke(name: str, fn: Any, event: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if name == "investigations":
            return _invoke_inv(name, fn, event, ctx)
        return _orig_invoke(name, fn, event, ctx)

    world.avx_api._invoke = _patched_invoke  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# pump_investigation — drive the tier2 runner with an SQS investigation record
# Ported from 49ff3719:scripts/tests/fi_loop_world/world.py:pump_investigation
# ---------------------------------------------------------------------------

def pump_investigation(world: Any, investigation_id: str) -> dict[str, Any]:
    """Receive the SQS message, invoke the tier2 runner, delete on success.

    Uses ReceiveMessage so ApproximateNumberOfMessagesNotVisible is 1 while the
    handler runs; DeleteMessage on success leaves the queue empty.  Leaves the
    message for DLQ redrive if the handler raises.

    Returns {"result": ..., "investigation": <DDB row>}.
    """
    from scripts.tests.fi_rw_world.world import HEALTH_FN, ACCOUNT as _ACCT  # noqa: PLC0415
    sqs = world.internal("sqs")
    resp = sqs.receive_message(
        QueueUrl=world._investigations_queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=0,
        AttributeNames=["All"],
    )
    msgs = resp.get("Messages", [])
    assert msgs, (
        f"no message in queue {INVESTIGATIONS_QUEUE}; "
        f"pump called with investigation_id={investigation_id!r}"
    )
    msg = msgs[0]
    receipt_handle = msg["ReceiptHandle"]
    body = msg["Body"]
    assert body == investigation_id, (
        f"queue message body {body!r} != investigation_id {investigation_id!r}"
    )
    sqs_event = {
        "Records": [{
            "messageId": msg["MessageId"],
            "receiptHandle": receipt_handle,
            "body": body,
            "attributes": {
                "ApproximateReceiveCount": (msg.get("Attributes") or {}).get(
                    "ApproximateReceiveCount", "1"),
                "SentTimestamp": (msg.get("Attributes") or {}).get(
                    "SentTimestamp", "1698000000000"),
                "SenderId": _ACCT,
                "ApproximateFirstReceiveTimestamp": (msg.get("Attributes") or {}).get(
                    "ApproximateFirstReceiveTimestamp", "1698000000001"),
            },
            "messageAttributes": msg.get("MessageAttributes", {}),
            "md5OfBody": msg.get("MD5OfBody", ""),
            "eventSource": "aws:sqs",
            "eventSourceARN": f"arn:aws:sqs:{REGION}:{_ACCT}:{INVESTIGATIONS_QUEUE}",
            "awsRegion": REGION,
        }],
    }
    inv_env = dict(world.lam._env(HEALTH_FN))
    inv_env.update({
        "AVX_INVESTIGATIONS_TABLE": INVESTIGATIONS_TABLE,
        "AVX_INVESTIGATIONS_QUEUE_URL": world._investigations_queue_url,
        "SQS_MAX_RECEIVE_COUNT": "2",
        "ADP_THREAD_WRITER_ARN": ADP_WRITER_ARN,
        "FI_INVESTIGATION_MODEL_ENABLED": "false",
    })
    with lambda_env(inv_env, HEALTH_FN, REGION):
        result = world.handlers["runner"].lambda_handler(sqs_event, None)
    # Delete only when the runner did not report this message as failed.
    # The ESM uses reportBatchItemFailures=true; failures come back as
    # {"batchItemFailures": [{"itemIdentifier": messageId}, ...]}.
    # On failure, ChangeMessageVisibility(0) makes the message immediately
    # visible so a second pump plus moto's redrive (maxReceiveCount=2)
    # moves it to the DLQ — matching live ESM behaviour.
    # Note: moto's clock is frozen; change_visibility(0) sets
    # visible_at = frozen_now and the strict ">" check keeps the message
    # invisible until the clock advances.  Advance 1 s so the next
    # receive_message call sees current_time > visible_at.
    failed_ids = {
        item["itemIdentifier"]
        for item in (result or {}).get("batchItemFailures", [])
    }
    if msg["MessageId"] in failed_ids:
        sqs.change_message_visibility(
            QueueUrl=world._investigations_queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=0,
        )
        world.clock.advance(1)
    else:
        sqs.delete_message(
            QueueUrl=world._investigations_queue_url,
            ReceiptHandle=receipt_handle,
        )
    table = world.table(INVESTIGATIONS_TABLE)
    item = table.get_item(
        Key={"investigation_id": investigation_id}, ConsistentRead=True,
    ).get("Item", {})
    return {"result": result, "investigation": item}

# ---------------------------------------------------------------------------
# seed_live_world — load live data files and seed DDB + S3 for E.2 tests
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).parent / "data"
_LIVE_FP_FILE = _DATA_DIR / "live_fault_patterns_items.json"
_LIVE_CORPUS_FILE = _DATA_DIR / "live_corpus_items.json"
_LIVE_CACHE_IDS_FILE = _DATA_DIR / "live_cache_pattern_ids.json"

# Live patternIds (45 cache ids), loaded once at import time.
# Used by E.2 tests to build a minimal patterns cache.
LIVE_CACHE_PATTERN_IDS: list[str] = json.loads(_LIVE_CACHE_IDS_FILE.read_text())

# From the brief §E.2:
LIVE_PICK_1 = "702366b561f68391"   # U007 model_year 2025, FM-COMM-01, OPT-FM-COMM-01-A
LIVE_PICK_2 = "78229d78d0d19904"   # P1A0 model_year 2023, FM-BCP-01, OPT-FM-BCP-01-B


def _ddb_str(v: object) -> str:
    """Extract a str from a plain Python str or DDB-typed {"S": "..."}."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and "S" in v:
        return v["S"]
    return ""


def seed_live_world(world: object, *, lock_first: bool = False) -> None:
    """Seed the world with live DDB-typed items from the three data files.

    Loads adp-staging-fault-patterns, adp-staging-engineering-corpus, and a
    minimal patterns S3 cache covering all 45 live patternIds.

    When *lock_first* is True, a lock row ``lock#LIVE_PICK_1`` is inserted in
    the investigations table so that the pick falls through to LIVE_PICK_2.

    Does NOT write VIN lists (stripped from live_fault_patterns_items.json).
    """
    import boto3 as _b  # noqa: PLC0415
    import datetime as _dt  # noqa: PLC0415

    # ── Load data files ────────────────────────────────────────────────────
    fp_items: list[dict] = json.loads(_LIVE_FP_FILE.read_text())
    corpus_items: list[dict] = json.loads(_LIVE_CORPUS_FILE.read_text())

    # ── Seed fault-patterns table (us-east-1) ─────────────────────────────
    r_e = _b.resource("dynamodb", region_name="us-east-1")
    fp_table = r_e.Table(f"adp-{STAGE}-fault-patterns")
    for raw in fp_items:
        # Convert DDB-typed item to plain Python types
        plain: dict = {}
        for k, v in raw.items():
            if k == "affected_vins":
                continue  # stripped
            if isinstance(v, dict):
                if "S" in v:
                    plain[k] = v["S"]
                elif "N" in v:
                    # keep as Decimal for DDB resource
                    from decimal import Decimal  # noqa: PLC0415
                    plain[k] = Decimal(v["N"])
                elif "BOOL" in v:
                    plain[k] = v["BOOL"]
                elif "L" in v:
                    plain[k] = [_ddb_str(i) for i in v["L"]]
                elif "NULL" in v:
                    plain[k] = None
                else:
                    plain[k] = str(v)
            else:
                plain[k] = v
        try:
            fp_table.put_item(Item={k: v for k, v in plain.items() if v is not None})
        except Exception:  # noqa: BLE001 — moto idempotent
            pass

    # ── Seed engineering-corpus table (us-east-1) ─────────────────────────
    corpus_table = r_e.Table(f"adp-{STAGE}-engineering-corpus")
    for raw in corpus_items:
        plain2: dict = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                if "S" in v:
                    plain2[k] = v["S"]
                elif "BOOL" in v:
                    plain2[k] = v["BOOL"]
                elif "L" in v:
                    # list of maps (options) or list of strings (symptom_codes)
                    inner = v["L"]
                    if inner and isinstance(inner[0], dict) and "M" in inner[0]:
                        # options: list of maps
                        opts = []
                        for opt_wrap in inner:
                            m = opt_wrap.get("M", {})
                            opts.append({ok: _ddb_str(ov) if isinstance(ov, dict) else ov
                                         for ok, ov in m.items()})
                        plain2[k] = opts
                    else:
                        plain2[k] = [_ddb_str(i) for i in inner]
                elif "NULL" in v:
                    plain2[k] = None
                else:
                    plain2[k] = str(v)
            else:
                plain2[k] = v
        try:
            corpus_table.put_item(Item={k: v for k, v in plain2.items() if v is not None})
        except Exception:  # noqa: BLE001
            pass

    # ── Seed S3 patterns cache (us-east-1) ────────────────────────────────
    import boto3 as _b3  # noqa: PLC0415
    from scripts.tests.fi_rw_world.world import ACCOUNT  # noqa: PLC0415, attr-defined
    bucket = f"cms-{STAGE}-athena-results-{ACCOUNT}-us-east-1"
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    # Minimal pattern items: one per live cache patternId with the code_family
    # derived from fault-patterns, or a placeholder.  Only fields needed by
    # _pick_pattern: patternId, codeFamily, topCause.
    cf_by_gid: dict[str, str] = {}
    for raw in fp_items:
        gid = raw.get("pattern_group_id", {}).get("S", "") if isinstance(raw.get("pattern_group_id"), dict) else raw.get("pattern_group_id", "")
        cf = raw.get("code_family", {}).get("S", "") if isinstance(raw.get("code_family"), dict) else raw.get("code_family", "")
        if gid and cf:
            cf_by_gid.setdefault(gid, cf)
    pat_list = []
    for pid in LIVE_CACHE_PATTERN_IDS:
        cf = cf_by_gid.get(pid, "P0SW1")
        pat_list.append({
            "patternId": pid, "codeFamily": cf,
            "topCause": {"cohortDimension": "model_year", "cohortValue": "2025",
                         "candidateRank": 1, "rateRatio": 3.0, "pValue": 0.001},
            "candidateCount": 1, "vehiclesAffected": 10, "vehiclesInService": 100,
            "rate": 0.1, "firstDetectedMonth": "2026-01", "trend": "rising",
            "method": "cohort", "computedAt": now_iso, "parametersVersion": "1.0",
        })
    s3_client = _b3.client("s3", region_name="us-east-1")
    cache_payloads = {
        "_cache/fault-patterns-v1.json": {"computedAt": now_iso, "data": pat_list},
        "_cache/quality-signals-v1.json": {"computedAt": now_iso, "data": []},
        "_cache/health-rollup-v1.json": {"computedAt": now_iso, "totalManagedVins": 100, "data": []},
        "_cache/digital-thread-list-v1.json": {"computedAt": now_iso, "data": []},
        "_cache/software-campaigns-v1.json": {"computedAt": now_iso, "data": []},
    }
    for key, payload in cache_payloads.items():
        try:
            s3_client.put_object(
                Bucket=bucket,
                Key=f"fleet-intelligence/{key}",
                Body=json.dumps(payload).encode(),
            )
        except Exception:  # noqa: BLE001
            pass

    # ── Optionally lock the first pick ────────────────────────────────────
    if lock_first:
        r_w = _b.resource("dynamodb", region_name="us-west-2")
        try:
            r_w.Table(INVESTIGATIONS_TABLE).put_item(
                Item={"investigation_id": f"lock#{LIVE_PICK_1}"}
            )
        except Exception:  # noqa: BLE001
            pass
