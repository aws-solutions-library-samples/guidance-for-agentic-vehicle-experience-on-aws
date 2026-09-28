"""Live verification for the diagnostics source's VIN attribution: real DynamoDB, read-only.

Spec: `.kiro/specs/2026-09-25-avx-own-vehicle-findings/tasks.md` § Fix Group 7 (F7.1).
Issue: `issues/2026-09-28-diagnostics-vin-taken-from-dtc-rows/`.

Per `~/.kiro/steering/spec-workflow.md` § "Live-Verification Gate": every unit test of
`CmsDtcSource` feeds it a fake whose rows carry one consistent `vin`. The staging table does
not look like that. Its `vin` attribute is stale, empty or absent on most rows, and that is
the defect F7.1 fixes, which no stub could show. This test runs the real `fetch_groups`
against the real tables and checks the property that matters: every group's VIN is the
registry VIN of exactly one registered vehicle.

## Isolation

Read-only. It makes one scan of dtc-history, one registry `GetItem` and one `vin-index`
Query per vehicle, and one dtc-history partition Query per kept VIN. It writes nothing and
prints no VIN.

## Running it

    LIVE_TESTS=1 AWS_PROFILE=default AWS_REGION=us-west-2 \\
      CMS_DTC_HISTORY_TABLE=cms-staging-storage-dtc-history \\
      CMS_VEHICLES_TABLE=cms-staging-storage-vehicles \\
      .venv/bin/python3 -m pytest agents/health/tests/test_cms_dtc_source_live.py -v

This runs under operator credentials, not the Tier 2 role. Passing it says the read path and
the attribution are right. It does not say the deployed Lambda is authorised: the deployed
run's `tier2_run` log line and the Findings it writes are that check (T4.5).
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

pytestmark = pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1", reason="LIVE_TESTS=1 not set")


@pytest.fixture(scope="module")
def live():
    import boto3

    from agents.health.cms_dtc_source import CmsDtcSource, DiagnosticsReadError

    region = os.environ.get("AWS_REGION", "us-west-2")
    dtc_table = os.environ.get("CMS_DTC_HISTORY_TABLE", "cms-staging-storage-dtc-history")
    vehicles_table = os.environ.get("CMS_VEHICLES_TABLE", "cms-staging-storage-vehicles")
    ddb = boto3.client("dynamodb", region_name=region)
    source = CmsDtcSource(dtc_table=dtc_table, vehicles_table=vehicles_table, ddb_client=ddb,
                          ddb_resource=boto3.resource("dynamodb", region_name=region))
    try:
        groups, failed = source.fetch_groups(), set()
    except DiagnosticsReadError as exc:
        groups, failed = exc.groups, exc.failed_vins
    return {"ddb": ddb, "vehicles_table": vehicles_table, "groups": groups, "failed": failed}


def _holders(ddb, table, vin):
    out, kw = [], {"TableName": table, "IndexName": "vin-index", "KeyConditionExpression": "#v = :v",
                   "ExpressionAttributeNames": {"#v": "vin"}, "ExpressionAttributeValues": {":v": {"S": vin}}}
    while True:
        r = ddb.query(**kw)
        out += [i["vehicleId"]["S"] for i in r.get("Items", [])]
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def test_the_live_read_completes(live):
    assert live["failed"] == set(), f"{len(live['failed'])} vehicle read(s) failed"
    assert live["groups"], "no groups at all: the scan or the registry read is not returning data"


def test_every_group_vin_is_the_registry_vin_of_exactly_one_vehicle(live):
    ddb, table = live["ddb"], live["vehicles_table"]
    bad = 0
    for vin in sorted({g.vin for g in live["groups"]}):
        holders = _holders(ddb, table, vin)
        if len(holders) != 1:
            bad += 1
            continue
        item = ddb.get_item(TableName=table, Key={"vehicleId": {"S": holders[0]}},
                            ProjectionExpression="#v", ExpressionAttributeNames={"#v": "vin"}).get("Item", {})
        if item.get("vin", {}).get("S", "").strip() != vin:
            bad += 1
    assert bad == 0, f"{bad} group VIN(s) are not the registry VIN of exactly one vehicle"
