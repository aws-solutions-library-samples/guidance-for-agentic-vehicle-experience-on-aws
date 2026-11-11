#!/usr/bin/env python3
"""One-off repair for FI stage E task 2.4 (spec 2026-10-06-fi-rebalancing-stage-e), staging only.

The 2026-10-10 seed wrote 9 of its 77 seed records in cms-staging-storage-vehicle-moves with 101
vehicleIds (VEH-...) in ``vins`` where VINs belong (CMS seed_fleet_locations.py defect, fixed on CMS
wip/fi-rebal-e-seed-vins). ADP's transformer refused those records. This script rewrites ``vins`` on
exactly those records, in place, each vehicleId replaced by that vehicle's own ``vin`` from its
cms-staging-storage-vehicles row. The DynamoDB MODIFY then flows to ADP.

Run:  python3 scripts/rewrite-fi-rebalancing-seed-vins.py --stage staging --account-id <id>         # dry run
      python3 scripts/rewrite-fi-rebalancing-seed-vins.py --stage staging --account-id <id> --apply

Safety:
  - Dry run is the default and makes read calls only (a botocore guard admits exactly
    GetCallerIdentity, DescribeTable, Scan and GetItem). Both modes need --account-id matching STS.
  - Both modes refuse (exit 2) unless the plan is exactly --expect-records records holding exactly
    --expect-vins non-VIN entries (defaults 9 and 101), and every check below holds.
  - Each non-VIN entry must be the vehicleId of a managed-fleet vehicle whose ``vin`` is a 17-character
    ISO 3779 VIN with WMI MRD, whose ``locationId`` is the record's ``toLocationId`` and whose fleet is
    the record's fleet. The rewritten list must be duplicate-free, its VINs in no other seed record,
    and its length unchanged (so vehiclesApproved/vehiclesRecommended stay right).
  - --apply: the guard admits only reads plus UpdateItem on the vehicle-moves table. One UpdateItem
    per record, conditional on the record still holding the exact old ``vins`` list and
    decidedByKind = seed; each is read back (consistent read) before the next.
  - Rollback: the on-demand backup fi-rebal-e-t24-pre-seed-vehicle-moves-20261010 (taken before
    the seed). DynamoDB restores only into a new table.
Exit codes: 0 done; 1 a write or read-back failed (stops at once); 2 refused before writing.
"""
from __future__ import annotations

import argparse
import re
import sys

CMS_REGION = "us-west-2"
FLEETS = ("demo-rental-carshare", "demo-lastmile-delivery", "demo-field-service", "demo-city-public")
VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
ADMITTED_WMIS = ("MRD",)  # ADP stream_transformer/allowlist.py ALLOWED_WMIS, 2026-10-10
# Exact operations this script calls; anything else is refused (an exact list, not prefixes:
# security review cycle 1 W found "Batch"/"Update" prefixes would admit BatchWriteItem/UpdateTable).
READ_OPS = frozenset({"GetCallerIdentity", "DescribeTable", "Scan", "GetItem"})


def _guard(allow_update_table: str | None):
    """botocore before-parameter-build hook: these reads only, plus UpdateItem on one table."""
    def _check(params, model, **_kw):
        op = model.name
        if op in READ_OPS:
            return
        if allow_update_table and op == "UpdateItem" and params.get("TableName") == allow_update_table:
            return
        raise RuntimeError(f"REFUSED by guard: {op} on {params.get('TableName')!r}")
    return _check


def _s(item, key):
    v = item.get(key)
    return v.get("S") if isinstance(v, dict) else None


def _scan(ddb, table, projection, names=None):
    kw = {"TableName": table, "ProjectionExpression": projection}
    if names:
        kw["ExpressionAttributeNames"] = names
    out = []
    for page in ddb.get_paginator("scan").paginate(**kw):
        out += page["Items"]
    return out


