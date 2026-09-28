#!/usr/bin/env python3
"""Verify the Tier 2 cross-account ADP reads before anything is built on them.

Spec: `.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md` § D3 / D3a
Task: T1.2 (Group 1 hard gate)

Why this exists
---------------
The CVX Tier 2 initiative has already shipped one wrong conclusion by trusting a
schema instead of checking the data: PRD OQ8 assumed an EV brake-wear signal was
derivable because ``vehicle_telemetry_aggregated``'s schema documents
``motor_torque_nm`` as *"negative under regen"*. The generator emits
``50.0 + 200.0 * rand()`` — always positive. The ADP spec built on that assumption
was withdrawn.

So this gate proves, against live infrastructure, that each component's data is
actually queryable and actually carries the signal the agent needs — before a line
of agent code is written.

Run
---
    python3 scripts/verify-tier2-adp-reads.py --stage staging
    python3 scripts/verify-tier2-adp-reads.py --stage staging --component battery

Exit codes
----------
    0  every checked component is buildable
    1  at least one component is NOT buildable (details on stdout)

Non-interactive per ~/.kiro/steering/non-interactive.md. Read-only: this script
issues no writes to any ADP surface.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import boto3

# ADP's lake and Glue catalog live in us-east-1 for BOTH CVX stages, even though
# CVX staging compute runs in us-west-2. Verified 2026-08-03: us-west-2 has zero
# Glue databases and no cvx-*-analytics workgroup; us-east-1 has both. This is
# why `agents/supervisor/config.py` defaults `adp_region` to us-east-1.
ADP_REGION = "us-east-1"

POLL_SECONDS = 2
POLL_ATTEMPTS = 90


def _run_query(athena: Any, sql: str, workgroup: str) -> tuple[str, list[list[str | None]], str | None]:
    """Start a query, poll to terminal state, return (state, rows, failure_reason)."""
    started = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup)
    qid = started["QueryExecutionId"]

    state = "UNKNOWN"
    reason: str | None = None
    for _ in range(POLL_ATTEMPTS):
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = status["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason")
            break
        # Athena exposes no boto3 waiter for query completion; a bounded poll is
        # the documented pattern. The loop is capped at POLL_ATTEMPTS and exits
        # on any terminal state above.
        # Keep `nosemgrep` as the last comment line before the target.
        # nosemgrep: arbitrary-sleep
        time.sleep(POLL_SECONDS)

    if state != "SUCCEEDED":
        return state, [], reason

    result = athena.get_query_results(QueryExecutionId=qid, MaxResults=25)
    rows = [[cell.get("VarCharValue") for cell in row["Data"]] for row in result["ResultSet"]["Rows"]]
    return state, rows, None


def _print_rows(rows: list[list[str | None]]) -> None:
    for row in rows:
        print("      " + " | ".join("" if c is None else c for c in row))


def check_battery(athena: Any, stage: str, workgroup: str) -> bool:
    """Battery reads ADP `energy_usage`.

    Spec D3a is explicit that this must NOT read
    `vehicle_telemetry_aggregated.state_of_health_pct`, which declares the same
    column name but is generated from a global row index and is therefore
    effectively identical for every VIN. `energy_usage` derives SoH from
    `battery_age_days` (~2%/yr), so vehicles genuinely differ — which is the whole
    basis for per-owner battery judgment.
    """
    print("\n[battery] source: adp_%s_energy_usage.energy_usage" % stage)
    sql = f"""
        SELECT ROUND(MIN(state_of_health_pct), 2)    AS fleet_soh_min,
               ROUND(MAX(state_of_health_pct), 2)    AS fleet_soh_max,
               ROUND(AVG(state_of_health_pct), 2)    AS fleet_soh_avg,
               ROUND(STDDEV(state_of_health_pct), 3) AS soh_stddev,
               COUNT(DISTINCT vin)                   AS vins,
               COUNT(*)                              AS total_rows
        FROM adp_{stage}_energy_usage.energy_usage
        WHERE state_of_health_pct IS NOT NULL
    """
    state, rows, reason = _run_query(athena, sql, workgroup)
    if state != "SUCCEEDED":
        print(f"    NOT BUILDABLE — query {state}: {reason}")
        return False
    _print_rows(rows)

    if len(rows) < 2:
        print("    NOT BUILDABLE — no aggregate row returned")
        return False

    header, values = rows[0], rows[1]
    data = dict(zip(header, values))
    total_rows = int(data.get("total_rows") or 0)
    stddev = float(data.get("soh_stddev") or 0.0)

    if total_rows == 0:
        print("    NOT BUILDABLE — zero rows")
        return False

    # The signal the agent needs is per-vehicle variation. A near-zero stddev
    # would mean every vehicle reports the same SoH, making "is my battery
    # degrading normally *for me*" unanswerable.
    if stddev < 0.5:
        print(f"    NOT BUILDABLE — SoH stddev {stddev} is too flat for per-owner judgment")
        return False

    print(f"    BUILDABLE — {total_rows:,} rows, SoH stddev {stddev} (genuine per-vehicle variation)")
    return True


def check_tires(athena: Any, stage: str, workgroup: str) -> bool:
    """Tires read ADP `tire_health`.

    Verified 2026-08-03 as NOT DEPLOYED — see the module docstring of the
    corresponding decisions.md entry. Retained so the gate re-runs green once ADP
    deploys the product, rather than being deleted and forgotten.
    """
    print("\n[tires] source: adp_%s_tire_health.tire_health" % stage)
    sql = f"""
        SELECT COUNT(*)                    AS total_rows,
               COUNT(DISTINCT vin)         AS vins,
               SUM(CASE WHEN needs_replacement THEN 1 ELSE 0 END) AS flagged
        FROM adp_{stage}_tire_health.tire_health
    """
    state, rows, reason = _run_query(athena, sql, workgroup)
    if state != "SUCCEEDED":
        print(f"    NOT BUILDABLE — query {state}: {reason}")
        print("    Expected as of 2026-08-03: the product is not deployed in either lake.")
        return False
    _print_rows(rows)
    if len(rows) < 2 or int((rows[1][0] or "0")) == 0:
        print("    NOT BUILDABLE — zero rows")
        return False
    print("    BUILDABLE")
    return True


def check_brakes(athena: Any, stage: str, workgroup: str) -> bool:
    """Brakes read ADP `service_records` filtered to service_type='brake_service'.

    Spec § D3b: the highest-value brake finding is whether a job is still open
    (outcome ∈ {parts_pending, follow_up_required}).  This check proves that signal
    is actually present in live data — not just that the table exists.

    The gate asserts:
    1. Total brake rows and VIN count are non-zero.
    2. At least one row carries an open outcome (the signal the agent depends on).

    If the open-outcome count is zero, the table is present but the signal the agent
    depends on is absent — mark NOT BUILDABLE rather than shipping a brakes component
    that can never surface the finding that matters most.

    SQL shape mirrors the verified working query from T6a.2 (qualified
    database.table, array_join for dtc_codes) but aggregates rather than
    returning per-VIN rows, keeping the verification script read-only and
    result-set-small.

    Note: `outcome` is VARCHAR in service_records; no cast required.
    """
    print("\n[brakes] source: adp_%s_service_records.service_records" % stage)
    sql = f"""
        SELECT COUNT(*)                AS total_rows,
               COUNT(DISTINCT vin)     AS vins,
               SUM(CASE WHEN outcome IN ('parts_pending', 'follow_up_required')
                        THEN 1 ELSE 0 END) AS open_jobs
        FROM adp_{stage}_service_records.service_records
        WHERE service_type = 'brake_service'
    """
    state, rows, reason = _run_query(athena, sql, workgroup)
    if state != "SUCCEEDED":
        print(f"    NOT BUILDABLE — query {state}: {reason}")
        return False
    _print_rows(rows)

    if len(rows) < 2:
        print("    NOT BUILDABLE — no aggregate row returned")
        return False

    header, values = rows[0], rows[1]
    data = dict(zip(header, values))
    total_rows = int(data.get("total_rows") or 0)
    vins = int(data.get("vins") or 0)
    open_jobs = int(data.get("open_jobs") or 0)

    if total_rows == 0:
        print("    NOT BUILDABLE — zero brake service rows")
        return False

    # The signal the agent depends on (spec § D3b): open, unresolved brake jobs
    # with outcome ∈ {parts_pending, follow_up_required}.  If this is absent from
    # live data, the brakes component's most valuable finding is undeliverable.
    if open_jobs == 0:
        print(
            f"    NOT BUILDABLE — {total_rows:,} brake rows / {vins:,} VINs found, "
            "but NO open jobs (outcome=parts_pending or follow_up_required) present. "
            "The open-job signal the agent depends on (spec § D3b) is absent."
        )
        return False

    print(
        f"    BUILDABLE — {total_rows:,} brake rows, {vins:,} VINs, "
        f"{open_jobs:,} open jobs (outcome=parts_pending|follow_up_required present)"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="staging", choices=["staging", "prod"])
    parser.add_argument(
        "--component",
        default="all",
        choices=["all", "tires", "battery", "brakes"],
        help="Limit the check to one component.",
    )
    args = parser.parse_args()

    workgroup = f"cvx-{args.stage}-analytics"
    print(f"stage={args.stage}  adp_region={ADP_REGION}  workgroup={workgroup}")

    athena = boto3.client("athena", region_name=ADP_REGION)

    results: dict[str, bool] = {}
    if args.component in ("all", "battery"):
        results["battery"] = check_battery(athena, args.stage, workgroup)
    if args.component in ("all", "tires"):
        results["tires"] = check_tires(athena, args.stage, workgroup)
    if args.component in ("all", "brakes"):
        results["brakes"] = check_brakes(athena, args.stage, workgroup)

    print("\n=== summary ===")
    for component, ok in sorted(results.items()):
        print(f"  {component:8s} {'BUILDABLE' if ok else 'NOT BUILDABLE'}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
