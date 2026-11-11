#!/usr/bin/env python3
"""Live verification for FI stage E task 2.4 (spec 2026-10-06-fi-rebalancing-stage-e): read-only.

Run: LIVE_TESTS=1 python3 scripts/live-verify-fi-rebalancing-locations.py --stage staging \
         --since 2026-10-10T15:52:33Z [--athena-workgroup primary]

Checks (each PASS / FAIL / BLOCKED; exit 1 on any FAIL, 2 when only BLOCKED remain):
  1. CMS: every vehicle in the four managed demo fleets has a locationId (4,250 expected).
  2. CMS: 77 fleet-locations items.
  3. CMS: one seed vehicle-moves record per location; their vins are exactly the managed VINs,
     each once, and each VIN's seed location is its vehicle row's locationId.
  4. ADP: cms_ingest.vehicles has a location_id for every managed VIN, equal to CMS's for a
     50-vehicle sample (deterministic: sha256 order).
  5. ADP: cms_ingest.fleet_locations has the 77 rows (BLOCKED when the reader holds no Lake
     Formation SELECT on the MERGE-created table).
  6. Stream readers since --since: zero Errors on the ADP transformer, the CS availability
     listener and the fleet-membership publisher; the transformer DLQ did not grow; every
     event source mapping on the three CMS streams is Enabled.
No writes: only DynamoDB Scan, Athena queries (results to the workgroup's own output),
CloudWatch GetMetricStatistics, SQS GetQueueAttributes, Lambda List/Get.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import sys
import time

FLEETS = ("demo-rental-carshare", "demo-lastmile-delivery", "demo-field-service", "demo-city-public")
EXPECTED_VEHICLES, EXPECTED_LOCATIONS = 4250, 77
CMS_REGION, ADP_REGION = "us-west-2", "us-east-1"
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
READERS = ("adp-staging-foundation-cms-ingest-transformer", "cms-staging-subscriptions-availability-listener")
READER_PREFIXES = ("cms-staging-storage-FleetMembershipPublisher",)

results: list[tuple[str, str, str]] = []


def record(name, status, detail):
    results.append((name, status, detail))
    print(f"{status:8} {name}: {detail}", flush=True)


def scan(ddb, table, projection, names=None):
    kw = {"TableName": table, "ProjectionExpression": projection}
    if names:
        kw["ExpressionAttributeNames"] = names
    items, page = [], ddb.get_paginator("scan")
    for p in page.paginate(**kw):
        items += p["Items"]
    return items


def s(item, key):
    v = item.get(key)
    return v.get("S") if v else None


def athena_rows(ath, sql, wg):
    qid = ath.start_query_execution(QueryString=sql, WorkGroup=wg)["QueryExecutionId"]
    for _ in range(300):
        st = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    if st["State"] != "SUCCEEDED":
        raise RuntimeError(f"{st['State']}: {st.get('StateChangeReason', '')}")
    rows, kw = [], {"QueryExecutionId": qid}
    while True:
        page = ath.get_query_results(**kw)
        rows += [[c.get("VarCharValue") for c in r["Data"]] for r in page["ResultSet"]["Rows"]]
        if not page.get("NextToken"):
            return rows[1:]
        kw["NextToken"] = page["NextToken"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--since", required=True, help="ISO UTC time the seed started")
    ap.add_argument("--athena-workgroup", default="primary")
    a = ap.parse_args(argv)
    if a.stage != "staging":
        print("REFUSED: staging only", file=sys.stderr)
        return 2
    if os.environ.get("LIVE_TESTS") != "1":
        print("REFUSED: set LIVE_TESTS=1", file=sys.stderr)
        return 2
    since = dt.datetime.fromisoformat(a.since.replace("Z", "+00:00"))
    import boto3

    ddb = boto3.client("dynamodb", region_name=CMS_REGION)
    pre = f"cms-{a.stage}-storage-"
    vehicles = [v for v in scan(ddb, pre + "vehicles", "vin, fleetId, locationId")
                if s(v, "fleetId") in FLEETS]
    loc_of = {s(v, "vin"): s(v, "locationId") for v in vehicles}
    missing = [vin for vin, loc in loc_of.items() if not loc]
    ok = len(vehicles) == EXPECTED_VEHICLES == len(loc_of) and not missing
    record("1 CMS vehicles have locationId", "PASS" if ok else "FAIL",
           f"{len(vehicles)} managed vehicles, {len(loc_of)} VINs, {len(missing)} without locationId")

    locs = scan(ddb, pre + "fleet-locations", "fleetId, locationId")
    loc_ids = {s(x, "locationId") for x in locs}
    record("2 CMS fleet-locations", "PASS" if len(locs) == len(loc_ids) == EXPECTED_LOCATIONS else "FAIL",
           f"{len(locs)} items, {len(loc_ids)} distinct ids")

    moves = scan(ddb, pre + "vehicle-moves", "moveId, toLocationId, vins, decidedByKind")
    seeds = [m for m in moves if s(m, "decidedByKind") == "seed"]
    seed_vins = [x["S"] for m in seeds for x in m.get("vins", {}).get("L", [])]
    seed_loc = {x["S"]: s(m, "toLocationId") for m in seeds for x in m.get("vins", {}).get("L", [])}
    wrong = [v for v in loc_of if seed_loc.get(v) != loc_of[v]]
    ok = (len(seeds) == EXPECTED_LOCATIONS and {s(m, "toLocationId") for m in seeds} == loc_ids
          and len(seed_vins) == len(set(seed_vins)) == len(loc_of) and set(seed_vins) == set(loc_of)
          and not wrong)
    record("3 CMS seed move records", "PASS" if ok else "FAIL",
           f"{len(seeds)} seed records ({len(moves)} records in all), {len(seed_vins)} VINs "
           f"({len(set(seed_vins))} distinct), {len(wrong)} VINs whose seed location != locationId")

    ath = boto3.client("athena", region_name=ADP_REGION)
    db = f"adp_{a.stage}_cms_ingest"
    fleets_sql = ", ".join(f"'{f}'" for f in FLEETS)
    try:
        rows = athena_rows(ath, f"SELECT vin, location_id FROM {db}.vehicles WHERE fleet_id IN ({fleets_sql})",
                           a.athena_workgroup)
        adp = {r[0]: r[1] for r in rows if r[0] and VIN_RE.match(r[0])}
        sample = sorted(loc_of, key=lambda v: hashlib.sha256(v.encode()).hexdigest())[:50]
        mism = [v for v in sample if adp.get(v) != loc_of[v]]
        no_loc = [v for v in loc_of if not adp.get(v)]
        ok = len(adp) == len(loc_of) and not no_loc and not mism
        record("4 ADP vehicles.location_id", "PASS" if ok else "FAIL",
               f"{len(rows)} rows, {len(adp)} VINs, {len(no_loc)} managed VINs without location_id, "
               f"{len(mism)} of 50 sampled differ from CMS")
    except Exception as e:  # noqa: BLE001 - reported, not swallowed: the item FAILs
        record("4 ADP vehicles.location_id", "FAIL", f"query failed: {e}"[:300])
    try:
        rows = athena_rows(ath, f"SELECT count(*), count(DISTINCT location_id) FROM {db}.fleet_locations",
                           a.athena_workgroup)
        n, d = int(rows[0][0]), int(rows[0][1])
        record("5 ADP fleet_locations", "PASS" if n == d == EXPECTED_LOCATIONS else "FAIL",
               f"{n} rows, {d} distinct ids")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        status = "BLOCKED" if "privilege" in msg or "AccessDenied" in msg or "not authorized" in msg else "FAIL"
        record("5 ADP fleet_locations", status, msg[:300])

    lam = boto3.client("lambda", region_name=CMS_REGION)
    names = list(READERS)
    for p in lam.get_paginator("list_functions").paginate():
        names += [f["FunctionName"] for f in p["Functions"] if f["FunctionName"].startswith(READER_PREFIXES)]
    cw = boto3.client("cloudwatch", region_name=CMS_REGION)
    now = dt.datetime.now(dt.timezone.utc)
    errs = {}
    for n in names:
        pts = cw.get_metric_statistics(Namespace="AWS/Lambda", MetricName="Errors",
                                       Dimensions=[{"Name": "FunctionName", "Value": n}],
                                       StartTime=since, EndTime=now, Period=300, Statistics=["Sum"])["Datapoints"]
        errs[n] = int(sum(p["Sum"] for p in pts))
    maps = []
    for t in ("vehicles", "fleet-locations", "vehicle-moves"):
        arn = ddb.describe_table(TableName=pre + t)["Table"]["LatestStreamArn"]
        maps += [(t, m["FunctionArn"].split(":")[-1], m["State"])
                 for p in lam.get_paginator("list_event_source_mappings").paginate(EventSourceArn=arn)
                 for m in p["EventSourceMappings"]]
    bad_maps = [m for m in maps if m[2] != "Enabled"]
    ok = len(names) >= 3 and not any(errs.values()) and not bad_maps and len(maps) >= 4
    record("6 stream readers", "PASS" if ok else "FAIL",
           f"Errors since {a.since}: {errs}; mappings: {sorted(maps)}")
    sqs_arns = set()
    for p in lam.get_paginator("list_event_source_mappings").paginate(FunctionName=READERS[0]):
        for m in p["EventSourceMappings"]:
            d = (m.get("DestinationConfig") or {}).get("OnFailure", {}).get("Destination")
            if d:
                sqs_arns.add(d)
    if sqs_arns:
        sqs = boto3.client("sqs", region_name=CMS_REGION)
        depth = {}
        for arn in sorted(sqs_arns):
            name = arn.split(":")[-1]
            url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
            at = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
            depth[name] = int(at["Attributes"]["ApproximateNumberOfMessages"])
        record("6b transformer on-failure queues", "PASS" if not any(depth.values()) else "FAIL",
               f"depth now: {depth} (compare with the pre-seed reading in the task note)")
    else:
        record("6b transformer on-failure queues", "FAIL", "no OnFailure destination found on the mappings")

    fails = [r for r in results if r[1] == "FAIL"]
    blocked = [r for r in results if r[1] == "BLOCKED"]
    print(f"\nSUMMARY: {len(results) - len(fails) - len(blocked)} PASS, {len(fails)} FAIL, {len(blocked)} BLOCKED")
    return 1 if fails else (2 if blocked else 0)


if __name__ == "__main__":
    sys.exit(main())
