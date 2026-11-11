#!/usr/bin/env python3
"""Live-verification of the AVX read surface against a DEPLOYED DynamoDB table.

This closes the gate that `~/.kiro/steering/spec-workflow.md` § "Live-Verification
Gate" describes and that Fix Groups 10-14 deferred: **every test in those groups stubs
the DynamoDB client, which is the exact condition that let seven defects pass both
review gates.** A stub cannot fail the way a service fails — it accepts any
`IndexName`, any `KeyConditionExpression`, and returns whatever the fixture supplies.

Each check below reproduces one defect from
`issues/2026-09-19-avx-findings-read-surface-nonfunctional/` against the real service,
using the REAL production code paths (imported, not reimplemented):

  F6  write path  — writer.py Queries `by_finding_key`; it named `finding_key-index`,
                    which does not exist, so every write would have raised
                    ValidationException.
  F3  projection  — the five scope GSIs are keyed on TOP-LEVEL attributes the writer
                    never wrote, so they were permanently empty.
  F1  by_vin      — read helpers passed `vin_index` as the KEY ATTRIBUTE name.
  F2  by_owner    — `_list_findings_by_owner` named a non-existent index `by_owner_id`.

Writes one synthetic Finding, reads it back four ways, then deletes it.
Opt-in by design so it never runs in a default CI pass.

USAGE
  LIVE_TESTS=1 AVX_FINDINGS_TABLE=vsa-staging-avx-findings AWS_REGION=us-west-2 \
    python3 scripts/live-verify-avx-read-surface.py

EXIT 0 all checks passed · 1 a check failed · 2 refused to run
"""
from __future__ import annotations

import os
import sys
import uuid
import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

if os.environ.get("LIVE_TESTS") != "1":
    print("refusing to run: set LIVE_TESTS=1 (this writes to a real DynamoDB table)")
    sys.exit(2)

TABLE = os.environ.get("AVX_FINDINGS_TABLE")
if not TABLE:
    print("refusing to run: AVX_FINDINGS_TABLE is required — this must name a real table")
    sys.exit(2)
REGION = os.environ.get("AWS_REGION", "us-west-2")

# Import the REAL modules. Reimplementing their queries here would test this script
# rather than the code, which is the substitution that caused the original defects.
sys.path.insert(0, str(REPO / "agents" / "tier2"))
sys.path.insert(0, str(REPO / "lambdas" / "api-avx-findings"))
os.environ.setdefault("AVX_FINDINGS_TABLE", TABLE)

import boto3  # noqa: E402

import writer as W  # noqa: E402
import handler as H  # noqa: E402

ddb_client = boto3.client("dynamodb", region_name=REGION)
ddb_res = boto3.resource("dynamodb", region_name=REGION)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))


# ── Build a synthetic Finding from the repo's own valid fixture ───────────────
fixture = json.loads(
    (REPO / "agents/tier2/schemas/fixtures/finding_health_tires_valid.json").read_text()
)
run_id = uuid.uuid4().hex[:12]
vin = f"LIVEVERIFY{run_id.upper()[:7]}"
owner_id = f"usr-live-{run_id}"

finding = dict(fixture)
finding["finding_id"] = f"AVX-LIVE-{run_id}"
finding["scope"] = {"vin": vin}
finding["finding_key"] = W.compute_finding_key(
    agent_id=finding["agent_id"], scope_field="vin", scope_value=vin,
    finding_kind=finding["finding_kind"],
)

owner_finding = dict(fixture)
owner_finding["finding_id"] = f"AVX-LIVE-OWNER-{run_id}"
owner_finding["scope"] = {"owner_id": owner_id}
owner_finding["finding_key"] = W.compute_finding_key(
    agent_id=fixture["agent_id"], scope_field="owner_id", scope_value=owner_id,
    finding_kind=fixture["finding_kind"],
)

