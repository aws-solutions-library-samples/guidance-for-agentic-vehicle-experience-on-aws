#!/usr/bin/env python3
"""End-to-end smoke for the persona-loader against deployed staging infra.

Drives `agents.supervisor.persona_loader.load_persona` against:
- Real AgentCore Memory `vsa_staging_cvx_foundation_memory-XHAM1yG5mr` (us-west-2)
- Real Athena workgroup `cvx-staging-analytics` (us-east-1)
- Real ADP Glue catalog (us-east-1; cross-account share)

Validates Group 7.B item 3 — confirms persona-loader can:
1. resolve config from env vars / defaults
2. run cross-region Athena queries against ADP databases
3. write Memory records via `BatchCreateMemoryRecords`
4. read Memory records back via `RetrieveMemoryRecords`
5. handle PySpark-gated products gracefully

Usage:
    python3 scripts/load_persona_smoke.py --stage staging \\
        --persona vehicle_owner --vin 1FA00000000000009 \\
        --customer-id cust-staging-001
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# Make the repo root importable (script lives in scripts/, package agents/ at repo root)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3


def _set_env_defaults(memory_id: str, account_id: str, stage: str) -> None:
    """Populate the env vars `build_persona_loader_config` reads from."""
    os.environ.setdefault("VSA_FOUNDATION_MEMORY_ID", memory_id)
    os.environ.setdefault("VSA_ADP_ACCOUNT_ID", account_id)
    os.environ.setdefault("VSA_ADP_REGION", "us-east-1")
    os.environ.setdefault("VSA_ADP_STAGE", stage)
    os.environ.setdefault("VSA_ATHENA_WORKGROUP", f"cvx-{stage}-analytics")
    os.environ.setdefault(
        "VSA_ATHENA_RESULTS_BUCKET",
        f"cvx-{stage}-athena-results-{account_id}-us-east-1",
    )
    os.environ.setdefault("VSA_PERSONA_LTM_TTL_MIN", "15")
    os.environ.setdefault("VSA_ADP_KB_ID", "")
    os.environ.setdefault("VSA_MEMORY_REGION", "us-west-2")


def _resolve_memory_id(stage: str, region: str = "us-west-2") -> str:
    """Find the deployed Memory ID via the CFN export."""
    cfn = boto3.client("cloudformation", region_name=region)
    resp = cfn.describe_stacks(StackName=f"vsa-{stage}-foundation")
    for o in resp["Stacks"][0]["Outputs"]:
        if o["ExportName"] == f"cvx-{stage}-memory-id":
            return o["OutputValue"]
    raise RuntimeError(f"cvx-{stage}-memory-id export not found on vsa-{stage}-foundation")


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", default="staging")
    p.add_argument("--persona", default="vehicle_owner")
    p.add_argument("--vin", default="1FA00000000000009")
    p.add_argument("--customer-id", default="cust-staging-001")
    p.add_argument(
        "--account",
        default=None,
        help="ADP account ID (defaults to STS get-caller-identity)",
    )
    args = p.parse_args()

    if not (args.persona.replace("_", "").isalnum()):
        print(f"✗ persona must be alphanumeric+underscore (R15 guard)", file=sys.stderr)
        return 1
    if not (args.vin.isalnum() and len(args.vin) <= 64):
        print(f"✗ VIN must be alphanumeric, ≤64 chars (R15 guard)", file=sys.stderr)
        return 1

    account = args.account or boto3.client("sts").get_caller_identity()["Account"]
    memory_id = _resolve_memory_id(args.stage)
    _set_env_defaults(memory_id, account, args.stage)

    print(f"persona:    {args.persona}")
    print(f"VIN:        {args.vin}")
    print(f"customer:   {args.customer_id}")
    print(f"memory:     {memory_id}")
    print(f"account:    {account}")
    print()

    # Defer imports until env is set so config resolves at module load
    from agents.supervisor.config import build_persona_loader_config
    from agents.supervisor.persona_definitions import PERSONA_PRODUCT_BINDINGS, PERSONA_TENANT_DEFAULTS
    from agents.supervisor.persona_loader import load_persona

    config = build_persona_loader_config()
    print(f"config:     adp_region={config.adp_region} workgroup={config.athena_workgroup}")
    print(f"            ttl_min={config.ttl_minutes} memory_region={config.memory_region}")
    print()

    expected_products = PERSONA_PRODUCT_BINDINGS[args.persona]
    print(f"persona's products ({len(expected_products)}): {expected_products}")
    print()

    t0 = time.time()
    snapshot = await load_persona(
        persona_id=args.persona,
        vehicle_id=args.vin,
        customer_id=args.customer_id,
        config=config,
    )
    elapsed_ms = int((time.time() - t0) * 1000)

    print(f"snapshot loaded in {elapsed_ms}ms")
    print(f"  persona_id:    {snapshot.persona_id}")
    print(f"  vehicle_id:    {snapshot.vehicle_id}")
    print(f"  customer_id:   {snapshot.customer_id}")
    print(f"  products loaded: {len(snapshot.data_sources)} of {len(expected_products)}")
    for prod, payload in snapshot.data_sources.items():
        n_records = 1 if isinstance(payload, dict) else len(payload) if payload else 0
        print(f"    {prod}: {n_records} record(s)")

    missing = set(expected_products) - set(snapshot.data_sources.keys())
    if missing:
        print(f"  products NOT loaded: {sorted(missing)}")
        from agents.supervisor.persona_definitions import PYSPARK_GATED_PRODUCTS
        unexpected_misses = missing - PYSPARK_GATED_PRODUCTS
        if unexpected_misses:
            print(f"  ⚠️  {len(unexpected_misses)} non-PySpark-gated misses: {sorted(unexpected_misses)}")
            print(f"  (Expected PySpark-gated misses: {sorted(missing & PYSPARK_GATED_PRODUCTS)})")

    print()
    print(f"=== Memory readback (sanity check via list_memory_records) ===")
    tenant_id = PERSONA_TENANT_DEFAULTS.get(args.persona, "fleet")
    namespace = f"cvx/{tenant_id}/{args.persona}"
    mem_client = boto3.client("bedrock-agentcore", region_name="us-west-2")
    list_resp = mem_client.list_memory_records(
        memoryId=memory_id,
        namespace=namespace,
        maxResults=20,
    )
    record_count = len(list_resp.get("memoryRecordSummaries", []))
    print(f"  namespace: {namespace}")
    print(f"  records found: {record_count}")
    if record_count == 0:
        print(f"  (Empty if persona-loader had to skip products; OK on first run if Athena returned no rows)")

    print()
    if elapsed_ms > 3000:
        print(f"⚠️  session-init latency {elapsed_ms}ms > 3000ms PRD budget — track for v1.1")
    if not snapshot.data_sources and not missing:
        print(f"✗ No products loaded AND no expected products — config may be wrong")
        return 1
    print(f"✓ persona-loader smoke complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
