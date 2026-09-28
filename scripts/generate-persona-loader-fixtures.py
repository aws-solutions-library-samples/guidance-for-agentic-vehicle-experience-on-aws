#!/usr/bin/env python3
"""Generate golden JSON fixtures for persona-loader tabular products.

Reads from ADP staging Athena (cross-region us-east-1) and emits 6 fixture
files under --output-dir.  Falls back to hand-curated synthetic rows when the
Athena workgroup is not yet deployed (Group 7 not yet shipped).

Usage:
    python scripts/generate-persona-loader-fixtures.py --stage staging --dry-run
    python scripts/generate-persona-loader-fixtures.py --stage staging

Spec: .kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md § "Eval extension"
      and § R6 (schema-conform shapes).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Anonymisation helpers
# ---------------------------------------------------------------------------

_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.IGNORECASE)
_CUST_RE = re.compile(r"\bCUST-[0-9A-F]{8}\b")

FIXTURE_VIN = "1FTRX18W6XKB12345"   # Tier 1 convention from evals/runner/mocks.py
FIXTURE_CUST = "cust-fixture-001"


def _anonymise(row: dict) -> dict:
    """Replace PII-bearing fields with synthetic placeholders."""
    out: dict = {}
    for k, v in row.items():
        sv = str(v) if v is not None else ""
        sv = _VIN_RE.sub(FIXTURE_VIN, sv)
        sv = _EMAIL_RE.sub("<email-redacted>", sv)
        sv = _CUST_RE.sub(FIXTURE_CUST, sv)
        # PII field names: replace any value in obviously-PII columns
        if k in {"full_name", "phone", "subject", "notes", "complaint_text"}:
            sv = "<redacted>"
        out[k] = sv
    return out


# ---------------------------------------------------------------------------
# Athena live fetch
# ---------------------------------------------------------------------------

_QUERIES: dict[str, str] = {
    "vehicle_identity": (
        "SELECT vin, model_year, make, model, trim, body_style, "
        "drive_type, powertrain_type, battery_chemistry, battery_pack_kwh, "
        "range_epa_mi, current_software_version "
        "FROM adp_{stage}_vehicle_identity.vehicle_identity LIMIT 5"
    ),
    "service_records": (
        "SELECT service_id, service_date, service_month, vin, "
        "service_type, labor_hours, total_cost_usd, warranty_covered, outcome "
        "FROM adp_{stage}_service_records.service_records LIMIT 5"
    ),
    "charging_sessions": (
        "SELECT session_id, vin, session_date, start_time, end_time, "
        "station_type, network_provider, kwh_delivered, peak_power_kw, cost_usd "
        "FROM adp_{stage}_charging_sessions.charging_sessions LIMIT 5"
    ),
    "customer_360": (
        "SELECT customer_id, snapshot_date, customer_segment, "
        "lifetime_value_usd, vehicles_owned_count, health_score, "
        "churn_probability, total_charging_sessions_30d, total_kwh_consumed_30d "
        "FROM adp_{stage}_customer_360.customer_360 LIMIT 5"
    ),
    "customer_interactions": (
        "SELECT interaction_id, customer_id, interaction_date, channel, "
        "interaction_type, outcome, duration_seconds, sentiment_score, csat_score "
        "FROM adp_{stage}_customer_interactions.customer_interactions LIMIT 5"
    ),
    "ota_campaigns": (
        "SELECT e.campaign_id, e.vin, e.dispatch_date, e.dispatch_time, "
        "e.final_status, e.previous_software_version, e.new_software_version, "
        "h.campaign_name, h.category, h.severity "
        "FROM adp_{stage}_ota_campaigns.ota_campaign_events e "
        "JOIN adp_{stage}_ota_campaigns.ota_campaigns h "
        "ON h.campaign_id = e.campaign_id LIMIT 5"
    ),
}

PRODUCTS = list(_QUERIES.keys())


def _athena_fetch(product: str, stage: str, workgroup: str, results_bucket: str) -> list[dict] | None:
    """Return list of anonymised row dicts from Athena, or None on any failure."""
    try:
        import boto3  # noqa: PLC0415
    except ImportError:
        log.warning("boto3 not available — using synthetic mode")
        return None
    try:
        athena = boto3.client("athena", region_name="us-east-1")
        sql = _QUERIES[product].format(stage=stage)
        start = athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Catalog": "AwsDataCatalog"},
            WorkGroup=workgroup,
            ResultConfiguration={"OutputLocation": f"s3://{results_bucket}/athena-results/"},
        )
        eid = start["QueryExecutionId"]
        for _ in range(30):
            # Bounded poll, not a debug sleep: Athena has no blocking-wait or
            # boto3 waiter for query completion, so callers must poll
            # get_query_execution. 1s x 30 gives a ~30s ceiling, enforced by the
            # for/else below. See issues/2026-08-03-cvx-probe-critical-triage/.
            # nosemgrep: arbitrary-sleep
            time.sleep(1)
            resp = athena.get_query_execution(QueryExecutionId=eid)
            state = resp["QueryExecution"]["Status"]["State"]
            if state == "SUCCEEDED":
                break
            if state in ("FAILED", "CANCELLED"):
                reason = resp["QueryExecution"]["Status"].get("StateChangeReason", "")
                log.warning("Athena %s for product=%s: %s", state, product, reason)
                return None
        else:
            log.warning("Athena timeout for product=%s", product)
            return None

        results = athena.get_query_results(QueryExecutionId=eid, MaxResults=6)
        rows_raw = results.get("ResultSet", {}).get("Rows", [])
        if len(rows_raw) < 2:
            log.warning("No data rows returned for product=%s", product)
            return None
        headers = [c.get("VarCharValue", "") for c in rows_raw[0]["Data"]]
        rows = [
            _anonymise(dict(zip(headers, [c.get("VarCharValue", "") for c in r["Data"]])))
            for r in rows_raw[1:]
        ]
        return rows or None
    except Exception as exc:
        log.warning("Athena fetch failed for product=%s: %s", product, exc)
        return None


# ---------------------------------------------------------------------------
# Synthetic fallback rows (schema-conform per cvx-integration-contract.md)
# ---------------------------------------------------------------------------

def _synthetic_rows(product: str) -> list[dict]:
    v = FIXTURE_VIN
    c = FIXTURE_CUST
    if product == "vehicle_identity":
        return [
            {"vin": v, "model_year": "2022", "make": "Ford", "model": "F-150",
             "trim": "Lariat", "body_style": "pickup", "drive_type": "AWD",
             "powertrain_type": "BEV", "battery_chemistry": "NMC",
             "battery_pack_kwh": "98.0", "range_epa_mi": "300",
             "current_software_version": "4.2.1"},
            {"vin": "1FMCU0J91KUB12345", "model_year": "2023", "make": "Ford",
             "model": "Mustang Mach-E", "trim": "GT", "body_style": "suv",
             "drive_type": "AWD", "powertrain_type": "BEV",
             "battery_chemistry": "NMC", "battery_pack_kwh": "91.0",
             "range_epa_mi": "270", "current_software_version": "5.0.3"},
            {"vin": "5YJSA1DG9PF123456", "model_year": "2023", "make": "Generic",
             "model": "Sedan-EV", "trim": "Standard", "body_style": "sedan",
             "drive_type": "RWD", "powertrain_type": "BEV",
             "battery_chemistry": "LFP", "battery_pack_kwh": "75.0",
             "range_epa_mi": "250", "current_software_version": "2024.12.1"},
        ]
    if product == "service_records":
        return [
            {"service_id": "SVC-001", "service_date": "2026-03-15",
             "service_month": "2026-03-01", "vin": v,
             "service_type": "scheduled_maintenance", "labor_hours": "2.5",
             "total_cost_usd": "350.00", "warranty_covered": "false",
             "outcome": "completed"},
            {"service_id": "SVC-002", "service_date": "2025-11-10",
             "service_month": "2025-11-01", "vin": v,
             "service_type": "charging_system", "labor_hours": "1.0",
             "total_cost_usd": "120.00", "warranty_covered": "true",
             "outcome": "completed"},
            {"service_id": "SVC-003", "service_date": "2025-06-22",
             "service_month": "2025-06-01", "vin": v,
             "service_type": "software_update", "labor_hours": "0.5",
             "total_cost_usd": "0.00", "warranty_covered": "true",
             "outcome": "completed"},
        ]
    if product == "charging_sessions":
        return [
            {"session_id": "sess-001", "vin": v, "session_date": "2026-06-01",
             "start_time": "2026-06-01T08:00:00Z", "end_time": "2026-06-01T09:30:00Z",
             "station_type": "public_dc_fast", "network_provider": "EA",
             "kwh_delivered": "55.2", "peak_power_kw": "150.0", "cost_usd": "22.08"},
            {"session_id": "sess-002", "vin": v, "session_date": "2026-05-28",
             "start_time": "2026-05-28T22:00:00Z", "end_time": "2026-05-29T06:00:00Z",
             "station_type": "home_l2", "network_provider": "",
             "kwh_delivered": "72.0", "peak_power_kw": "9.6", "cost_usd": "10.80"},
            {"session_id": "sess-003", "vin": v, "session_date": "2026-05-20",
             "start_time": "2026-05-20T12:15:00Z", "end_time": "2026-05-20T13:00:00Z",
             "station_type": "public_dc_fast", "network_provider": "EVgo",
             "kwh_delivered": "40.0", "peak_power_kw": "100.0", "cost_usd": "16.00"},
        ]
    if product == "customer_360":
        return [
            {"customer_id": c, "snapshot_date": "2026-06-01",
             "customer_segment": "fleet", "lifetime_value_usd": "85000.00",
             "vehicles_owned_count": "3", "health_score": "78",
             "churn_probability": "0.12", "total_charging_sessions_30d": "12",
             "total_kwh_consumed_30d": "660.0"},
            {"customer_id": "cust-fixture-002", "snapshot_date": "2026-06-01",
             "customer_segment": "enthusiast", "lifetime_value_usd": "42000.00",
             "vehicles_owned_count": "1", "health_score": "90",
             "churn_probability": "0.05", "total_charging_sessions_30d": "20",
             "total_kwh_consumed_30d": "980.0"},
            {"customer_id": "cust-fixture-003", "snapshot_date": "2026-06-01",
             "customer_segment": "commercial", "lifetime_value_usd": "120000.00",
             "vehicles_owned_count": "8", "health_score": "62",
             "churn_probability": "0.28", "total_charging_sessions_30d": "45",
             "total_kwh_consumed_30d": "2100.0"},
        ]
    if product == "customer_interactions":
        return [
            {"interaction_id": "int-001", "customer_id": c,
             "interaction_date": "2026-06-05", "channel": "mobile_app",
             "interaction_type": "support_request", "outcome": "resolved",
             "duration_seconds": "420", "sentiment_score": "0.6",
             "csat_score": "4"},
            {"interaction_id": "int-002", "customer_id": c,
             "interaction_date": "2026-05-18", "channel": "call_center",
             "interaction_type": "billing_inquiry", "outcome": "resolved",
             "duration_seconds": "300", "sentiment_score": "0.3",
             "csat_score": "3"},
            {"interaction_id": "int-003", "customer_id": c,
             "interaction_date": "2026-04-30", "channel": "service_center",
             "interaction_type": "service_scheduling", "outcome": "completed",
             "duration_seconds": "600", "sentiment_score": "0.8",
             "csat_score": "5"},
        ]
    if product == "ota_campaigns":
        return [
            {"campaign_id": "CAMP-001", "vin": v, "dispatch_date": "2026-05-01",
             "dispatch_time": "2026-05-01T03:00:00Z",
             "final_status": "installed",
             "previous_software_version": "4.1.0",
             "new_software_version": "4.2.1",
             "campaign_name": "FW-2026-MAY-SAFETY", "category": "safety_recall",
             "severity": "high"},
            {"campaign_id": "CAMP-002", "vin": v, "dispatch_date": "2025-11-15",
             "dispatch_time": "2025-11-15T04:00:00Z",
             "final_status": "installed",
             "previous_software_version": "3.9.5",
             "new_software_version": "4.1.0",
             "campaign_name": "FW-2025-NOV-FEATURE", "category": "feature_add",
             "severity": "medium"},
            {"campaign_id": "CAMP-003", "vin": v, "dispatch_date": "2025-06-10",
             "dispatch_time": "2025-06-10T02:00:00Z",
             "final_status": "installed",
             "previous_software_version": "3.8.0",
             "new_software_version": "3.9.5",
             "campaign_name": "FW-2025-JUN-BUGFIX", "category": "bug_fix",
             "severity": "low"},
        ]
    return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate persona-loader golden fixtures")
    parser.add_argument("--stage", default="staging")
    parser.add_argument("--output-dir",
                        default="agents/supervisor/tests/fixtures/persona_loader_golden")
    parser.add_argument("--workgroup", default="cvx-staging-analytics")
    parser.add_argument("--results-bucket", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    # Determine results bucket if not provided
    results_bucket = args.results_bucket
    if not results_bucket:
        try:
            import boto3  # noqa: PLC0415
            acct = boto3.client("sts").get_caller_identity()["Account"]
            results_bucket = f"cvx-{args.stage}-athena-results-{acct}-us-east-1"
        except Exception:
            results_bucket = f"cvx-{args.stage}-athena-results-unknown-us-east-1"

    # Probe workgroup availability
    athena_available = False
    if not args.dry_run:
        try:
            import boto3  # noqa: PLC0415
            boto3.client("athena", region_name="us-east-1").get_work_group(
                WorkGroup=args.workgroup
            )
            athena_available = True
            log.info("Workgroup '%s' found — using live-Athena mode", args.workgroup)
        except Exception as exc:
            log.info(
                "Workgroup '%s' not available (%s) — synthetic-fallback mode",
                args.workgroup, exc.__class__.__name__,
            )

    if args.dry_run:
        log.info("DRY-RUN: would process %d products: %s", len(PRODUCTS), ", ".join(PRODUCTS))
        for product in PRODUCTS:
            mode = "live-Athena" if athena_available else "synthetic-fallback"
            log.info("  product=%-30s  mode=%s", product, mode)
        log.info("DRY-RUN: output-dir=%s  stage=%s", output_dir, args.stage)
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)

    errors = 0
    for product in PRODUCTS:
        rows = None
        mode = "synthetic"
        if athena_available:
            rows = _athena_fetch(product, args.stage, args.workgroup, results_bucket)
            if rows:
                mode = "live-Athena"

        if not rows:
            rows = _synthetic_rows(product)
            mode = "synthetic-fallback"

        out_path = output_dir / f"{product}.json"
        out_path.write_text(json.dumps(rows, indent=2))
        log.info("  %-30s  mode=%-20s  rows=%d  -> %s", product, mode, len(rows), out_path)

    log.info("Done: %d products written to %s  errors=%d", len(PRODUCTS), output_dir, errors)
    return errors


if __name__ == "__main__":
    sys.exit(main())