written: list[str] = []
try:
    # ── F6: the real write path, which Queries by_finding_key ─────────────────
    try:
        # Record each id immediately after ITS OWN put. Appending both ids after
        # both puts (the earlier shape) meant a failure on the second write left the
        # first row committed with `written` still empty, so the `finally` block
        # deleted nothing and the run orphaned a synthetic row in a real table.
        W._put_item(table_name=TABLE, dynamodb_client=ddb_client, finding=finding)
        written.append(finding["finding_id"])
        W._put_item(table_name=TABLE, dynamodb_client=ddb_client, finding=owner_finding)
        written.append(owner_finding["finding_id"])
        check("F6 write path — real PutItem via writer._put_item", True)
    except Exception as exc:
        check("F6 write path — real PutItem via writer._put_item", False, repr(exc))

    # The open-condition lookup is the query that named a non-existent index.
    try:
        live = W._query_live_row(
            finding_key=finding["finding_key"], table_name=TABLE,
            dynamodb_client=ddb_client, now_str="2000-01-01T00:00:00Z",
        )
        check("F6 by_finding_key — real Query resolves (index name was wrong)",
              True, f"live row found: {live is not None}")
    except Exception as exc:
        check("F6 by_finding_key — real Query resolves", False, repr(exc))

    # ── F3: the projection must land as a TOP-LEVEL attribute ────────────────
    raw = ddb_res.Table(TABLE).get_item(Key={"finding_id": finding["finding_id"]}).get("Item", {})
    check("F3 projection — top-level 'vin' present on the persisted item",
          raw.get("vin") == vin, f"top-level vin={raw.get('vin')!r}")
    check("F3 artifact boundary — nested scope preserved",
          raw.get("scope", {}).get("vin") == vin)

    # ── F1: by_vin, keyed on `vin` (code passed `vin_index`) ─────────────────
    try:
        items, _ = H._list_findings_by_vin(vin)
        check("F1 by_vin — real Query via handler._list_findings_by_vin",
              any(i["finding_id"] == finding["finding_id"] for i in items),
              f"{len(items)} row(s) returned")
    except Exception as exc:
        check("F1 by_vin — real Query via handler._list_findings_by_vin", False, repr(exc))

    # ── F2: by_owner (code named a non-existent `by_owner_id`) ───────────────
    try:
        items, _ = H._list_findings_by_owner(owner_id)
        check("F2 by_owner — real Query via handler._list_findings_by_owner",
              any(i["finding_id"] == owner_finding["finding_id"] for i in items),
              f"{len(items)} row(s) returned")
    except Exception as exc:
        check("F2 by_owner — real Query via handler._list_findings_by_owner", False, repr(exc))

    # ── F3 end-to-end: a scope GSI actually returns the row ─────────────────
    try:
        resp = ddb_res.Table(TABLE).query(
            IndexName="by_owner",
            KeyConditionExpression=boto3.dynamodb.conditions.Key("owner_id").eq(owner_id),
        )
        check("F3 scope GSI — by_owner is populated, not permanently empty",
              resp.get("Count", 0) >= 1, f"Count={resp.get('Count')}")
    except Exception as exc:
        check("F3 scope GSI — by_owner is populated", False, repr(exc))

finally:
    orphaned: list[str] = []
    for fid in written:
        try:
            ddb_res.Table(TABLE).delete_item(Key={"finding_id": fid})
        except Exception as exc:  # pragma: no cover
            orphaned.append(fid)
            print(f"  CLEANUP FAILED: {fid}: {exc!r}")
    if written:
        print(f"\ncleaned up {len(written) - len(orphaned)}/{len(written)} synthetic row(s)")
    if orphaned:
        # A printed warning alongside exit 0 is how synthetic rows accumulate in a
        # real table unnoticed. Orphans are a failure of the run.
        results.append(("cleanup — no synthetic rows left behind", False, f"orphaned: {orphaned}"))

failed = [r for r in results if not r[1]]
print(f"\n{'FAIL' if failed else 'PASS'} — {len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