def plan_rewrites(vehicles: list[dict], moves: list[dict]) -> tuple[list[dict], list[str]]:
    """Pure: (plan, errors). vehicles/moves are low-level DynamoDB items.

    plan entries: {fleetId, moveId, toLocationId, old: [str], new: [str], swaps: [(vehicleId, vin)]}
    """
    errors: list[str] = []
    by_vid = {}
    for v in vehicles:
        if _s(v, "fleetId") in FLEETS:
            by_vid[_s(v, "vehicleId")] = v
    seeds = [m for m in moves if _s(m, "decidedByKind") == "seed"]
    plan = []
    for m in seeds:
        old = [x.get("S") for x in m.get("vins", {}).get("L", [])]
        if any(not isinstance(x, str) for x in old):
            errors.append(f"{_s(m, 'moveId')}: vins holds a non-string entry")
            continue
        bad = [x for x in old if not VIN_RE.fullmatch(x)]
        if not bad:
            continue
        loc, fleet = _s(m, "toLocationId"), _s(m, "fleetId")
        swaps = []
        for vid in bad:
            v = by_vid.get(vid)
            vin = _s(v, "vin") if v else None
            if v is None:
                errors.append(f"{_s(m, 'moveId')}: {vid!r} is no managed vehicle's vehicleId")
            elif not (vin and VIN_RE.fullmatch(vin) and vin[:3] in ADMITTED_WMIS):
                errors.append(f"{_s(m, 'moveId')}: {vid} has vin {vin!r}, not an admitted VIN")
            elif _s(v, "locationId") != loc or _s(v, "fleetId") != fleet:
                errors.append(f"{_s(m, 'moveId')}: {vid} is at {_s(v, 'locationId')!r} in "
                              f"{_s(v, 'fleetId')!r}, record is {loc!r} in {fleet!r}")
            else:
                swaps.append((vid, vin))
        new = sorted(dict(swaps).get(x, x) for x in old)
        if len(set(new)) != len(new):
            errors.append(f"{_s(m, 'moveId')}: rewritten vins would hold a duplicate")
        plan.append({"fleetId": fleet, "moveId": _s(m, "moveId"), "toLocationId": loc,
                     "old": old, "new": new, "swaps": swaps})
    # Each new VIN must be in no other seed record.
    elsewhere: dict[str, str] = {}
    planned = {p["moveId"] for p in plan}
    for m in seeds:
        if _s(m, "moveId") in planned:
            continue
        for x in m.get("vins", {}).get("L", []):
            elsewhere[x.get("S")] = _s(m, "moveId")
    for p in plan:
        for _vid, vin in p["swaps"]:
            if vin in elsewhere:
                errors.append(f"{p['moveId']}: {vin} is already in seed record {elsewhere[vin]}")
    # ...and in no two planned records (security review cycle 1, S1).
    seen: dict[str, str] = {}
    for p in plan:
        for _vid, vin in p["swaps"]:
            if vin in seen and seen[vin] != p["moveId"]:
                errors.append(f"{p['moveId']}: {vin} is also planned for {seen[vin]}")
            seen.setdefault(vin, p["moveId"])
    return plan, errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--account-id")
    ap.add_argument("--expect-records", type=int, default=9)
    ap.add_argument("--expect-vins", type=int, default=101)
    a = ap.parse_args(argv)
    if a.stage != "staging":
        print("REFUSED: staging only", file=sys.stderr)
        return 2
    if not a.account_id:
        print("REFUSED: --account-id is required (dry run too)", file=sys.stderr)
        return 2
    import boto3

    moves_table = f"cms-{a.stage}-storage-vehicle-moves"
    guard = _guard(moves_table if a.apply else None)
    sts = boto3.client("sts", region_name=CMS_REGION)
    sts.meta.events.register("before-parameter-build.sts", guard)
    ddb = boto3.client("dynamodb", region_name=CMS_REGION)
    ddb.meta.events.register("before-parameter-build.dynamodb", guard)

    account = sts.get_caller_identity()["Account"]
    if account != a.account_id:
        print(f"REFUSED: --account-id {a.account_id} but STS says {account}", file=sys.stderr)
        return 2
    keys = [k["AttributeName"] for k in ddb.describe_table(TableName=moves_table)["Table"]["KeySchema"]]
    if keys != ["fleetId", "moveId"]:
        print(f"REFUSED: {moves_table} key schema is {keys}, expected [fleetId, moveId]", file=sys.stderr)
        return 2

    vehicles = _scan(ddb, f"cms-{a.stage}-storage-vehicles", "vehicleId, vin, fleetId, locationId")
    moves = _scan(ddb, moves_table, "fleetId, moveId, toLocationId, vins, decidedByKind")
    plan, errors = plan_rewrites(vehicles, moves)

    mode = "APPLY" if a.apply else "DRY RUN"
    print(f"=== rewrite seed vins ({mode}, stage={a.stage}, account={account}) ===")
    n_vins = 0
    for p in plan:
        print(f"\n{p['fleetId']}  {p['moveId']}  {p['toLocationId']}  "
              f"vins={len(p['old'])}  to rewrite={len(p['swaps'])}")
        for vid, vin in p["swaps"]:
            print(f"    {vid} -> {vin}")
        n_vins += len(p["swaps"])
    print(f"\nRecords to rewrite: {len(plan)}   vehicleIds to replace: {n_vins}")
    if errors:
        print(f"REFUSED: {len(errors)} check(s) failed; nothing written:\n  " + "\n  ".join(errors[:20]),
              file=sys.stderr)
        return 2
    if (len(plan), n_vins) != (a.expect_records, a.expect_vins):
        print(f"REFUSED: expected {a.expect_records} records / {a.expect_vins} vehicleIds, "
              f"found {len(plan)} / {n_vins}; nothing written", file=sys.stderr)
        return 2
    if not a.apply:
        print("Dry run only. Re-run with --apply --account-id <id> to write.")
        return 0

    for i, p in enumerate(plan, 1):
        key = {"fleetId": {"S": p["fleetId"]}, "moveId": {"S": p["moveId"]}}
        try:
            ddb.update_item(
                TableName=moves_table, Key=key,
                UpdateExpression="SET vins = :new",
                ConditionExpression="vins = :old AND decidedByKind = :seed",
                ExpressionAttributeValues={
                    ":new": {"L": [{"S": x} for x in p["new"]]},
                    ":old": {"L": [{"S": x} for x in p["old"]]},
                    ":seed": {"S": "seed"},
                },
            )
        except Exception as e:  # noqa: BLE001 - reported and stops the run
            print(f"FAILED at record {i}/{len(plan)} {p['moveId']}: {e}", file=sys.stderr)
            return 1
        got = ddb.get_item(TableName=moves_table, Key=key, ConsistentRead=True,
                           ProjectionExpression="vins").get("Item", {})
        got_vins = [x.get("S") for x in got.get("vins", {}).get("L", [])]
        if got_vins != p["new"]:
            print(f"FAILED read-back at record {i} {p['moveId']}: vins now {got_vins}", file=sys.stderr)
            return 1
        print(f"updated {i}/{len(plan)} {p['moveId']} ({len(p['swaps'])} replaced), read back OK")

    left, errs = plan_rewrites(_scan(ddb, f"cms-{a.stage}-storage-vehicles", "vehicleId, vin, fleetId, locationId"),
                               _scan(ddb, moves_table, "fleetId, moveId, toLocationId, vins, decidedByKind"))
    print(f"After: records still holding non-VIN entries: {len(left)}; check errors: {len(errs)}")
    return 0 if not left and not errs else 1


if __name__ == "__main__":
    sys.exit(main())
